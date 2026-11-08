"""Deterministic generator for Maya's synthetic ledger: Alibi development and demo fixtures.

Maya (``subj_maya``) resells clothes on the mock platform ``threadline`` (issuer key
``merchant_demo``). Her Bank A account ``acc_maya_bank_a`` receives the credit ``txn_9001``
(+216000 pence, booked 2026-11-03T10:00:00Z). RFC A (bank_a) asks where that credit came
from; RFC B (bank_b) asks for documented sales and refunds over [2026-10-01, 2026-11-01).
Canonical amounts: sales 240000 - refunds 18000 - fees 6000 = credit 216000 (GBP pence).

One file per variant under ``alibi/fixtures/``:

* ``maya_supported.json``: the credit carries PO-5001 and STL-88001; the settlement is
  ``settled`` with ``effective_at`` before the booking.
* ``maya_contradiction.json``: the credit has no platform refs; the settlement is still
  ``pending``, observed after the booking.
* ``maya_pending_correction.json``: ``supported`` plus a signed revision 2 of the settlement
  restating it as ``pending`` (``corrects_revision`` 1), pushed after the first run.
* ``maya_ambiguous.json``: a second payout PO-5002 for the same amount, also before the
  booking, and a credit with no refs.
* ``retail_purchase.json``: a self-supplied retail receipt and its matching Bank A debit.

The JSON holds unsigned bodies. Sign at load time with a ``DemoKeystore`` (``sign_event``,
``sign_request``, ``settlement_lookup``) so private keys never enter the repo. Regenerate with
``uv run --frozen python -m alibi.fixtures.maya`` (``--check`` compares without writing).
"""

import argparse
import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from alibi.core.canon import digest, parse_json, to_plain
from alibi.core.schemas import (
    BankTxn,
    EvidenceRef,
    KeyPurpose,
    MerchantEvent,
    Period,
    RequestForContext,
    SigningDomain,
    parse_utc,
)
from alibi.core.signing import DemoKeystore, SignatureError

FIXTURE_DIR = Path(__file__).resolve().parent
SCHEMA = "alibi.fixture.v1"
VARIANTS = ("supported", "contradiction", "pending_correction", "ambiguous")
RETAIL = "retail_purchase"
ALL_FIXTURES = VARIANTS + (RETAIL,)

SUBJECT_REF = "subj_maya"
ACCOUNT_REF = "acc_maya_bank_a"
ISSUER_ID = "threadline"
MERCHANT_KID = "merchant_demo"
BANK_CONNECTOR = "bank_feed"
CONNECTION_ID = "conn_bank_a_maya"
FEED_VERSION = "bank_a_feed.v1"
CURRENCY = "GBP"

SALES_PENCE = 240000
REFUNDS_PENCE = 18000
FEES_PENCE = 6000
CREDIT_PENCE = 216000
CREDIT_TXN_ID = "txn_9001"
CREDIT_BOOKED_AT = "2026-11-03T10:00:00Z"

RFC_A_ID = "rfc_a_001"
RFC_B_ID = "rfc_b_001"
RFC_B_PERIOD = {"from": "2026-10-01T00:00:00Z", "to": "2026-11-01T00:00:00Z"}

RETAIL_ISSUER_ID = "northgate_retail"
# Self-supplied evidence lives in the store's "self_" record-id namespace.
RETAIL_RECEIPT_ID = "self_rcpt_4001"
RETAIL_TXN_ID = "txn_9002"
RETAIL_PAYMENT_REF = "NGR-731942"
RETAIL_PENCE = 4599

# Status vocabulary from DESIGN.md, used here only to derive the gold settlement timing.
_SETTLED_STATUSES = frozenset({"settled", "paid", "completed", "succeeded"})

# record_id, occurred_at, items as (description, category, quantity, unit_amount_pence).
_ORDERS = (
    ("ord_1001", "2026-10-04T14:12:00Z", (("Vintage wool overcoat, charcoal", "outerwear", 1,
                                           60000),)),
    ("ord_1002", "2026-10-11T09:30:00Z", (("Denim jacket, washed indigo", "outerwear", 1, 42000),
                                          ("Leather ankle boots, tan", "footwear", 1, 30000))),
    ("ord_1003", "2026-10-18T16:45:00Z", (("Silk midi dress, emerald", "dresses", 1, 30000),
                                          ("Cashmere scarf, oatmeal", "accessories", 1, 18000))),
    ("ord_1004", "2026-10-25T11:05:00Z", (("Corduroy trousers, rust", "trousers", 2, 15000),
                                          ("Hand-knitted cardigan, cream", "knitwear", 1, 30000))),
)
ORDER_IDS = tuple(record_id for record_id, _, _ in _ORDERS)
COMPONENT_IDS = ORDER_IDS + ("rfd_2001", "fee_3001")

