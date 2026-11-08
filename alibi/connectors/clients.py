"""Passport-side clients for the simulated services, and the wiring into the Gateway.

A *client* here is anything with httpx's ``get``/``post`` interface: an ``httpx.Client`` with a
``base_url`` for a running service, or ``fastapi.testclient.TestClient(app)`` for an in-process
one. The classes below only translate between the service routes (``SERVICES.md``) and the
callables the core expects:

* ``MerchantClient.settlement_lookup`` is a ``alibi.gateway.SettlementLookup``: it returns the
  signed ``{"event", "signature"}`` the merchant serves for a payout reference, or ``None``.
  The gateway validates, ingests and traces it; nothing is interpreted here.
* ``MerchantClient.lookup`` is the general four-key retrieval. When a ``Gateway`` is passed, each
  call is recorded in the run's trace as a ``tool_call`` by actor ``connector`` with the query
  and a summary of what came back (ids, revisions, kinds, statuses; never amounts or text).
* ``BankClient`` drives one recipient identity of the mock bank with its bearer token.
* ``connect`` bundles the connector callables for ``alibi.api.run_investigation``.
* ``bootstrap`` seeds a passport from the running services: merchant pushes, feed sync, imports
  and the banks' RFCs, all through the real ingest paths.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from datetime import datetime
from typing import Any

from alibi import api
from alibi.connectors.case_source import CaseClock, MappingError, parse_time
from alibi.core.schemas import format_utc
from alibi.connectors.plug import Sink, webhook_sink
from alibi.core.ingest import IngestError
from alibi.core.schemas import EvidenceRef
from alibi.core.signing import TrustRegistry
from alibi.core.store import Store, StoreError

__all__ = ["BankClient", "ClientError", "MerchantClient", "bootstrap", "connect"]

LOOKUP_KEYS = ("payout_reference", "record_id", "covers_record_id", "payout_date")
CONNECTOR_ACTOR = "connector"
# A lookup key value travels in a path or a query string: one line, no path syntax, bounded.
_KEY_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}$")   # the core Ident pattern
# What the trace summary keeps of a merchant answer: short scalars only (``_summary``).
SUMMARY_TEXT_LIMIT = 64
MAX_SAFE_INT = 2**53
# Push outcomes that mean the payload reached *a* passport. ``bootstrap`` replays the webhook
# log when a push reports anything else, or when a landed record is missing from the passport
# it seeds (the merchant's sink was bound to another store).
LANDED = frozenset({"inserted", "duplicate", "corrected", "stale", "refused"})


class ClientError(Exception):
    """A service answered with an error. The message carries the status and the refusal code,
    never a token or payload contents."""

    def __init__(self, status: int, code: str | None, reason: str | None):
        self.status, self.code, self.reason = status, code, reason
        text = f"service refused ({status}"
        if code:
            text += f", {code}"
        text += ")"
        if reason:
            text += f": {reason}"
        super().__init__(text)


def _clip(text: Any, limit: int = 300) -> str:
    text = " ".join(str(text).split()) or "-"
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _body(response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def _refusal_code(response) -> str | None:
    body = _body(response)
    code = body.get("code") if isinstance(body, dict) else None
    return code if isinstance(code, str) else None


def _raise_for(response) -> dict[str, Any]:
    """The JSON object a 2xx answer carries. A refusal raises ``ClientError`` with its code; a
    2xx answer that is not a JSON object raises ``ClientError(status, "malformed_response",
    ...)`` too, so no caller ever indexes a list, ``None`` or text."""
    body = _body(response)
    if 200 <= response.status_code < 300:
        if not isinstance(body, dict):
            raise _malformed(response.status_code, "no JSON object")
        return body
    code = reason = None
    if isinstance(body, dict):
        code, reason = body.get("code"), body.get("reason")
    raise ClientError(response.status_code, _clip(code, 64) if isinstance(code, str) else None,
                      _clip(reason) if isinstance(reason, str) and reason else None)


def _malformed(status: int, what: str) -> ClientError:
    return ClientError(status, "malformed_response", f"the service answered with {what}")


def _list_of_dicts(body: dict[str, Any], key: str, status: int) -> list[dict[str, Any]]:
    """``body[key]`` as a list of JSON objects (missing or null is empty); anything else is a
    malformed answer."""
    items = body.get(key)
    if items is None:
        return []
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise _malformed(status, f"a {key} value that is not a list of objects")
    return items


def _envelopes(body: dict[str, Any], status: int) -> list[dict[str, Any]]:
    """The merchant's ``records`` as envelopes whose ``event`` and ``signature`` are objects
    and whose ``revision`` is an integer, else ``malformed_response``."""
    records = _list_of_dicts(body, "records", status)
    for envelope in records:
        event, signature = envelope.get("event"), envelope.get("signature")
        if not isinstance(event, dict) or not isinstance(signature, dict):
            raise _malformed(status, "a record that is not an {event, signature} envelope")
        revision = event.get("revision")
        if isinstance(revision, bool) or not isinstance(revision, int):
            raise _malformed(status, "a record without an integer revision")
    return records


def _scalar(value: Any, limit: int = SUMMARY_TEXT_LIMIT) -> Any:
    """A short, JSON-safe scalar for the trace summary; anything else is dropped (``None``)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value if abs(value) <= MAX_SAFE_INT else None
    if isinstance(value, str):
        return _clip(value, limit)
    return None


