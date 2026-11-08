"""The mock resale platform: the merchant side of the simulated services (SERVICES.md, "Mock
merchant").

``create_merchant_app(sink=None, *, keystore, passport_url=None, case_loader=...)`` builds a
FastAPI app that

* loads one team case without gold (``POST /admin/seed``), keeps a ``CaseClock``, and pushes
  every visible ``merchant_deliveries`` entry to the passport in arrival order, duplicates,
  conflicts and stale revisions included, so the passport decides their fate;
* serves the follow-up lookup routes (``/records/{id}``, ``/settlements``, ``/payouts``) over
  ``CaseClock.lookup``, so it serves exactly what the data layer defines, including lookup-only
  records that appear at their ``available_from`` and corrections made through
  ``POST /admin/correct``;
* signs everything with ``keys.sign_merchant_event`` (kid ``merchant_mock``, issuer
  ``mock-marketplace``). Pushes carry exactly ``{"event", "signature"}``; lookups return
  ``{"event", "signature", "extra"}``, where ``extra`` holds the case fields the core type cannot
  hold (``payout_reference``, ``status_as_of``, ...) and never enters a signed payload.

The sink is ``alibi.connectors.plug.Sink``: ``sink(payload, received_at) -> {"status", ...}``.
``http_sink(url)`` is the HTTP form of it (POST to a running passport). Without a sink, pushes
are recorded with outcome ``not_delivered`` and the lookup routes still work.

State lives on ``app.state.merchant`` (``MerchantState``): the clock, the delivery log with the
exact signed payload of each push and its canonical sha256 (for ``/admin/retry``), the
corrections, and the served-lookups log (eval's ``lookups`` count). Admin routes are
unauthenticated local demo controls. Nothing here logs tokens or keys.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import httpx
from fastapi import Body, FastAPI, Query

from alibi.connectors import keys
from alibi.connectors.case_source import (
    CaseClock,
    MappingError,
    load_case_without_gold,
    map_merchant_event,
    parse_time,
)
from alibi.connectors.http import Refusal, install_refusals
from alibi.connectors.plug import Sink
from alibi.core import canon
from alibi.core.schemas import format_utc
from alibi.core.signing import DemoKeystore

__all__ = [
    "CORRECTION_IDENTITY_FIELDS", "MAX_RANGE_DAYS", "OUTCOMES", "SERVICE_NAME", "Delivery",
    "Lookup", "MerchantState", "create_merchant_app", "http_sink",
]

SERVICE_NAME = "mock-marketplace"
OUTCOMES = ("inserted", "duplicate", "corrected", "stale", "refused", "error", "not_delivered")
# Case fields a correction may never change: the record's identity. ``revision`` is set by the
# correction itself (latest + 1), so a posted value would be silently overridden; it is refused.
CORRECTION_IDENTITY_FIELDS = frozenset({"record_id", "issuer_id", "subject_ref", "kind",
                                        "event_id", "revision"})
MAX_RANGE_DAYS = 31

CaseLoader = Callable[[int], dict[str, Any]]


# --------------------------------------------------------------------------- state


@dataclass
class Delivery:
    """One push to the passport: what was sent, when, and what the passport said."""

    seq: int
    event_id: str
    record_id: str
    revision: int
    received_at: str
    outcome: str
    reason: str | None
    payload: dict[str, Any]
    payload_sha256: str

    def summary(self) -> dict[str, Any]:
        """The log entry the admin routes return (never the payload itself)."""
        return {"seq": self.seq, "event_id": self.event_id, "record_id": self.record_id,
                "revision": self.revision, "received_at": self.received_at,
                "outcome": self.outcome, "reason": self.reason,
                "payload_sha256": self.payload_sha256}


@dataclass
class Lookup:
    """One served lookup, for eval's ``lookups`` count."""

    at: str
    route: str
    query: dict[str, Any]
    record_ids: list[str]

    def summary(self) -> dict[str, Any]:
        return {"at": self.at, "route": self.route, "query": self.query,
                "record_ids": list(self.record_ids)}


