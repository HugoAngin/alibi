"""Tool gateway: bounded, scoped reads for the investigation agents and the rules baseline.

A ``Gateway`` serves one investigation run for one subject. Every public call:

1. counts one call against the run's budget before doing anything else; a call past the
   budget raises ``BudgetExceeded`` and is traced as a refusal without being counted;
2. checks subject scope: another subject's record or txn raises ``ScopeError`` without
   revealing anything about it, and a missing one raises ``NotFoundError``;
3. runs and appends a ``TraceEvent`` to ``gateway.trace``. Its result is a short summary
   (counts, ids, revisions, kinds, statuses), never amounts, text or full payloads.

Connector lookups are injected callables (``SettlementLookup``, ``TxnRefresh``), so a later
session can plug in the mock services; without one the call raises ``Unavailable``. A
connector is only asked about this subject's own data: a txn it holds, or a payment reference
carried by the subject's own evidence: in the ``payment_ref``, ``settlement_ref`` or
``withdrawal_ref`` field of one of its issuer-verified records (then the settlement must come
from that record's issuer), or else in the ``text`` of one of those records or on one of the
subject's own bank transactions (``refs`` or a reference-shaped token of the description), in
which case the settlement must come from one of the issuers of the subject's issuer-verified
records. A settlement lookup may return a ``settlement`` record or a ``payout`` record that
carries a ``status`` (the team's payouts fold the settlement state in); either is settlement
evidence for that payment reference. What a connector returns is validated and ingested into
the passport (``alibi.core.ingest``) before it is returned, so evidence fetched during an
investigation is kept with its provenance. ``flush_trace`` saves the trace to the store.
"""

import math
import re
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from enum import Enum
from typing import Any

from pydantic import BaseModel, TypeAdapter, ValidationError

from alibi.core import canon
from alibi.core.ingest import (
    CONNECTOR_BANK_REFRESH,
    CONNECTOR_LOOKUP,
    IngestError,
    ingest_bank_txns,
    ingest_merchant_event,
)
from alibi.core.schemas import (
    MAX_SAFE_INT,
    BankTxn,
    EvidenceRecord,
    EvidenceRef,
    Ident,
    Period,
    RecordKind,
    Ref,
    SourceTrust,
    TraceEvent,
    format_utc,
    utc_now,
)
from alibi.core.reconcile import SETTLEMENT_EVIDENCE_KINDS
from alibi.core.signing import TrustRegistry
from alibi.core.store import NotFound, Store, StoreError

__all__ = [
    "DEFAULT_BUDGET", "DEFAULT_WINDOW", "BudgetExceeded", "Gateway", "GatewayError",
    "NotFoundError", "RejectedResult", "ScopeError", "SettlementLookup", "TxnRefresh",
    "Unavailable",
]

SettlementLookup = Callable[[str], dict | None]   # payment_ref -> signed merchant event payload
TxnRefresh = Callable[[str], dict | None]          # txn_id -> bank txn JSON

DEFAULT_BUDGET = 12
DEFAULT_WINDOW = timedelta(days=5)
MAX_TRACE_ITEMS = 50   # items listed in one trace result; the count is always exact
ACTOR = "gateway"

_IDENT = TypeAdapter(Ident)
_REF = TypeAdapter(Ref)
# A reference-shaped token of a bank description: upper-case letters, digits and hyphens, at
# least four characters and at least one digit (so TRANSFER or PAYOUT never counts).
_REF_TOKEN = re.compile(r"[A-Z0-9][A-Z0-9-]{3,}")


class GatewayError(Exception):
    """Base gateway error. The message is safe to show an agent and to log."""

    outcome = "error"


class ScopeError(GatewayError):
    """The id names data outside this run's subject. Nothing about it is returned."""

    outcome = "refused"


class BudgetExceeded(GatewayError):
    """The run's call budget is spent. The refused call is traced but not counted."""

    outcome = "budget_exceeded"


class Unavailable(GatewayError):
    """A connector is not configured, failed, or returned nothing this time."""

    outcome = "unavailable"


class NotFoundError(GatewayError):
    """No record or transaction has that id or revision."""

    outcome = "not_found"


