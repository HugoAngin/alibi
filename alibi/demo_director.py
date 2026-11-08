"""The demo director: one small HTTP service that drives the live demo from the web apps.

The three Astro sites call it server-side (never from the browser) with the demo bearer:

    GET  /demo/state     {version, stage, as_of, steps, ...}: cheap; the sites poll it for live updates
    POST /demo/withdraw  Maya withdraws her marketplace payout: the clocks move to the balance
                         release, the credit landing in her bank feed, and the bank's monitoring
                         window, so the passport alert and the bank's fraud flag appear
    POST /demo/request   Bank A asks for Maya's alibi: the clocks move to the request's issue time
                         (if behind) and the bank issues and forwards it; the body is the bank's
    POST /demo/reset     every service back to the start of the case, in about two seconds

``version`` is a digest of everything a screen can show (bank state, passport alerts and
requests), so a site re-renders exactly when something changed, whoever changed it.

One clock: every step moves the merchant and the bank through their ``/admin/advance`` routes,
and the holder app reads the bank's clock in-process (``Settings.clock``), so a package is never
signed "in the future" for the bank.

Credential: ``ALIBI_DEMO_TOKEN`` when set (the hosted Workers share it), else the stack's admin
token from the shared tokens file. Missing or wrong is 401. The director binds loopback only;
on Modal the public router forwards ``/demo/*`` to it.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
from collections.abc import Callable
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from alibi.connectors.http import ADMIN

__all__ = ["CASE1_STEPS", "Director", "create_director_app", "demo_token"]

#: Case 1's live demo, in simulated time. Withdraw: the merchant releases the payout to Maya's
#: bank (10-26 18:05), the £2,160 credit lands in her feed (10-28 07:30), and a day later both
#: monitors have flagged it (the passport's unexplained-credit alert is due 24 h after the sync).
CASE1_STEPS = {
    "withdraw": ("2026-10-26T18:05:00Z", "2026-10-28T07:30:00Z", "2026-10-29T08:00:00Z"),
    "flagged_at": "2026-10-29T08:00:00Z",
    "credit_txn": "txn-0001-004",
    "requests": {"bank_a": "rfc-0001-01", "bank_b": "rfc-0001-02"},
}
TIMEOUT = 30.0


def demo_token(tokens: dict[str, str]) -> str:
    return os.environ.get("ALIBI_DEMO_TOKEN", "").strip() or tokens[ADMIN]


class Director:
    """Steps over a running stack. ``stack`` exposes ``urls`` (app, merchant, bank), ``tokens``,
    ``bank_service()`` (the in-process BankService) and ``restart(reset=True)``."""

    def __init__(self, stack: Any, steps: dict[str, Any] | None = None):
        self.stack = stack
        self.steps = steps or CASE1_STEPS
        self.lock = threading.Lock()

    # ------------------------------------------------------------------ helpers

    def _client(self) -> httpx.Client:
        return httpx.Client(timeout=TIMEOUT)

    def _admin(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.stack.tokens[ADMIN]}"}

    def as_of(self) -> str | None:
        bank = self.stack.bank_service()
        return bank.state.as_of if bank is not None and bank.state.seeded else None

    def advance(self, to: str) -> dict[str, Any]:
        """Merchant and bank clocks to ``to`` (never backwards), then the passport's feed sync,
        which also runs the passport's monitor. The app's clock follows the bank's."""
        now = self.as_of()
        if now is not None and now >= to:
            return {"as_of": now, "skipped": True}
        urls = self.stack.urls
        out: dict[str, Any] = {"as_of": to}
        with self._client() as client:
            for name in ("merchant", "bank"):
                r = client.post(f"{urls[name]}/admin/advance", params={"as_of": to}, headers=self._admin())
                out[name] = r.status_code
                if r.status_code >= 400:
                    raise StepError(502, "advance_failed", f"the {name} refused to advance: {r.text[:200]}")
            r = client.post(f"{urls['app']}/connect/bank/sync", json={}, headers={
                "Accept": "application/json", **self.stack.holder_headers()})
            out["feed"] = r.status_code
            if r.status_code >= 400:
                raise StepError(502, "feed_sync_failed", f"the passport feed sync failed: {r.text[:200]}")
        return out

    # ------------------------------------------------------------------ steps

    def withdraw(self) -> dict[str, Any]:
        with self.lock:
            moved = [self.advance(t) for t in self.steps["withdraw"]]
        return {"ok": True, "advanced": moved, **self.state()}

    def request(self, recipient: str = "bank_a") -> tuple[int, dict[str, Any]]:
        request_id = self.steps["requests"].get(recipient)
        if request_id is None:
            raise StepError(400, "unknown_recipient", f"no demo request for {recipient}")
        with self.lock:
            now = self.as_of() or ""
            if now < self.steps["flagged_at"]:
                raise StepError(409, "not_flagged_yet",
                                "nothing is flagged yet: withdraw the payout on the marketplace first")
            bank = self.stack.bank_service()
            rfc = next((r for r in (bank.case or {}).get("rfcs", []) if r.get("request_id") == request_id), None)
            if rfc is not None and isinstance(rfc.get("issued_at"), str):
                self.advance(rfc["issued_at"])
            token = self.stack.tokens[recipient]
            with self._client() as client:
                r = client.post(f"{self.stack.urls['bank']}/rfc", json={"request_id": request_id},
                                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
            try:
                body = r.json()
            except ValueError:
                body = {"code": "bank_bad_response", "reason": f"HTTP {r.status_code} without JSON"}
            return r.status_code, body

    def reset(self) -> dict[str, Any]:
        with self.lock:
            self.stack.restart(reset=True)
        return {"ok": True, "reset": True, **self.state()}

    # ------------------------------------------------------------------ state

    def state(self) -> dict[str, Any]:
        bank = self.stack.bank_service()
        as_of = self.as_of()
        bank_json: dict[str, Any] = bank.state.to_json() if bank is not None else {}
        # Audit trails grow on reads, so they never count as a change a screen should show.
        for rec in (bank_json.get("recipients") or {}).values():
            rec.pop("audit", None)
        passport: dict[str, Any] = {}
        try:
            with self._client() as client:
                r = client.get(f"{self.stack.urls['app']}/passport",
                               headers={"Accept": "application/json", **self.stack.holder_headers()})
                if r.status_code == 200:
                    body = r.json()
                    passport = {k: body.get(k) for k in ("alerts", "requests", "sharing", "transactions")}
        except httpx.HTTPError:
            passport = {"unreachable": True}
        version = hashlib.sha256(json.dumps({"bank": bank_json, "passport": passport},
                                            sort_keys=True, default=str).encode()).hexdigest()[:16]
        req_id = self.steps["requests"]["bank_a"]
        bank_a = bank.state.recipient("bank_a") if bank is not None else None
        requested = bool(bank_a and req_id in bank_a.requests)
        received = [e.package_id for e in (bank_a.inbox.values() if bank_a else [])
                    if e.request_id == req_id and e.status == "verified"]
        withdrawn = bool(as_of and as_of >= self.steps["withdraw"][0])
        flagged = bool(as_of and as_of >= self.steps["flagged_at"])
        stage = ("received" if received else "requested" if requested else "flagged" if flagged
                 else "withdrawn" if withdrawn else "ready")
        return {"version": version, "stage": stage, "as_of": as_of,
                "steps": {"withdrawn": withdrawn, "flagged": flagged, "requested": requested,
                          "received": bool(received)},
                "request_id": req_id, "package_id": received[-1] if received else None,
                "credit_txn": self.steps["credit_txn"], "flagged_at": self.steps["flagged_at"]}


class StepError(Exception):
    def __init__(self, status: int, code: str, reason: str):
        super().__init__(reason)
        self.status, self.code, self.reason = status, code, reason


def create_director_app(director: Director, token: Callable[[], str]) -> FastAPI:
    app = FastAPI(title="alibi demo director", docs_url=None, redoc_url=None, openapi_url=None)

    def authorised(request: Request) -> bool:
        header = request.headers.get("authorization", "")
        given = header[7:].strip() if header.lower().startswith("bearer ") else ""
        expected = token()
        return bool(given) and secrets.compare_digest(given.encode(), expected.encode())

    def refuse(status: int, code: str, reason: str) -> JSONResponse:
        return JSONResponse({"code": code, "reason": reason}, status_code=status)

    @app.middleware("http")
    async def gate(request: Request, call_next):
        if not authorised(request):
            return refuse(401, "not_authenticated", "the demo director needs the demo bearer token")
        return await call_next(request)

    def run(fn: Callable[[], Any]):
        try:
            result = fn()
        except StepError as exc:
            return refuse(exc.status, exc.code, exc.reason)
        except httpx.HTTPError as exc:
            return refuse(503, "service_unreachable", f"a demo service did not answer ({type(exc).__name__})")
        if isinstance(result, tuple):
            status, body = result
            return JSONResponse(body, status_code=status)
        return JSONResponse(result)

    @app.get("/demo/state")
    def state():
        return run(director.state)

    @app.post("/demo/withdraw")
    def withdraw():
        return run(director.withdraw)

    @app.post("/demo/request")
    async def request_(request: Request):
        try:
            body = await request.json()
        except ValueError:
            body = {}
        recipient = body.get("recipient", "bank_a") if isinstance(body, dict) else "bank_a"
        return run(lambda: director.request(str(recipient)))

    @app.post("/demo/reset")
    def reset():
        return run(director.reset)

    return app