MESSAGE_TEXT = (
    "Your October earnings of £2,160.00 have been released to your seller balance. "
    "This covers sales of £2,400.00 less refunds of £180.00 and fees of £60.00. "
    "Withdrawals from your seller balance to your bank account are sent separately as payouts "
    "and usually arrive within 1-3 working days."
)
RFC_A_Q1 = (
    "Please explain the source of the £2,160.00 credit received on 3 November 2026 (txn_9001). "
    "Which payment does it correspond to, and what records support that?"
)
RFC_B_Q1 = "What were your total documented marketplace sales from 1 to 31 October 2026?"
RFC_B_Q2 = "What refunds did you issue to buyers over the same period?"
RETAIL_RECEIPT_TEXT = (
    "Northgate Retail. Receipt NGR-731942. 1 x Rechargeable desk lamp, matte black: £45.99. "
    "Total £45.99, paid by card."
)

_COMMON_NOTE = (
    "Synthetic, labelled data: no real people, merchants, accounts or money. Bodies are "
    "unsigned: sign merchant events at load time with alibi.fixtures.maya.sign_event and a "
    "DemoKeystore, and RFCs with sign_request. merchant_events are pushed by webhook before any "
    "RFC; lookup_events are reachable only by follow-up retrieval (settlement_lookup); "
    "corrections are pushed later; self_supplied items go through the self-supplied import. "
    "Money is integer pence, times are UTC ISO-8601, periods are half-open [from, to). "
    "expected is development gold from explicit fields only (statuses, references, amounts, "
    "times): statuses lists every acceptable claim status and evidence the revision-pinned refs "
    "that decide it. Development and demo data, never holdout."
)
_RFC_B_NOTE = (
    "RFC B (bank_b) asks for documented sales and refunds over [2026-10-01, 2026-11-01): "
    "240000 and 18000, supported in every variant."
)
_NOTES = {
    "supported": (
        "Supported control: the Bank A credit txn_9001 carries PO-5001 and STL-88001, and the "
        "settlement stl_88001 is settled with effective_at 2026-11-03T07:45:00Z, before the "
        "10:00 booking, so the credit links to payout po_5001 by reference and RFC A q1 is "
        "supported."
    ),
    "contradiction": (
        "Contradiction: the credit carries no platform reference, so it links to po_5001 by "
        "amount and date only. The follow-up lookup shows stl_88001 still pending at "
        "2026-11-03T12:00:00Z, after the booking: the claim that PO-5001 explains the credit is "
        "contradicted and the credit's origin stays unresolved. The 'released to your seller "
        "balance' message is a release to the platform balance, not a bank transfer."
    ),
    "pending_correction": (
        "Pending correction: the supported case plus a signed revision 2 of stl_88001 (status "
        "pending, observed 2026-11-04T09:00:00Z, corrects_revision 1), pushed after the first "
        "investigation. RFC A q1 is supported before it arrives; afterwards the earlier claim is "
        "invalidated and a rerun gives contradicted or unresolved. The link and RFC B do not "
        "change."
    ),
    "ambiguous": (
        "Ambiguous: a second payout record po_5002 (PO-5002, 216000) for the same October "
        "balance is also initiated before the booking, both settlements were effective before "
        "it, and the credit carries no reference, so amount, date, components and timing fit "
        "both payouts. The link stays ambiguous and RFC A q1 unresolved."
    ),
}
_RETAIL_NOTE = (
    "Retail purchase through the same record model: a receipt Maya imports herself "
    "(self_supplied: issuer_id names the shop, but no shop key signs it, so it never becomes "
    "issuer_verified) and the matching Bank A card debit txn_9002 (-4599). The receipt's "
    "payment_ref NGR-731942 appears in the debit's refs. No RFC is attached; merchant_kid is "
    "null because nothing here is issuer-signed. The receipt documents an outflow, so "
    "expected.reconciliation counts it with sign -1 (total_pence -4599) against the signed "
    "debit (target_pence -4599), and difference_pence is total_pence - target_pence as in "
    "schemas.Calculation. alibi.core.reconcile reproduces it with its default signs, "
    "where a purchase receipt counts as -1."
)


