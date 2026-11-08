"""Load a team case (``agents.data.api``) into the passport through the real ingest paths.

Data's cases describe merchant events, bank rows and requests in the team's shared shape
(``agents/data/README.md``). The core passport (``alibi.core.schemas``) uses S1's shape. This
module is the adapter between the two. It changes field names and record kinds only; it never
invents a record, an amount, a status or a time.

Kind mapping (data -> core)::

    sale             -> order
    fee              -> fee
    refund           -> refund
    balance_release  -> payout, status ``released_to_balance`` (the withdrawal the merchant
                        announced; its payment reference is the withdrawal named in the message)
    payout           -> settlement (the bank transfer: status settled | pending | failed,
                        ``effective_at`` = ``settled_at``)
    purchase         -> receipt (the holder buying something; a sensitive distractor)
    notice, statement -> message (free text, no amount)

A balance release and the payout that follows it are two different records with two record
ids. They are never two revisions of one record. A correction in the case (same ``record_id``,
higher ``revision``) stays a newer revision of the same record.

Events are signed at load time with the app keystore's key for the issuer, the way the mock
merchant would sign them. Lookup-only records stay outside the passport until an investigation
retrieves them through ``settlement_lookup_for``.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Callable
from datetime import datetime
from typing import Any

from agents.data import api as data_api
from alibi import api
from alibi.core.canon import to_plain
from alibi.core.ingest import IngestError
from alibi.core.schemas import SCHEMA_MERCHANT_EVENT, SigningDomain, format_utc, parse_utc
from alibi.core.signing import DemoKeystore, SignatureError
from alibi.core.store import Store, StoreError

__all__ = [
    "BANK_CONNECTOR_VERSION", "KIND_MAP", "LoadSummary", "connection_id", "issuer_kid",
    "load_case", "sign_event", "sign_request", "settlement_lookup_for", "to_bank_txn",
    "to_merchant_event", "to_rfc",
]

KIND_MAP = {
    "sale": "order", "fee": "fee", "refund": "refund", "balance_release": "payout",
    "payout": "settlement", "purchase": "receipt", "notice": "message", "statement": "message",
}
RELEASED_STATUS = "released_to_balance"
BANK_CONNECTOR_VERSION = "mock_bank_feed.v1"
ISSUER_KIDS = {"mock-marketplace": "mock_marketplace", "second-platform": "second_platform"}
# The reference a release message names, in the team's canonical form ("PO-0001-01").
_REFERENCE = re.compile(r"\b([A-Z]{1,6}-\d{3,}-\d{2,})\b")
# Data fields that have no place on a core record and are dropped by the adapter.
_DROPPED = {"item", "category", "fee_type", "destination", "release_reference", "covers_record_ids",
            "message", "settled_at", "status_as_of", "settlement_batch_id", "withdrawal_id",
            "order_number", "notice_type", "statement_type", "related_record_id", "text",
            "payout_reference", "status", "amount_minor", "source_kind", "currency_code",
            "value_pence", "net_payable_pence", "transfer_ref", "transfer_status", "paid_at",
            "line_record_ids", "statement_no", "note", "correction_reason"}


def issuer_kid(issuer_id: str) -> str:
    """The app keystore key id that signs for a case issuer."""
    try:
        return ISSUER_KIDS[issuer_id]
    except KeyError:
        raise SignatureError(f"no demo signing key for issuer {issuer_id!r}") from None


def connection_id(case: dict) -> str:
    return f"conn-bank-a-{case['subject_ref']}"


def _references_in(text: str | None) -> list[str]:
    return list(dict.fromkeys(_REFERENCE.findall(text or "")))


def to_merchant_event(raw: dict, *, received_at: str | None = None) -> dict:
    """A core ``MerchantEvent`` JSON object from a data case event (any issuer)."""
    ev = data_api.to_contract(raw)
    kind = ev.get("kind")
    if kind not in KIND_MAP:
        raise IngestError(f"unknown case record kind {kind!r}")
    out: dict[str, Any] = {
        "schema_version": SCHEMA_MERCHANT_EVENT,
        "event_id": ev["event_id"], "record_id": ev["record_id"], "revision": ev["revision"],
        "issuer_id": ev["issuer_id"], "subject_ref": ev["subject_ref"],
        "kind": KIND_MAP[kind], "occurred_at": ev["occurred_at"],
    }
    if ev.get("amount_minor") is not None:
        out["amount_pence"] = ev["amount_minor"]
        out["currency"] = ev["currency"]
    status = ev.get("status")
    related = []
    if kind == "sale":
        out["items"] = [{"description": ev["item"]}] if ev.get("item") else None
    elif kind == "purchase":
        item = {"description": ev.get("item")}
        if ev.get("category"):
            item["category"] = ev["category"]
        out["items"] = [item] if item.get("description") else None
    elif kind in ("fee", "refund"):
        if ev.get("related_record_id"):
            related = [ev["related_record_id"]]
    elif kind == "balance_release":
        status = RELEASED_STATUS
        out["text"] = ev.get("message")
        related = list(ev.get("covers_record_ids") or [])
        refs = _references_in(ev.get("message"))
        if len(refs) == 1:
            out["payment_ref"] = refs[0]
    elif kind == "payout":
        out["payment_ref"] = ev.get("payout_reference") or ev.get("withdrawal_id")
        out["settlement_ref"] = ev.get("settlement_batch_id") or ev.get("withdrawal_id")
        related = list(ev.get("covers_record_ids") or [])
        settled_at = ev.get("settled_at")
        observed = ev.get("status_as_of")
        if status == "settled" and settled_at:
            out["effective_at"] = settled_at
            out["occurred_at"] = settled_at
        elif observed:
            out["occurred_at"] = observed
        if ev.get("note"):
            out["text"] = ev["note"]
    elif kind in ("notice", "statement"):
        out["text"] = ev.get("text") or ev.get("message")
    if status:
        out["status"] = status
    if related:
        out["related_record_ids"] = related
    if ev.get("correction_reason"):
        out["corrects_revision"] = ev["revision"] - 1
        out["correction_reason"] = ev["correction_reason"]
    known = set(out) | _DROPPED | {"schema_version", "event_id", "record_id", "revision", "issuer_id",
                                    "subject_ref", "kind", "occurred_at", "currency"}
    extra = sorted(set(ev) - known)
    if extra:
        # A field the adapter does not know is sender-chosen data; keep it out of a record that
        # will be signed, and keep the name out of any message.
        pass
    return {k: v for k, v in out.items() if v is not None}


def to_bank_txn(row: dict, *, subject_ref: str, connection_id: str, received_at: str) -> dict:
    """A core ``BankTxn`` JSON object from a data bank row."""
    description = row["description"]
    if row.get("counterparty_name"):
        description = f"{description} ({row['counterparty_name']})"
    return {
        "txn_id": row["txn_id"], "revision": row["revision"], "subject_ref": subject_ref,
        "account_ref": row["account_ref"], "amount_pence": row["amount_minor"],
        "currency": row["currency"], "booked_at": row["booked_at"], "description": description,
        "refs": [row["payment_reference"]] if row.get("payment_reference") else [],
        "provenance": {"connector": "bank_feed", "connection_id": connection_id,
                       "retrieved_at": received_at, "source_version": BANK_CONNECTOR_VERSION},
    }


def to_rfc(raw: dict) -> dict:
    """A core ``RequestForContext`` JSON object from a data case RFC."""
    out: dict[str, Any] = {
        "issuer": raw["audience"], "request_id": raw["request_id"],
        "subject_ref": raw["subject_ref"], "audience": raw["audience"],
        "questions": [{"question_id": q["question_id"], "text": q["text"]} for q in raw["questions"]],
        "issued_at": raw["issued_at"], "expires_at": raw["expires_at"],
    }
    if raw.get("parent_request_id"):
        out["parent_request_id"] = raw["parent_request_id"]
    if raw.get("period"):
        out["period"] = {"from": raw["period"]["from"], "to": raw["period"]["to"]}
    if raw.get("txn_ids"):
        out["txn_ids"] = list(raw["txn_ids"])
    return out


def sign_event(event: dict, keystore: DemoKeystore) -> dict:
    """``{"event", "signature"}`` signed by the issuer's demo key in ``alibi:record:v1``."""
    body = copy.deepcopy(to_plain(event))
    envelope = keystore.sign(body, kid=issuer_kid(body["issuer_id"]), domain=SigningDomain.RECORD)
    return {"event": body, "signature": envelope.to_json_dict()}