def _summary(envelopes: Iterable[dict[str, Any]]) -> tuple[dict[str, Any], list[EvidenceRef]]:
    """``{"count", "records": [{record_id, revision, kind, status}]}`` for the trace: ids,
    revisions, kinds and statuses as bounded scalars, at most 50 items; a nested value, a long
    string or an unsafe number from the merchant is dropped, so amounts and text never ride
    along."""
    items, refs = [], []
    for envelope in envelopes:
        event = envelope.get("event") if isinstance(envelope, dict) else None
        if not isinstance(event, dict):
            continue
        item = {}
        for name in ("record_id", "revision", "kind", "status"):
            if name in event:
                value = _scalar(event[name])
                if value is not None:
                    item[name] = value
        items.append(item)
        try:
            refs.append(EvidenceRef(ref_type="record", ref_id=event["record_id"],
                                    revision=event["revision"]))
        except (KeyError, TypeError, ValueError):
            pass
    return {"count": len(items), "records": items[:50]}, refs


# --------------------------------------------------------------------------- merchant


class MerchantClient:
    """The passport's view of the mock merchant (``SERVICES.md``, "Mock merchant")."""

    def __init__(self, client, *, headers: dict[str, str] | None = None):
        self.client = client
        self.headers = dict(headers or {})

    # ----------------------------------------------------------------------- lookups

    def lookup(self, *, gateway=None, question_id: str | None = None,
               **key: str) -> list[dict[str, Any]]:
        """Follow-up retrieval by exactly one key (``payout_reference``, ``record_id``,
        ``covers_record_id`` or ``payout_date``). Returns the merchant's envelopes
        ``{"event", "signature", "extra"}``. With ``gateway``, the call and a summary of the
        answer are appended to the run's trace."""
        given = {k: v for k, v in key.items() if v is not None}
        if len(given) != 1 or next(iter(given)) not in LOOKUP_KEYS:
            raise ValueError(f"give exactly one of {', '.join(LOOKUP_KEYS)}")
        (name, value), = given.items()
        if not isinstance(value, str) or not _KEY_VALUE.match(value):
            raise ValueError(f"{name} must be a short identifier without path characters")
        if name == "payout_reference":
            path, params = "/settlements", {"payout_reference": value}
        elif name == "record_id":
            path, params = f"/records/{value}", {}
        elif name == "covers_record_id":
            path, params = "/payouts", {"covers": value}
        else:
            path, params = "/payouts", {"payout_date": value}
        try:
            response = self.client.get(path, params=params, headers=self.headers)
            if name == "record_id" and response.status_code == 404 \
                    and _refusal_code(response) == "not_found":
                # The merchant's "no record is visible" answer. Any other 404 (``unknown_case``
                # before a seed, an unknown route) is a service problem and raises below.
                records: list[dict[str, Any]] = []
            else:
                records = _envelopes(_raise_for(response), response.status_code)
        except Exception as exc:
            if gateway is not None:
                gateway.log_step("tool_call", actor=CONNECTOR_ACTOR, tool="merchant_lookup",
                                 args={name: value}, outcome="unavailable",
                                 reason=_clip(f"merchant lookup failed ({type(exc).__name__})"),
                                 question_id=question_id)
            raise
        if gateway is not None:
            summary, refs = _summary(records)
            gateway.log_step("tool_call", actor=CONNECTOR_ACTOR, tool="merchant_lookup",
                             args={name: value}, result=summary, refs=refs,
                             question_id=question_id)
        return records

    def settlement_lookup(self, payment_ref: str) -> dict[str, Any] | None:
        """``alibi.gateway.SettlementLookup``: the signed record the merchant serves for a payout
        reference (its highest visible revision), without ``extra``; ``None`` when none."""
        records = self.lookup(payout_reference=payment_ref)
        best = None
        for envelope in records:   # every envelope has an integer revision (``_envelopes``)
            if best is None or envelope["event"]["revision"] > best["event"]["revision"]:
                best = envelope
        if best is None:
            return None
        return {"event": best["event"], "signature": best["signature"]}

    # ----------------------------------------------------------------------- admin

    def identity(self) -> dict[str, Any]:
        return _raise_for(self.client.get("/", headers=self.headers))

    def seed(self, case: int, as_of: datetime | str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"case": case}
        if as_of is not None:
            params["as_of"] = as_of if isinstance(as_of, str) else format_utc(as_of)
        return _raise_for(self.client.post("/admin/seed", params=params, headers=self.headers))

    def advance(self, as_of: datetime | str) -> dict[str, Any]:
        value = as_of if isinstance(as_of, str) else format_utc(as_of)
        return _raise_for(self.client.post("/admin/advance", params={"as_of": value},
                                           headers=self.headers))

    def retry(self, event_id: str) -> dict[str, Any]:
        return _raise_for(self.client.post("/admin/retry", json={"event_id": event_id},
                                           headers=self.headers))

    def correct(self, record_id: str, changes: dict[str, Any], reason: str) -> dict[str, Any]:
        return _raise_for(self.client.post(
            "/admin/correct", json={"record_id": record_id, "changes": changes,
                                    "correction_reason": reason}, headers=self.headers))

    def deliveries(self) -> list[dict[str, Any]]:
        """The merchant's webhook log with the exact pushed payloads (``GET /admin/deliveries``),
        for a passport that pulls instead of receiving pushes."""
        response = self.client.get("/admin/deliveries", headers=self.headers)
        return _list_of_dicts(_raise_for(response), "deliveries", response.status_code)

    def state(self) -> dict[str, Any]:
        return _raise_for(self.client.get("/admin/state", headers=self.headers))


