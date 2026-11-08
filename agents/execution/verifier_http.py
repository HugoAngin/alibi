"""HTTP handlers for bank-verifier probes SP-25 to SP-32, on eval's ProbeSession.

    from agents.execution.api import verifier_http_handlers
    handler = verifier_http_handlers()["SP-25"]
    outcome = handler(ctx)        # ctx: agents.eval.http_probe_runtime.ProbeSession

Outcomes follow agents/eval/http_probe_runtime.py:
    blocked      the check passed: every attack step was refused with a planned code, or a
                 positive control (SP-30, SP-32, SP-31's identical replay) was accepted correctly
    not_blocked  an observed regression: an attack was accepted, or a valid package was refused
    not_run      a prerequisite is missing; raised as ProbeUnavailable so eval records the reason

Each handler builds its attack bytes the same way as the local reference run
(agents/execution/verifier_probes.py). Refusal probes first deliver a valid baseline package, so a
bank that refuses everything, or a wrong route, is never credited with a block.

What the ctx must provide (base_urls["routes"], base_urls["fixtures"], tokens):
    routes   "verifier_deliver"  POST signed package bytes into an inbox, template param {recipient}
             "verifier_inbox"    GET a recipient's packages, param {recipient} (optional check)
    fixtures "alibi_package_key", "alibi_package_key_id"          the package key the bank pins
             "merchant_record_key", "merchant_record_key_id"      the issuer-record key it pins
                 keys as 32-byte Ed25519 private seeds in hex (or Ed25519PrivateKey objects)
             optional: "verifier_service" (default "bank"), "verifier_deliver_actor" (default
                 "alibi"), "now" (UTC ISO 8601), "verifier_codes" ({step_id: [codes]}) when the
                 live bank names its refusal codes differently from eval's reference verifier
    tokens   the deliver actor, and bank_a / bank_b for the inbox check

Package ids are unique per call, so reruns against a persistent bank never collide.
"""
from __future__ import annotations

import copy
import hashlib
import uuid
from datetime import datetime, timedelta, timezone

import rfc8785
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agents.execution.verifier_probes import ATTEMPTS, _body, _payout_record

BLOCKED, NOT_BLOCKED, NOT_RUN = "blocked", "not_blocked", "not_run"
ROGUE = Ed25519PrivateKey.from_private_bytes(bytes(range(196, 228)))
PLANNED_CODES = {a["step_id"]: a["codes"] for a in ATTEMPTS}


def _unavailable(reason: str):
    from agents.eval.http_probe_runtime import ProbeUnavailable

    return ProbeUnavailable(reason)


def _private_key(value) -> Ed25519PrivateKey:
    if isinstance(value, Ed25519PrivateKey):
        return value
    try:
        seed = bytes.fromhex(value) if isinstance(value, str) else bytes(value)
        return Ed25519PrivateKey.from_private_bytes(seed)
    except (TypeError, ValueError):
        raise _unavailable("fixture key is not a 32-byte Ed25519 seed") from None