class RejectedResult(GatewayError):
    """A connector returned something that failed validation (schema, signature, subject,
    kind or reference). Nothing from it is returned; a rejected merchant event is audited."""

    outcome = "refused"


# --------------------------------------------------------------------------- helpers


def _config_ident(value: Any, name: str) -> str:
    try:
        return _IDENT.validate_python(value)
    except ValidationError:
        raise ValueError(f"{name} must be an identifier") from None


def _lookup_id(value: Any, name: str) -> str:
    """An id argument. Wrong types are errors; a malformed string names nothing."""
    if not isinstance(value, str):
        raise GatewayError(f"{name} must be a string")
    try:
        return _IDENT.validate_python(value)
    except ValidationError:
        raise NotFoundError(f"no {name.removesuffix('_id')} has that id") from None


def _revision(value: Any) -> int | None:
    if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
        raise GatewayError("revision must be an integer")
    return value


def _kinds(kinds: Any) -> list[RecordKind] | None:
    if kinds is None:
        return None
    if isinstance(kinds, str):
        kinds = [kinds]
    try:
        return [RecordKind(kind) for kind in kinds]
    except (TypeError, ValueError):
        names = ", ".join(kind.value for kind in RecordKind)
        raise GatewayError(f"kinds must be record kinds ({names})") from None


def _period(period: Any) -> Period | None:
    if period is None or isinstance(period, Period):
        return period
    try:
        return Period.model_validate(period)
    except ValidationError:
        raise GatewayError("period must be {from, to} UTC timestamps with from before to") from None


def _window(value: Any) -> timedelta:
    if isinstance(value, timedelta):
        span = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        try:
            span = timedelta(seconds=value)
        except OverflowError:
            raise GatewayError("window is too large") from None
    else:
        raise GatewayError("window must be a timedelta or a number of seconds")
    if span < timedelta(0):
        raise GatewayError("window cannot be negative")
    return span


def _seconds(span: timedelta) -> int | float:
    total = span.total_seconds()
    return int(total) if total == int(total) else total


def _clip(text: Any, limit: int = 300) -> str:
    """One line of valid UTF-8, at most ``limit`` characters."""
    text = " ".join(str(text).encode("utf-8", "replace").decode("utf-8").split()) or "-"
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _arg(value: Any, depth: int = 0) -> Any:
    """A bounded, JSON-safe copy of a call argument for the trace."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, Enum):
        return _arg(value.value, depth)
    if isinstance(value, str):
        return _clip(value, 128)
    if isinstance(value, int):
        return value if abs(value) <= MAX_SAFE_INT else _clip(value, 40)
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, timedelta):
        return _seconds(value)
    if isinstance(value, datetime):
        return format_utc(value) if value.utcoffset() is not None else value.isoformat()
    if isinstance(value, BaseModel):
        return _arg(value.model_dump(mode="json", by_alias=True, exclude_none=True), depth)
    if depth < 3:
        if isinstance(value, (set, frozenset)):
            value = sorted(value, key=str)
        if isinstance(value, (list, tuple)):
            return [_arg(item, depth + 1) for item in value[:20]]
        if isinstance(value, dict):
            return {_clip(key, 64): _arg(item, depth + 1) for key, item in list(value.items())[:20]}
    return f"<{type(value).__name__}>"


def _connector_object(payload: Any) -> dict[str, Any]:
    """A connector's answer as a JSON object, parsed strictly when it is text or bytes."""
    if isinstance(payload, (bytes, bytearray, str)):
        try:
            payload = canon.parse_json(bytes(payload) if isinstance(payload, bytearray) else payload)
        except (canon.CanonError, RecursionError):
            raise RejectedResult("the connector returned invalid JSON") from None
    if not isinstance(payload, dict):
        raise RejectedResult("the connector returned something other than a JSON object")
    return payload


def _record_summary(record: EvidenceRecord) -> dict[str, Any]:
    summary = {"record_id": record.record_id, "revision": record.revision,
               "kind": record.kind.value, "source_trust": record.source_trust.value}
    if record.status is not None:
        summary["status"] = record.status
    return summary


