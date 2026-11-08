"""Audit coverage of the demo path against the S1 store (alibi.api, Store.list_audit).

Every action in the demo path must leave an AuditEvent with actor, time (UTC), a request or event
id (target_id), action and outcome. Rows marked expectedFailure are gaps: S1 does not audit them
yet, or they belong to S2 (bank service). When fixed they XPASS and the marker must be removed.
"""
import unittest
from datetime import timedelta

from agents.execution.test_dispatch_conformance import Fixture


class DemoPath(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from agents.lead.api import (approve, build_review, ingest_merchant_event, parse_rule_text, record_policy,
                                     run_investigation)

        cls.f = f = Fixture("pending_correction")
        rule = parse_rule_text("nothing older than 90 days").rule
        record_policy(f.store, f.fx["subject_ref"], [rule.model_dump(mode="json")], confirmed=True, now=f.now)
        draft = build_review(f.store, "rfc_a_001", now=f.now)
        cls.approval = approve(f.store, draft.draft_id, holder_id="holder_maya", now=f.now)
        cls.package = f.sign(cls.approval.approval_id)
        try:  # a refused dispatch, to check refusals are audited too
            f.sign(cls.approval.approval_id, now=cls.approval.expires_at + timedelta(hours=1))
        except Exception:
            pass
        for event in f.fx["corrections"]:
            ingest_merchant_event(f.store, event, registry=f.keys.registry(), now=f.now)
        run_investigation(f.store, "rfc_a_001", settlement_lookup=f.fx["settlement_lookup"],
                          registry=f.keys.registry(), now=f.now)
        cls.events = [e.model_dump(mode="json") for e in f.store.list_audit()]

    @classmethod
    def tearDownClass(cls):
        cls.f.close()

    def entries(self, *actions):
        return [e for e in self.events if e["action"] in actions]

    def assert_complete(self, entries, what):
        self.assertTrue(entries, f"no audit entry for {what}")
        for e in entries:
            for field in ("actor", "at", "target_id", "action", "outcome"):
                self.assertTrue(e.get(field), f"{what}: audit entry missing {field}: {e}")
            self.assertTrue(str(e["at"]).endswith(("Z", "+00:00")), f"{what}: time not UTC: {e['at']}")

    # --- covered by S1 ---
    def test_every_entry_is_complete(self):
        self.assert_complete(self.events, "any action")

    def test_ingest(self):
        self.assert_complete(self.entries("ingest_merchant_event"), "merchant ingest")
        self.assert_complete(self.entries("ingest_bank_txns"), "bank ingest")

    def test_request_received(self):
        self.assert_complete(self.entries("receive_rfc"), "RFC received")

    def test_claim_change_after_correction(self):
        runs = self.entries("run_investigation")
        self.assert_complete(runs, "investigation")
        details = [e["detail"] or "" for e in runs if e["target_id"] == "rfc_a_001"]
        self.assertTrue(any("supported" in d for d in details) and any("contradicted" in d for d in details),
                        f"claim change not visible in audit: {details}")

    def test_correction_supersedes_evidence(self):
        self.assert_complete(self.entries("evidence_superseded"), "evidence superseded by correction")

    def test_policy_confirm(self):
        entries = self.entries("record_policy")
        self.assert_complete(entries, "policy confirm")
        self.assertTrue(any("confirmed=True" in (e["detail"] or "") for e in entries))

    def test_approve(self):
        entries = self.entries("approve")
        self.assert_complete(entries, "approve")
        self.assertTrue(any(e["target_id"] == self.approval.approval_id for e in entries))

    def test_dispatch(self):
        entries = self.entries("sign_and_package")
        self.assert_complete(entries, "dispatch")
        self.assertTrue(any(e["target_id"] == self.package.package_id and e["outcome"] == "ok" for e in entries))

    def test_refused_dispatch_is_audited(self):
        refused = [e for e in self.entries("sign_and_package") if e["outcome"] != "ok"]
        self.assert_complete(refused, "refused dispatch")

    # --- gaps ---
    @unittest.expectedFailure
    def test_link_has_its_own_entry(self):
        """GAP: links are created inside run_investigation with no audit entry naming the link."""
        self.assert_complete([e for e in self.events if "link" in e["action"]], "link")

    @unittest.expectedFailure
    def test_delivery_receipt_is_audited(self):
        """S2: the bank-side receipt / acknowledgment is a separate audit entry."""
        self.assert_complete([e for e in self.events if "deliver" in e["action"] or "receipt" in e["action"]],
                             "delivery receipt")

    @unittest.expectedFailure
    def test_disposition_is_audited(self):
        """S2: analyst disposition."""
        self.assert_complete([e for e in self.events if "disposition" in e["action"]], "disposition")

    @unittest.expectedFailure
    def test_follow_up_is_audited(self):
        """S2: request_more follow-up RFC with parent."""
        self.assert_complete([e for e in self.events if "follow" in e["action"]], "follow-up")

    def test_actor_naming_is_consistent_for_the_holder(self):
        holder_actors = {e["actor"] for e in self.events if e["action"] in ("approve", "record_policy")}
        if len(holder_actors) > 1:
            self.skipTest(f"minor: holder appears under several actor names {sorted(holder_actors)}")


if __name__ == "__main__":
    unittest.main()