def sign_request(rfc: dict, keystore: DemoKeystore) -> dict:
    """``{"request", "signature"}`` signed by the issuing bank's demo key."""
    body = copy.deepcopy(to_plain(rfc))
    envelope = keystore.sign(body, kid=body["issuer"], domain=SigningDomain.REQUEST)
    return {"request": body, "signature": envelope.to_json_dict()}


def settlement_lookup_for(case: dict, keystore: DemoKeystore,
                          clock: Callable[[], datetime]) -> Callable[[str], dict | None]:
    """The merchant's follow-up retrieval as the gateway expects it: ``payment_ref`` -> signed
    settlement event or ``None``. It serves only what the merchant has available at the clock
    time (``agents.data.api.lookup``), so a record the merchant has not produced yet is not
    found."""
    def lookup(payment_ref: str) -> dict | None:
        at = format_utc(clock())
        hits = [ev for ev in data_api.lookup(case, at, payout_reference=payment_ref)
                if data_api.to_contract(ev).get("kind") == "payout"]
        if not hits:
            return None
        latest = max(hits, key=lambda ev: (ev["revision"], ev["occurred_at"]))
        return sign_event(to_merchant_event(latest), keystore)
    return lookup


class LoadSummary(dict):
    """``{subject_ref, connection_id, records, txns, requests, statuses, skipped}``."""