class _Kit:
    def __init__(self, ctx):
        from agents.eval import api as eval_api

        self.ctx, self.api = ctx, eval_api
        self.alibi = _private_key(ctx.require("alibi_package_key"))
        self.alibi_kid = ctx.require("alibi_package_key_id")
        self.merchant = _private_key(ctx.require("merchant_record_key"))
        self.merchant_kid = ctx.require("merchant_record_key_id")
        fixtures = getattr(ctx, "fixtures", {}) or {}
        self.service = fixtures.get("verifier_service", "bank")
        self.actor = fixtures.get("verifier_deliver_actor", "alibi")
        self.codes = {**PLANNED_CODES, **(fixtures.get("verifier_codes") or {})}
        now = fixtures.get("now")
        self.now = (datetime.fromisoformat(now.replace("Z", "+00:00")) if isinstance(now, str)
                    else datetime.now(timezone.utc))
        self.run_id = uuid.uuid4().hex[:8]

    # --- transport ---
    def deliver(self, recipient: str, raw: bytes):
        path = self.ctx.route("verifier_deliver", recipient=recipient)
        resp = self.ctx.http("POST", self.service, path, self.actor, content=raw,
                             headers={"Content-Type": "application/json"})
        if resp.status_code >= 500:
            raise _unavailable(f"bank answered HTTP {resp.status_code}")
        return resp

    def inbox_ids(self, recipient: str) -> list[str] | None:
        try:
            path = self.ctx.route("verifier_inbox", recipient=recipient)
            resp = self.ctx.http("GET", self.service, path, recipient)
        except Exception:
            return None  # optional check
        if resp.status_code != 200:
            return None
        payload = self.ctx.payload(resp)
        rows = payload.get("packages", payload.get("receipts", []))
        return [r.get("package_id") for r in rows if isinstance(r, dict)] if isinstance(rows, list) else None

    def refusal(self, step_id: str, recipient: str, raw: bytes) -> str:
        return self.ctx.denial(self.deliver(recipient, raw), self.codes[step_id])

    def baseline(self) -> None:
        """A refusal only counts if the same route accepts a valid package first."""
        resp = self.deliver("bank_a", self.seal(self.body("baseline")))
        self.ctx.require_success(resp)

    # --- bytes ---
    def pid(self, tag: str) -> str:
        return f"pkg-http-{tag}-{self.run_id}"

    def payout(self) -> dict:
        return {"attachment_id": "att-payout", "trust": "issuer_verified",
                "record": self.api.sign_record(_payout_record(), self.merchant, self.merchant_kid)}

    def body(self, tag: str, attachment=None, *, extra=(), issued=None, expires=None) -> dict:
        issued = issued or self.now - timedelta(minutes=1)
        expires = expires or self.now + timedelta(days=7)
        return _body(self.pid(tag), attachment or self.payout(), extra=extra, issued=issued, expires=expires)

    def seal(self, body: dict, key=None, kid=None) -> bytes:
        return self.api.sign_package(body, key or self.alibi, kid or self.alibi_kid,
                                     approved_body_digest=self.api.body_digest(body))


def _receipt_digest(ctx, resp) -> str | None:
    payload = ctx.payload(resp)
    r = payload.get("receipt", payload)
    d = r.get("digest") if isinstance(r, dict) else None
    return d.removeprefix("sha256:") if isinstance(d, str) else None


def _all_refused(kit: _Kit, attempts: list[tuple[str, str, bytes]]) -> str:
    """Every (step_id, recipient, bytes) must be refused with a planned code."""
    worst = BLOCKED
    for step_id, recipient, raw in attempts:
        outcome = kit.refusal(step_id, recipient, raw)
        if outcome == NOT_BLOCKED:
            return NOT_BLOCKED
        if outcome == NOT_RUN:
            worst = NOT_RUN
    if worst == NOT_RUN:
        raise _unavailable("a refusal came back without a planned code; see the captured exchange")
    return BLOCKED


# --------------------------------------------------------------------------- handlers

def _sp25(kit: _Kit) -> str:
    kit.baseline()
    env = kit.api.parse_envelope(kit.seal(kit.body("sp25a")))
    env["body"]["claims"][0]["rationale"] = env["body"]["claims"][0]["rationale"].replace("before", "befare", 1)
    env2 = kit.api.parse_envelope(kit.seal(kit.body("sp25b")))
    sig = env2["signature"]["signature"]
    env2["signature"]["signature"] = ("B" if sig[0] != "B" else "C") + sig[1:]
    return _all_refused(kit, [("sp25-body-edit", "bank_a", rfc8785.dumps(env)),
                              ("sp25-raw-flip", "bank_a", rfc8785.dumps(env2))])


def _sp26(kit: _Kit) -> str:
    kit.baseline()
    body = kit.body("sp26")
    outcome = _all_refused(kit, [("sp26-wrong-inbox", "bank_b", kit.seal(body))])
    listed = kit.inbox_ids("bank_b")
    if listed is not None and body["package_id"] in listed:
        return NOT_BLOCKED
    return outcome


