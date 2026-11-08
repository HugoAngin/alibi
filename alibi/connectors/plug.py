"""Passport-side plug: the sinks the simulated services push into, and the connector callables
the Gateway takes (``settlement_lookup``, ``txn_refresh``).

This module is the only place the connectors touch the passport. Everything goes through
``alibi.api``: signed merchant payloads through ``ingest_merchant_event`` (webhook), bank-signed
requests through ``receive_rfc``. Refusals come back as data (``{"status": "refused", ...}``),
never as exceptions, so a simulated service can report them without knowing the core.

Phase 1 of S2 ships the sinks; the gateway callables and the merchant/bank clients follow in
the same file (phase 2).
"""

from __future__ import annotations

from collections.abc import Callable, Collection
from datetime import datetime
from typing import Any

from alibi import api
from alibi.connectors.case_source import MappingError, parse_time
from alibi.core.ingest import IngestError
from alibi.core.signing import TrustRegistry
from alibi.core.store import Store, StoreError

__all__ = ["RfcSink", "Sink", "SinkResult", "rfc_sink", "webhook_sink"]

# A merchant webhook sink: (signed payload, received_at ISO text) -> outcome dict.
Sink = Callable[[dict[str, Any], str], dict[str, Any]]
# A bank RFC sink: (signed request, authenticated issuer, account refs, now ISO text) -> outcome.
RfcSink = Callable[[dict[str, Any], str, Collection[str] | None, str], dict[str, Any]]
SinkResult = dict[str, Any]

OUTCOMES = ("inserted", "duplicate", "corrected", "stale", "refused", "error")


def _clip(text: Any, limit: int = 300) -> str:
    text = " ".join(str(text).split()) or "-"
    return text if len(text) <= limit else text[: limit - 3] + "..."


def webhook_sink(store: Store, registry: TrustRegistry, *, subject_ref: str | None = None,
                 now: datetime | None = None) -> Sink:
    """The passport's merchant webhook as an in-process callable.

    The merchant calls ``sink(payload, received_at)``. The payload is ingested with
    ``alibi.api.ingest_merchant_event`` at ``received_at`` (or ``now`` when given, for a fixed
    clock), bound to ``subject_ref`` when given. Returns ``{"status": inserted | duplicate |
    corrected | stale | refused | error, "record_id", "revision", "reason"}``.
    """

    def sink(payload: dict[str, Any], received_at: str) -> SinkResult:
        try:
            at = now if now is not None else parse_time(received_at, "received_at")
        except MappingError as exc:
            return {"status": "refused", "reason": _clip(exc)}
        try:
            put = api.ingest_merchant_event(store, payload, registry=registry,
                                            subject_ref=subject_ref, now=at)
        except IngestError as exc:
            return {"status": "refused", "reason": _clip(exc.reason)}
        except StoreError as exc:
            return {"status": "error", "reason": _clip(f"passport store error: {exc}")}
        result: SinkResult = {"status": put.status, "record_id": put.record.record_id,
                              "revision": put.record.revision, "active": put.active}
        invalidated = {k: v for k, v in (put.invalidated or {}).items() if v}
        if invalidated:
            result["invalidated"] = invalidated
        return result

    return sink


def rfc_sink(store: Store, registry: TrustRegistry) -> RfcSink:
    """The passport's RFC intake as an in-process callable for the mock bank.

    ``sink(signed_request, authenticated_issuer, account_refs, now)`` calls
    ``alibi.api.receive_rfc``. Returns ``{"status": "accepted" | "refused", "request_id",
    "reason"}``.
    """

    def sink(payload: dict[str, Any], authenticated_issuer: str,
             account_refs: Collection[str] | None, now: str) -> SinkResult:
        try:
            at = parse_time(now, "now")
        except MappingError as exc:
            return {"status": "refused", "reason": _clip(exc)}
        try:
            rfc = api.receive_rfc(store, payload, authenticated_issuer=authenticated_issuer,
                                  registry=registry, account_refs=account_refs, now=at)
        except IngestError as exc:
            return {"status": "refused", "reason": _clip(exc.reason)}
        except StoreError as exc:
            return {"status": "error", "reason": _clip(f"passport store error: {exc}")}
        return {"status": "accepted", "request_id": rfc.request_id, "audience": rfc.audience}

    return sink
