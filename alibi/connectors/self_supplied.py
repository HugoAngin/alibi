"""Self-supplied imports: the holder's own receipts and exports into the passport.

Two doors, both ending in ``alibi.api.import_self_supplied``, which tags the stored record
``self_supplied`` under a ``self_`` id whatever the payload claims:

* ``import_case_imports`` replays a case's visible ``imports`` (``CaseClock.visible_imports``),
  each mapped with ``case_source.map_import`` and stored at its own ``received_at``.
* ``import_record`` takes one holder-supplied record: a case-shaped one (``amount_minor``, or a
  case kind without ``amount_pence``) is mapped first; a core-shaped one passes straight through.

A record that fits neither schema is ``IngestError`` with a reason naming fields, positions and
identifiers only (never the record's text), and a refused audit event, like a core refusal.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import TypeAdapter, ValidationError

from alibi import api
from alibi.connectors.case_source import KIND_MAP, CaseClock, MappingError, map_import, parse_time
from alibi.core import canon
from alibi.core.ingest import IngestError
from alibi.core.schemas import AuditEvent, Ident, format_utc
from alibi.core.store import PutResult, Store, StoreError

__all__ = ["import_case_imports", "import_record", "is_case_shaped"]

HOLDER = "holder"               # the core's actor for self-supplied imports
_IDENT = TypeAdapter(Ident)
_REASON_MAX = 300


def _is_ident(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        _IDENT.validate_python(value)
    except ValidationError:
        return False
    return True


def _clip(text: Any, limit: int = _REASON_MAX) -> str:
    text = " ".join(str(text).split()) or "-"
    return text if len(text) <= limit else text[: limit - 3] + "..."


def is_case_shaped(record: Any) -> bool:
    """Whether a record uses the team's case schema rather than the core wire schema: it has
    ``amount_minor``, or its ``kind`` is a case kind (``KIND_MAP``) and it has no
    ``amount_pence``. Anything that is not an object, or whose ``kind`` is not text, is not."""
    if not isinstance(record, dict):
        return False
    if "amount_minor" in record:
        return True
    kind = record.get("kind")
    return isinstance(kind, str) and kind in KIND_MAP and "amount_pence" not in record


def import_case_imports(store: Store, clock: CaseClock, *,
                        since: datetime | str | None = None) -> list[PutResult]:
    """Import the case's holder imports visible at the clock (and after ``since`` when given),
    each mapped with ``map_import`` and stored at its ``received_at``. Returns one
    ``PutResult`` per import, in arrival order. Raises ``IngestError``."""
    results: list[PutResult] = []
    for entry in clock.visible_imports(since):
        at = parse_time(entry["received_at"], "received_at")
        results.append(import_record(store, entry["record"], subject_ref=clock.subject_ref, now=at))
    return results


def _audit_refusal(store: Store, reason: str, at: datetime, target_id: Any,
                   fingerprint: Any) -> None:
    """One refused audit event for a record the connector would not hand to the core."""
    target = target_id if _is_ident(target_id) else None
    detail = _clip(f"refused: {reason}")
    audit_id = canon.make_id("aud", "import_self_supplied", HOLDER, target, "refused", detail,
                             format_utc(at), fingerprint)
    try:
        store.append_audit(AuditEvent(audit_id=audit_id, at=at, actor=HOLDER,
                                      action="import_self_supplied", target_id=target,
                                      outcome="refused", detail=detail))
    except (StoreError, ValidationError):
        pass   # the refusal stands whether or not it could be recorded


def _map_case_record(record: dict[str, Any]) -> dict[str, Any]:
    """``map_import`` with a reason that names fields and identifiers only: the ``event_id`` is
    checked as an identifier before it can appear in a message, and an unknown ``kind`` is named
    as a field, not echoed."""
    event_id = record.get("event_id")
    label = f"event {event_id}" if _is_ident(event_id) else "import"
    if event_id is not None and not _is_ident(event_id):
        raise MappingError(f"{label} has an event_id that is not an identifier")
    kind = record.get("kind")
    if not isinstance(kind, str) or kind not in KIND_MAP:
        raise MappingError(f"{label} has a kind that is not a case kind")
    try:
        return map_import(record).body
    except MappingError as exc:
        text = str(exc)
        if event_id is not None:
            text = text.replace(f"event {event_id}", label)
        raise MappingError(text) from None


def import_record(store: Store, record: Any, *, subject_ref: str, now: datetime) -> PutResult:
    """Import one holder-supplied record at ``now``.

    A case-shaped record is mapped with ``map_import`` (its trust claim and import source never
    reach the body); a core-shaped one is passed through unchanged. Either way the stored record
    is ``self_supplied`` under a ``self_`` id. A record that fits neither schema raises
    ``IngestError`` whose reason names fields and identifiers only, and leaves a refused audit
    event.
    """
    payload = record
    if is_case_shaped(record):
        try:
            payload = _map_case_record(record)
        except MappingError as exc:
            reason = _clip(f"import refused: {exc}")
            _audit_refusal(store, reason, now, record.get("event_id"), _fingerprint(record))
            raise IngestError(reason) from None
    return api.import_self_supplied(store, payload, subject_ref=subject_ref, now=now)


def _fingerprint(record: Any) -> str | None:
    try:
        return canon.digest(record)
    except (canon.CanonError, RecursionError, TypeError, ValueError):
        return None
