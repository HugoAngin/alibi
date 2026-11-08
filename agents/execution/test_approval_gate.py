"""One test per consent_matrix.md row (C1-C16) plus flows F1 and F3, with verbatim reasons."""
import copy
import re
import unittest
from datetime import datetime, timedelta, timezone

from agents.execution import api
from agents.execution.approval_gate import CONSENT_MATRIX, body_digest

T0 = datetime(2026, 11, 8, 10, 0, tzinfo=timezone.utc)
REQ_A, REQ_B, REQ_F = "rfc-0001-01", "rfc-0001-02", "rfc-0001-03"
BODY = {"request_id": REQ_A, "audience": "bank_a", "claims": [{"question_id": "q.source_of_funds", "status": "supported"}],
        "attachments": [{"ref": "rec-0001-021", "revision": 1}, {"ref": "inc_03", "revision": 1}]}
REVS = {"rec-0001-021": 1, "txn-0001-004": 1}


def gate_with_draft(body=BODY, request=REQ_A, audience="bank_a"):
    g = api.approval_gate()
    g.register_request(REQ_A, audience="bank_a", expires_at=T0 + timedelta(days=7))
    g.register_request(REQ_B, audience="bank_b", expires_at=T0 + timedelta(days=7))
    g.register_request(REQ_F, audience="bank_a", expires_at=T0 + timedelta(days=7), parent_request_id=REQ_A)
    d = g.set_draft(request, body, policy_version="v3", source_revisions=REVS)
    return g, d


def approved(**kw):
    g, d = gate_with_draft(**kw)
    r = g.approve(REQ_A, digest=d, audience="bank_a", policy_version="v3", holder="maya", now=T0)
    assert r["ok"], r
    return g, d


def last(g, action):
    return [e for e in g.audit if e["action"] == action][-1]


