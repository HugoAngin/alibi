"""Validated write path into the passport: merchant events, self-supplied imports, the bank
feed and inbound Requests for Context.

Every function checks the payload's schema, the caller, the subject and the scope before
anything is stored, and writes one ``AuditEvent`` per outcome (``ok``, ``duplicate`` or
``refused``). Audit ids are derived from the event's own fields, so they are deterministic and
never shared by two different events. Audit details hold ids, revisions and reasons only, never
amounts, references, free text or key material. A refusal raises ``IngestError``, whose message
is safe to show the caller.

* ``ingest_merchant_event``: the issuer path (webhook push or follow-up lookup). It claims
  issuer provenance, so a signature is mandatory and is verified over the raw received
  ``event`` object (domain ``alibi:record:v1``, a key pinned for ``event.issuer_id``). Stored as
  ``issuer_verified``.
* ``import_self_supplied``: the holder's own receipts and exports. No signature is needed and
  none is honoured: trust claims are stripped, the record id moves into the ``self_`` namespace
  and trust is always ``self_supplied``.
* ``ingest_bank_txns``: one connection's feed batch; the batch and its cursor commit together.
  A txn id and an account belong to the connection that first delivered them.
* ``receive_rfc``: a bank's request, bound to the authenticated bank, to the subject's own
  transactions and to the bank's own accounts (``account_refs``, required with ``txn_ids``).
  A follow-up must stay inside its parent's scope (same scope kind; a period inside the
  parent's period; ``txn_ids`` a subset of the parent's), else ``scope_wider_than_parent``.

Refusal reasons never echo a JSON key that is not a schema field: such a key is
sender-chosen text, so it is named only as ``<extra field>``.

Idempotency is the store's: merchant events by ``(connector, issuer_id, event_id)`` plus the
payload digest, bank txns by ``(txn_id, revision)`` plus content, requests by
``(issuer, request_id)``: request ids are the bank's own, so two banks may use the same request id
independently and never collide.
"""

import copy
from collections.abc import Collection, Iterable
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, TypeAdapter, ValidationError

from alibi.core import canon
from alibi.core.schemas import (
    SCHEMA_EVIDENCE,
    SCHEMA_MERCHANT_EVENT,
    AuditEvent,
    BankTxn,
    EvidenceRecord,
    Ident,
    ItemDetail,
    MerchantEvent,
    Period,
    Provenance,
    Question,
    RequestForContext,
    SignatureEnvelope,
    SignedMerchantEvent,
    SignedRequest,
    SigningDomain,
    SourceTrust,
    format_utc,
    utc_now,
)
from alibi.core.signing import SignatureError, TrustRegistry, verify
from alibi.core.store import (
    SELF_SUPPLIED_PREFIX,
    IdempotencyConflict,
    NamespaceError,
    NotFound,
    PutResult,
    RevisionConflict,
    Store,
    StoreError,
)

__all__ = [
    "CONNECTOR_BANK", "CONNECTOR_BANK_REFRESH", "CONNECTOR_IMPORT", "CONNECTOR_LOOKUP",
    "CONNECTOR_RFC", "CONNECTOR_WEBHOOK", "HOLDER", "ISSUER_CONNECTORS", "SELF_SUPPLIED_ISSUER",
    "REFUSAL_SCOPE_WIDER", "TRUST_CLAIM_KEYS", "IngestError", "import_self_supplied",
    "ingest_bank_txns", "ingest_merchant_event", "receive_rfc",
]

CONNECTOR_WEBHOOK = "merchant_webhook"
CONNECTOR_LOOKUP = "merchant_lookup"
CONNECTOR_IMPORT = "self_supplied_import"
CONNECTOR_BANK = "bank_feed"
CONNECTOR_BANK_REFRESH = "bank_refresh"   # the gateway's scoped re-read of one transaction
CONNECTOR_RFC = "bank_rfc"                # audit actor for inbound requests
ISSUER_CONNECTORS = frozenset({CONNECTOR_WEBHOOK, CONNECTOR_LOOKUP})
SELF_SUPPLIED_ISSUER = "self_supplied"
HOLDER = "holder"

# Fields only the backend sets on stored evidence. An import that carries them is claiming a
# provenance it cannot have, so they are dropped (and the audit detail names them).
TRUST_CLAIM_KEYS = ("signature", "issuer_signature", "signed_body", "source_trust", "connector",
                    "received_at", "payload_sha256")

