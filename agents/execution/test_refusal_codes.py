"""refusal_from_problems against real alibi.core.approval.check_approval output."""
import base64
import hashlib
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from agents.execution import api
from agents.execution.refusal_codes import FALLBACK


class CoreProblemsMapToMatrixCodes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from agents.lead.api import approve, build_review, default_keystore, load_fixture, open_store, run_investigation

        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        key = base64.urlsafe_b64encode(hashlib.sha256(b"SYNTHETIC refusal-code test").digest())
        cls.store = open_store(root / "passport.db", master_key=key)
        keys = default_keystore(root / "keys")
        fx = load_fixture(cls.store, "supported", keystore=keys)
        cls.now = fx["now"]
        run_investigation(cls.store, "rfc_a_001", settlement_lookup=fx["settlement_lookup"],
                          registry=keys.registry(), now=cls.now)
        cls.draft = build_review(cls.store, "rfc_a_001", now=cls.now)
        cls.approval = approve(cls.store, cls.draft.draft_id, holder_id="holder_maya", now=cls.now)

    @classmethod
    def tearDownClass(cls):
        cls.store.close()
        cls.tmp.cleanup()

    def check(self, **change):
        from alibi.core.approval import check_approval, current_revisions

        args = {"body": self.draft.body, "audience": self.approval.audience, "request_id": self.approval.request_id,
                "policy_version": self.approval.policy_version,
                "revisions": current_revisions(self.store, self.approval.source_revisions), "now": self.now}
        approval = change.pop("approval", self.approval)
        args.update(change)
        return check_approval(approval, **args)

    def code(self, **change):
        problems = self.check(**change)
        self.assertTrue(problems, change)
        return api.refusal_from_problems(problems)

    def test_valid_approval_has_no_problems(self):
        self.assertEqual(self.check(), [])

    def test_c2_body_changed(self):
        body = self.draft.body.model_dump(mode="json")
        body["claims"][0]["rationale"] = body["claims"][0]["rationale"] + " (edited)"
        r = self.code(body=body)
        self.assertEqual(r["code"], "approval_digest_mismatch")
        self.assertEqual(r["reason"], "the package body changed after it was approved")

    def test_c4_other_recipient(self):
        self.assertEqual(self.code(audience="bank_b")["code"], "approval_audience_mismatch")

    def test_c7_policy_changed(self):
        self.assertEqual(self.code(policy_version="v999")["code"], "policy_version_changed")

    def test_c8_cited_source_revised(self):
        from alibi.core.approval import current_revisions

        revs = current_revisions(self.store, self.approval.source_revisions)
        token = next(iter(revs))
        revs[token] = (revs[token] or 1) + 1
        self.assertEqual(self.code(revisions=revs)["code"], "source_revision_changed")

    def test_c10_expired(self):
        self.assertEqual(self.code(now=self.approval.expires_at + timedelta(seconds=1))["code"], "approval_expired")

    def test_c13_other_request(self):
        self.assertEqual(self.code(request_id="rfc_a_999")["code"], "approval_request_mismatch")

    def test_consumed_approval_is_missing(self):
        used = self.approval.model_copy(update={"status": "consumed", "package_id": "pkg_x"})
        self.assertEqual(self.code(approval=used)["code"], "approval_missing")

    def test_invalidated_for_a_policy_change_keeps_that_code(self):
        inv = self.approval.model_copy(update={"status": "invalidated",
                                               "invalidated_reason": "the disclosure policy changed after approval"})
        self.assertEqual(self.code(approval=inv)["code"], "policy_version_changed")

    def test_matrix_order_wins_when_several_problems(self):
        r = self.code(audience="bank_b", policy_version="v999", now=self.approval.expires_at + timedelta(seconds=1))
        self.assertEqual(r["code"], "approval_expired")
        self.assertGreaterEqual(len(r["problems"]), 3)

    def test_unknown_problem_never_passes(self):
        self.assertEqual(api.refusal_from_problems(["something new went wrong"])["code"], FALLBACK)
        self.assertEqual(api.refusal_from_problems([])["code"], FALLBACK)


if __name__ == "__main__":
    unittest.main()