class ConsentMatrix(unittest.TestCase):
    def refused(self, r, code):
        self.assertFalse(r["ok"])
        self.assertEqual((r["status"], r["code"]), (409, code))
        return r["reason"]

    def test_c1_no_approval(self):
        g, _ = gate_with_draft()
        reason = self.refused(g.dispatch(REQ_A, BODY, package_id="P1", audience="bank_a", now=T0), "approval_missing")
        self.assertEqual(reason, "dispatch refused: no holder approval recorded for request rfc-0001-01.")
        self.assertEqual((last(g, "dispatch.refused")["outcome"], last(g, "dispatch.refused")["detail"]),
                         ("refused", "approval_missing"))
        self.assertEqual(g.sent, {})

    def test_c2_item_removed_after_approval(self):
        g, d1 = approved()
        edited = copy.deepcopy(BODY)
        edited["attachments"] = edited["attachments"][:1]
        d2 = g.set_draft(REQ_A, edited, policy_version="v3", source_revisions=REVS, change_note="inc_03 removed")
        r = g.dispatch(REQ_A, edited, package_id="P1", audience="bank_a", now=T0)
        reason = self.refused(r, "approval_digest_mismatch")
        self.assertEqual(reason, f"dispatch refused: approved digest sha256:{d1[7:19]} does not match body digest "
                                 f"sha256:{d2[7:19]}; the body changed after approval (inc_03 removed). Review and "
                                 "approve the revised body.")
        self.assertEqual((r["expected_digest"], r["actual_digest"]), (d1, d2))
        self.assertEqual(last(g, "dispatch.refused")["detail"], "approval_digest_mismatch: body changed after approval.")
        # F1: fresh approval of D2 then dispatches
        self.assertTrue(g.approve(REQ_A, digest=d2, audience="bank_a", policy_version="v3", holder="maya", now=T0)["ok"])
        self.assertTrue(g.dispatch(REQ_A, edited, package_id="P1", audience="bank_a", now=T0)["ok"])

    def test_c3_regenerated_draft(self):
        g, _ = approved()
        regen = copy.deepcopy(BODY)
        regen["claims"][0]["rationale"] = "reworded"
        g.set_draft(REQ_A, regen, policy_version="v3", source_revisions=REVS, change_note="draft regenerated")
        reason = self.refused(g.dispatch(REQ_A, regen, package_id="P1", audience="bank_a", now=T0), "approval_digest_mismatch")
        self.assertIn("(draft regenerated)", reason)

    def test_c4_recipient_change(self):
        g, _ = approved()
        reason = self.refused(g.dispatch(REQ_A, BODY, package_id="P1", audience="bank_b", now=T0), "approval_audience_mismatch")
        self.assertEqual(reason, "dispatch refused: approval is for bank_a; this dispatch is addressed to bank_b. "
                                 "Each recipient needs its own approval.")

    def test_c4_refusal_invalidates_the_approval(self):
        g, _ = approved()
        g.dispatch(REQ_A, BODY, package_id="P1", audience="bank_b", now=T0)
        self.refused(g.dispatch(REQ_A, BODY, package_id="P1", audience="bank_a", now=T0), "approval_missing")

    def test_c5_audience_forged_at_approval(self):
        g, d = gate_with_draft()
        reason = self.refused(g.approve(REQ_A, digest=d, audience="bank_b", policy_version="v3", holder="maya", now=T0),
                              "approval_audience_mismatch")
        self.assertEqual(reason, "approval refused: request rfc-0001-01 is addressed to bank_a, not bank_b.")
        self.assertEqual((last(g, "approval.refused")["detail"], last(g, "approval.refused")["actor"]),
                         ("audience_mismatch", "maya"))

    def test_c6_stale_preview(self):
        g, d1 = gate_with_draft()
        changed = copy.deepcopy(BODY)
        changed["claims"][0]["status"] = "unresolved"
        d2 = g.set_draft(REQ_A, changed, policy_version="v3", source_revisions=REVS)
        reason = self.refused(g.approve(REQ_A, digest=d1, audience="bank_a", policy_version="v3", holder="maya", now=T0),
                              "approval_digest_not_current")
        self.assertEqual(reason, f"approval refused: the preview you approved (sha256:{d1[7:19]}) is no longer the "
                                 f"current draft (sha256:{d2[7:19]}). Review the current draft.")
        self.assertEqual(last(g, "approval.refused")["detail"], "digest_not_current")

    def test_c7_policy_change(self):
        g, _ = approved()
        g.policy_changed("v4", now=T0)
        self.assertEqual(last(g, "approval.invalidated")["detail"], "policy v3 -> v4")
        reason = self.refused(g.dispatch(REQ_A, BODY, package_id="P1", audience="bank_a", now=T0), "policy_version_changed")
        self.assertEqual(reason, "dispatch refused: privacy rules changed after approval (v3 to v4). The draft was "
                                 "re-filtered; review and approve it again.")

    def test_c8_cited_source_revised(self):
        g, _ = approved()
        g.source_corrected("rec-0001-021", 2, now=T0, event_id="evt-0001-021-2")
        self.assertIn("1 draft invalidated", last(g, "record.corrected")["detail"])
        self.assertEqual(last(g, "approval.invalidated")["detail"], "source_revision_changed")
        reason = self.refused(g.dispatch(REQ_A, BODY, package_id="P1", audience="bank_a", now=T0), "source_revision_changed")
        self.assertEqual(reason, "dispatch refused: cited record rec-0001-021 changed from r1 to r2 after approval. "
                                 "The draft is stale; review the updated draft.")

    def test_c9_uncited_source_revised_still_sends(self):
        g, _ = approved()
        g.source_corrected("rec-0001-099", 2, now=T0)
        r = g.dispatch(REQ_A, BODY, package_id="P1", audience="bank_a", now=T0)
        self.assertTrue(r["ok"])
        self.assertEqual(last(g, "dispatch.sent")["outcome"], "ok")

    def test_c10_approval_expired(self):
        g, _ = approved()
        later = T0 + timedelta(minutes=15)
        reason = self.refused(g.dispatch(REQ_A, BODY, package_id="P1", audience="bank_a", now=later), "approval_expired")
        self.assertEqual(reason, "dispatch refused: approval expired at 2026-11-08T10:15:00Z. Review and approve again.")

    def test_c11_request_expired(self):
        g = api.approval_gate()
        g.register_request(REQ_A, audience="bank_a", expires_at=T0 + timedelta(minutes=5))
        d = g.set_draft(REQ_A, BODY, policy_version="v3", source_revisions=REVS)
        g.approve(REQ_A, digest=d, audience="bank_a", policy_version="v3", holder="maya", now=T0)
        reason = self.refused(g.dispatch(REQ_A, BODY, package_id="P1", audience="bank_a", now=T0 + timedelta(minutes=6)),
                              "request_expired")
        self.assertEqual(reason, "dispatch refused: request rfc-0001-01 expired at 2026-11-08T10:05:00Z. "
                                 "The bank must send a new request.")

    def test_c12_reuse_on_second_request(self):
        g, _ = approved()
        reason = self.refused(g.dispatch(REQ_B, BODY, package_id="P3", audience="bank_b", now=T0), "approval_missing")
        self.assertEqual(reason, "dispatch refused: no holder approval recorded for request rfc-0001-02.")

    def test_c13_reuse_on_follow_up(self):
        g, _ = approved()
        reason = self.refused(g.dispatch(REQ_F, BODY, package_id="P2", audience="bank_a", now=T0), "approval_missing")
        self.assertEqual(reason, "dispatch refused: no holder approval recorded for request rfc-0001-03.")

    def test_explicit_approval_for_other_request(self):
        g, _ = approved()
        aid = g.by_request[REQ_A]
        reason = self.refused(g.dispatch(REQ_F, BODY, package_id="P2", audience="bank_a", now=T0, approval_id=aid),
                              "approval_request_mismatch")
        self.assertIn("is for request rfc-0001-01, not rfc-0001-03", reason)

    def test_c14_identical_retry_deduplicated(self):
        g, _ = approved()
        first = g.dispatch(REQ_A, BODY, package_id="P1", audience="bank_a", now=T0)
        again = g.dispatch(REQ_A, BODY, package_id="P1", audience="bank_a", now=T0 + timedelta(hours=1))
        self.assertTrue(again["ok"] and again["deduplicated"])
        self.assertEqual(again["receipt"], first["receipt"])
        self.assertEqual(len([e for e in g.audit if e["action"] == "dispatch.sent"]), 1)
        self.assertEqual(last(g, "dispatch.deduplicated")["detail"], "same package_id and digest")

    def test_c15_package_id_reused_with_different_bytes(self):
        g, _ = approved()
        g.dispatch(REQ_A, BODY, package_id="P1", audience="bank_a", now=T0)
        other = copy.deepcopy(BODY)
        other["claims"][0]["status"] = "unresolved"
        reason = self.refused(g.dispatch(REQ_A, other, package_id="P1", audience="bank_a", now=T0), "package_id_reused")
        self.assertEqual(reason, "dispatch refused: package P1 was already sent with a different digest.")

    def test_c16_valid_control(self):
        g, d = approved()
        self.assertEqual(last(g, "approval.recorded")["detail"], f"Approved digest sha256:{d[7:19]}... for bank_a, valid 15 min.")
        r = g.dispatch(REQ_A, BODY, package_id="P1", audience="bank_a", now=T0 + timedelta(minutes=14),
                       signer_key_id="alibi-demo-2026")
        self.assertTrue(r["ok"])
        self.assertEqual(r["receipt"]["digest"], body_digest(BODY))
        self.assertEqual(last(g, "dispatch.sent")["detail"], "Approval revalidated; body signed with alibi-demo-2026.")

    def test_approval_is_single_use(self):
        g, _ = approved()
        self.assertTrue(g.dispatch(REQ_A, BODY, package_id="P1", audience="bank_a", now=T0)["ok"])
        self.refused(g.dispatch(REQ_A, BODY, package_id="P1b", audience="bank_a", now=T0), "approval_missing")

    def test_invalidated_approval_cannot_be_revived_by_reverting_the_body(self):
        g, _ = approved()
        edited = copy.deepcopy(BODY)
        edited["attachments"] = edited["attachments"][:1]
        g.set_draft(REQ_A, edited, policy_version="v3", source_revisions=REVS)
        self.refused(g.dispatch(REQ_A, edited, package_id="P1", audience="bank_a", now=T0), "approval_digest_mismatch")
        g.set_draft(REQ_A, BODY, policy_version="v3", source_revisions=REVS)
        self.refused(g.dispatch(REQ_A, BODY, package_id="P1", audience="bank_a", now=T0), "approval_missing")

    def test_f3_second_recipient_needs_its_own_approval(self):
        g, d2 = approved()
        g.dispatch(REQ_A, BODY, package_id="P1", audience="bank_a", now=T0)
        body_b = dict(BODY, request_id=REQ_B, audience="bank_b")
        d4 = g.set_draft(REQ_B, body_b, policy_version="v3", source_revisions=REVS)
        self.refused(g.dispatch(REQ_B, body_b, package_id="P3", audience="bank_b", now=T0), "approval_missing")
        self.refused(g.approve(REQ_B, digest=d2, audience="bank_b", policy_version="v3", holder="maya", now=T0),
                     "approval_digest_not_current")
        self.assertTrue(g.approve(REQ_B, digest=d4, audience="bank_b", policy_version="v3", holder="maya", now=T0)["ok"])
        self.assertTrue(g.dispatch(REQ_B, body_b, package_id="P3", audience="bank_b", now=T0)["ok"])
        self.assertEqual(sorted((r["package_id"], r["recipient"]) for r in g.sent.values()),
                         [("P1", "bank_a"), ("P3", "bank_b")])

    def test_audit_details_never_carry_body_content(self):
        g, _ = approved()
        g.dispatch(REQ_A, BODY, package_id="P1", audience="bank_a", now=T0)
        for e in g.audit:
            self.assertNotIn("supported", e["detail"])
            self.assertNotIn("q.source_of_funds", e["detail"])

    def test_matrix_rows_and_templates_match_consent_matrix_md(self):
        md = (api.OUT / "consent_matrix.md").read_text(encoding="utf-8")
        covered = {row for rows, _ in CONSENT_MATRIX.values() for row in rows}
        self.assertEqual(covered | {"C9", "C14", "C16"}, {f"C{n}" for n in range(1, 17)})
        for code, (rows, template) in CONSENT_MATRIX.items():
            fixed = [part for part in re.split(r"\{[a-z0-9_]+\}", template) if len(part.strip()) > 12]
            for part in fixed:
                self.assertIn(part.strip().split(" (")[0][:40], md, code)


if __name__ == "__main__":
    unittest.main()