_IDENT = TypeAdapter(Ident)
_DETAIL_MAX = 300
_STORE_REFUSALS = (IdempotencyConflict, RevisionConflict, NamespaceError)


class IngestError(Exception):
    """An inbound payload was refused. ``reason`` is safe to show the caller and to log."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class _Refused(Exception):
    """Internal: a check refused the payload. Becomes a refused audit event + IngestError."""

    def __init__(self, reason: str, target_id: Any = None):
        super().__init__(reason)
        self.reason = reason
        self.target_id = target_id


# --------------------------------------------------------------------------- helpers


def _clock(now: datetime | None) -> datetime:
    if now is None:
        return utc_now()
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise IngestError("now must be a timezone-aware datetime")
    return now.astimezone(timezone.utc)


def _is_ident(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        _IDENT.validate_python(value)
    except ValidationError:
        return False
    return True


def _clip(text: Any, limit: int = _DETAIL_MAX) -> str:
    """One line of valid UTF-8, at most ``limit`` characters."""
    text = " ".join(str(text).encode("utf-8", "replace").decode("utf-8").split()) or "-"
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _field_names(*models: type[BaseModel]) -> frozenset[str]:
    names: set[str] = set()
    for model in models:
        for name, info in model.model_fields.items():
            names.update(n for n in (name, info.alias) if n)
    return frozenset(names)


# Field names of every model an inbound payload is validated against.
_KNOWN_FIELDS = _field_names(SignedMerchantEvent, MerchantEvent, SignatureEnvelope, ItemDetail,
                             BankTxn, Provenance, SignedRequest, RequestForContext, Question,
                             Period)


def _schema_errors(exc: ValidationError) -> str:
    """Field locations and messages only: the rejected input values could be sensitive, and a
    key that is not a schema field is sender-chosen text, so it shows as ``<extra field>``."""
    parts = []
    for err in exc.errors()[:3]:
        loc = ".".join(str(p) if isinstance(p, int) or p in _KNOWN_FIELDS else "<extra field>"
                       for p in err["loc"])
        parts.append(f"{loc or 'value'}: {err['msg']}")
    return "; ".join(parts)


def _json_problem(exc: canon.CanonError) -> str:
    """The parser's complaint without a duplicate key's name, which is sender-chosen text."""
    text = str(exc)
    return "duplicate JSON key" if text.startswith("duplicate JSON key") else text


def _fingerprint(payload: Any) -> str | None:
    """Digest of the received payload, used only to keep audit ids distinct."""
    try:
        if isinstance(payload, (bytes, bytearray)):
            return canon.sha256_hex(bytes(payload))
        if isinstance(payload, str):
            return canon.sha256_hex(payload.encode("utf-8", "replace"))
        return canon.digest(payload)
    except Exception:  # not canonical JSON: the audit id still covers every other field
        return None


def _parse_object(payload: Any, what: str = "payload") -> dict[str, Any]:
    """The received JSON object: a dict (copied) or JSON text/bytes parsed strictly
    (duplicate keys, NaN and Infinity are rejected)."""
    try:
        if isinstance(payload, (bytes, bytearray, str)):
            payload = canon.parse_json(bytes(payload) if isinstance(payload, bytearray) else payload)
        elif isinstance(payload, dict):
            payload = copy.deepcopy(payload)
    except canon.CanonError as exc:
        raise _Refused(f"{what} is not strict JSON: {_json_problem(exc)}") from None
    except RecursionError:
        raise _Refused(f"{what} is nested too deeply") from None
    if not isinstance(payload, dict):
        raise _Refused(f"{what} must be a JSON object")
    return payload


def _digest(raw: dict[str, Any]) -> str:
    try:
        return canon.digest(raw)
    except (canon.CanonError, RecursionError):
        raise _Refused("payload cannot be canonicalised as JSON") from None


def _validate(model: type[BaseModel], data: Any, target: Any = None, what: str = "payload"):
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise _Refused(f"{what} failed schema validation: {_schema_errors(exc)}", target) from None


def _audit(store: Store, *, action: str, actor: str, outcome: str, at: datetime,
           target_id: Any = None, detail: str | None = None,
           fingerprint: str | None = None) -> AuditEvent:
    """Append one audit event whose id is derived from all of its fields."""
    target = target_id if _is_ident(target_id) else None
    text = _clip(detail) if detail else None
    audit_id = canon.make_id("aud", action, actor, target, outcome, text, format_utc(at),
                             fingerprint)
    event = AuditEvent(audit_id=audit_id, at=at, actor=actor, action=action, target_id=target,
                       outcome=outcome, detail=text)
    store.append_audit(event)
    return event


def _refusal(store: Store, refused: _Refused, *, action: str, actor: str, at: datetime,
             fingerprint: str | None, target_id: Any = None,
             prefix: str = "refused") -> IngestError:
    """Audit a refusal and return the ``IngestError`` to raise."""
    error = IngestError(refused.reason)
    error.__suppress_context__ = True
    target = refused.target_id if refused.target_id is not None else target_id
    try:
        _audit(store, action=action, actor=actor, outcome="refused", at=at, target_id=target,
               detail=f"{prefix}: {refused.reason}", fingerprint=fingerprint)
    except StoreError as exc:  # the refusal stands; keep why it could not be audited
        error.__cause__ = exc
    return error


def _put_source(store: Store, record: EvidenceRecord, raw: dict[str, Any]) -> PutResult:
    try:
        return store.put_source(record, raw_payload=raw)
    except _STORE_REFUSALS as exc:
        raise _Refused(f"rejected by the passport: {exc}", record.record_id) from None


def _outcome(result: PutResult) -> str:
    return "duplicate" if result.status == "duplicate" else "ok"


def _put_detail(result: PutResult, ident: str, revision: int) -> str:
    if result.status == "duplicate":
        return f"duplicate of {ident} revision {revision}; nothing changed"
    if result.status == "stale":
        return f"stale: {ident} revision {revision} kept as history; a later revision stays active"
    if result.status == "corrected":
        changed = ", ".join(f"{len(ids)} {kind}" for kind, ids in result.invalidated.items()
                            if ids) or "nothing"
        return f"corrected: {ident} revision {revision} is now active; invalidated {changed}"
    return f"inserted {ident} revision {revision}"


# --------------------------------------------------------------------------- merchant evidence


def ingest_merchant_event(store: Store, registry: TrustRegistry | None, payload: Any, *,
                          connector: str = CONNECTOR_WEBHOOK, subject_ref: str | None = None,
                          now: datetime | None = None) -> PutResult:
    """Verify and store one issuer-signed merchant event (webhook push or follow-up lookup).

    ``payload`` is ``{"event": {...}, "signature": {...}}`` as a dict, JSON text or bytes. The
    signature is mandatory and is checked over the raw ``event`` object against the pinned
    ``registry`` (domain ``alibi:record:v1``, a key pinned for ``event.issuer_id``). With
    ``subject_ref``, an event for any other subject is refused. Returns the store's
    ``PutResult`` (``duplicate`` for a replay of identical bytes); raises ``IngestError``.
    """
    at = _clock(now)
    actor = connector if _is_ident(connector) else "unknown_connector"
    fingerprint = _fingerprint(payload)
    try:
        if connector not in ISSUER_CONNECTORS:
            raise _Refused("this connector cannot deliver issuer evidence")
        raw = _parse_object(payload)
        body = raw.get("event")
        target = body.get("record_id") if isinstance(body, dict) else None
        if raw.get("signature") is None:
            raise _Refused("missing issuer signature: issuer evidence must be signed", target)
        payload_sha256 = _digest(raw)
        signed = _validate(SignedMerchantEvent, raw, target)
        event = signed.event
        if subject_ref is not None and event.subject_ref != subject_ref:
            # No target: another subject's record id is not written to this passport's audit.
            raise _Refused("the event's subject is not this connection's subject")
        if registry is None:
            raise _Refused("no trust registry is configured to verify issuer signatures", target)
        try:
            entry = verify(body, signed.signature, registry, domain=SigningDomain.RECORD,
                           issuer_id=event.issuer_id)
        except SignatureError as exc:
            raise _Refused(f"issuer signature rejected: {exc.reason}", target) from None
        record = EvidenceRecord.from_event(
            event, source_trust=SourceTrust.ISSUER_VERIFIED, connector=connector,
            received_at=at, payload_sha256=payload_sha256, issuer_signature=signed.signature,
            signed_body=body)
        with store.transaction():
            result = _put_source(store, record, raw)
            stored = result.record
            detail = (f"{_put_detail(result, stored.record_id, stored.revision)}; "
                      f"issuer signature verified with key {entry.kid}")
            _audit(store, action="ingest_merchant_event", actor=actor, outcome=_outcome(result),
                   at=at, target_id=stored.record_id, detail=detail, fingerprint=fingerprint)
        return result
    except _Refused as refused:
        raise _refusal(store, refused, action="ingest_merchant_event", actor=actor, at=at,
                       fingerprint=fingerprint)


def _strip_trust_claims(raw: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """The event body without backend-only trust fields, and the names that were dropped."""
    stripped: set[str] = set()
    body = raw
    if "event" in raw:  # a pasted {"event", "signature"} wrapper
        if not isinstance(raw["event"], dict) or set(raw) - {"event", *TRUST_CLAIM_KEYS}:
            raise _Refused("an import wrapper may hold only the event and ignored trust claims")
        stripped |= set(raw) - {"event"}
        body = raw["event"]
    body = dict(body)
    for key in TRUST_CLAIM_KEYS:
        if key in body:
            del body[key]
            stripped.add(key)
    if body.get("schema_version") in (SCHEMA_MERCHANT_EVENT, SCHEMA_EVIDENCE):
        del body["schema_version"]  # a pasted event or exported record; re-validated below
    return body, sorted(stripped)


def import_self_supplied(store: Store, payload: Any, *, subject_ref: str,
                         now: datetime | None = None) -> PutResult:
    """Import evidence the holder supplies (a receipt, a customer export) at ``self_supplied``
    trust.

    ``payload`` is an event-like object (dict, JSON text or bytes), optionally wrapped as
    ``{"event": ...}``. No signature is needed and none is honoured: ``signature``,
    ``source_trust`` and the other backend-only fields are dropped. The record id moves into
    the ``self_`` namespace (``self_<id>``, or ``self_<digest>`` when missing); a missing
    ``event_id`` is derived from the payload digest; ``issuer_id`` defaults to
    ``self_supplied`` (a given one stays as data) and ``revision`` to 1. A missing
    ``subject_ref`` becomes the holder's and a different one is refused.
    """
    at = _clock(now)
    fingerprint = _fingerprint(payload)
    try:
        if not _is_ident(subject_ref):
            raise _Refused("subject_ref must be an identifier")
        raw = _parse_object(payload)
        payload_sha256 = _digest(raw)
        body, stripped = _strip_trust_claims(raw)
        target = body.get("record_id")
        claimed = body.get("subject_ref")
        if claimed is None:
            body["subject_ref"] = subject_ref
        elif claimed != subject_ref:
            raise _Refused("the import names a subject other than the holder's", target)
        if body.get("issuer_id") is None:
            body["issuer_id"] = SELF_SUPPLIED_ISSUER
        if body.get("revision") is None:
            body["revision"] = 1
        record_id, moved = body.get("record_id"), False
        if record_id is None:
            content = {k: v for k, v in body.items() if k not in ("event_id", "record_id")}
            body["record_id"] = canon.make_id("self", content)
        elif isinstance(record_id, str) and not record_id.startswith(SELF_SUPPLIED_PREFIX):
            body["record_id"], moved = SELF_SUPPLIED_PREFIX + record_id, True
        if body.get("event_id") is None:
            body["event_id"] = canon.make_id("evt_self", payload_sha256)
        event = _validate(MerchantEvent, body, body.get("record_id"))
        record = EvidenceRecord.from_event(
            event, source_trust=SourceTrust.SELF_SUPPLIED, connector=CONNECTOR_IMPORT,
            received_at=at, payload_sha256=payload_sha256)
        notes = ["trust self_supplied"]
        if stripped:
            notes.append("stripped trust claims: " + ", ".join(stripped))
        if moved:
            notes.append("record id moved into the self_ namespace")
        with store.transaction():
            result = _put_source(store, record, raw)
            stored = result.record
            detail = "; ".join([_put_detail(result, stored.record_id, stored.revision), *notes])
            _audit(store, action="import_self_supplied", actor=HOLDER, outcome=_outcome(result),
                   at=at, target_id=stored.record_id, detail=detail, fingerprint=fingerprint)
        return result
    except _Refused as refused:
        raise _refusal(store, refused, action="import_self_supplied", actor=HOLDER, at=at,
                       fingerprint=fingerprint)


# --------------------------------------------------------------------------- bank feed


def _batch(txns: Any) -> list[Any]:
    if isinstance(txns, (bytes, bytearray, str)):
        try:
            txns = canon.parse_json(bytes(txns) if isinstance(txns, bytearray) else txns)
        except canon.CanonError as exc:
            raise _Refused(f"batch is not strict JSON: {_json_problem(exc)}") from None
        except RecursionError:
            raise _Refused("batch is nested too deeply") from None
        if not isinstance(txns, list):
            raise _Refused("a bank batch must be a JSON array")
        return txns
    if isinstance(txns, (dict, BaseModel)) or not isinstance(txns, Iterable):
        raise _Refused("a bank batch must be a list of transactions")
    return list(txns)


def _bank_txn(item: Any, index: int, *, subject_ref: str, connection_id: str) -> BankTxn:
    data = item.to_json_dict() if isinstance(item, BankTxn) else _parse_object(item, f"txn {index}")
    txn_id = data.get("txn_id")
    target = txn_id if _is_ident(txn_id) else None
    label = f"txn {txn_id}" if target else f"txn at position {index}"
    claimed = data.get("subject_ref")
    if claimed is None:
        data["subject_ref"] = subject_ref  # the connector sets the subject
    elif claimed != subject_ref:
        raise _Refused(f"{label} names a subject other than the connection's", target)
    txn = _validate(BankTxn, data, target, what=label)
    if txn.provenance.connection_id != connection_id:
        raise _Refused(f"{label} comes from another connection", target)
    return txn


def _check_connection_owns(store: Store, txns: list[BankTxn], *, subject_ref: str,
                           connection_id: str) -> None:
    """A held txn id and an account belong to the connection that delivered them, so another
    connection can neither revise that txn nor add txns to that account."""
    foreign_accounts = {t.account_ref for t in store.list_bank_txns(subject_ref)
                        if t.provenance.connection_id != connection_id}
    for txn in txns:
        try:
            held = store.get_bank_txn(txn.txn_id)
        except NotFound:
            held = None
        if held is not None and held.provenance.connection_id != connection_id:
            raise _Refused(f"txn {txn.txn_id} was delivered by another connection", txn.txn_id)
        if txn.account_ref in foreign_accounts:
            raise _Refused(f"txn {txn.txn_id} is on an account another connection delivers",
                           txn.txn_id)


def ingest_bank_txns(store: Store, txns: Any, *, subject_ref: str, connection_id: str,
                     connector: str = CONNECTOR_BANK, cursor: str | None = None,
                     now: datetime | None = None) -> list[PutResult]:
    """Validate and store one connection's feed batch with its cursor, atomically.

    ``txns`` is a list of dicts or ``BankTxn`` models (or a JSON array as text/bytes). Each is
    validated as ``BankTxn``. The connector sets the subject: a txn without ``subject_ref``
    gets ``subject_ref``, one naming another subject is refused, and every txn must come from
    ``connection_id``, which must also be the connection that delivered any txn id or account
    already held. Nothing, cursor included, is written unless the whole batch is valid. Each
    txn gets an audit event, and a batch that commits a cursor (or holds no txns) gets one
    for the batch. Returns one ``PutResult`` per txn.
    """
    at = _clock(now)
    actor = connector if _is_ident(connector) else "unknown_connector"
    fingerprint = _fingerprint(txns) if isinstance(txns, (bytes, bytearray, str, list, tuple)) \
        else None
    try:
        for name, value in (("connector", connector), ("subject_ref", subject_ref),
                            ("connection_id", connection_id)):
            if not _is_ident(value):
                raise _Refused(f"{name} must be an identifier")
        if cursor is not None and not (isinstance(cursor, str) and 1 <= len(cursor) <= 300):
            raise _Refused("cursor must be text of 1 to 300 characters", connection_id)
        prepared = [_bank_txn(item, index, subject_ref=subject_ref, connection_id=connection_id)
                    for index, item in enumerate(_batch(txns))]
        with store.transaction():
            _check_connection_owns(store, prepared, subject_ref=subject_ref,
                                   connection_id=connection_id)
            try:
                results = store.put_bank_txns(prepared, connector=connector, cursor=cursor,
                                              connection_id=connection_id)
            except (IdempotencyConflict, RevisionConflict) as exc:
                raise _Refused(f"rejected by the passport: {exc}", connection_id) from None
            for result in results:
                txn = result.record
                detail = f"{_put_detail(result, txn.txn_id, txn.revision)}; via {connection_id}"
                _audit(store, action="ingest_bank_txns", actor=actor, outcome=_outcome(result),
                       at=at, target_id=txn.txn_id, detail=detail, fingerprint=canon.digest(txn))
            if cursor is not None or not results:
                # The cursor text stays out of the detail; its digest keeps audit ids distinct.
                note = "cursor committed" if cursor is not None else "nothing changed"
                _audit(store, action="ingest_bank_txns", actor=actor, outcome="ok", at=at,
                       target_id=connection_id,
                       detail=f"batch of {len(results)} txns via {connection_id}; {note}",
                       fingerprint=canon.digest([fingerprint, cursor]))
        return results
    except _Refused as refused:
        raise _refusal(store, refused, action="ingest_bank_txns", actor=actor, at=at,
                       fingerprint=fingerprint, target_id=connection_id)


# --------------------------------------------------------------------------- requests


REFUSAL_SCOPE_WIDER = "scope_wider_than_parent"


def _scope_widening(store: Store, parent: RequestForContext,
                    rfc: RequestForContext) -> str | None:
    """Why ``rfc`` is not structurally inside ``parent``'s scope, or ``None`` when it is.

    A request is transaction-scoped when it has no ``period``, else period-scoped. A follow-up
    keeps its parent's kind: a transaction-scoped parent takes only a subset of its
    ``txn_ids``; a period-scoped parent takes only a period inside its own and, when the
    follow-up also names transactions, only ones booked in the parent's period (and in the
    parent's ``txn_ids`` when it has any). Ids and times are never echoed.
    """
    if parent.period is None:
        if rfc.period is not None:
            return "the parent request is transaction-scoped, so a follow-up cannot ask for a period"
        allowed = set(parent.txn_ids or ())
        outside = [t for t in rfc.txn_ids or [] if t not in allowed]
        if outside:
            return (f"txn_ids name {len(outside)} transaction(s) outside the parent request's "
                    "txn_ids")
        return None
    if rfc.period is None:
        return "the parent request is period-scoped, so a follow-up must name a period inside it"
    if rfc.period.from_ < parent.period.from_ or rfc.period.to > parent.period.to:
        return "the follow-up's period is not inside the parent request's period"
    for txn_id in rfc.txn_ids or []:
        if parent.txn_ids is not None and txn_id not in parent.txn_ids:
            return "txn_ids name a transaction outside the parent request's txn_ids"
        try:
            booked_at = store.get_bank_txn(txn_id).booked_at
        except NotFound:
            return "txn_ids name a transaction the passport does not hold"
        if not parent.period.contains(booked_at):
            return "txn_ids name a transaction booked outside the parent request's period"
    return None


def _check_rfc_scope(store: Store, rfc: RequestForContext,
                     account_refs: Collection[str] | None) -> None:
    """The subject must be held, every txn must be the subject's and on one of the bank's
    own accounts (fail closed: txn_ids without ``account_refs`` are refused), and a parent
    must be this issuer's request for this subject whose scope contains the follow-up's."""
    target = rfc.request_id
    if not store.list_bank_txns(rfc.subject_ref) and not store.list_sources(rfc.subject_ref):
        raise _Refused("the passport holds no subject with that subject_ref", target)
    if rfc.txn_ids and account_refs is None:
        raise _Refused("txn_ids cannot be checked: the authenticated bank's accounts were not "
                       "given", target)
    allowed = set(account_refs or ())
    for txn_id in rfc.txn_ids or []:
        try:
            txn = store.get_bank_txn(txn_id)
        except NotFound:
            txn = None
        if txn is None or txn.subject_ref != rfc.subject_ref or txn.account_ref not in allowed:
            raise _Refused("txn_ids include a transaction outside this request's scope", target)
    if rfc.parent_request_id is not None:
        try:   # only this bank's own requests: another bank's ids are invisible here
            parent = store.get_request(rfc.parent_request_id, issuer=rfc.issuer)
        except NotFound:
            parent = None
        if parent is None or parent.issuer != rfc.issuer or parent.subject_ref != rfc.subject_ref:
            raise _Refused("parent_request_id does not name an earlier request from this issuer "
                           "for this subject", target)
        widening = _scope_widening(store, parent, rfc)
        if widening is not None:
            raise _Refused(f"{REFUSAL_SCOPE_WIDER}: {widening}", target)


def receive_rfc(store: Store, registry: TrustRegistry | None, payload: Any, *,
                authenticated_issuer: str, now: datetime | None = None,
                account_refs: Collection[str] | None = None) -> RequestForContext:
    """Accept a bank's Request for Context and return the stored request.

    ``payload`` is ``{"request": {...}, "signature": {...}}`` or a bare request (dict, JSON
    text or bytes). The authenticated issuer must be the request's ``issuer`` and its
    ``audience``. A present signature must verify over the raw request (domain
    ``alibi:request:v1``, a key pinned for that issuer). The request must not have expired, its
    subject must be one the passport holds, every ``txn_id`` must belong to that subject and
    sit on one of ``account_refs`` (the authenticated bank's own accounts; a request naming
    txn_ids is refused without them), and a ``parent_request_id`` must name an earlier request
    from the same issuer for the same subject whose scope contains this one (same scope kind,
    a period inside the parent's, ``txn_ids`` within the parent's; otherwise the refusal
    reads ``scope_wider_than_parent: ...``). Request ids are the issuer's own namespace: the
    same issuer sending the same ``request_id`` with identical content is idempotent and with
    different content is refused, while another bank may use the same id for its own request. Another bank's use of that id neither conflicts nor shows: a parent is
    looked up among this bank's own requests only, and naming another bank's request is refused
    exactly like naming none.
    """
    at = _clock(now)
    fingerprint = _fingerprint(payload)
    sender = authenticated_issuer if _is_ident(authenticated_issuer) else None
    prefix = f"refused from {sender}" if sender else "refused from an unauthenticated sender"
    try:
        if sender is None:
            raise _Refused("the sender is not authenticated")
        raw = _parse_object(payload)
        if "request" in raw:
            body = raw["request"]
            target = body.get("request_id") if isinstance(body, dict) else None
            signed = _validate(SignedRequest, raw, target)
            rfc, envelope = signed.request, signed.signature
        else:
            body, envelope = raw, None
            rfc = _validate(RequestForContext, raw, raw.get("request_id"))
        target = rfc.request_id
        if rfc.issuer != sender:
            raise _Refused("the authenticated sender is not the request's issuer", target)
        if rfc.audience != sender:
            raise _Refused("the request's audience must be the authenticated bank", target)
        if envelope is not None:
            if registry is None:
                raise _Refused("no trust registry is configured to verify request signatures",
                               target)
            try:
                verify(body, envelope, registry, domain=SigningDomain.REQUEST, issuer_id=rfc.issuer)
            except SignatureError as exc:
                raise _Refused(f"request signature rejected: {exc.reason}", target) from None
        if at >= rfc.expires_at:
            raise _Refused("the request has expired", target)
        _check_rfc_scope(store, rfc, account_refs)
        with store.transaction():
            try:
                status = store.save_request(rfc)
            except IdempotencyConflict:
                raise _Refused("request_id was already received with different content",
                               target) from None
            stored = store.get_request(rfc.request_id, issuer=rfc.issuer)
            signed_note = "signed" if envelope is not None else "unsigned"
            verb = "duplicate" if status == "duplicate" else "accepted"
            _audit(store, action="receive_rfc", actor=CONNECTOR_RFC,
                   outcome="duplicate" if status == "duplicate" else "ok", at=at,
                   target_id=rfc.request_id, fingerprint=fingerprint,
                   detail=f"{verb} from {sender}; {signed_note}; {len(rfc.questions)} questions")
        return stored
    except _Refused as refused:
        raise _refusal(store, refused, action="receive_rfc", actor=CONNECTOR_RFC, at=at,
                       fingerprint=fingerprint, prefix=prefix)