# --------------------------------------------------------------------------- record builders


def _money(pence: int) -> dict[str, Any]:
    return {"amount_pence": pence, "currency": CURRENCY}


def _items(rows) -> list[dict[str, Any]]:
    return [{"description": d, "category": c, "quantity": q, "unit_amount_pence": u}
            for d, c, q, u in rows]


def _key(ref_type: str, ref_id: str, revision: int = 1) -> str:
    """Revision-pinned evidence key, e.g. ``record:po_5001@1``."""
    return EvidenceRef(ref_type=ref_type, ref_id=ref_id, revision=revision).key()


def _event(record_id: str, kind: str, occurred_at: str, *, revision: int = 1,
           issuer_id: str = ISSUER_ID, **fields: Any) -> dict[str, Any]:
    """A validated MerchantEvent body as JSON; ``event_id`` is ``evt_<record_id>_r<revision>``."""
    body = {"event_id": f"evt_{record_id}_r{revision}", "record_id": record_id,
            "revision": revision, "issuer_id": issuer_id, "subject_ref": SUBJECT_REF,
            "kind": kind, "occurred_at": occurred_at, **fields}
    return MerchantEvent.model_validate(body).to_json_dict()


def _payout(record_id: str, payment_ref: str, occurred_at: str) -> dict[str, Any]:
    return _event(record_id, "payout", occurred_at, status="initiated", payment_ref=payment_ref,
                  related_record_ids=list(COMPONENT_IDS), **_money(CREDIT_PENCE))


def _settlement(record_id: str, payment_ref: str, settlement_ref: str, occurred_at: str,
                status: str, *, effective_at: str | None = None, revision: int = 1,
                corrects_revision: int | None = None,
                correction_reason: str | None = None) -> dict[str, Any]:
    """A settlement record: reachable only by follow-up lookup, or pushed as a correction."""
    return _event(record_id, "settlement", occurred_at, revision=revision, status=status,
                  effective_at=effective_at, payment_ref=payment_ref,
                  settlement_ref=settlement_ref, corrects_revision=corrects_revision,
                  correction_reason=correction_reason, **_money(CREDIT_PENCE))


def _ledger_events() -> list[dict[str, Any]]:
    """Orders, partial refund, fee, balance-release message and payout PO-5001."""
    events = []
    for record_id, occurred_at, rows in _ORDERS:
        items = _items(rows)
        total = sum(item["quantity"] * item["unit_amount_pence"] for item in items)
        events.append(_event(record_id, "order", occurred_at, status="completed", items=items,
                             **_money(total)))
    events.append(_event("rfd_2001", "refund", "2026-10-22T10:00:00Z", status="completed",
                         items=_items((("Cashmere scarf, oatmeal", "accessories", 1, 18000),)),
                         related_record_ids=["ord_1003"], **_money(REFUNDS_PENCE)))
    events.append(_event("fee_3001", "fee", "2026-10-31T18:00:00Z", status="completed",
                         **_money(FEES_PENCE)))
    events.append(_event("msg_7001", "message", "2026-11-01T08:00:00Z",
                         status="released_to_balance", text=MESSAGE_TEXT, **_money(CREDIT_PENCE)))
    events.append(_payout("po_5001", "PO-5001", "2026-11-02T08:30:00Z"))
    return events


def _txn(txn_id: str, amount_pence: int, booked_at: str, description: str, refs: list[str],
         retrieved_at: str) -> dict[str, Any]:
    body = {"txn_id": txn_id, "revision": 1, "subject_ref": SUBJECT_REF,
            "account_ref": ACCOUNT_REF, "amount_pence": amount_pence, "currency": CURRENCY,
            "booked_at": booked_at, "description": description, "refs": refs,
            "provenance": {"connector": BANK_CONNECTOR, "connection_id": CONNECTION_ID,
                           "retrieved_at": retrieved_at, "source_version": FEED_VERSION}}
    return BankTxn.model_validate(body).to_json_dict()