def _list_summary(records: list[EvidenceRecord]) -> dict[str, Any]:
    summary: dict[str, Any] = {"count": len(records),
                               "items": [_record_summary(r) for r in records[:MAX_TRACE_ITEMS]]}
    if len(records) > MAX_TRACE_ITEMS:
        summary["truncated"] = True
    return summary


def _by_time(records: list[EvidenceRecord]) -> list[EvidenceRecord]:
    return sorted(records, key=lambda r: (r.occurred_at, r.record_id))


def _txn_tokens(txn: BankTxn) -> set[str]:
    """The references a bank transaction carries, upper-cased: its ``refs`` plus the
    reference-shaped tokens of its description (same rule as
    ``alibi.core.investigate_rules.reference_tokens``, repeated here to avoid an import cycle)."""
    words = {token for token in _REF_TOKEN.findall(txn.description.upper())
             if any(char.isdigit() for char in token)}
    return {ref.upper() for ref in txn.refs} | words


def _text_carries(text: str | None, ref: str) -> bool:
    """Does free text name ``ref`` as a whole token (case-insensitive)? The text is data: only
    the exact reference is looked for, nothing in it is interpreted."""
    if not text:
        return False
    return re.search(rf"(?<![A-Za-z0-9-]){re.escape(ref)}(?![A-Za-z0-9-])", text,
                     re.IGNORECASE) is not None


# --------------------------------------------------------------------------- gateway