@dataclass
class MerchantState:
    """Everything the mock merchant knows: one seeded case and what it has done with it."""

    keystore: DemoKeystore
    sink: Sink | None
    clock: CaseClock | None = None
    pushed: list[Delivery] = field(default_factory=list)
    corrections: list[dict[str, Any]] = field(default_factory=list)
    lookups: list[Lookup] = field(default_factory=list)
    delivered: set[int] = field(default_factory=set)       # indexes into the case's pool
    cached: dict[str, Delivery] = field(default_factory=dict)   # event_id -> first push

    # ----------------------------------------------------------------- the case and clock

    def require_clock(self) -> CaseClock:
        """The clock of the seeded case; ``404 unknown_case`` before any seed."""
        if self.clock is None:
            raise Refusal("unknown_case", "no case is seeded; POST /admin/seed first")
        return self.clock

    def seed(self, case: dict[str, Any], as_of: str | None) -> None:
        """Reset everything and start the clock on ``case`` (gold removed by ``CaseClock``)."""
        self.clock = CaseClock(case, as_of)
        self.pushed = []
        self.corrections = []
        self.lookups = []
        self.delivered = set()
        self.cached = {}

    @property
    def pool(self) -> list[dict[str, Any]]:
        """The case's pushed deliveries, corrections appended (shared with ``CaseClock.lookup``)."""
        return self.require_clock().case.setdefault("merchant_deliveries", [])

    # ----------------------------------------------------------------- pushing

    def pending(self) -> list[int]:
        """Indexes of visible deliveries not pushed yet, in arrival order.

        Arrival order is the list position of ``merchant_deliveries`` (the data README defines
        the list as "webhook pushes in arrival order", and ``gold.ingest`` indexes it by
        position), not ``received_at``: cases 4, 5, 6, 41 and 50 carry entries whose
        ``received_at`` is earlier than their predecessors', and eval zips the push log against
        gold by position.
        """
        clock = self.require_clock()
        due = []
        for index, entry in enumerate(self.pool):
            if index in self.delivered:
                continue
            if parse_time(entry["received_at"], "received_at") <= clock.as_of:
                due.append(index)
        return due

    def push_pending(self) -> list[Delivery]:
        """Push every visible, not yet pushed delivery, strictly in arrival order."""
        out = []
        for index in self.pending():
            entry = self.pool[index]
            self.delivered.add(index)
            out.append(self.push(entry["event"], entry["received_at"]))
        return out

    def push(self, case_event: dict[str, Any], received_at: str) -> Delivery:
        """Map, sign and send one case event; log the outcome. Never raises for a sink failure."""
        seq = len(self.pushed) + 1
        try:
            body = map_merchant_event(case_event).body
        except MappingError as exc:
            delivery = Delivery(seq, str(case_event.get("event_id")), str(case_event.get("record_id")),
                                int(case_event.get("revision") or 0), received_at, "error",
                                f"mapping failed: {exc}", {}, "")
            self.pushed.append(delivery)
            return delivery
        payload = keys.sign_merchant_event(body, self.keystore)
        sha = canon.digest(payload)
        outcome = self.send(payload, received_at)
        delivery = Delivery(seq, body["event_id"], body["record_id"], body["revision"],
                            received_at, outcome["status"], outcome.get("reason"), payload, sha)
        self.pushed.append(delivery)
        self.cached.setdefault(delivery.event_id, delivery)
        return delivery

    def send(self, payload: dict[str, Any], received_at: str) -> dict[str, Any]:
        """Hand a signed payload to the sink; a sink that raises or answers badly is ``error``."""
        if self.sink is None:
            return {"status": "not_delivered"}
        try:
            result = self.sink(payload, received_at)
        except Exception as exc:  # noqa: BLE001 - a sink failure must never break the route
            return {"status": "error", "reason": f"sink raised {type(exc).__name__}"}
        if not isinstance(result, dict) or result.get("status") not in OUTCOMES:
            return {"status": "error", "reason": "sink returned no recognised status"}
        return {"status": result["status"], "reason": _text(result.get("reason"))}

    def retry(self, event_id: str) -> dict[str, Any]:
        """Re-send the exact cached payload of a pushed event; ``404 unknown_event`` otherwise."""
        cached = self.cached.get(event_id)
        if cached is None or not cached.payload:
            raise Refusal("unknown_event", f"event {event_id} has not been pushed")
        payload = copy.deepcopy(cached.payload)
        sha = canon.digest(payload)
        outcome = self.send(payload, cached.received_at)
        self.pushed.append(Delivery(len(self.pushed) + 1, cached.event_id, cached.record_id,
                                    cached.revision, cached.received_at, outcome["status"],
                                    outcome.get("reason"), payload, sha))
        return {"event_id": event_id, "payload_sha256": sha,
                "same_bytes": sha == cached.payload_sha256, "outcome": outcome["status"],
                "reason": outcome.get("reason")}

    # ----------------------------------------------------------------- corrections

    def latest_visible(self, record_id: str) -> dict[str, Any]:
        """The highest visible revision of a record; ``404 unknown_record`` when none."""
        clock = self.require_clock()
        found = clock.lookup(record_id=record_id)
        if not found:
            raise Refusal("unknown_record", f"no record {record_id} is visible at {clock.as_of_text}")
        return max(found, key=lambda ev: (ev.get("revision") or 0))

    def correct(self, record_id: str, changes: dict[str, Any], reason: str) -> dict[str, Any]:
        """Apply case-field changes to the latest visible revision as a new signed revision,
        append it to the pool at the clock time and push it. Identity fields cannot change."""
        if not isinstance(changes, dict) or not changes:
            raise Refusal("invalid_correction", "changes must be a non-empty object of case fields")
        locked = sorted(CORRECTION_IDENTITY_FIELDS & set(changes))
        if locked:
            raise Refusal("invalid_correction",
                          f"a correction cannot change {', '.join(locked)} of record {record_id}")
        if not isinstance(reason, str) or not reason.strip():
            raise Refusal("invalid_correction", "correction_reason must be a non-empty string")
        clock = self.require_clock()
        latest = self.latest_visible(record_id)
        event = copy.deepcopy(latest)
        event.update(copy.deepcopy(changes))
        revision = int(latest["revision"]) + 1
        event["revision"] = revision
        event["event_id"] = f"{latest['event_id']}-r{revision}"
        event["correction_reason"] = reason
        try:
            mapped = map_merchant_event(event)
        except MappingError as exc:
            raise Refusal("invalid_correction", f"record {record_id}: {exc}") from None
        except (TypeError, ValueError):
            # A value of the wrong shape before validation (a scalar covers_record_ids, say).
            raise Refusal("invalid_correction",
                          f"record {record_id}: a changed field has the wrong shape") from None
        received_at = clock.as_of_text
        self.pool.append({"received_at": received_at, "event": event})
        self.delivered.add(len(self.pool) - 1)
        delivery = self.push(event, received_at)
        self.corrections.append({"event_id": delivery.event_id, "record_id": record_id,
                                 "revision": revision, "received_at": received_at,
                                 "fields": sorted(changes), "outcome": delivery.outcome})
        return {**delivery.summary(), "record": self.envelope(mapped.source)}

    # ----------------------------------------------------------------- lookups

    def envelope(self, case_event: dict[str, Any]) -> dict[str, Any]:
        """``{"event", "signature", "extra"}`` for one case event."""
        mapped = map_merchant_event(case_event)
        signed = keys.sign_merchant_event(mapped.body, self.keystore)
        return {**signed, "extra": mapped.extra}

    def serve(self, route: str, query: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
        """Envelope the events, log the lookup, and shape the response."""
        clock = self.require_clock()
        seen: set[str] = set()
        unique = []
        for event in events:
            if event["event_id"] in seen:
                continue
            seen.add(event["event_id"])
            unique.append(event)
        records = [self.envelope(event) for event in unique]
        self.lookups.append(Lookup(clock.as_of_text, route, dict(query),
                                   [event["record_id"] for event in unique]))
        return {"as_of": clock.as_of_text, "query": dict(query), "records": records}

    def payouts_between(self, start: date, end: date) -> list[dict[str, Any]]:
        """Payouts initiated or settled on any day of ``[start, end]`` (inclusive)."""
        clock = self.require_clock()
        out: list[dict[str, Any]] = []
        # Count days rather than stepping past ``end``: ``date.max + 1 day`` overflows.
        for offset in range((end - start).days + 1):
            day = start + timedelta(days=offset)
            out.extend(clock.lookup(payout_date=day.isoformat()))
        return out

    def snapshot(self) -> dict[str, Any]:
        clock = self.clock
        return {"case_number": None if clock is None else clock.case_number,
                "as_of": None if clock is None else clock.as_of_text,
                "pushed": [d.summary() for d in self.pushed],
                "corrections": copy.deepcopy(self.corrections),
                "lookups_served": len(self.lookups)}


# --------------------------------------------------------------------------- helpers


def _text(value: Any) -> str | None:
    if value is None:
        return None
    return " ".join(str(value).split())[:300] or None


def _as_of(value: str | None, name: str = "as_of") -> str | None:
    """Validate a clock timestamp (``400 invalid_time``) and normalise it to a whole UTC second.

    The clock text is compared as a string by ``agents.data.api.lookup`` and as a datetime by
    ``pending()``. A fractional second (``...:00.5Z``) sorts before ``...:00Z`` as text while
    being later as a time, so the two paths would disagree about what is visible; the case
    data is whole-second, so flooring the clock keeps exactly the same visible set.
    """
    if value is None:
        return None
    try:
        moment = parse_time(value, name)
    except MappingError as exc:
        raise Refusal("invalid_time", f"{name}: {exc}") from None
    return format_utc(moment.replace(microsecond=0))


def _date(value: str, name: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise Refusal("invalid_time", f"{name} must be a YYYY-MM-DD date") from None


def _counts(deliveries: list[Delivery]) -> dict[str, int]:
    counts = {name: 0 for name in OUTCOMES}
    for delivery in deliveries:
        counts[delivery.outcome] = counts.get(delivery.outcome, 0) + 1
    return counts


# --------------------------------------------------------------------------- HTTP sink


def http_sink(url: str, client: httpx.Client | None = None) -> Sink:
    """A ``Sink`` that POSTs each payload as JSON to ``{url}?received_at=<utc>`` (a running
    passport's merchant webhook). A 2xx body is the outcome (its ``status`` key); a 4xx body
    gives ``refused`` with the body's ``reason`` (and ``code``); connection errors, other
    statuses and non-JSON bodies give ``error``. Never raises into the route."""
    own = client if client is not None else httpx.Client(timeout=10.0)

    def sink(payload: dict[str, Any], received_at: str) -> dict[str, Any]:
        try:
            response = own.post(url, params={"received_at": received_at}, json=payload)
        except httpx.HTTPError as exc:
            return {"status": "error", "reason": f"passport unreachable: {type(exc).__name__}"}
        try:
            body = response.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            body = {}
        if 200 <= response.status_code < 300:
            status = body.get("status")
            if status not in OUTCOMES:
                return {"status": "error", "reason": f"HTTP {response.status_code} without a status"}
            return {k: v for k, v in body.items() if k in ("status", "reason", "record_id", "revision")}
        if 400 <= response.status_code < 500:
            reason = body.get("reason") or f"HTTP {response.status_code}"
            result = {"status": "refused", "reason": _text(reason)}
            if body.get("code") is not None:
                result["code"] = str(body["code"])
            return result
        return {"status": "error", "reason": f"HTTP {response.status_code}"}

    return sink


# --------------------------------------------------------------------------- the app


def create_merchant_app(sink: Sink | None = None, *, keystore: DemoKeystore,
                        passport_url: str | None = None,
                        case_loader: CaseLoader = load_case_without_gold) -> FastAPI:
    """The mock marketplace as a FastAPI app (routes in SERVICES.md, "Mock merchant").

    ``sink`` receives every push; with ``passport_url`` instead, pushes go over HTTP
    (``http_sink``); with neither they are recorded ``not_delivered``. ``keystore`` must hold
    ``merchant_mock`` (``keys.connector_keystore``). ``case_loader(n)`` returns a case without
    gold or raises ``KeyError`` (``404 unknown_case``).
    """
    if sink is None and passport_url:
        sink = http_sink(passport_url)
    app = FastAPI(title="Alibi mock merchant", docs_url=None, redoc_url=None)
    install_refusals(app)
    state = MerchantState(keystore=keystore, sink=sink)
    app.state.merchant = state

    def load(case_number: int) -> dict[str, Any]:
        try:
            case = case_loader(case_number)
        except (KeyError, LookupError):
            raise Refusal("unknown_case", f"case {case_number} is not available") from None
        if not isinstance(case, dict) or "subject_ref" not in case:
            raise Refusal("unknown_case", f"case {case_number} is not a case object")
        return case

    @app.get("/")
    def identity() -> dict[str, Any]:
        clock = state.clock
        return {"service": SERVICE_NAME, "issuer_id": keys.CASE_ISSUER, "kid": keys.MERCHANT_KID,
                "case_number": None if clock is None else clock.case_number,
                "as_of": None if clock is None else clock.as_of_text}

    @app.post("/admin/seed")
    def seed(case: int = Query(1), as_of: str | None = Query(None)) -> dict[str, Any]:
        loaded = load(case)
        state.seed(loaded, _as_of(as_of))
        clock = state.require_clock()
        pushed = state.push_pending()
        return {"case_number": clock.case_number, "subject_ref": clock.subject_ref,
                "issuer_id": keys.CASE_ISSUER, "as_of": clock.as_of_text,
                "pushed": [d.summary() for d in pushed], "counts": _counts(pushed)}

    @app.post("/admin/advance")
    def advance(as_of: str = Query(...)) -> dict[str, Any]:
        clock = state.require_clock()
        normalised = _as_of(as_of)
        moment = parse_time(normalised, "as_of")
        if moment < clock.as_of:
            raise Refusal("clock_backwards", f"the clock is at {clock.as_of_text} and cannot "
                                             f"move back to {normalised}")
        clock.advance(moment)
        pushed = state.push_pending()
        return {"as_of": clock.as_of_text, "pushed": [d.summary() for d in pushed],
                "counts": _counts(pushed)}

    @app.post("/admin/retry")
    def retry(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        state.require_clock()
        event_id = body.get("event_id") if isinstance(body, dict) else None
        if not isinstance(event_id, str) or not event_id:
            raise Refusal("invalid_request", "body needs a string event_id")
        return state.retry(event_id)

    @app.post("/admin/correct")
    def correct(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        state.require_clock()
        record_id = body.get("record_id") if isinstance(body, dict) else None
        if not isinstance(record_id, str) or not record_id:
            raise Refusal("invalid_request", "body needs a string record_id")
        changes = body.get("changes")
        if not isinstance(changes, dict):
            raise Refusal("invalid_correction", "body needs a changes object")
        return state.correct(record_id, changes, body.get("correction_reason"))

    @app.get("/records/{record_id}")
    def records(record_id: str) -> dict[str, Any]:
        clock = state.require_clock()
        found = clock.lookup(record_id=record_id)
        if not found:
            raise Refusal("not_found", f"no record {record_id} is visible at {clock.as_of_text}")
        return state.serve("/records/{record_id}", {"record_id": record_id}, found)

    @app.get("/settlements")
    def settlements(payout_reference: str | None = Query(None)) -> dict[str, Any]:
        clock = state.require_clock()
        if not payout_reference:
            raise Refusal("invalid_query", "payout_reference is required")
        found = clock.lookup(payout_reference=payout_reference)
        return state.serve("/settlements", {"payout_reference": payout_reference}, found)

    @app.get("/payouts")
    def payouts(covers: str | None = Query(None), payout_date: str | None = Query(None),
                payout_reference: str | None = Query(None),
                from_: str | None = Query(None, alias="from"),
                to: str | None = Query(None)) -> dict[str, Any]:
        clock = state.require_clock()
        given = {name: value for name, value in (("covers", covers), ("payout_date", payout_date),
                                                 ("payout_reference", payout_reference),
                                                 ("from", from_), ("to", to))
                 if value is not None}
        empty = sorted(name for name, value in given.items() if value == "")
        if empty:
            raise Refusal("invalid_query", f"{', '.join(empty)} must not be empty")
        if (from_ is None) != (to is None):
            raise Refusal("invalid_query", "from and to must be given together")
        keys_given = len(given) - (1 if from_ is not None else 0)
        if keys_given != 1:
            raise Refusal("invalid_query", "give exactly one of covers, payout_date, "
                                           "payout_reference, or from and to")
        if covers is not None:
            found = clock.lookup(covers_record_id=covers)
        elif payout_reference is not None:
            found = [ev for ev in clock.lookup(payout_reference=payout_reference)
                     if ev.get("kind") == "payout"]
        elif payout_date is not None:
            # The data layer matches the date as text, so serve the canonical YYYY-MM-DD form
            # whatever ISO spelling (basic, week) ``date.fromisoformat`` accepted.
            found = clock.lookup(payout_date=_date(payout_date, "payout_date").isoformat())
        else:
            start, end = _date(from_, "from"), _date(to, "to")
            if end < start:
                raise Refusal("invalid_query", "from must not be after to")
            if (end - start).days + 1 > MAX_RANGE_DAYS:
                raise Refusal("invalid_query", f"from and to span at most {MAX_RANGE_DAYS} days")
            found = state.payouts_between(start, end)
        return state.serve("/payouts", given, found)

    @app.get("/admin/lookups")
    def lookups() -> dict[str, Any]:
        return {"lookups": [entry.summary() for entry in state.lookups]}

    @app.get("/admin/deliveries")
    def deliveries() -> dict[str, Any]:
        """The webhook log with the exact pushed payloads, in push order, so a passport that
        was not reachable at seed time can replay the pushes (``clients.bootstrap``)."""
        return {"as_of": None if state.clock is None else state.clock.as_of_text,
                "deliveries": [{**d.summary(), "payload": copy.deepcopy(d.payload)}
                               for d in state.pushed]}

    @app.get("/admin/state")
    def admin_state() -> dict[str, Any]:
        return state.snapshot()

    return app
