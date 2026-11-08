import tempfile
import unittest
from datetime import datetime, timezone

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agents.eval import api as eval_api
from agents.execution import api

ALIBI = Ed25519PrivateKey.from_private_bytes(bytes(range(1, 33)))
MERCHANT = Ed25519PrivateKey.from_private_bytes(bytes(range(33, 65)))
NOW = datetime(2026, 11, 8, 2, 0, tzinfo=timezone.utc)
KEYS = {"alibi": (ALIBI, "alibi-demo-t1"), "merchant": (MERCHANT, "mock-marketplace-t1")}


class FakeBank:
    """HTTP-shaped wrapper over eval's BankInbox, standing in for the S2 bank service."""

    def __init__(self, root, mode="verify"):
        reg = eval_api.TrustRegistry()
        reg.add("alibi-demo-t1", eval_api.PURPOSE_PACKAGE, "alibi", ALIBI)
        reg.add("mock-marketplace-t1", eval_api.PURPOSE_ISSUER_RECORD, "mock-marketplace", MERCHANT)
        self.inbox = eval_api.BankInbox(root, reg, {"cred-a": "bank_a", "cred-b": "bank_b"})
        self.mode, self.exchanges, self.notes = mode, [], {}
        self.keys = KEYS

    def now(self):
        return NOW

    def route(self, name, **params):
        return {"deliver": f"/bank/{params.get('recipient')}/deliveries",
                "inbox": f"/bank/{params.get('recipient')}/inbox"}.get(name)

    def note(self, probe_id, text):
        self.notes[probe_id] = text

    def http(self, method, path, *, recipient=None, data=None, headers=None):
        self.exchanges.append((method, path, recipient))
        cred = {"bank_a": "cred-a", "bank_b": "cred-b"}[recipient]
        if self.mode == "down":
            return 503, b"unavailable"
        if method == "GET":
            return 200, {"packages": [r.to_dict() for r in self.inbox.list_receipts(cred)]}
        if self.mode == "refuse_all":
            return 400, {"code": "nope"}
        if self.mode == "no_route":
            return 404, b"not found"
        if self.mode == "accept_all":
            return 201, {"receipt": {"digest": "sha256:" + __import__("hashlib").sha256(data).hexdigest()}}
        try:
            d = self.inbox.deliver(recipient, data, now=NOW)
        except Exception as exc:
            return (409 if getattr(exc, "code", "") == "package_id_conflict" else 400), {"code": getattr(exc, "code", "")}
        return (201 if d.created else 200), {"receipt": d.receipt.to_dict()}


EXPECTED_CORRECT = {"SP-25": "blocked", "SP-26": "blocked", "SP-27": "blocked", "SP-28": "blocked",
                    "SP-29": "blocked", "SP-30": "not_blocked", "SP-31": "blocked", "SP-32": "not_blocked"}


class VerifierHttpHandlers(unittest.TestCase):
    def run_all(self, mode="verify", **override):
        with tempfile.TemporaryDirectory() as root:
            bank = FakeBank(root, mode)
            for k, v in override.items():
                setattr(bank, k, v)
            out = {pid: h(bank) for pid, h in api.verifier_http_handlers().items()}
            return out, bank

    def test_exports_eight_handlers(self):
        self.assertEqual(sorted(api.verifier_http_handlers()), [f"SP-{n}" for n in range(25, 33)])

    def test_correct_bank_blocks_attacks_and_allows_controls(self):
        out, bank = self.run_all()
        self.assertEqual(out, EXPECTED_CORRECT, bank.notes)
        self.assertEqual(bank.inbox.list_receipts("cred-b"), [])

    def test_bank_that_accepts_everything_is_caught(self):
        out, _ = self.run_all("accept_all")
        for pid in ("SP-25", "SP-26", "SP-27", "SP-28", "SP-29", "SP-31"):
            self.assertEqual(out[pid], "not_blocked", pid)

    def test_unavailable_bank_is_not_run_never_blocked(self):
        out, _ = self.run_all("down")
        self.assertEqual(set(out.values()), {"not_run"})

    def test_bank_that_refuses_everything_is_not_credited(self):
        out, _ = self.run_all("refuse_all")
        for pid in ("SP-25", "SP-26", "SP-27", "SP-28", "SP-29", "SP-31"):
            self.assertEqual(out[pid], "not_run", pid)
        self.assertEqual(out["SP-30"], "blocked")
        self.assertEqual(out["SP-32"], "blocked")

    def test_404_route_is_not_run(self):
        out, _ = self.run_all("no_route")
        self.assertEqual(set(out.values()), {"not_run"})

    def test_missing_routes_or_keys_are_not_run(self):
        out, bank = self.run_all(route=lambda name, **p: None)
        self.assertEqual(set(out.values()), {"not_run"})
        self.assertEqual(bank.exchanges, [])
        out, _ = self.run_all(keys={})
        self.assertEqual(set(out.values()), {"not_run"})


if __name__ == "__main__":
    unittest.main()