class Gateway:
    """Scoped, budgeted reads for one investigation run of one subject (see module doc).

    ``now`` fixes the clock for traces and ingestion (tests, replays); otherwise each call
    uses the current UTC time. ``request_id`` (optional) is stamped on every trace event.
    """

    def __init__(self, store: Store, *, subject_ref: str, run_id: str,
                 registry: TrustRegistry | None = None, budget: int = DEFAULT_BUDGET,
                 settlement_lookup: SettlementLookup | None = None,
                 txn_refresh: TxnRefresh | None = None, now: datetime | None = None,
                 request_id: str | None = None):
        if isinstance(budget, bool) or not isinstance(budget, int) or budget < 0:
            raise ValueError("budget must be a non-negative integer")
        if now is not None and (not isinstance(now, datetime) or now.utcoffset() is None):
            raise ValueError("now must be a timezone-aware datetime")
        self.store = store
        self.subject_ref = _config_ident(subject_ref, "subject_ref")
        self.run_id = _config_ident(run_id, "run_id")
        self.request_id = None if request_id is None else _config_ident(request_id, "request_id")
        self.registry = registry
        self.budget = budget
        self.settlement_lookup = settlement_lookup
        self.txn_refresh = txn_refresh
        self.now = now
        self.calls_used = 0
        self.trace: list[TraceEvent] = []
        self._seq = 0

    # ----------------------------------------------------------------------- passport reads

    def get_source(self, record_id: str, revision: int | None = None) -> EvidenceRecord:
        """One of this subject's records: the active revision, or the named one."""
        def run():
            record = self._record(record_id, revision)
            return record, _record_summary(record), [record.ref()]
        return self._call("get_source", {"record_id": record_id, "revision": revision}, run)

    def get_txn(self, txn_id: str, revision: int | None = None) -> BankTxn:
        """One of this subject's bank txns: the active revision, or the named one."""
        def run():
            txn = self._txn(txn_id, revision)
            return txn, {"txn_id": txn.txn_id, "revision": txn.revision}, [txn.ref()]
        return self._call("get_txn", {"txn_id": txn_id, "revision": revision}, run)

    def list_records(self, *, kinds: Any, period: Period | dict | None = None
                     ) -> list[EvidenceRecord]:
        """This subject's active records of ``kinds`` (``None``: every kind), optionally only
        those with ``occurred_at`` in the half-open ``period``, by (occurred_at, record_id)."""
        def run():
            wanted, window = _kinds(kinds), _period(period)
            records = self.store.list_sources(self.subject_ref, kinds=wanted)
            if window is not None:
                records = [r for r in records if window.contains(r.occurred_at)]
            records = _by_time(records)
            return records, _list_summary(records), [r.ref() for r in records]
        return self._call("list_records", {"kinds": kinds, "period": period}, run)

    def list_candidates(self, txn_id: str, window: timedelta = DEFAULT_WINDOW,
                        kinds: Any = (RecordKind.PAYOUT,)) -> list[EvidenceRecord]:
        """Active records of ``kinds`` for this subject with ``occurred_at`` in
        ``[booked_at - window, booked_at + window)`` of the txn, by (occurred_at, record_id)."""
        def run():
            txn = self._txn(txn_id)
            span, wanted = _window(window), _kinds(kinds)
            try:
                start, end = txn.booked_at - span, txn.booked_at + span
            except OverflowError:
                raise GatewayError("window is too large") from None
            records = _by_time([r for r in self.store.list_sources(self.subject_ref, kinds=wanted)
                                if start <= r.occurred_at < end])
            summary = {"txn_id": txn.txn_id, "txn_revision": txn.revision, **_list_summary(records)}
            return records, summary, [txn.ref(), *(r.ref() for r in records)]
        return self._call("list_candidates",
                          {"txn_id": txn_id, "window_seconds": window, "kinds": kinds}, run)

    # ----------------------------------------------------------------------- connector lookups

    def lookup_settlement(self, payment_ref: str) -> EvidenceRecord | None:
        """Ask the merchant connector for the settlement of ``payment_ref``.

        The reference must be carried by this subject's own evidence (else ``ScopeError``,
        and the connector is not called): the ``payment_ref``, ``settlement_ref`` or
        ``withdrawal_ref`` of an active issuer-verified record, the ``text`` of one, or one of
        the subject's bank transactions (``refs`` or a reference-shaped description token).
        The answer must be a ``settlement`` event, or a ``payout`` event carrying a ``status``,
        for exactly this reference and this subject, signed by the issuer of a record carrying
        the reference (or, for a text or bank-side reference, by one of the issuers of the
        subject's issuer-verified records); it is ingested with connector ``merchant_lookup``
        and the stored record is returned (a replay is a duplicate). If the passport already
        holds a later revision of that record, the active revision is returned. ``None`` when
        the connector knows no settlement for the reference.
        """
        def run():
            if not isinstance(payment_ref, str):
                raise GatewayError("payment_ref must be a string")
            try:
                ref = _REF.validate_python(payment_ref)
            except ValidationError:
                raise GatewayError("payment_ref is not a valid reference") from None
            issuers = self._issuers_of(ref)
            if not issuers:
                raise ScopeError("payment_ref is not carried by this subject's issuer evidence")
            if self.settlement_lookup is None:
                raise Unavailable("no settlement lookup connector is configured")
            if self.registry is None:
                raise Unavailable("no trust registry is configured to verify lookup results")
            try:
                payload = self.settlement_lookup(ref)
            except Exception as exc:  # whatever the connector raised, report only its type
                raise Unavailable(f"the settlement lookup failed ({type(exc).__name__})") from None
            if payload is None:
                return None, {"found": False}, []
            raw = _connector_object(payload)
            event = raw.get("event")
            kinds = {kind.value for kind in SETTLEMENT_EVIDENCE_KINDS}
            if not isinstance(event, dict) or event.get("kind") not in kinds:
                raise RejectedResult("the lookup returned something other than a settlement record")
            if event.get("kind") == RecordKind.PAYOUT.value and not isinstance(event.get("status"), str):
                raise RejectedResult("the lookup returned a payout record with no settlement status, "
                                     "which is not a settlement record")
            if event.get("payment_ref") != ref:
                raise RejectedResult("the lookup returned a settlement for another payment reference")
            issuer = event.get("issuer_id")
            if not isinstance(issuer, str) or issuer not in issuers:
                raise RejectedResult("the lookup returned a settlement from another issuer")
            try:
                put = ingest_merchant_event(self.store, self.registry, raw,
                                            connector=CONNECTOR_LOOKUP,
                                            subject_ref=self.subject_ref, now=self._clock())
            except IngestError as exc:
                raise RejectedResult(f"the lookup result was rejected: {exc.reason}") from None
            record = put.record if put.active else self.store.get_source(put.record.record_id)
            if record.kind not in SETTLEMENT_EVIDENCE_KINDS or record.payment_ref != ref \
                    or record.subject_ref != self.subject_ref or record.issuer_id not in issuers:
                raise RejectedResult("the passport's settlement record does not match the lookup")
            summary = {"found": True, **_record_summary(record), "put_status": put.status}
            if put.record.revision != record.revision:
                summary["returned_revision"] = put.record.revision
            return record, summary, [record.ref()]
        return self._call("lookup_settlement", {"payment_ref": payment_ref}, run)

    def refresh_txn(self, txn_id: str) -> BankTxn:
        """Re-read one of this subject's txns from the bank and return its active revision.

        The answer is validated and stored through the bank path (connector
        ``bank_refresh``, the txn's own connection); a new revision invalidates what depended
        on the old one.
        """
        def run():
            current = self._txn(txn_id)
            if self.txn_refresh is None:
                raise Unavailable("no bank refresh connector is configured")
            try:
                payload = self.txn_refresh(current.txn_id)
            except Exception as exc:  # whatever the connector raised, report only its type
                raise Unavailable(f"the bank refresh failed ({type(exc).__name__})") from None
            if payload is None:
                raise Unavailable("the bank returned no transaction for this id")
            data = _connector_object(payload.to_json_dict() if isinstance(payload, BankTxn)
                                     else payload)
            if data.get("txn_id") != current.txn_id:
                raise RejectedResult("the bank returned a different transaction")
            try:
                [put] = ingest_bank_txns(self.store, [data], subject_ref=self.subject_ref,
                                         connection_id=current.provenance.connection_id,
                                         connector=CONNECTOR_BANK_REFRESH, now=self._clock())
            except IngestError as exc:
                raise RejectedResult(f"the refreshed transaction was rejected: {exc.reason}") from None
            active = self.store.get_bank_txn(current.txn_id)
            summary: dict[str, Any] = {"txn_id": active.txn_id, "revision": active.revision,
                                       "previous_revision": current.revision,
                                       "put_status": put.status}
            invalidated = {kind: ids for kind, ids in put.invalidated.items() if ids}
            if invalidated:
                summary["invalidated"] = invalidated
            return active, summary, [active.ref()]
        return self._call("refresh_txn", {"txn_id": txn_id}, run)

    # ----------------------------------------------------------------------- trace

    def log_step(self, step: str, *, actor: str, tool: str | None = None,
                 args: dict | None = None, result: dict | None = None,
                 refs: Iterable[EvidenceRef] = (), outcome: str = "ok",
                 reason: str | None = None, question_id: str | None = None) -> TraceEvent:
        """Append the caller's own step (validation, calculation, status change, note) to this
        run's trace, in the gateway's sequence so trace ids stay unique. Not counted against
        the budget. Keep ``result`` to a short summary of JSON values."""
        return self._append(step=step, actor=actor, tool=tool, args=_arg(args or {}),
                            result=result, refs=refs, outcome=outcome, reason=reason,
                            question_id=question_id)

    def flush_trace(self, store: Store | None = None) -> int:
        """Save the trace (to ``store``, default the gateway's). Re-saving identical events is
        a no-op, so flushing again is safe. Returns the number of events in the trace."""
        (self.store if store is None else store).append_traces(self.trace)
        return len(self.trace)

    # ----------------------------------------------------------------------- internals

    def _clock(self) -> datetime:
        return self.now if self.now is not None else utc_now()

    def _issuers_of(self, payment_ref: str) -> set[str]:
        """The issuers whose settlement for ``payment_ref`` a lookup may return, or an empty
        set when the reference is not this subject's to ask about.

        A reference carried in the ``payment_ref``, ``settlement_ref`` or ``withdrawal_ref``
        field of an active issuer-verified record binds the lookup to that record's issuer.
        Otherwise a reference that appears in the ``text`` of such a record, or on one of the
        subject's own bank transactions (``refs`` or a reference-shaped description token),
        may be asked about from any issuer of the subject's issuer-verified records. Self-
        supplied records never open a lookup, and another subject's data is never consulted.
        """
        verified = [r for r in self.store.list_sources(self.subject_ref)
                    if r.source_trust == SourceTrust.ISSUER_VERIFIED]
        carriers = {r.issuer_id for r in verified
                    if payment_ref in (r.payment_ref, r.settlement_ref, r.withdrawal_ref)}
        if carriers:
            return carriers
        own = {r.issuer_id for r in verified}
        if not own:
            return set()
        if any(_text_carries(r.text, payment_ref) for r in verified):
            return own
        wanted = payment_ref.upper()
        if any(wanted in _txn_tokens(txn) for txn in self.store.list_bank_txns(self.subject_ref)):
            return own
        return set()

    def _call(self, tool: str, args: dict[str, Any], run: Callable[[], tuple]) -> Any:
        args = {name: _arg(value) for name, value in args.items() if value is not None}
        if self.calls_used >= self.budget:
            reason = f"the call budget of {self.budget} is spent"
            self._append(step="refusal", tool=tool, args=args, outcome="budget_exceeded",
                         reason=reason)
            raise BudgetExceeded(reason)
        self.calls_used += 1
        try:
            value, result, refs = run()
        except GatewayError as exc:
            step = "refusal" if exc.outcome == "refused" else "tool_call"
            self._append(step=step, tool=tool, args=args, outcome=exc.outcome, reason=str(exc))
            raise
        except StoreError as exc:  # store messages name ids and fields only
            self._append(step="tool_call", tool=tool, args=args, outcome="error",
                         reason="passport store error")
            raise GatewayError(f"passport store error: {exc}") from exc
        except Exception as exc:
            self._append(step="tool_call", tool=tool, args=args, outcome="error",
                         reason=f"unexpected error ({type(exc).__name__})")
            raise
        self._append(step="tool_call", tool=tool, args=args, result=result, refs=refs,
                     outcome="ok")
        return value

    def _append(self, *, step: str, tool: str | None, args: dict[str, Any], outcome: str,
                actor: str = ACTOR, result: dict[str, Any] | None = None,
                refs: Iterable[EvidenceRef] = (), reason: str | None = None,
                question_id: str | None = None) -> TraceEvent:
        event = TraceEvent(
            trace_id=canon.make_id("trc", self.run_id, self._seq), run_id=self.run_id,
            seq=self._seq, at=self._clock(), actor=actor, step=step, tool=tool,
            request_id=self.request_id, question_id=question_id, args=args,
            result=result or {}, refs=list(refs), outcome=outcome,
            reason=_clip(reason) if reason else None)
        self.trace.append(event)
        self._seq += 1
        return event

    def _record(self, record_id: Any, revision: Any = None) -> EvidenceRecord:
        """A record in scope. Scope is decided on the active revision, so a request for
        another subject's revision never reveals which revisions exist."""
        record_id, revision = _lookup_id(record_id, "record_id"), _revision(revision)
        try:
            record = self.store.get_source(record_id)
        except NotFound:
            raise NotFoundError(f"no record {record_id!r}") from None
        if record.subject_ref != self.subject_ref:
            raise ScopeError(f"record {record_id!r} is outside this run's subject")
        if revision is not None and revision != record.revision:
            try:
                record = self.store.get_source(record_id, revision)
            except NotFound:
                raise NotFoundError(f"no record {record_id!r} revision {revision}") from None
        return record

    def _txn(self, txn_id: Any, revision: Any = None) -> BankTxn:
        """A bank txn in scope (same rules as ``_record``)."""
        txn_id, revision = _lookup_id(txn_id, "txn_id"), _revision(revision)
        try:
            txn = self.store.get_bank_txn(txn_id)
        except NotFound:
            raise NotFoundError(f"no txn {txn_id!r}") from None
        if txn.subject_ref != self.subject_ref:
            raise ScopeError(f"txn {txn_id!r} is outside this run's subject")
        if revision is not None and revision != txn.revision:
            try:
                txn = self.store.get_bank_txn(txn_id, revision)
            except NotFound:
                raise NotFoundError(f"no txn {txn_id!r} revision {revision}") from None
        return txn
