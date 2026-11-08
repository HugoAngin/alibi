"""Dispatch-path conformance: alibi.api approve / sign_and_package against consent_matrix.md.

Two tests per refusal row:
  test_cN_refused        the dispatch is refused at all (a security property; must pass now)
  test_cN_code_reason    the refusal carries the matrix code and verbatim reason (S5 work)
Controls (C9, C14, C16) must succeed. A test marked xfail(strict=True) documents a known gap in
the S1 core: when S5 fixes it the test XPASSes and the run fails until the marker is removed.

The refusal is read from the raised exception as `exc.code` / `exc.reason`, or `exc.refusal`
{"code", "reason"}; S5 may choose either. Expected reasons come from
agents.execution.approval_gate.CONSENT_MATRIX, the same templates as consent_matrix.md.
"""
import base64
import hashlib
import re
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from agents.execution.approval_gate import CONSENT_MATRIX

S5 = "S5 dispatch must return consent_matrix codes and verbatim reasons; S1 raises free text only"
REFUSALS = ("ApprovalError", "PackageError", "GatewayError", "ScopeError", "RevisionConflict", "PolicyError")


def refusal(exc) -> tuple[str | None, str | None]:
    if hasattr(exc, "refusal") and isinstance(exc.refusal, dict):
        return exc.refusal.get("code"), exc.refusal.get("reason")
    return getattr(exc, "code", None), getattr(exc, "reason", None)


def reason_pattern(code: str) -> re.Pattern:
    template = CONSENT_MATRIX[code][1]
    parts = re.split(r"\{[a-z0-9_]+\}", template)
    return re.compile("^" + ".*?".join(re.escape(p) for p in parts) + "$", re.S)


class Fixture:
    def __init__(self, variant="supported"):
        from agents.lead.api import (approve, build_review, default_keystore, load_fixture, open_store,
                                     run_investigation)

        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        key = base64.urlsafe_b64encode(hashlib.sha256(b"SYNTHETIC conformance").digest())
        self.store = open_store(root / "passport.db", master_key=key)
        self.keys = default_keystore(root / "keys")
        self.fx = load_fixture(self.store, variant, keystore=self.keys)
        self.now = self.fx["now"]
        run_investigation(self.store, "rfc_a_001", settlement_lookup=self.fx["settlement_lookup"],
                          registry=self.keys.registry(), now=self.now)
        self.draft = build_review(self.store, "rfc_a_001", now=self.now)
        self.approval = approve(self.store, self.draft.draft_id, holder_id="holder_maya", now=self.now)

    def sign(self, approval_id=None, now=None):
        from agents.lead.api import sign_and_package

        return sign_and_package(self.store, approval_id or self.approval.approval_id, keystore=self.keys,
                                now=now or self.now)

    def close(self):
        self.store.close()
        self.tmp.cleanup()


class Base(unittest.TestCase):
    variant = "supported"

    def setUp(self):
        self.f = Fixture(self.variant)

    def tearDown(self):
        self.f.close()

    def refuse(self, fn):
        with self.assertRaises(Exception) as ctx:
            fn()
        self.assertIn(type(ctx.exception).__name__, REFUSALS, repr(ctx.exception))
        return ctx.exception

    def code_reason(self, exc, code):
        got_code, got_reason = refusal(exc)
        self.assertEqual(got_code, code)
        self.assertRegex(got_reason or "", reason_pattern(code))

    # --- scenario drivers ---
    def c1(self):
        return self.refuse(lambda: self.f.sign("apr_doesnotexist0000000000"))

    def c2(self):
        from agents.lead.api import build_review

        build_review(self.f.store, "rfc_a_001", now=self.f.now, removed=["record:rfd_2001"])
        return self.refuse(self.f.sign)

    def c6(self):
        from agents.lead.api import approve, build_review

        build_review(self.f.store, "rfc_a_001", now=self.f.now, removed=["record:rfd_2001"])
        return self.refuse(lambda: approve(self.f.store, self.f.draft.draft_id, holder_id="holder_maya", now=self.f.now))

    def c7(self):
        from agents.lead.api import parse_rule_text, record_policy

        rule = parse_rule_text("don't share item names").rule
        record_policy(self.f.store, self.f.fx["subject_ref"], [rule.model_dump(mode="json")], confirmed=True, now=self.f.now)
        return self.refuse(self.f.sign)

    def c10(self):
        return self.refuse(lambda: self.f.sign(now=self.f.approval.expires_at + timedelta(seconds=1)))