def _credit(refs: list[str], description: str) -> dict[str, Any]:
    return _txn(CREDIT_TXN_ID, CREDIT_PENCE, CREDIT_BOOKED_AT, description, refs,
                retrieved_at="2026-11-03T10:05:00Z")


def _rfc_a() -> dict[str, Any]:
    return RequestForContext.model_validate({
        "issuer": "bank_a", "audience": "bank_a", "request_id": RFC_A_ID,
        "subject_ref": SUBJECT_REF, "txn_ids": [CREDIT_TXN_ID],
        "questions": [{"question_id": "q1", "text": RFC_A_Q1}],
        "issued_at": "2026-11-04T09:00:00Z", "expires_at": "2026-11-11T09:00:00Z",
    }).to_json_dict()


def _rfc_b() -> dict[str, Any]:
    return RequestForContext.model_validate({
        "issuer": "bank_b", "audience": "bank_b", "request_id": RFC_B_ID,
        "subject_ref": SUBJECT_REF, "period": RFC_B_PERIOD,
        "questions": [{"question_id": "q1", "text": RFC_B_Q1},
                      {"question_id": "q2", "text": RFC_B_Q2}],
        "issued_at": "2026-11-05T09:00:00Z", "expires_at": "2026-11-12T09:00:00Z",
    }).to_json_dict()


# --------------------------------------------------------------------------- gold


def _timing(settlement: dict[str, Any], credit: dict[str, Any], available: str) -> dict[str, Any]:
    """Gold timing: a settled status is effective at ``effective_at`` (else ``occurred_at``);
    any other status has no effective time. Ok only when effective by the booking."""
    effective = None
    if settlement.get("status") in _SETTLED_STATUSES:
        effective = settlement.get("effective_at", settlement["occurred_at"])
    ok = effective is not None and parse_utc(effective) <= parse_utc(credit["booked_at"])
    return {"record_id": settlement["record_id"], "revision": settlement["revision"],
            "payment_ref": settlement["payment_ref"], "status": settlement["status"],
            "observed_at": settlement["occurred_at"], "effective_time": effective,
            "credit_txn_id": credit["txn_id"], "credit_booked_at": credit["booked_at"],
            "ok": ok, "available": available}


