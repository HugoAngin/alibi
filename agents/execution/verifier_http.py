"""HTTP handlers for bank-verifier probes SP-25 to SP-32.

    from agents.execution.api import verifier_http_handlers
    handlers = verifier_http_handlers()          # {"SP-25": callable(ctx) -> str, ...}
    outcome = handlers["SP-25"](ctx)             # "blocked" | "not_blocked" | "not_run"

Each handler builds its attack bytes the same way as the local reference run
(agents/execution/verifier_probes.py) and sends them to the configured bank routes. Refusal
probes return "blocked" when the bank refused every attack step. Controls (SP-30, SP-32) and the
positive half of SP-31 (identical replay) return "not_blocked" when the bank accepted correctly,
so eval reads every result against the probe's kind in PROBES.md. Any missing prerequisite, a 5xx,
a network error, a 404/405 on the delivery route, or a bank that also refuses a valid baseline
package returns "not_run", never "blocked".

The ctx contract (duck-typed; eval's shared HTTP context supplies it):

    ctx.route(name, **params) -> str | None
        Explicitly configured paths. Names used here:
          "deliver"  POST signed package bytes to a recipient's inbox, params recipient
          "inbox"    GET a recipient's received packages, params recipient (optional check)
        None or a missing method means the route is not available -> not_run.
    ctx.http(method, path, *, recipient=None, data=None, headers=None) -> (status: int, body)
        Sends with the named recipient's credential and captures the exchange. body is the
        parsed JSON (dict or list) or raw bytes/str.
    ctx.keys -> {"alibi": (Ed25519PrivateKey, key_id), "merchant": (Ed25519PrivateKey, key_id)}
        The synthetic demo keys the bank under test has pinned (package and issuer-record
        purposes). Without them a valid control cannot be built -> not_run.
    ctx.now() -> datetime (optional, timezone-aware; default: current UTC time)
    ctx.note(probe_id, text) (optional) receives a one-line reason for every result.

Package ids are unique per call (uuid suffix), so reruns against a persistent bank do not collide.
"""
from __future__ import annotations

import copy
import hashlib
import uuid
from datetime import datetime, timedelta, timezone

import rfc8785
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agents.execution.verifier_probes import _body, _payout_record

BLOCKED, NOT_BLOCKED, NOT_RUN = "blocked", "not_blocked", "not_run"
ROGUE = Ed25519PrivateKey.from_private_bytes(bytes(range(196, 228)))


class _NotRun(Exception):
    pass


class _Kit:
    """Everything a handler needs, resolved from ctx once; raises _NotRun when something is missing."""

    def __init__(self, ctx, probe_id: str):
        from agents.eval import api as eval_api

        self.ctx, self.probe_id, self.api = ctx, probe_id, eval_api
        keys = getattr(ctx, "keys", None) or {}
        if "alibi" not in keys or "merchant" not in keys:
            raise _NotRun("ctx.keys lacks the bank's pinned alibi and merchant demo keys")
        self.alibi, self.alibi_kid = keys["alibi"]
        self.merchant, self.merchant_kid = keys["merchant"]
        now = getattr(ctx, "now", None)
        self.now = now() if callable(now) else datetime.now(timezone.utc)
        self.run_id = uuid.uuid4().hex[:8]

    # --- routes and transport ---
    def path(self, name: str, **params) -> str:
        route = getattr(self.ctx, "route", None)
        p = route(name, **params) if callable(route) else None
        if not p:
            raise _NotRun(f"route {name!r} not configured")
        return p

    def deliver(self, recipient: str, raw: bytes) -> tuple[int, object]:
        try:
            status, body = self.ctx.http("POST", self.path("deliver", recipient=recipient), recipient=recipient,
                                         data=raw, headers={"Content-Type": "application/json"})
        except _NotRun:
            raise
        except Exception as exc:
            raise _NotRun(f"transport error: {exc!r}") from exc
        if not isinstance(status, int) or status >= 500:
            raise _NotRun(f"bank returned {status}")
        if status in (404, 405):
            raise _NotRun(f"delivery route answered {status}; treated as missing, not as a refusal")
        return status, body

    def baseline(self) -> None:
        """A refusal only counts if the same route accepts a valid package first."""
        status, _ = self.deliver("bank_a", self.seal(self.body("baseline")))
        if not _accepted(status):
            raise _NotRun(f"precondition: valid package refused with {status}, so refusals prove nothing")

    def inbox_ids(self, recipient: str) -> list[str] | None:
        try:
            status, body = self.ctx.http("GET", self.path("inbox", recipient=recipient), recipient=recipient)
        except Exception:
            return None
        if status != 200:
            return None
        rows = body.get("packages", body.get("receipts", [])) if isinstance(body, dict) else body
        if not isinstance(rows, list):
            return None
        return [r.get("package_id") for r in rows if isinstance(r, dict)]

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


def _refused(status: int) -> bool:
    return 400 <= status < 500


def _accepted(status: int) -> bool:
    return 200 <= status < 300


def _receipt_digest(body) -> str | None:
    if isinstance(body, dict):
        r = body.get("receipt", body)
        d = r.get("digest") if isinstance(r, dict) else None
        return d.removeprefix("sha256:") if isinstance(d, str) else None
    return None


def _all_refused(kit: _Kit, attempts: list[tuple[str, bytes]]) -> tuple[str, str]:
    seen = []
    for recipient, raw in attempts:
        status, _ = kit.deliver(recipient, raw)
        seen.append(status)
        if not _refused(status):
            return NOT_BLOCKED, f"accepted with {status}"
    return BLOCKED, f"refused with {seen}"