# --------------------------------------------------------------------------- bank


class BankClient:
    """One recipient identity (``bank_a`` or ``bank_b``) of the mock bank, or the holder's feed
    connection, driven with its bearer token."""

    def __init__(self, client, token: str, *, recipient: str | None = None):
        self.client = client
        self._token = token
        self.recipient = recipient

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    def __repr__(self) -> str:  # never show the token
        return f"BankClient(recipient={self.recipient!r})"

    def whoami(self) -> dict[str, Any]:
        return _raise_for(self.client.get("/whoami", headers=self.headers))

    def seed(self, case: int, as_of: datetime | str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"case": case}
        if as_of is not None:
            params["as_of"] = as_of if isinstance(as_of, str) else format_utc(as_of)
        return _raise_for(self.client.post("/admin/seed", params=params))

    def advance(self, as_of: datetime | str) -> dict[str, Any]:
        value = as_of if isinstance(as_of, str) else format_utc(as_of)
        return _raise_for(self.client.post("/admin/advance", params={"as_of": value}))

    def issue_rfc(self, request_id: str | None = None,
                  request: dict[str, Any] | None = None) -> dict[str, Any]:
        """``POST /rfc``: the signed request plus ``account_refs`` and ``forwarded``."""
        body: dict[str, Any] = {}
        if request_id is not None:
            body["request_id"] = request_id
        if request is not None:
            body["request"] = request
        return _raise_for(self.client.post("/rfc", json=body, headers=self.headers))

    def requests_sent(self) -> list[dict[str, Any]]:
        response = self.client.get("/rfc", headers=self.headers)
        return _list_of_dicts(_raise_for(response), "requests", response.status_code)

    def deliver(self, package: dict[str, Any]) -> tuple[int, Any]:
        """``POST /deliver``. Returns ``(status_code, body)`` without raising, because a
        rejection (4xx with a receipt) is a normal outcome the caller records."""
        response = self.client.post("/deliver", json=package, headers=self.headers)
        return response.status_code, _body(response)

    def inbox(self) -> dict[str, Any]:
        return _raise_for(self.client.get("/inbox", headers=self.headers))

    def package(self, package_id: str) -> dict[str, Any]:
        return _raise_for(self.client.get(f"/packages/{package_id}", headers=self.headers))

    def attachments(self, package_id: str) -> dict[str, Any]:
        return _raise_for(self.client.get(f"/packages/{package_id}/attachments",
                                          headers=self.headers))

    def disposition(self, package_id: str, decision: str, reason: str,
                    **fields: Any) -> dict[str, Any]:
        body = {"decision": decision, "reason": reason, **fields}
        return _raise_for(self.client.post(f"/packages/{package_id}/disposition", json=body,
                                           headers=self.headers))

    def audit(self) -> list[dict[str, Any]]:
        response = self.client.get("/audit", headers=self.headers)
        return _list_of_dicts(_raise_for(response), "entries", response.status_code)


