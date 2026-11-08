"""consent_matrix.md flows F1-F3 as black-box HTTP sequences on eval's ProbeSession.

    from agents.execution.consent_flows_http import run_flows
    results = run_flows(base_urls, tokens)      # [{"flow", "outcome", "steps", "reason"}]

    ALIBI_LIVE_CONFIG=path/to/live.json uv run --frozen python -m agents.execution.consent_flows_http

live.json is {"base_urls": {...}, "tokens": {...}} in the shape agents.eval.http_probe_runtime
expects. Route keys and defaults match agents/eval/http_probe_consent.py, so one config drives
both: run, review, approve, dispatch, inbox, package (S4 defaults), plus three with S4-sample
defaults that S2 may override: disposition, followup, remove (the last only if the review does not
carry a remove_action). Fixtures: rfc_bank_a, rfc_bank_b, policy_version.

Outcomes: pass (every step as consent_matrix.md says), fail (a step went the wrong way: the
regression is named), not_run (a route, fixture or setup step was unavailable; never a pass).
Flows share one app state in order F1 -> F2 -> F3, as in consent_matrix.md.
"""
from __future__ import annotations

import json
import os
import re
import sys

APP, HOLDER = "app", "holder"
DEFAULTS = {
    "run": "/requests/{request_id}/run", "review": "/requests/{request_id}",
    "approve": "/requests/{request_id}/approve", "dispatch": "/requests/{request_id}/dispatch",
    "inbox": "/bank/{recipient}/inbox", "package": "/bank/{recipient}/packages/{package_id}",
    "disposition": "/bank/{recipient}/packages/{package_id}/disposition",
    "followup": "/bank/{recipient}/requests",
}


class Fail(Exception):
    """A step contradicted consent_matrix.md: a regression, not a missing prerequisite."""


def _unavailable(msg):
    from agents.eval.http_probe_runtime import ProbeUnavailable

    return ProbeUnavailable(msg)


class Flow:
    def __init__(self, session, name):
        self.s, self.name, self.steps = session, name, []

    def _route(self, key, **values):
        return self.s.route(key, DEFAULTS.get(key), **values)

    def call(self, step, method, key, actor, *, data=None, **values):
        resp = self.s.http(method, APP, self._route(key, **values), actor, data=data)
        if resp.status_code >= 500:
            raise _unavailable(f"{self.name} step {step}: HTTP {resp.status_code}")
        payload = self.s.payload(resp)
        self.steps.append({"step": step, "status": resp.status_code, "code": self.s.code(resp)})
        return resp, payload

    def ok(self, step, method, key, actor, **kw):
        resp, payload = self.call(step, method, key, actor, **kw)
        if not 200 <= resp.status_code < 300:
            raise Fail(f"step {step}: expected success, got {resp.status_code} {self.s.code(resp)}")
        return payload

    def refused(self, step, code, method, key, actor, **kw):
        resp, payload = self.call(step, method, key, actor, **kw)
        if 200 <= resp.status_code < 300:
            raise Fail(f"step {step}: expected refusal {code}, got {resp.status_code}")
        got = self.s.code(resp)
        if got != code:
            raise _unavailable(f"step {step}: refused with {resp.status_code} {got}, consent matrix says {code}")
        return payload


def _review(payload):
    section = payload.get("review")
    return section if isinstance(section, dict) else payload


def _digest(f: Flow, step, rid) -> tuple[dict, str]:
    payload = f.ok(step, "GET", "review", HOLDER, request_id=rid)
    d = (_review(payload).get("approval") or {}).get("digest")
    if not isinstance(d, str) or not re.fullmatch(r"(sha256:)?[0-9a-f]{12,64}", d.strip().lower()):
        raise _unavailable(f"step {step}: review exposes no approval digest")
    return payload, d


def _same(a, b):
    ha, hb = (str(a or "").lower().removeprefix("sha256:"), str(b or "").lower().removeprefix("sha256:"))
    return bool(ha and hb) and (ha.startswith(hb) or hb.startswith(ha))


def _receipt(payload):
    level = payload.get("dispatch", payload)
    r = level.get("receipt") if isinstance(level.get("receipt"), dict) else level
    return {"package_id": level.get("package_id") or r.get("package_id"),
            "digest": r.get("package_digest") or r.get("digest") or level.get("package_digest")}