def _sp27(kit: _Kit) -> str:
    kit.baseline()
    issued = kit.now - timedelta(minutes=10)
    return _all_refused(kit, [("sp27-expired", "bank_a",
                               kit.seal(kit.body("sp27", issued=issued, expires=issued + timedelta(minutes=1))))])


def _sp28(kit: _Kit) -> str:
    kit.baseline()
    return _all_refused(kit, [
        ("sp28-unknown-key", "bank_a", kit.seal(kit.body("sp28a"), key=ROGUE, kid="alibi-rogue-1")),
        ("sp28-merchant-key", "bank_a", kit.seal(kit.body("sp28b"), key=kit.merchant, kid=kit.merchant_kid)),
    ])


def _sp29(kit: _Kit) -> str:
    kit.baseline()
    bad = kit.payout()
    bad["record"]["body"]["amount_minor"] += 100
    bare = {"attachment_id": "att-payout", "trust": "issuer_verified", "record": {"body": _payout_record()}}
    return _all_refused(kit, [("sp29-bad-issuer-sig", "bank_a", kit.seal(kit.body("sp29a", bad))),
                              ("sp29-missing-issuer-sig", "bank_a", kit.seal(kit.body("sp29b", bare)))])


def _sp30(kit: _Kit) -> str:
    own = {"attachment_id": "att-seller-export", "trust": "self_supplied",
           "record": {"body": {"kind": "receipt", "label": "SYNTHETIC seller export", "amount_minor": 240000, "currency": "GBP"}},
           "provenance": {"connector": "import", "retrieved_at": "2026-11-06T08:00:00Z"}}
    resp = kit.deliver("bank_a", kit.seal(kit.body("sp30", extra=[own])))
    return BLOCKED if 200 <= resp.status_code < 300 else NOT_BLOCKED


def _sp31(kit: _Kit) -> str:
    body = kit.body("sp31")
    raw = kit.seal(body)
    first = kit.deliver("bank_a", raw)
    kit.ctx.require_success(first)
    again = kit.deliver("bank_a", raw)
    want = hashlib.sha256(raw).hexdigest()
    d1, d2 = _receipt_digest(kit.ctx, first), _receipt_digest(kit.ctx, again)
    if not 200 <= again.status_code < 300 or (d1 and d2 and d1 != d2) or (d2 and d2 != want):
        return NOT_BLOCKED  # identical replay not deduplicated
    listed = kit.inbox_ids("bank_a")
    if listed is not None and listed.count(body["package_id"]) > 1:
        return NOT_BLOCKED
    changed = copy.deepcopy(body)
    changed["issued_at"] = (kit.now - timedelta(seconds=59)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return _all_refused(kit, [("sp31-reuse-id", "bank_a", kit.seal(changed))])


def _sp32(kit: _Kit) -> str:
    raw = kit.seal(kit.body("sp32"))
    resp = kit.deliver("bank_a", raw)
    if not 200 <= resp.status_code < 300:
        return NOT_BLOCKED
    d = _receipt_digest(kit.ctx, resp)
    return NOT_BLOCKED if d and d != hashlib.sha256(raw).hexdigest() else BLOCKED


_HANDLERS = {"SP-25": _sp25, "SP-26": _sp26, "SP-27": _sp27, "SP-28": _sp28,
             "SP-29": _sp29, "SP-30": _sp30, "SP-31": _sp31, "SP-32": _sp32}


def _wrap(probe_id: str, fn):
    def handler(ctx) -> str:
        from agents.eval.http_probe_runtime import ProbeUnavailable

        try:
            return fn(_Kit(ctx))
        except ProbeUnavailable:
            raise  # eval's attempt() records it as not_run with the reason
        except Exception as exc:  # a crash is never a block
            raise ProbeUnavailable(f"{probe_id} handler error: {type(exc).__name__}") from None

    handler.__name__ = f"probe_{probe_id.replace('-', '_').lower()}"
    return handler


def handlers() -> dict:
    return {pid: _wrap(pid, fn) for pid, fn in _HANDLERS.items()}