# --------------------------------------------------------------------------- wiring


def connect(merchant: MerchantClient | None = None, feed=None) -> dict[str, Callable]:
    """The connector keyword arguments for ``alibi.api.run_investigation``: the merchant's
    ``settlement_lookup`` and the feed client's ``refresh`` as ``txn_refresh``. Pass them as
    ``run_investigation(store, request_id, registry=registry, now=now, **connect(m, f))``."""
    out: dict[str, Callable] = {}
    if merchant is not None:
        out["settlement_lookup"] = merchant.settlement_lookup
    if feed is not None:
        out["txn_refresh"] = feed.refresh
    return out


def bootstrap(store: Store, registry: TrustRegistry, *, case: int, merchant: MerchantClient,
              banks: dict[str, BankClient], feed=None, clock: CaseClock | None = None,
              as_of: datetime | str | None = None, sink: Sink | None = None,
              imports: bool = True) -> dict[str, Any]:
    """Seed a passport from the running services for one case, through the real ingest paths.

    1. The merchant is seeded (``POST /admin/seed``). When it was built with an in-process sink,
       its pushes already landed; otherwise its webhook log (``GET /admin/deliveries``) is
       replayed into ``sink`` (default: ``webhook_sink`` over this store), so a merchant without
       a passport URL still fills the passport in arrival order.
    2. The banks are seeded, and the feed client (when given) syncs the bank rows with their
       cursor.
    3. The case's visible imports go through the self-supplied import.
    4. Each bank issues its visible RFCs (``POST /rfc``), which go into ``receive_rfc`` bound to
       that bank and its accounts.

    Returns a summary with per-step counts and outcomes. ``clock`` fixes the subject and the
    moment. Without ``clock`` and ``as_of``, the moment is the merchant's current clock when it
    already serves this case (so a second call never rewinds the services behind the passport),
    else the case's first RFC time.

    The webhook log is replayed into ``sink`` whenever a push did not land in this passport:
    an outcome outside ``LANDED`` (``not_delivered``, ``error``), or a landed record that this
    store does not hold (the merchant pushed to another passport). ``summary["replayed_from"]``
    says which path filled the store: ``"pushes"`` or ``"webhook_log"``.
    """
    if clock is None and as_of is None:
        as_of = _service_clock(merchant, case)
    clock = clock or CaseClock(api_case(case), as_of)
    moment = clock.as_of_text
    subject = clock.subject_ref
    summary: dict[str, Any] = {"case": case, "as_of": moment, "subject_ref": subject}

    seeded = merchant.seed(case, moment)
    pushed = [p for p in seeded.get("pushed") or [] if isinstance(p, dict)]
    summary["merchant_pushed"] = len(pushed)
    if _needs_replay(store, pushed):
        sink = sink or webhook_sink(store, registry, subject_ref=subject)
        outcomes = []
        for delivery in merchant.deliveries():
            payload = delivery.get("payload")
            if not isinstance(payload, dict):
                continue
            received_at = delivery.get("received_at")
            result = sink(payload, received_at if isinstance(received_at, str) else moment)
            outcomes.append(result.get("status") if isinstance(result, dict) else "error")
        summary["replayed"] = outcomes
        summary["replayed_from"] = "webhook_log"
    else:
        summary["replayed"] = [p.get("outcome") for p in pushed]
        summary["replayed_from"] = "pushes"

    for recipient, bank in banks.items():
        bank.seed(case, moment)
    if feed is not None:
        summary["feed"] = feed.sync(now=clock.as_of)

    if imports:
        from alibi.connectors.self_supplied import import_case_imports
        puts = import_case_imports(store, clock)
        summary["imports"] = [put.status for put in puts]

    rfcs: dict[str, Any] = {}
    for rfc in clock.visible_rfcs():
        bank = banks.get(rfc["audience"])
        if bank is None:
            rfcs[rfc["request_id"]] = "no client for " + rfc["audience"]
            continue
        issued = bank.issue_rfc(rfc["request_id"])
        signed, account_refs = issued.get("rfc"), issued.get("account_refs")
        if not isinstance(signed, dict) or not isinstance(signed.get("request"), dict) \
                or not isinstance(signed.get("signature"), dict):
            raise _malformed(201, "a POST /rfc body without a signed rfc")
        if account_refs is not None and (not isinstance(account_refs, list)
                                         or not all(isinstance(a, str) for a in account_refs)):
            raise _malformed(201, "a POST /rfc body whose account_refs is not a list of ids")
        try:
            stored = api.receive_rfc(store, signed, authenticated_issuer=rfc["audience"],
                                     registry=registry, account_refs=account_refs,
                                     now=clock.as_of)
            rfcs[rfc["request_id"]] = {"status": "accepted", "audience": stored.audience}
        except IngestError as exc:
            rfcs[rfc["request_id"]] = {"status": "refused", "reason": _clip(exc.reason)}
    summary["rfcs"] = rfcs
    return summary