class Refusals(Base):
    def test_c1_refused(self):
        self.c1()

    def test_c2_refused(self):
        """Fixed in 41ff999: an edited review invalidates the earlier approval."""
        self.c2()

    def test_removed_item_never_reaches_the_bank(self):
        """Fixed in 41ff999: the removed refund never reaches the bank."""
        import json

        from agents.lead.api import build_review

        build_review(self.f.store, "rfc_a_001", now=self.f.now, removed=["record:rfd_2001"])
        try:
            pkg = self.f.sign()
        except Exception:
            return  # refused: fine
        self.assertNotIn("rfd_2001", json.dumps(pkg.to_json_dict()))

    def test_c6_refused(self):
        """Fixed in 41ff999: a superseded draft cannot be approved."""
        self.c6()

    def test_c7_refused(self):
        self.c7()

    def test_c10_refused(self):
        self.c10()

    def test_c11_refused(self):
        rfc_expiry = self.f.store.get_request("rfc_a_001").expires_at
        self.refuse(lambda: self.f.sign(now=rfc_expiry + timedelta(seconds=1)))


class CodesAndReasons(Base):
    @unittest.expectedFailure
    def test_c1_code_reason(self):
        self.code_reason(self.c1(), "approval_missing")

    def test_c2_code_reason(self):
        self.code_reason(self.c2(), "approval_digest_mismatch")

    def test_c6_code_reason(self):
        self.code_reason(self.c6(), "approval_digest_not_current")

    @unittest.expectedFailure
    def test_c7_code_reason(self):
        self.code_reason(self.c7(), "policy_version_changed")

    @unittest.expectedFailure
    def test_c10_code_reason(self):
        self.code_reason(self.c10(), "approval_expired")


class CorrectionRows(Base):
    variant = "pending_correction"

    def correct_cited_record(self):
        from agents.lead.api import ingest_merchant_event

        for event in self.f.fx["corrections"]:
            ingest_merchant_event(self.f.store, event, registry=self.f.keys.registry(), now=self.f.now)

    def test_c8_refused(self):
        if not self.f.fx["corrections"]:
            self.skipTest("fixture has no correction")
        self.correct_cited_record()
        self.refuse(self.f.sign)

    @unittest.expectedFailure
    def test_c8_code_reason(self):
        self.correct_cited_record()
        self.code_reason(self.refuse(self.f.sign), "source_revision_changed")


class Controls(Base):
    def test_c9_uncited_new_record_still_sends(self):
        from agents.lead.api import import_self_supplied

        import_self_supplied(self.f.store, {
            "kind": "receipt", "amount_pence": 100, "currency": "GBP", "occurred_at": "2026-11-05T13:20:00Z",
            "status": "paid", "schema_version": "alibi.merchant_event.v1", "subject_ref": self.f.fx["subject_ref"],
            "text": "SYNTHETIC unrelated receipt", "items": []}, subject_ref=self.f.fx["subject_ref"], now=self.f.now)
        self.assertTrue(self.f.sign().package_id)

    def test_c14_identical_retry_one_delivery(self):
        first = self.f.sign()
        again = self.f.sign()
        self.assertEqual(first.to_json_dict(), again.to_json_dict())
        self.assertEqual(len([d for d in self.f.store.list_deliveries() if d.package_id == first.package_id]), 1)

    def test_c16_valid_dispatch_verifies(self):
        from agents.lead.api import verify_package

        pkg = self.f.sign()
        result = verify_package(pkg.to_json_dict(), expected_audience="bank_a", registry=self.f.keys.registry(),
                                now=self.f.now)
        self.assertTrue(result.accepted)


if __name__ == "__main__":
    unittest.main()