def _inbox(f: Flow, step, recipient) -> set:
    payload = f.ok(step, "GET", "inbox", recipient, recipient=recipient)
    rows = payload.get("packages") or payload.get("receipts") or []
    return {r.get("package_id") for r in rows if isinstance(r, dict)}


def _approve(f: Flow, step, rid, digest, audience, policy, *, expect=None):
    data = {"digest": digest, "audience": audience, "policy_version": policy}
    if expect:
        return f.refused(step, expect, "POST", "approve", HOLDER, data=data, request_id=rid)
    return f.ok(step, "POST", "approve", HOLDER, data=data, request_id=rid)


# --------------------------------------------------------------------------- flows

def flow_f1(f: Flow, cfg: dict, state: dict) -> None:
    rid, policy = cfg["rfc_bank_a"], cfg["policy_version"]
    f.ok("1-2 run", "POST", "run", HOLDER, request_id=rid)
    review, d1 = _digest(f, "3 review", rid)
    _approve(f, "4 approve D1", rid, d1, "bank_a", policy)
    removable = [i for i in _review(review).get("included", []) if i.get("removable") and i.get("remove_action")]
    if not removable:
        raise _unavailable("step 5: review offers no removable item")
    path = removable[0]["remove_action"]
    resp = f.s.http("POST", APP, path, HOLDER)
    f.steps.append({"step": "5 remove item", "status": resp.status_code, "code": f.s.code(resp)})
    if not 200 <= resp.status_code < 300:
        raise _unavailable(f"step 5: remove returned {resp.status_code}")
    _, d2 = _digest(f, "5 review D2", rid)
    if _same(d1, d2):
        raise Fail("step 5: removing an item did not change the digest")
    before = _inbox(f, "6 inbox before", "bank_a")
    f.refused("6 dispatch stale", "approval_digest_mismatch", "POST", "dispatch", HOLDER, request_id=rid)
    if _inbox(f, "7 inbox after refusal", "bank_a") != before:
        raise Fail("step 7: a refused dispatch reached the bank_a inbox")
    _approve(f, "8 approve D2", rid, d2, "bank_a", policy)
    receipt = _receipt(f.ok("9 dispatch", "POST", "dispatch", HOLDER, request_id=rid))
    if not receipt["package_id"] or not _same(receipt["digest"], d2):
        raise Fail("step 9: dispatch receipt does not carry the approved digest")
    pkg = f.ok("10 bank_a reads P1", "GET", "package", "bank_a", recipient="bank_a", package_id=receipt["package_id"])
    auth = pkg.get("authentication") or {}
    if auth.get("status") not in (None, "valid") or auth.get("valid") is False:
        raise Fail("step 10: bank_a could not authenticate its own package")
    state.update(p1=receipt["package_id"], d2=d2)


def flow_f2(f: Flow, cfg: dict, state: dict) -> None:
    if "p1" not in state:
        raise _unavailable("F2 needs F1's package")
    parent, policy, p1 = cfg["rfc_bank_a"], cfg["policy_version"], state["p1"]
    f.ok("1 request_more", "POST", "disposition", "bank_a", recipient="bank_a", package_id=p1,
         data={"disposition": "request_more", "analyst_name": "ignored-by-server",
               "note": "Need the settlement record for PO-0001-01"})
    f.refused("2b wider scope", "scope_wider_than_parent", "POST", "followup", "bank_a", recipient="bank_a",
              data={"parent_request_id": parent, "period_from": "2026-01-01T00:00:00Z",
                    "period_to": "2026-11-01T00:00:00Z", "question": "Everything this year."})
    created = f.ok("2 follow-up", "POST", "followup", "bank_a", recipient="bank_a",
                   data={"parent_request_id": parent,
                         "question": "Provide the settlement record for payout PO-0001-01."})
    child = created.get("request_id") or (created.get("request") or {}).get("request_id")
    if not child or child == parent:
        raise Fail("step 2: follow-up did not get a new request_id")
    if (created.get("parent_request_id") or (created.get("request") or {}).get("parent_request_id")) not in (None, parent):
        raise Fail("step 2: follow-up has the wrong parent")
    f.ok("5 run follow-up", "POST", "run", HOLDER, request_id=child)
    f.refused("6 dispatch before approval", "approval_missing", "POST", "dispatch", HOLDER, request_id=child)
    _, d3 = _digest(f, "7 review D3", child)
    _approve(f, "7 approve D3", child, d3, "bank_a", policy)
    p2 = _receipt(f.ok("8 dispatch", "POST", "dispatch", HOLDER, request_id=child))["package_id"]
    if not {p1, p2} <= _inbox(f, "9 bank_a inbox", "bank_a"):
        raise Fail("step 9: bank_a inbox lacks P1 or P2")
    f.ok("10 clear P2", "POST", "disposition", "bank_a", recipient="bank_a", package_id=p2,
         data={"disposition": "clear", "analyst_name": "ignored-by-server", "note": "Settlement shown."})
    f.refused("11 clear again", "disposition_final", "POST", "disposition", "bank_a", recipient="bank_a",
              package_id=p2, data={"disposition": "escalate", "analyst_name": "x", "note": "again"})
    state["p2"] = p2


