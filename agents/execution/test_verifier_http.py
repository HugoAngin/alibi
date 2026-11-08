"""SP-25..SP-32 HTTP handlers through eval's real ProbeSession and attempt(), against a local bank.

The bank is a tiny HTTP server over eval's BankInbox (synthetic demo keys), standing in for the S2
bank service until its routes land. Modes make it misbehave so regressions are seen.
"""
import hashlib
import json
import re
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agents.eval import api as eval_api
from agents.eval.http_probe_runtime import attempt
from agents.execution import api

ALIBI_SEED, MERCHANT_SEED = bytes(range(1, 33)), bytes(range(33, 65))
NOW = datetime(2026, 11, 8, 2, 0, tzinfo=timezone.utc)
TOKENS = {"alibi": "tok-alibi-test", "bank_a": "tok-bank-a-test", "bank_b": "tok-bank-b-test"}
PROBES = [f"SP-{n}" for n in range(25, 33)]


class Bank:
    def __init__(self, root):
        reg = eval_api.TrustRegistry()
        reg.add("alibi-demo-t1", eval_api.PURPOSE_PACKAGE, "alibi", Ed25519PrivateKey.from_private_bytes(ALIBI_SEED))
        reg.add("mock-marketplace-t1", eval_api.PURPOSE_ISSUER_RECORD, "mock-marketplace",
                Ed25519PrivateKey.from_private_bytes(MERCHANT_SEED))
        self.inbox = eval_api.BankInbox(root, reg, {"cred-a": "bank_a", "cred-b": "bank_b"})
        self.mode = "correct"


def make_handler(bank: Bank):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, status, payload):
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _actor(self):
            token = self.headers.get("Authorization", "").removeprefix("Bearer ")
            return {v: k for k, v in TOKENS.items()}.get(token)

        def do_GET(self):
            m = re.fullmatch(r"/bank/(bank_[ab])/inbox", self.path)
            if not m or self._actor() != m[1]:
                return self._send(404, {"code": "not_found", "reason": "no such inbox"})
            cred = {"bank_a": "cred-a", "bank_b": "cred-b"}[m[1]]
            self._send(200, {"packages": [r.to_dict() for r in bank.inbox.list_receipts(cred)]})

        def do_POST(self):
            m = re.fullmatch(r"/bank/(bank_[ab])/deliveries", self.path)
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if not m or self._actor() != "alibi":
                return self._send(404, {"code": "not_found", "reason": "no such route"})
            if bank.mode == "down":
                return self._send(503, {"code": "unavailable", "reason": "down"})
            if bank.mode == "refuse_all":
                return self._send(400, {"code": "bad_signature", "reason": "refuses everything"})
            if bank.mode == "accept_all":
                return self._send(201, {"receipt": {"digest": "sha256:" + hashlib.sha256(raw).hexdigest()}})
            try:
                d = bank.inbox.deliver(m[1], raw, now=NOW)
            except Exception as exc:
                code = getattr(exc, "code", "error")
                return self._send(409 if code == "package_id_conflict" else 400, {"code": code, "reason": str(exc)})
            self._send(201 if d.created else 200, {"receipt": d.receipt.to_dict()})

    return Handler


class SharedContextCompatibility(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.bank = Bank(cls.tmp.name)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls.bank))
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.tmp.cleanup()

    def base_urls(self, **drop):
        urls = {
            "bank": self.base,
            "routes": {"verifier_deliver": "/bank/{recipient}/deliveries", "verifier_inbox": "/bank/{recipient}/inbox"},
            "fixtures": {"alibi_package_key": ALIBI_SEED.hex(), "alibi_package_key_id": "alibi-demo-t1",
                         "merchant_record_key": MERCHANT_SEED.hex(), "merchant_record_key_id": "mock-marketplace-t1",
                         "now": "2026-11-08T02:00:00Z"},
        }
        for section, key in drop.items():
            urls[section] = {k: v for k, v in urls[section].items() if k != key}
        return urls

    def run_all(self, mode="correct", **drop):
        self.bank.mode = mode
        try:
            return {p: attempt(p, self.base_urls(**drop), TOKENS) for p in PROBES}
        finally:
            self.bank.mode = "correct"

    def test_eight_handlers_exported(self):
        self.assertEqual(sorted(api.verifier_http_handlers()), PROBES)

    def test_correct_bank_every_check_passes(self):
        out = self.run_all()
        self.assertEqual({p: r["outcome"] for p, r in out.items()}, {p: "blocked" for p in PROBES},
                         {p: r.get("reason") for p, r in out.items()})
        self.assertEqual(self.bank.inbox.list_receipts("cred-b"), [])

    def test_exchanges_are_captured_and_tokens_redacted(self):
        result = self.run_all()["SP-25"]
        self.assertGreaterEqual(len(result["exchanges"]), 3)
        self.assertNotIn("tok-alibi-test", json.dumps(result["exchanges"]))

    def test_bank_that_accepts_everything_is_a_regression(self):
        out = self.run_all("accept_all")
        for p in ("SP-25", "SP-26", "SP-27", "SP-28", "SP-29", "SP-31"):
            self.assertEqual(out[p]["outcome"], "not_blocked", p)

    def test_bank_that_refuses_everything_gets_no_credit(self):
        out = self.run_all("refuse_all")
        for p in ("SP-25", "SP-26", "SP-27", "SP-28", "SP-29", "SP-31"):
            self.assertEqual(out[p]["outcome"], "not_run", p)
        self.assertEqual(out["SP-30"]["outcome"], "not_blocked")
        self.assertEqual(out["SP-32"]["outcome"], "not_blocked")

    def test_unavailable_bank_is_not_run(self):
        out = self.run_all("down")
        self.assertEqual({r["outcome"] for r in out.values()}, {"not_run"})

    def test_missing_route_or_fixture_is_not_run_with_reason(self):
        out = self.run_all(routes="verifier_deliver")
        self.assertEqual({r["outcome"] for r in out.values()}, {"not_run"})
        self.assertIn("missing route", out["SP-32"]["reason"])
        out = self.run_all(fixtures="alibi_package_key")
        self.assertIn("missing fixture", out["SP-25"]["reason"])


if __name__ == "__main__":
    unittest.main()
