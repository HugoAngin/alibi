"""Bridge from the team's case schema (``agents.data.api``) to the core wire types.

The cases in ``agents/data/out/cases/`` are the ground truth for shapes and IDs. The core
(``alibi.core.schemas``) has its own record model. This module maps one to the other, without
interpreting anything: no reference is parsed out of free text, no status is guessed. Each
``map_*`` function returns a ``Mapped`` with

* ``body``: the core wire object as JSON (validated with the core model), which the merchant
  signs and the passport ingests;
* ``extra``: every case field the core type cannot hold under its own name (``payout_reference``,
  ``withdrawal_id``, ``settlement_batch_id``, ``covers_record_ids``, ``status``, ``settled_at``,
  ``status_as_of``, ``release_reference``, ``destination``, ``related_record_id``,
  ``counterparty_name``, ``correction_reason``, ...), plus ``case_kind``. The services return it
  beside the signed body; it never enters a signed payload, because the core models reject
  unknown fields;
* ``source``: a deep copy of the case object.

The mapping is documented field by field in ``alibi/connectors/MAPPING.md``.

``CaseClock`` is the simulated clock: ``as_of`` defaults to the case's first RFC ``issued_at``.
Deliveries with ``received_at`` after ``as_of``, and lookup-only records with ``available_from``
after it, are not visible. The clock only moves forward.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pydantic import ValidationError

from agents.data.api import load_case, lookup, to_contract, without_gold
from alibi.core.schemas import (
    SCHEMA_BANK_TXN,
    SCHEMA_MERCHANT_EVENT,
    SCHEMA_RFC,
    BankTxn,
    MerchantEvent,
    RequestForContext,
    format_utc,
    parse_utc,
)

__all__ = [
    "CASE_SCHEMA_VERSION", "FEED_CONNECTOR", "FEED_VERSION", "IDENTITY_FIELDS", "KIND_MAP",
    "CaseClock", "Mapped", "MappingError", "default_as_of", "load_case_without_gold",
    "map_bank_row", "map_import", "map_merchant_event", "map_rfc", "parse_time",
]

CASE_SCHEMA_VERSION = "1"
FEED_CONNECTOR = "bank_feed"
FEED_VERSION = "mock_bank_feed.v1"

# case kind -> core RecordKind. A balance release and the free-text documents are messages: a
# release to a platform balance is not a bank settlement, and a notice or statement carries
# text and no amount. A purchase by the holder, and an imported receipt, are receipts (an
# outflow, like S1's retail receipt). The case's payout carries its own settlement status, so
# it is one core payout.
KIND_MAP = {
    "sale": "order",
    "fee": "fee",
    "refund": "refund",
    "purchase": "receipt",
    "receipt": "receipt",
    "balance_release": "message",
    "payout": "payout",
    "notice": "message",
    "statement": "message",
    # second-platform raw kinds (schema sp-2.1); ``agents.data.api.to_contract`` maps them to the
    # contract kinds above before the field mapping runs
    "order_completed": "order",
    "order_returned": "refund",
    "service_charge": "fee",
    "weekly_statement": "payout",
    "earnings_update": "message",
}

# Case fields that become core identity or money fields. Everything else goes to ``extra``.
IDENTITY_FIELDS = frozenset({"schema_version", "event_id", "record_id", "revision", "issuer_id",
                             "subject_ref", "kind", "occurred_at", "amount_minor", "currency"})

# Import-only metadata. ``claimed_trust`` is a trust claim: it never reaches a core body, and
# the import path tags the record self_supplied whatever it says.
IMPORT_META = ("import_source", "claimed_trust")

_BANK_CORE_FIELDS = frozenset({"txn_id", "revision", "account_ref", "amount_minor", "currency",
                               "booked_at", "description"})


class MappingError(ValueError):
    """A case object cannot be expressed as a core object. The message names fields only."""


@dataclass(frozen=True)
class Mapped:
    """A core wire body plus the case fields the core type cannot hold."""

    body: dict[str, Any]
    extra: dict[str, Any] = field(default_factory=dict)
    source: dict[str, Any] = field(default_factory=dict)

    @property
    def record_id(self) -> str | None:
        return self.body.get("record_id") or self.body.get("txn_id") or self.body.get("request_id")


# --------------------------------------------------------------------------- helpers


def parse_time(value: Any, name: str = "time") -> datetime:
    """A UTC datetime from an ISO-8601 string with a timezone, or a timezone-aware datetime."""
    if isinstance(value, datetime):
        if value.utcoffset() is None:
            raise MappingError(f"{name} must be timezone-aware")
        return parse_utc(format_utc(value))
    if not isinstance(value, str):
        raise MappingError(f"{name} must be an ISO-8601 UTC timestamp")
    try:
        return parse_utc(value)
    except (ValueError, TypeError):
        raise MappingError(f"{name} is not an ISO-8601 UTC timestamp") from None


def _require(obj: dict[str, Any], name: str, what: str) -> Any:
    if name not in obj or obj[name] is None:
        raise MappingError(f"{what} is missing {name}")
    return obj[name]


def _validate(model, body: dict[str, Any], what: str) -> dict[str, Any]:
    try:
        return model.model_validate(body).to_json_dict()
    except ValidationError as exc:
        where = "; ".join(".".join(str(p) for p in err["loc"]) + ": " + err["msg"]
                          for err in exc.errors()[:3])
        raise MappingError(f"{what} does not fit the core schema: {where}") from None


def _item(event: dict[str, Any]) -> list[dict[str, Any]] | None:
    detail = {}
    if event.get("item") is not None:
        detail["description"] = event["item"]
    if event.get("category") is not None:
        detail["category"] = event["category"]
    return [detail] if detail else None


# --------------------------------------------------------------------------- merchant events


def map_merchant_event(case_event: dict[str, Any]) -> Mapped:
    """A case merchant event (pushed, lookup-only or imported) as a core ``MerchantEvent`` body.

    Field mapping (full table in MAPPING.md): ``amount_minor`` -> ``amount_pence``;
    ``payout_reference`` (payout), ``release_reference`` (balance release) and ``order_number``
    (sale) -> ``payment_ref``; ``settlement_batch_id`` -> ``settlement_ref``; ``settled_at`` ->
    ``effective_at``; ``covers_record_ids`` and ``related_record_id`` -> ``related_record_ids``;
    ``message``, ``note`` and ``text`` -> ``text``; ``item``/``category`` -> one ``items`` entry;
    a ``correction_reason`` on revision n sets ``corrects_revision`` n-1. ``status`` passes
    through unchanged. ``withdrawal_id``, ``status_as_of``, ``destination`` and the like have no
    core slot and live only in ``extra``.
    """
    if not isinstance(case_event, dict):
        raise MappingError("a case event must be a JSON object")
    raw = copy.deepcopy(case_event)
    what = f"event {raw.get('event_id', '?')}"
    raw_kind = _require(raw, "kind", what)
    core_kind = KIND_MAP.get(raw_kind)
    if core_kind is None:
        raise MappingError(f"{what} has an unknown case kind {raw_kind!r}")
    # a second-platform event (schema sp-2.1) is first mapped onto the contract field names by
    # the data layer's reference mapping; mock-marketplace events come back unchanged
    source = to_contract(raw) if raw.get("issuer_id") is not None else raw
    kind = source.get("kind", raw_kind)
    if KIND_MAP.get(kind) != core_kind:
        raise MappingError(f"{what}: contract kind {kind!r} does not agree with {raw_kind!r}")
    if kind == "receipt" and source.get("amount_minor") is None:
        core_kind = "message"   # a receipt with text and no amount is a document, not money
    body: dict[str, Any] = {
        "schema_version": SCHEMA_MERCHANT_EVENT,
        "event_id": _require(source, "event_id", what),
        "record_id": _require(source, "record_id", what),
        "revision": _require(source, "revision", what),
        "issuer_id": _require(source, "issuer_id", what),
        "subject_ref": _require(source, "subject_ref", what),
        "kind": core_kind,
        "occurred_at": _require(source, "occurred_at", what),
    }
    if source.get("amount_minor") is not None:
        body["amount_pence"] = source["amount_minor"]
        body["currency"] = source.get("currency")
    if source.get("status") is not None:
        body["status"] = source["status"]
    if kind == "payout":
        body["payment_ref"] = source.get("payout_reference")
        body["settlement_ref"] = source.get("settlement_batch_id")
        body["effective_at"] = source.get("settled_at")
        body["related_record_ids"] = list(source.get("covers_record_ids") or [])
        body["text"] = source.get("note")
    elif kind == "balance_release":
        body["payment_ref"] = source.get("release_reference")
        body["related_record_ids"] = list(source.get("covers_record_ids") or [])
        body["text"] = source.get("message")
    elif kind in ("sale", "purchase", "receipt"):
        body["items"] = _item(source)
        if kind == "receipt":
            body["text"] = source.get("text")
        if kind == "sale" and source.get("order_number") is not None:
            body["payment_ref"] = str(source["order_number"])   # an order number may be numeric
    elif kind in ("fee", "refund"):
        related = source.get("related_record_id")
        body["related_record_ids"] = [related] if related else []
    else:  # notice, statement
        body["text"] = source.get("text")
    if source.get("correction_reason") is not None:
        body["correction_reason"] = source["correction_reason"]
        revision = body["revision"]
        body["corrects_revision"] = revision - 1 if isinstance(revision, int) else None
    body = {k: v for k, v in body.items() if v is not None}
    extra = {k: copy.deepcopy(v) for k, v in source.items() if k not in IDENTITY_FIELDS}
    extra["case_kind"] = raw_kind
    return Mapped(body=_validate(MerchantEvent, body, what), extra=extra, source=raw)


def map_import(record: dict[str, Any]) -> Mapped:
    """A holder import as a core event body for ``import_self_supplied``.

    The body carries no trust claim: ``claimed_trust`` and ``import_source`` go to ``extra``
    (``extra["tagged"]`` is ``"self_supplied"``). The backend tags the stored record
    ``self_supplied`` and moves its id into the ``self_`` namespace whatever the record says.
    """
    if not isinstance(record, dict):
        raise MappingError("an import must be a JSON object")
    stripped = {k: v for k, v in record.items() if k not in IMPORT_META}
    mapped = map_merchant_event(stripped)
    extra = dict(mapped.extra)
    for name in IMPORT_META:
        if name in record:
            extra[name] = copy.deepcopy(record[name])
    extra["tagged"] = "self_supplied"
    return Mapped(body=mapped.body, extra=extra, source=copy.deepcopy(record))


# --------------------------------------------------------------------------- bank rows


def map_bank_row(row: dict[str, Any], *, subject_ref: str, connection_id: str,
                 retrieved_at: datetime | str, source_version: str = FEED_VERSION,
                 cursor: str | None = None) -> Mapped:
    """A case bank row as a core ``BankTxn`` with provenance.

    ``amount_minor`` -> ``amount_pence`` (signed, holder's view); ``payment_reference`` ->
    ``refs`` (one entry, or none when null); ``counterparty_name`` has no core slot and lives
    in ``extra``. Provenance carries the connection, the retrieval time, the feed version and
    the cursor the row was read under.
    """
    if not isinstance(row, dict):
        raise MappingError("a bank row must be a JSON object")
    source = copy.deepcopy(row)
    what = f"bank row {source.get('txn_id', '?')}"
    provenance: dict[str, Any] = {
        "connector": FEED_CONNECTOR, "connection_id": connection_id,
        "retrieved_at": format_utc(parse_time(retrieved_at, "retrieved_at")),
        "source_version": source_version,
    }
    if cursor is not None:
        provenance["cursor"] = cursor
    body: dict[str, Any] = {
        "schema_version": SCHEMA_BANK_TXN,
        "txn_id": _require(source, "txn_id", what),
        "revision": _require(source, "revision", what),
        "subject_ref": subject_ref,
        "account_ref": _require(source, "account_ref", what),
        "amount_pence": _require(source, "amount_minor", what),
        "currency": _require(source, "currency", what),
        "booked_at": _require(source, "booked_at", what),
        "description": _require(source, "description", what),
        "refs": [source["payment_reference"]] if source.get("payment_reference") else [],
        "provenance": provenance,
    }
    extra = {k: copy.deepcopy(v) for k, v in source.items() if k not in _BANK_CORE_FIELDS}
    return Mapped(body=_validate(BankTxn, body, what), extra=extra, source=source)


# --------------------------------------------------------------------------- requests


def map_rfc(rfc: dict[str, Any]) -> Mapped:
    """A case RFC as a core ``RequestForContext`` body. The issuer is the audience (the bank
    that asks is the bank that receives). Exactly one of ``period`` (object) or ``txn_ids``
    (non-empty) is kept; the other is omitted. ``question_ids`` is derivable and goes to
    ``extra``."""
    if not isinstance(rfc, dict):
        raise MappingError("an RFC must be a JSON object")
    source = copy.deepcopy(rfc)
    what = f"rfc {source.get('request_id', '?')}"
    audience = _require(source, "audience", what)
    period, txn_ids = source.get("period"), source.get("txn_ids") or []
    if (period is None) == (not txn_ids):
        raise MappingError(f"{what} must have exactly one of period or txn_ids")
    body: dict[str, Any] = {
        "schema_version": SCHEMA_RFC,
        "issuer": source.get("issuer") or audience,
        "request_id": _require(source, "request_id", what),
        "subject_ref": _require(source, "subject_ref", what),
        "audience": audience,
        "questions": _require(source, "questions", what),
        "issued_at": _require(source, "issued_at", what),
        "expires_at": _require(source, "expires_at", what),
    }
    if source.get("parent_request_id") is not None:
        body["parent_request_id"] = source["parent_request_id"]
    if period is not None:
        body["period"] = period
    else:
        body["txn_ids"] = list(txn_ids)
    extra = {k: copy.deepcopy(v) for k, v in source.items() if k == "question_ids"}
    return Mapped(body=_validate(RequestForContext, body, what), extra=extra, source=source)


# --------------------------------------------------------------------------- the simulated clock


def load_case_without_gold(case_number: int) -> dict[str, Any]:
    """The case as a system under test may see it: gold removed. Raises KeyError for a case the
    lockbox hides."""
    return without_gold(load_case(case_number))


def default_as_of(case: dict[str, Any]) -> datetime:
    """The case's first RFC ``issued_at``; with no RFC, the latest delivery time."""
    times = [parse_time(r["issued_at"], "issued_at") for r in case.get("rfcs", [])]
    if times:
        return min(times)
    times = [parse_time(d["received_at"], "received_at")
             for d in [*case.get("merchant_deliveries", []), *case.get("bank_deliveries", []),
                       *case.get("imports", [])]]
    times += [parse_time(d["available_from"], "available_from")
              for d in case.get("merchant_lookup", [])]
    if not times:
        raise MappingError("the case has no timed content to set a clock from")
    return max(times)


class CaseClock:
    """One case and the moment the simulated services believe it is.

    Visibility is by time: a delivery is visible once ``received_at <= as_of``, a lookup-only
    record once ``available_from <= as_of``, an RFC once ``issued_at <= as_of``. The clock
    never moves backwards (``advance`` raises ``MappingError``), so nothing seen is unseen.
    """

    def __init__(self, case: dict[str, Any], as_of: datetime | str | None = None):
        if not isinstance(case, dict) or "subject_ref" not in case:
            raise MappingError("a case is a JSON object with subject_ref and deliveries")
        self.case = without_gold(case)
        self.as_of = default_as_of(self.case) if as_of is None else parse_time(as_of, "as_of")

    @property
    def case_number(self) -> int | None:
        return self.case.get("case_number")

    @property
    def subject_ref(self) -> str:
        return self.case["subject_ref"]

    @property
    def as_of_text(self) -> str:
        return format_utc(self.as_of)

    def advance(self, to: datetime | str) -> datetime:
        """Move the clock forward to ``to``. Same moment is a no-op; earlier is refused."""
        moment = parse_time(to, "as_of")
        if moment < self.as_of:
            raise MappingError(f"the clock is at {self.as_of_text} and cannot move back to "
                               f"{format_utc(moment)}")
        self.as_of = moment
        return self.as_of

    def _visible(self, items: list[dict[str, Any]], key: str,
                 since: datetime | str | None = None) -> list[dict[str, Any]]:
        after = None if since is None else parse_time(since, "since")
        out = []
        for item in items:
            at = parse_time(item[key], key)
            if at <= self.as_of and (after is None or at > after):
                out.append(copy.deepcopy(item))
        return out

    def visible_merchant_deliveries(self, since: datetime | str | None = None) -> list[dict]:
        """Pushed merchant deliveries ``{received_at, event}`` in arrival order, visible now
        (and after ``since`` when given), duplicates, conflicts and stale revisions included."""
        return self._visible(self.case.get("merchant_deliveries", []), "received_at", since)

    def visible_bank_rows(self, since: datetime | str | None = None) -> list[dict]:
        return self._visible(self.case.get("bank_deliveries", []), "received_at", since)

    def visible_imports(self, since: datetime | str | None = None) -> list[dict]:
        return self._visible(self.case.get("imports", []), "received_at", since)

    def visible_rfcs(self) -> list[dict]:
        return self._visible(self.case.get("rfcs", []), "issued_at")

    def visible_lookup_records(self) -> list[dict]:
        """Lookup-only records ``{available_from, event}`` available now."""
        return self._visible(self.case.get("merchant_lookup", []), "available_from")

    def lookup(self, *, payout_reference: str | None = None, record_id: str | None = None,
               covers_record_id: str | None = None, payout_date: str | None = None) -> list[dict]:
        """The merchant's follow-up retrieval at this moment: exactly one key, over pushed
        deliveries received and lookup-only records available by ``as_of``
        (``agents.data.api.lookup``). Returns case events in arrival order."""
        return lookup(self.case, self.as_of_text, payout_reference=payout_reference,
                      record_id=record_id, covers_record_id=covers_record_id,
                      payout_date=payout_date)