# --------------------------------------------------------------------------- handlers

def _sp25(kit: _Kit):
    kit.baseline()
    env = kit.api.parse_envelope(kit.seal(kit.body("sp25a")))
    env["body"]["claims"][0]["rationale"] = env["body"]["claims"][0]["rationale"].replace("before", "befare", 1)
    edited = rfc8785.dumps(env)
    env2 = kit.api.parse_envelope(kit.seal(kit.body("sp25b")))
    sig = env2["signature"]["signature"]
    env2["signature"]["signature"] = ("B" if sig[0] != "B" else "C") + sig[1:]
    return _all_refused(kit, [("bank_a", edited), ("bank_a", rfc8785.dumps(env2))])


def _sp26(kit: _Kit):
    kit.baseline()
    body = kit.body("sp26")
    outcome, why = _all_refused(kit, [("bank_b", kit.seal(body))])
    listed = kit.inbox_ids("bank_b")
    if outcome == BLOCKED and listed is not None and body["package_id"] in listed:
        return NOT_BLOCKED, "refused but listed in bank_b inbox"
    return outcome, why


def _sp27(kit: _Kit):
    kit.baseline()
    issued = kit.now - timedelta(minutes=10)
    return _all_refused(kit, [("bank_a", kit.seal(kit.body("sp27", issued=issued, expires=issued + timedelta(minutes=1))))])


def _sp28(kit: _Kit):
    kit.baseline()
    return _all_refused(kit, [
        ("bank_a", kit.seal(kit.body("sp28a"), key=ROGUE, kid="alibi-rogue-1")),
        ("bank_a", kit.seal(kit.body("sp28b"), key=kit.merchant, kid=kit.merchant_kid)),
    ])


def _sp29(kit: _Kit):
    kit.baseline()
    bad = kit.payout()
    bad["record"]["body"]["amount_minor"] += 100
    bare = {"attachment_id": "att-payout", "trust": "issuer_verified", "record": {"body": _payout_record()}}
    return _all_refused(kit, [("bank_a", kit.seal(kit.body("sp29a", bad))), ("bank_a", kit.seal(kit.body("sp29b", bare)))])


def _sp30(kit: _Kit):
    own = {"attachment_id": "att-seller-export", "trust": "self_supplied",
           "record": {"body": {"kind": "receipt", "label": "SYNTHETIC seller export", "amount_minor": 240000, "currency": "GBP"}},
           "provenance": {"connector": "import", "retrieved_at": "2026-11-06T08:00:00Z"}}
    status, _ = kit.deliver("bank_a", kit.seal(kit.body("sp30", extra=[own])))
    return (NOT_BLOCKED, f"accepted with {status}") if _accepted(status) else (BLOCKED, f"control refused with {status}")


def _sp31(kit: _Kit):
    body = kit.body("sp31")
    raw = kit.seal(body)
    s1, b1 = kit.deliver("bank_a", raw)
    if not _accepted(s1):
        raise _NotRun(f"precondition: valid package refused with {s1}")
    s2, b2 = kit.deliver("bank_a", raw)
    d1, d2 = _receipt_digest(b1), _receipt_digest(b2)
    want = hashlib.sha256(raw).hexdigest()
    if not _accepted(s2) or (d1 and d2 and d1 != d2) or (d2 and d2 != want):
        return NOT_BLOCKED, f"identical replay not deduplicated: {s2}"
    listed = kit.inbox_ids("bank_a")
    if listed is not None and listed.count(body["package_id"]) > 1:
        return NOT_BLOCKED, "identical replay listed twice"
    changed = copy.deepcopy(body)
    changed["issued_at"] = (kit.now - timedelta(seconds=59)).strftime("%Y-%m-%dT%H:%M:%SZ")
    s3, _ = kit.deliver("bank_a", kit.seal(changed))
    if not _refused(s3):
        return NOT_BLOCKED, f"reused package_id with different bytes accepted with {s3}"
    return BLOCKED, f"replay {s2} deduplicated, conflicting bytes refused {s3}"


def _sp32(kit: _Kit):
    raw = kit.seal(kit.body("sp32"))
    status, body = kit.deliver("bank_a", raw)
    if not _accepted(status):
        return BLOCKED, f"control refused with {status}"
    d = _receipt_digest(body)
    if d and d != hashlib.sha256(raw).hexdigest():
        return BLOCKED, "receipt digest does not match delivered bytes"
    return NOT_BLOCKED, f"accepted with {status}" + ("" if d else "; no receipt digest in response")


_HANDLERS = {"SP-25": _sp25, "SP-26": _sp26, "SP-27": _sp27, "SP-28": _sp28,
             "SP-29": _sp29, "SP-30": _sp30, "SP-31": _sp31, "SP-32": _sp32}


def _wrap(probe_id: str, fn):
    def handler(ctx) -> str:
        try:
            outcome, why = fn(_Kit(ctx, probe_id))
        except _NotRun as exc:
            outcome, why = NOT_RUN, str(exc)
        except Exception as exc:  # a crash is never a block
            outcome, why = NOT_RUN, f"handler error: {exc!r}"
        note = getattr(ctx, "note", None)
        if callable(note):
            note(probe_id, why)
        return outcome

    handler.__name__ = f"probe_{probe_id.replace('-', '_').lower()}"
    return handler


def handlers() -> dict:
    return {pid: _wrap(pid, fn) for pid, fn in _HANDLERS.items()}
