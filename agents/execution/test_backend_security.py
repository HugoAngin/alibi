"""Security review of the S2 bank service, the mock merchant and the public router (lead, 01:57Z item 3).

Findings F1-F3 were strict expected failures until S5 fixed them (alibi/REPORTS/S5.md 1g: one
admin bearer on every /admin/* route, loopback clients only); they are ordinary tests now and guard
against regressions. Controls that hold are ordinary passing tests.
Full list with severity and fixes: agents/execution/out/BACKEND_SECURITY_REVIEW.md.
"""
import sys
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from alibi.bank.service import create_bank_app
from alibi.connectors import keys
from alibi.connectors.merchant import create_merchant_app

ROOT = Path(__file__).resolve().parents[2]
# The hosting router is eval's (agents/eval/deploy/router.py, 40e03c1), which replaced research's.
sys.path.insert(0, str(ROOT / "agents" / "eval" / "deploy"))
from router import is_blocked  # noqa: E402

TOKENS = {"bank_a": "tok-a-security", "bank_b": "tok-b-security", "feed": "tok-feed-security",
          "admin": "tok-admin-security"}
AS_OF = "2026-11-04T14:00:00Z"
RFC_A = "rfc-0001-01"


def auth(identity: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKENS[identity]}"}


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import tempfile

        cls.tmp = tempfile.TemporaryDirectory()
        cls.keystore = keys.connector_keystore(Path(cls.tmp.name) / "keys", seed="execution-security")
        cls.registry = cls.keystore.registry()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def bank(self, seed=True) -> TestClient:
        client = TestClient(create_bank_app(keystore=self.keystore, registry=self.registry, tokens=TOKENS))
        if seed:
            # Since S5 (F1 fixed) the seed carries the shared admin credential.
            r = client.post("/admin/seed", params={"case": 1, "as_of": AS_OF}, headers=auth("admin"))
            assert r.status_code < 300, f"seed refused: {r.status_code}"
        return client

    def merchant(self) -> TestClient:
        return TestClient(create_merchant_app(None, keystore=self.keystore))


class Findings(Base):
    """F1-F3: internal admin routes need the admin credential, and the public router blocks them."""

    def test_f1_bank_seed_requires_a_credential(self):
        r = self.bank(seed=False).post("/admin/seed", params={"case": 1, "as_of": AS_OF})
        self.assertIn(r.status_code, (401, 403, 404), "anyone can reset both banks' inboxes")

    def test_f1_bank_clock_advance_requires_a_credential(self):
        r = self.bank().post("/admin/advance", params={"as_of": "2026-12-31T00:00:00Z"})
        self.assertIn(r.status_code, (401, 403, 404), "anyone can expire every package and request")

    def test_f2_merchant_correction_requires_a_credential(self):
        m = self.merchant()
        m.post("/admin/seed", params={"case": 1})
        r = m.post("/admin/correct", json={"record_id": "rec-0001-021", "changes": {"status": "failed"},
                                            "correction_reason": "SYNTHETIC unauthenticated test"})
        self.assertIn(r.status_code, (401, 403, 404), "anyone can make the merchant sign a correction")

    def test_f2_merchant_state_dump_requires_a_credential(self):
        m = self.merchant()
        m.post("/admin/seed", params={"case": 1})
        self.assertIn(m.get("/admin/state").status_code, (401, 403, 404))

    def test_f3_public_router_blocks_every_admin_route(self):
        """Fixed by eval in agents/eval/deploy/router.py (40e03c1)."""
        for path in ("/bank/admin/seed", "/bank/admin/advance", "/merchant/admin/seed",
                     "/merchant/admin/correct", "/merchant/admin/state", "/bank/admin/reset/"):
            self.assertTrue(is_blocked(path), path)


class ControlsThatHold(Base):
    def test_recipient_routes_need_a_valid_token(self):
        bank = self.bank()
        for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": f"Basic {TOKENS['bank_a']}"},
                        {"Authorization": f"Bearer {TOKENS['feed']}"}):
            self.assertEqual(bank.get("/inbox", headers=headers).status_code, 401, headers)

    def test_feed_token_cannot_act_as_a_bank_and_bank_cannot_read_feed(self):
        bank = self.bank()
        self.assertEqual(bank.get("/feed/transactions", headers=auth("bank_a")).status_code, 401)
        self.assertEqual(bank.post("/rfc", json={}, headers={"Authorization": f"Bearer {TOKENS['feed']}"}).status_code, 401)

    def test_a_bank_cannot_read_another_banks_request(self):
        bank = self.bank()
        self.assertEqual(bank.post("/rfc", json={"request_id": RFC_A}, headers=auth("bank_a")).status_code, 201)
        self.assertEqual(bank.get(f"/rfc/{RFC_A}", headers=auth("bank_a")).status_code, 200)
        self.assertEqual(bank.get(f"/rfc/{RFC_A}", headers=auth("bank_b")).status_code, 404)
        self.assertNotIn(RFC_A, str(bank.get("/rfc", headers=auth("bank_b")).json()))

    def test_another_banks_package_id_is_not_found(self):
        bank = self.bank()
        r = bank.get("/packages/pkg-guess-0001", headers=auth("bank_b"))
        self.assertEqual(r.status_code, 404)
        self.assertEqual(bank.post("/packages/pkg-guess-0001/disposition", headers=auth("bank_b"),
                                   json={"decision": "clear", "reason": "x"}).status_code, 404)

    def test_tokens_never_appear_in_responses_or_audit(self):
        bank = self.bank()
        bank.post("/rfc", json={"request_id": RFC_A}, headers=auth("bank_a"))
        text = " ".join(str(bank.get(p, headers=auth("bank_a")).json()) for p in ("/inbox", "/audit", "/rfc", "/whoami"))
        for token in TOKENS.values():
            self.assertNotIn(token, text)

    def test_router_still_blocks_the_known_reset_paths(self):
        for path in ("/reset", "/__debug", "/admin/reset", "/bank/admin/reset", "/bank/admin/reset/"):
            self.assertTrue(is_blocked(path), path)


if __name__ == "__main__":
    unittest.main()