def _service_clock(merchant: MerchantClient, case: int) -> str | None:
    """The merchant's current ``as_of`` when it already serves ``case``, else ``None``. A
    merchant that is down or unseeded gives ``None`` (the seed that follows reports it)."""
    try:
        identity = merchant.identity()
    except Exception:  # noqa: BLE001 - the seed call reports the real failure
        return None
    as_of = identity.get("as_of")
    if identity.get("case_number") == case and isinstance(as_of, str):
        return as_of
    return None


def _needs_replay(store: Store, pushed: list[dict[str, Any]]) -> bool:
    """True when some push did not land in ``store``: an outcome outside ``LANDED``, or a
    revision reported inserted, corrected or duplicate that this store does not hold."""
    for push in pushed:
        if push.get("outcome") not in LANDED:
            return True
    for push in pushed:
        if push.get("outcome") not in ("inserted", "corrected", "duplicate"):
            continue
        record_id, revision = push.get("record_id"), push.get("revision")
        if not isinstance(record_id, str) or not isinstance(revision, int):
            continue
        try:
            store.get_source(record_id, revision)
        except StoreError:   # NotFound, or a revision outside the store's range
            return True
    return False


def api_case(case: int) -> dict[str, Any]:
    from alibi.connectors.case_source import load_case_without_gold
    try:
        return load_case_without_gold(case)
    except KeyError:
        raise MappingError(f"case {case} is not a visible development case") from None
