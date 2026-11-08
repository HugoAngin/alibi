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
from alibi.core.store import Store

__all__ = ["BankClient", "ClientError", "MerchantClient", "bootstrap", "connect"]

LOOKUP_KEYS = ("payout_reference", "record_id", "covers_record_id", "payout_date")
CONNECTOR_ACTOR = "connector"


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


def _raise_for(response) -> Any:
    body = _body(response)
    if 200 <= response.status_code < 300:
        return body
    code = reason = None
    if isinstance(body, dict):
        code, reason = body.get("code"), body.get("reason")
    raise ClientError(response.status_code, code, _clip(reason) if reason else None)


def _summary(envelopes: Iterable[dict[str, Any]]) -> tuple[dict[str, Any], list[EvidenceRef]]:
    items, refs = [], []
    for envelope in envelopes:
        event = envelope.get("event") if isinstance(envelope, dict) else None
        if not isinstance(event, dict):
            continue
        item = {k: event.get(k) for k in ("record_id", "revision", "kind") if k in event}
        if "status" in event:
            item["status"] = event["status"]
        items.append(item)
        try:
            refs.append(EvidenceRef(ref_type="record", ref_id=event["record_id"],
                                    revision=event["revision"]))
        except (KeyError, ValueError):
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
            if name == "record_id" and response.status_code == 404:
                records: list[dict[str, Any]] = []
            else:
                records = list(_raise_for(response).get("records") or [])
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
        for envelope in records:
            event = envelope.get("event") or {}
            if best is None or event.get("revision", 0) > (best["event"].get("revision", 0)):
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
        body = _raise_for(self.client.get("/admin/deliveries", headers=self.headers))
        return list(body.get("deliveries") or [])

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
        return list(_raise_for(self.client.get("/rfc", headers=self.headers)).get("requests") or [])

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
        return list(_raise_for(self.client.get("/audit", headers=self.headers)).get("entries") or [])


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
    moment; by default it is the case at its first RFC time.
    """
    clock = clock or CaseClock(api_case(case), as_of)
    moment = clock.as_of_text
    subject = clock.subject_ref
    summary: dict[str, Any] = {"case": case, "as_of": moment, "subject_ref": subject}

    seeded = merchant.seed(case, moment)
    pushed = seeded.get("pushed") or []
    summary["merchant_pushed"] = len(pushed)
    if any(p.get("outcome") == "not_delivered" for p in pushed):
        sink = sink or webhook_sink(store, registry, subject_ref=subject)
        outcomes = []
        for delivery in merchant.deliveries():
            payload = delivery.get("payload")
            if not isinstance(payload, dict):
                continue
            outcomes.append(sink(payload, delivery.get("received_at", moment)).get("status"))
        summary["replayed"] = outcomes
    else:
        summary["replayed"] = [p.get("outcome") for p in pushed]

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
        try:
            stored = api.receive_rfc(store, issued["rfc"], authenticated_issuer=rfc["audience"],
                                     registry=registry, account_refs=issued.get("account_refs"),
                                     now=clock.as_of)
            rfcs[rfc["request_id"]] = {"status": "accepted", "audience": stored.audience}
        except IngestError as exc:
            rfcs[rfc["request_id"]] = {"status": "refused", "reason": _clip(exc.reason)}
    summary["rfcs"] = rfcs
    return summary


def api_case(case: int) -> dict[str, Any]:
    from alibi.connectors.case_source import load_case_without_gold
    try:
        return load_case_without_gold(case)
    except KeyError:
        raise MappingError(f"case {case} is not a visible development case") from None