def _gold(variant: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Hand-written gold for the credit's link and RFC A q1 (explicit-field semantics)."""
    po1, po2 = _key("record", "po_5001"), _key("record", "po_5002")
    stl1, stl2 = _key("record", "stl_88001"), _key("record", "stl_88001", 2)
    credit = _key("txn", CREDIT_TXN_ID)
    by_ref = {"link_state": "linked", "record_id": "po_5001", "candidates": ["po_5001"],
              "method": "reference"}
    if variant == "supported":
        return by_ref, {"statuses": ["supported"], "evidence": [po1, stl1, credit]}
    if variant == "pending_correction":
        after = {"statuses": ["contradicted", "unresolved"], "evidence": [po1, stl2, credit],
                 "earlier_claim_state": "invalidated", "correction": stl2}
        return ({**by_ref, "after_correction": dict(by_ref)},
                {"statuses": ["supported"], "evidence": [po1, stl1, credit],
                 "after_correction": after})
    if variant == "contradiction":
        link = {"link_state": "linked", "record_id": "po_5001", "candidates": ["po_5001"],
                "method": "amount_date"}
        return link, {"statuses": ["contradicted", "unresolved"], "evidence": [po1, stl1, credit]}
    link = {"link_state": "ambiguous", "record_id": None, "candidates": ["po_5001", "po_5002"],
            "method": "amount_date"}
    return link, {"statuses": ["unresolved"], "evidence": [po1, po2, credit]}


def _maya_expected(variant: str, events: list[dict], lookup: list[dict], corrections: list[dict],
                   credit: dict[str, Any]) -> dict[str, Any]:
    """Gold links and claims plus reconciliation numbers summed from the generated records."""
    def total(kind: str) -> int:
        return sum(e["amount_pence"] for e in events if e["kind"] == kind)

    sales, refunds, fees = total("order"), total("refund"), total("fee")
    net = sales - refunds - fees
    payouts = [e for e in events if e["kind"] == "payout"]
    period = Period.model_validate(RFC_B_PERIOD)
    in_period = [e for e in events if period.contains(parse_utc(e["occurred_at"]))]

    def period_claim(kind: str) -> dict[str, Any]:
        rows = [e for e in in_period if e["kind"] == kind]
        return {"statuses": ["supported"], "currency": CURRENCY,
                "total_pence": sum(e["amount_pence"] for e in rows),
                "evidence": [_key("record", e["record_id"], e["revision"]) for e in rows]}

    link, rfc_a_q1 = _gold(variant)
    trust = {f"record:{e['record_id']}": "issuer_verified" for e in events + lookup + corrections}
    trust[f"txn:{credit['txn_id']}"] = "bank_observed"
    return {
        "links": {credit["txn_id"]: link},
        "claims": {RFC_A_ID: {"q1": rfc_a_q1},
                   RFC_B_ID: {"q1": period_claim("order"), "q2": period_claim("refund")}},
        "reconciliation": {
            "currency": CURRENCY, "sales_pence": sales, "refunds_pence": refunds,
            "fees_pence": fees, "net_pence": net, "components": list(COMPONENT_IDS),
            "payout_record_ids": [p["record_id"] for p in payouts],
            "payout_pence": payouts[0]["amount_pence"], "credit_txn_id": credit["txn_id"],
            "credit_pence": credit["amount_pence"],
            "difference_pence": net - credit["amount_pence"],
            "balanced": net == credit["amount_pence"],
        },
        "settlement_timing": ([_timing(s, credit, "lookup") for s in lookup]
                              + [_timing(s, credit, "correction") for s in corrections]),
        "source_trust": trust,
    }


# --------------------------------------------------------------------------- fixtures


def build_fixture(variant: str) -> dict[str, Any]:
    """Build one fixture in memory (a Maya variant, or ``retail_purchase``)."""
    if variant == RETAIL:
        return build_retail_fixture()
    if variant not in VARIANTS:
        raise ValueError(f"unknown fixture variant {variant!r}; expected one of "
                         f"{', '.join(ALL_FIXTURES)}")
    events = _ledger_events()
    settled = _settlement("stl_88001", "PO-5001", "STL-88001", "2026-11-03T08:00:00Z", "settled",
                          effective_at="2026-11-03T07:45:00Z")
    corrections: list[dict[str, Any]] = []
    if variant in ("supported", "pending_correction"):
        lookup = [settled]
        credit = _credit(["PO-5001", "STL-88001"], "THREADLINE PAYOUT PO-5001 STL-88001")
    elif variant == "contradiction":
        lookup = [_settlement("stl_88001", "PO-5001", "STL-88001", "2026-11-03T12:00:00Z",
                              "pending")]
        credit = _credit([], "INCOMING TRANSFER")
    else:  # ambiguous
        events.append(_payout("po_5002", "PO-5002", "2026-11-02T15:10:00Z"))
        lookup = [settled,
                  _settlement("stl_88002", "PO-5002", "STL-88002", "2026-11-03T08:45:00Z",
                              "settled", effective_at="2026-11-03T08:30:00Z")]
        credit = _credit([], "INCOMING TRANSFER")
    if variant == "pending_correction":
        corrections = [_settlement(
            "stl_88001", "PO-5001", "STL-88001", "2026-11-04T09:00:00Z", "pending", revision=2,
            corrects_revision=1,
            correction_reason=("Status restated: the transfer had not completed and is still "
                               "pending with the receiving bank."))]
    events.sort(key=lambda e: (e["occurred_at"], e["record_id"]))
    return {
        "fixture": "maya", "variant": variant, "schema": SCHEMA,
        "subject_ref": SUBJECT_REF, "account_ref": ACCOUNT_REF,
        "issuer_id": ISSUER_ID, "merchant_kid": MERCHANT_KID,
        "merchant_events": events, "lookup_events": lookup, "corrections": corrections,
        "bank_txns": [credit], "self_supplied": [], "rfcs": [_rfc_a(), _rfc_b()],
        "expected": _maya_expected(variant, events, lookup, corrections, credit),
        "notes": " ".join([_NOTES[variant], _RFC_B_NOTE, _COMMON_NOTE]),
    }


def build_retail_fixture() -> dict[str, Any]:
    """A retail purchase through the same record model: self-supplied receipt + card debit."""
    receipt = _event(RETAIL_RECEIPT_ID, "receipt", "2026-11-05T13:20:00Z",
                     issuer_id=RETAIL_ISSUER_ID, status="paid", payment_ref=RETAIL_PAYMENT_REF,
                     items=_items((("Rechargeable desk lamp, matte black", "homeware", 1,
                                    RETAIL_PENCE),)),
                     text=RETAIL_RECEIPT_TEXT, **_money(RETAIL_PENCE))
    debit = _txn(RETAIL_TXN_ID, -RETAIL_PENCE, "2026-11-06T09:00:00Z",
                 "NORTHGATE RETAIL CARD PAYMENT", [RETAIL_PAYMENT_REF],
                 retrieved_at="2026-11-06T09:05:00Z")
    # A purchase receipt documents an outflow, so it counts with sign -1 against the signed
    # debit; difference_pence is total_pence - target_pence, as in schemas.Calculation.
    receipt_sign = -1
    total = receipt_sign * receipt["amount_pence"]
    difference = total - debit["amount_pence"]
    return {
        "fixture": RETAIL, "variant": RETAIL, "schema": SCHEMA,
        "subject_ref": SUBJECT_REF, "account_ref": ACCOUNT_REF,
        "issuer_id": RETAIL_ISSUER_ID, "merchant_kid": None,
        "merchant_events": [], "lookup_events": [], "corrections": [],
        "bank_txns": [debit], "self_supplied": [receipt], "rfcs": [],
        "expected": {
            "links": {RETAIL_TXN_ID: {"link_state": "linked", "record_id": RETAIL_RECEIPT_ID,
                                      "candidates": [RETAIL_RECEIPT_ID], "method": "reference"}},
            "claims": {},
            "reconciliation": {
                "currency": CURRENCY, "receipt_record_id": RETAIL_RECEIPT_ID,
                "receipt_pence": receipt["amount_pence"], "debit_txn_id": RETAIL_TXN_ID,
                "debit_pence": debit["amount_pence"],
                "terms": [{"ref": _key("record", receipt["record_id"], receipt["revision"]),
                           "kind": receipt["kind"], "sign": receipt_sign,
                           "amount_pence": receipt["amount_pence"], "currency": CURRENCY}],
                "total_pence": total,
                "target_ref": _key("txn", debit["txn_id"], debit["revision"]),
                "target_pence": debit["amount_pence"], "difference_pence": difference,
                "balanced": difference == 0,
            },
            "settlement_timing": [],
            "source_trust": {f"record:{RETAIL_RECEIPT_ID}": "self_supplied",
                             f"txn:{RETAIL_TXN_ID}": "bank_observed"},
        },
        "notes": " ".join([_RETAIL_NOTE, _COMMON_NOTE]),
    }


def fixture_path(variant: str, directory: Path | str = FIXTURE_DIR) -> Path:
    """File for a Maya variant (``maya_<variant>.json``) or ``retail_purchase.json``."""
    if variant == RETAIL:
        name = f"{RETAIL}.json"
    elif variant in VARIANTS:
        name = f"maya_{variant}.json"
    else:
        raise ValueError(f"unknown fixture variant {variant!r}; expected one of "
                         f"{', '.join(ALL_FIXTURES)}")
    return Path(directory) / name


def load_variant(variant: str, directory: Path | str = FIXTURE_DIR) -> dict[str, Any]:
    """Read a committed fixture file (unsigned bodies) as a dict."""
    return parse_json(fixture_path(variant, directory).read_text(encoding="utf-8"))


def dumps(obj: Any) -> str:
    """Deterministic JSON text: sorted keys, 2-space indent, UTF-8 characters, final newline."""
    return json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def write_all(directory: Path | str = FIXTURE_DIR) -> list[Path]:
    """Write every fixture file into ``directory`` and return their paths."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    paths = []
    for name in ALL_FIXTURES:
        path = fixture_path(name, directory)
        path.write_text(dumps(build_fixture(name)), encoding="utf-8", newline="\n")
        paths.append(path)
    return paths


# --------------------------------------------------------------------------- signing at load time


def sign_event(event_json: dict[str, Any], keystore: DemoKeystore,
               kid: str = MERCHANT_KID) -> dict[str, Any]:
    """Wrap a merchant event body as ``{"event", "signature"}``, signed in alibi:record:v1."""
    event = copy.deepcopy(to_plain(event_json))
    envelope = keystore.sign(event, kid=kid, domain=SigningDomain.RECORD)
    return {"event": event, "signature": envelope.to_json_dict()}


def _request_kid(keystore: DemoKeystore, issuer: str) -> str:
    for kid, (purpose, issuer_id) in keystore.specs.items():
        if purpose == KeyPurpose.REQUEST_SIGNING and issuer_id == issuer:
            return kid
    raise SignatureError(f"no request-signing demo key for issuer {issuer!r}")


def sign_request(rfc_json: dict[str, Any], keystore: DemoKeystore) -> dict[str, Any]:
    """Wrap an RFC body as ``{"request", "signature"}``, signed by the issuer's key
    (bank_a / bank_b) in alibi:request:v1."""
    request = copy.deepcopy(to_plain(rfc_json))
    kid = _request_kid(keystore, request["issuer"])
    envelope = keystore.sign(request, kid=kid, domain=SigningDomain.REQUEST)
    return {"request": request, "signature": envelope.to_json_dict()}


def _latest(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Highest revision per record; across records, the most recently observed one."""
    newest: dict[str, dict[str, Any]] = {}
    for event in events:
        current = newest.get(event["record_id"])
        if current is None or event["revision"] > current["revision"]:
            newest[event["record_id"]] = event
    return max(newest.values(), key=lambda e: (parse_utc(e["occurred_at"]), e["record_id"]))


def settlement_lookup(fixture: dict[str, Any], keystore: DemoKeystore, *,
                      include_corrections: bool = False) -> Callable[[str], dict | None]:
    """Mock follow-up retrieval: ``payment_ref`` -> the latest available settlement revision,
    signed as ``{"event", "signature"}``, or ``None`` for an unknown reference.

    Available revisions are ``lookup_events`` plus, with ``include_corrections``, the pushed
    ``corrections``. The fixture is copied when the lookup is created.
    """
    pool = list(fixture.get("lookup_events", []))
    if include_corrections:
        pool += fixture.get("corrections", [])
    settlements = copy.deepcopy([e for e in pool if e.get("kind") == "settlement"])
    kid = fixture.get("merchant_kid") or MERCHANT_KID

    def lookup(payment_ref: str) -> dict | None:
        matches = [e for e in settlements if e.get("payment_ref") == payment_ref]
        return sign_event(_latest(matches), keystore, kid) if matches else None

    return lookup


def txn_refresh(fixture: dict[str, Any]) -> Callable[[str], dict | None]:
    """Mock bank refresh: ``txn_id`` -> the latest revision of that transaction, or ``None``.
    The fixture is copied when the refresher is created."""
    txns = copy.deepcopy(list(fixture.get("bank_txns", [])))

    def refresh(txn_id: str) -> dict | None:
        matches = [t for t in txns if t.get("txn_id") == txn_id]
        return copy.deepcopy(max(matches, key=lambda t: t["revision"])) if matches else None

    return refresh


# --------------------------------------------------------------------------- command line


def main(argv: list[str] | None = None) -> int:
    """Regenerate the fixture files (or ``--check`` them) and print a one-line JSON summary."""
    parser = argparse.ArgumentParser(prog="python -m alibi.fixtures.maya",
                                     description="Regenerate Alibi's Maya fixture JSON files.")
    parser.add_argument("--out", type=Path, default=FIXTURE_DIR,
                        help="directory to write or check (default: alibi/fixtures)")
    parser.add_argument("--check", action="store_true",
                        help="compare the files in --out with a fresh build; write nothing")
    args = parser.parse_args(argv)

    if args.check:
        stale = []
        for name in ALL_FIXTURES:
            try:
                current = load_variant(name, args.out)
            except (OSError, ValueError):
                current = None
            if current != build_fixture(name):
                stale.append(fixture_path(name, args.out).name)
        print(json.dumps({"check": "stale" if stale else "ok", "stale": stale}, sort_keys=True))
        return 1 if stale else 0

    paths = write_all(args.out)
    reconciliation = build_fixture("supported")["expected"]["reconciliation"]
    summary = {
        "written": len(paths),
        "files": {path.name: digest(build_fixture(name))
                  for name, path in zip(ALL_FIXTURES, paths)},
        "canonical": {key: reconciliation[key] for key in
                      ("sales_pence", "refunds_pence", "fees_pence", "credit_pence", "balanced")},
    }
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
