"""F1-F3 over real HTTP through eval's ProbeSession.

FakeApp is an in-process stand-in built on the reference ApprovalGate, used to check the flow
scripts themselves until S2 lands. LiveStack runs the same flows against the real stack and is
skipped unless ALIBI_LIVE_CONFIG points at {"base_urls", "tokens"}.
"""
import json
import os
import re
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

from agents.execution import api
from agents.execution.consent_flows_http import run_flows

T0 = datetime(2026, 11, 8, 10, 0, tzinfo=timezone.utc)
TOKENS = {"holder": "tok-holder", "bank_a": "tok-a", "bank_b": "tok-b"}
FIXTURES = {"rfc_bank_a": "rfc-0001-01", "rfc_bank_b": "rfc-0001-02", "policy_version": "v3"}


class FakeApp:
    def __init__(self, mode="correct"):
        self.mode = mode
        self.gate = api.approval_gate()
        self.gate.register_request("rfc-0001-01", audience="bank_a", expires_at=T0 + timedelta(days=7))
        self.gate.register_request("rfc-0001-02", audience="bank_b", expires_at=T0 + timedelta(days=7))
        self.drafts, self.inbox, self.packages, self.dispositions = {}, {"bank_a": [], "bank_b": []}, {}, {}
        self.n = 0

    def draft(self, rid):
        return {"request_id": rid, "items": list(self.drafts[rid])}

    def set_draft(self, rid, note=""):
        return self.gate.set_draft(rid, self.draft(rid), policy_version="v3", source_revisions={"rec-0001-021": 1},
                                   change_note=note)


def handler_for(app: FakeApp):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send(self, status, payload):
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def actor(self):
            tok = self.headers.get("Authorization", "").removeprefix("Bearer ")
            return {v: k for k, v in TOKENS.items()}.get(tok)

        def form(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
            return {k: v[0] for k, v in parse_qs(raw).items()}

        def do_GET(self):
            p, who = self.path, self.actor()
            if m := re.fullmatch(r"/requests/([\w-]+)", p):
                rid = m[1]
                d = app.gate.requests[rid].draft_digest
                items = [{"item_id": i, "removable": True, "remove_action": f"/requests/{rid}/items/{i}/remove"}
                         for i in app.drafts.get(rid, [])]
                return self.send(200, {"review": {"approval": {"digest": d}, "included": items}})
            if m := re.fullmatch(r"/bank/(bank_[ab])/inbox", p):
                if who != m[1]:
                    return self.send(404, {"code": "not_found"})
                return self.send(200, {"packages": [{"package_id": x} for x in app.inbox[m[1]]]})
            if m := re.fullmatch(r"/bank/(bank_[ab])/packages/([\w-]+)", p):
                visible = m[2] in app.inbox[m[1]] or (app.mode == "leaky" and m[2] in app.packages)
                if who != m[1] or not visible:
                    return self.send(404, {"code": "not_found", "reason": "no such package"})
                return self.send(200, {"authentication": {"status": "valid"}})
            self.send(404, {"code": "not_found"})

        def do_POST(self):
            p, f = self.path, self.form()
            if m := re.fullmatch(r"/requests/([\w-]+)/run", p):
                app.drafts[m[1]] = ["inc_01", "inc_02", "inc_03"]
                app.set_draft(m[1])
                return self.send(200, {"status": "draft"})
            if m := re.fullmatch(r"/requests/([\w-]+)/items/([\w-]+)/remove", p):
                app.drafts[m[1]].remove(m[2])
                app.set_draft(m[1], f"{m[2]} removed")
                return self.send(200, {"status": "draft"})
            if m := re.fullmatch(r"/requests/([\w-]+)/approve", p):
                r = app.gate.approve(m[1], digest=f["digest"], audience=f["audience"], policy_version=f["policy_version"],
                                     holder="maya", now=T0)
                return self.send(200 if r["ok"] else r["status"], r)
            if m := re.fullmatch(r"/requests/([\w-]+)/dispatch", p):
                rid = m[1]
                aud = app.gate.requests[rid].audience
                app.n += 1
                pid = f"pkg-{app.n}"
                if app.mode == "no_gate":
                    r = {"ok": True, "receipt": {"package_id": pid, "digest": app.gate.requests[rid].draft_digest}}
                else:
                    r = app.gate.dispatch(rid, app.draft(rid), package_id=pid, audience=aud, now=T0)
                if not r["ok"]:
                    return self.send(r["status"], r)
                app.inbox[aud].append(pid)
                app.packages[pid] = aud
                return self.send(200, {"receipt": {"package_id": pid, "package_digest": r["receipt"]["digest"]}})
            if m := re.fullmatch(r"/bank/(bank_[ab])/packages/([\w-]+)/disposition", p):
                prev = app.dispositions.get(m[2])
                if prev in ("clear", "escalate"):
                    return self.send(409, {"code": "disposition_final", "reason": "final for this package"})
                app.dispositions[m[2]] = f["disposition"]
                return self.send(200, {"disposition": f["disposition"]})
            if m := re.fullmatch(r"/bank/(bank_[ab])/requests", p):
                if f.get("period_from"):
                    return self.send(409, {"code": "scope_wider_than_parent", "reason": "follow-up scope must stay within the parent"})
                child = "rfc-0001-03"
                app.gate.register_request(child, audience=m[1], expires_at=T0 + timedelta(days=7),
                                          parent_request_id=f["parent_request_id"])
                return self.send(201, {"request_id": child, "parent_request_id": f["parent_request_id"]})
            self.send(404, {"code": "not_found"})

    return H


def serve(app):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(app))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, {"app": f"http://127.0.0.1:{server.server_address[1]}", "fixtures": FIXTURES, "routes": {}}


class FlowScriptsAgainstFakeApp(unittest.TestCase):
    def run_mode(self, mode):
        server, urls = serve(FakeApp(mode))
        try:
            return {r["flow"]: r for r in run_flows(urls, TOKENS)}
        finally:
            server.shutdown()

    def test_correct_app_passes_all_three(self):
        out = self.run_mode("correct")
        self.assertEqual({k: v["outcome"] for k, v in out.items()}, {"F1": "pass", "F2": "pass", "F3": "pass"},
                         {k: v["reason"] for k, v in out.items()})

    def test_dispatch_without_gate_fails_f1(self):
        out = self.run_mode("no_gate")
        self.assertEqual(out["F1"]["outcome"], "fail")
        self.assertIn("step 6", out["F1"]["reason"])

    def test_cross_recipient_read_fails_f3(self):
        out = self.run_mode("leaky")
        self.assertEqual(out["F3"]["outcome"], "fail")
        self.assertIn("step 8", out["F3"]["reason"])

    def test_missing_fixtures_are_not_run(self):
        server, urls = serve(FakeApp())
        urls["fixtures"] = {}
        try:
            out = run_flows(urls, TOKENS)
        finally:
            server.shutdown()
        self.assertEqual({r["outcome"] for r in out}, {"not_run"})


@unittest.skipUnless(os.environ.get("ALIBI_LIVE_CONFIG"), "live stack not configured (S2 routes not published)")
class LiveStack(unittest.TestCase):
    def test_flows_on_live_stack(self):
        cfg = json.loads(open(os.environ["ALIBI_LIVE_CONFIG"], encoding="utf-8").read())
        out = run_flows(cfg["base_urls"], cfg["tokens"])
        self.assertFalse([r for r in out if r["outcome"] == "fail"], out)


if __name__ == "__main__":
    unittest.main()