def load_case(store: Store, case: dict, keystore: DemoKeystore, *,
              up_to: datetime | None = None, account_bank: str = "bank_a") -> LoadSummary:
    """Replay the case's deliveries into the passport in arrival order, each at its own
    ``received_at``, through ``alibi.api``. Lookup-only records are not loaded. Idempotent: a
    second load is all duplicates. Deliveries after ``up_to`` are skipped (none when None).
    Deliveries the passport refuses (a conflicting redelivery, a stale revision) are recorded in
    ``skipped`` with the backend's reason; the case data expects those refusals."""
    subject = case["subject_ref"]
    conn = connection_id(case)
    registry = keystore.registry()
    limit = format_utc(up_to) if up_to is not None else None
    events: list[tuple[str, int, str, dict]] = []
    for d in case.get("merchant_deliveries", []):
        events.append((d["received_at"], 0, "merchant", d["event"]))
    for d in case.get("imports", []):
        events.append((d["received_at"], 1, "import", d["record"]))
    for d in case.get("bank_deliveries", []):
        events.append((d["received_at"], 2, "bank", d["row"]))
    for r in case.get("rfcs", []):
        events.append((r["issued_at"], 3, "rfc", r))
    events.sort(key=lambda e: (e[0], e[1]))
    summary = LoadSummary(subject_ref=subject, connection_id=conn, records=[], txns=[],
                          requests=[], statuses={}, skipped=[])
    accounts = sorted({d["row"]["account_ref"] for d in case.get("bank_deliveries", [])})
    for received_at, _, kind, payload in events:
        if limit is not None and received_at > limit:
            continue
        at = parse_utc(received_at)
        try:
            if kind == "merchant":
                put = api.ingest_merchant_event(store, sign_event(to_merchant_event(payload), keystore),
                                                registry=registry, subject_ref=subject, now=at)
                summary["records"].append(put.record.record_id)
                summary["statuses"][f"{put.record.record_id}@{put.record.revision}"] = put.status
            elif kind == "import":
                put = api.import_self_supplied(store, to_merchant_event(payload), subject_ref=subject,
                                               now=at)
                summary["records"].append(put.record.record_id)
                summary["statuses"][f"{put.record.record_id}@{put.record.revision}"] = put.status
            elif kind == "bank":
                txn = to_bank_txn(payload, subject_ref=subject, connection_id=conn,
                                  received_at=received_at)
                [put] = api.ingest_bank_txns(store, [txn], subject_ref=subject, connection_id=conn,
                                             cursor=received_at, now=at)
                summary["txns"].append(put.record.txn_id)
                summary["statuses"][f"{put.record.txn_id}@{put.record.revision}"] = put.status
            else:
                rfc = to_rfc(payload)
                stored = api.receive_rfc(store, sign_request(rfc, keystore),
                                         authenticated_issuer=rfc["issuer"], registry=registry,
                                         account_refs=accounts if rfc.get("txn_ids") and
                                         rfc["issuer"] == account_bank else
                                         (accounts if rfc.get("txn_ids") else None), now=at)
                summary["requests"].append(stored.request_id)
        except (IngestError, StoreError) as exc:
            ident = payload.get("record_id") or payload.get("txn_id") or payload.get("request_id")
            summary["skipped"].append({"kind": kind, "id": ident, "received_at": received_at,
                                       "reason": str(exc)})
    return summary