def flow_f3(f: Flow, cfg: dict, state: dict) -> None:
    rid, policy = cfg["rfc_bank_b"], cfg["policy_version"]
    f.ok("2 run", "POST", "run", HOLDER, request_id=rid)
    f.refused("3 dispatch unapproved", "approval_missing", "POST", "dispatch", HOLDER, request_id=rid)
    if "d2" in state:
        _approve(f, "4 approve with bank_a digest", rid, state["d2"], "bank_b", policy, expect="approval_digest_not_current")
    _, d4 = _digest(f, "5 review D4", rid)
    _approve(f, "5 approve D4", rid, d4, "bank_b", policy)
    p3 = _receipt(f.ok("6 dispatch", "POST", "dispatch", HOLDER, request_id=rid))["package_id"]
    b_inbox = _inbox(f, "7 bank_b inbox", "bank_b")
    if p3 not in b_inbox or b_inbox & {state.get("p1"), state.get("p2")} - {None}:
        raise Fail("step 7: bank_b inbox is not exactly its own package")
    if "p1" in state:
        resp, _ = f.call("8 bank_b reads P1", "GET", "package", "bank_b", recipient="bank_b", package_id=state["p1"])
        if 200 <= resp.status_code < 300:
            raise Fail("step 8: bank_b could read bank_a's package")
        if resp.status_code != 404:
            raise Fail(f"step 8: refusal was {resp.status_code}, expected 404 with no existence leak")
    if p3 in _inbox(f, "9 bank_a inbox", "bank_a"):
        raise Fail("step 9: bank_a sees bank_b's package")


FLOWS = (("F1", flow_f1), ("F2", flow_f2), ("F3", flow_f3))


def run_flows(base_urls: dict, tokens: dict) -> list[dict]:
    from agents.eval.http_probe_runtime import ProbeSession, ProbeUnavailable

    session = ProbeSession(base_urls, tokens)
    cfg, state, out = {}, {}, []
    try:
        for key in ("rfc_bank_a", "rfc_bank_b", "policy_version"):
            cfg[key] = session.fixtures.get(key)
        for name, fn in FLOWS:
            f = Flow(session, name)
            row = {"flow": name, "outcome": "pass", "steps": f.steps, "reason": ""}
            try:
                if not all(cfg.values()):
                    raise ProbeUnavailable("missing fixtures rfc_bank_a, rfc_bank_b or policy_version")
                fn(f, cfg, state)
            except Fail as exc:
                row.update(outcome="fail", reason=str(exc))
            except ProbeUnavailable as exc:
                row.update(outcome="not_run", reason=session._redact(str(exc)))
            out.append(row)
    finally:
        session.close()
    return out


def main() -> None:
    path = os.environ.get("ALIBI_LIVE_CONFIG")
    if not path:
        print("not_run: set ALIBI_LIVE_CONFIG to a JSON file with base_urls and tokens for the live stack")
        print(json.dumps({"flows_pass": 0, "flows_fail": 0, "flows_not_run": 3}))
        return
    cfg = json.loads(open(path, encoding="utf-8").read())
    results = run_flows(cfg["base_urls"], cfg["tokens"])
    for r in results:
        print(f"{r['flow']}: {r['outcome']:8} {r['reason']}")
    print(json.dumps({f"flows_{k}": sum(r["outcome"] == k for r in results) for k in ("pass", "fail", "not_run")}))
    if any(r["outcome"] == "fail" for r in results):
        sys.exit(1)


if __name__ == "__main__":
    main()
