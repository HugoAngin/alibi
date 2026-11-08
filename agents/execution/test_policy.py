import json
import unittest

from agents.data.api import canonical_case
from agents.eval.api import build_analyst_preview
from agents.execution import api, policy

NOW = "2026-11-08T10:00:00Z"


def ids_in(pkg):
    return json.dumps(pkg)


class Compile(unittest.TestCase):
    def test_text_and_typed_inputs_agree(self):
        a = api.compile_policy("Don't show what I bought.")
        b = api.compile_policy({"kind": "deny", "target": "record_kind", "value": "purchase", "params": {}})
        self.assertEqual([(r["kind"], r["target"], r["value"]) for r in a["rules"]],
                         [(r["kind"], r["target"], r["value"]) for r in b["rules"]])
        self.assertEqual(a["version"], b["version"])

    def test_parser_names_are_renamed_to_contract_names(self):
        c = api.compile_policy([{"kind": "age_limit", "target": "all", "value": "", "params": {"max_age_days": 90}},
                                {"kind": "recipient_restrict", "target": "field", "value": "item",
                                 "params": {"recipients": ["bank_a"]}}])
        self.assertEqual([r["kind"] for r in c["rules"]], ["age", "recipient"])

    def test_unknown_value_or_recipient_is_unresolved_never_guessed(self):
        c = api.compile_policy({"kind": "deny", "target": "field", "value": "buyer_handle", "params": {}})
        self.assertEqual((c["rules"], c["held"]), ([], True))
        c = api.compile_policy("Only Bank C can see my rent.")
        self.assertEqual((c["rules"], c["held"]), ([], True))

    def test_low_confidence_holds(self):
        c = api.compile_policy({"kind": "deny", "target": "category", "value": "health", "params": {}, "confidence": 0.6})
        self.assertTrue(c["held"])

    def test_alert_preferences_are_not_disclosure_rules(self):
        c = api.compile_policy("Warn me whenever a merchant isn't verified.")
        self.assertEqual(c["rules"], [])
        self.assertEqual(c["alerts"][0]["alert_kind"], "merchant_verification_gap")

    def test_every_fixture_rule_compiles_to_its_expected_shape(self):
        for case in api.load_policy_rules():
            c = api.compile_policy(case["text"])
            if case["outcome"] in ("clarify", "rules+clarify"):
                self.assertTrue(c["unresolved"] or c["held"], case["id"])
            if case["outcome"] == "none":
                self.assertEqual(c["rules"], [], case["id"])


class Apply(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.case = canonical_case()
        cls.pkg = build_analyst_preview(cls.case)
        cls.records = api.record_index(cls.case)

    def run_rule(self, rule, recipient="bank_a"):
        return api.apply_policy(self.pkg, api.compile_policy(rule), recipient, records=self.records, now=NOW)

    def test_no_rules_changes_nothing(self):
        out = api.apply_policy(self.pkg, api.compile_policy([]), "bank_a", records=self.records, now=NOW)
        self.assertEqual(out["package"], self.pkg)
        self.assertEqual(out["gaps"], [])

    def test_deny_refunds_withholds_and_leaves_generic_gap(self):
        out = self.run_rule({"kind": "deny", "target": "record_kind", "value": "refund", "params": {}})
        text = ids_in(out["package"])
        for rid in ("rec-0001-005", "rec-0001-012"):
            self.assertNotIn(rid, text)
        self.assertEqual({g["description"] for g in out["gaps"]}, {policy.WITHHELD_GAP})
        breakdown = next(q for q in out["package"]["evidence_by_question"] if q["question_id"] == "q.breakdown")
        self.assertEqual((breakdown["claim"]["status"], breakdown["calculation"]), ("unresolved", None))
        self.assertTrue(out["package"]["reconciliation"]["withheld"])
        self.assertEqual(len(out["private_review"]["excluded"]), 2)
        self.assertNotIn("excluded", text)

    def test_gap_text_gives_no_hint_of_what_was_withheld(self):
        a = self.run_rule({"kind": "deny", "target": "record_kind", "value": "refund", "params": {}})
        b = self.run_rule({"kind": "deny", "target": "record_kind", "value": "fee", "params": {}})
        self.assertEqual([g["description"] for g in a["gaps"]], [g["description"] for g in b["gaps"]])
        for out in (a, b):
            for word in ("refund", "fee"):
                self.assertNotIn(word, json.dumps(out["gaps"]).lower())

    def test_recipient_rule_depends_on_recipient(self):
        rule = {"kind": "recipient_restrict", "target": "record_kind", "value": "sale", "params": {"recipients": ["bank_b"]}}
        to_a = self.run_rule(rule, "bank_a")
        to_b = self.run_rule(rule, "bank_b")
        self.assertNotIn("rec-0001-001", ids_in(to_a["package"]))
        self.assertIn("rec-0001-001", ids_in(to_b["package"]))

    def test_deny_wins_over_recipient_allowance(self):
        out = self.run_rule([
            {"kind": "recipient_restrict", "target": "record_kind", "value": "fee", "params": {"recipients": ["bank_a"]}},
            {"kind": "deny", "target": "record_kind", "value": "fee", "params": {}}])
        self.assertNotIn("rec-0001-002", ids_in(out["package"]))

    def test_age_limit_withholds_old_records(self):
        out = self.run_rule({"kind": "age_limit", "target": "all", "value": "", "params": {"max_age_days": 14}})
        text = ids_in(out["package"])
        self.assertNotIn("rec-0001-001", text)   # sale on 2026-10-05
        self.assertIn("txn-0001-004", text)      # credit booked 2026-10-28

    def test_amount_field_deny_strips_amounts_and_reconciliation(self):
        out = self.run_rule({"kind": "deny", "target": "field", "value": "amount_minor", "params": {}})
        self.assertNotIn("minor units", json.dumps(out["package"]["source_trust"]))
        self.assertTrue(out["package"]["reconciliation"]["withheld"])

    def test_unresolved_rule_holds_the_whole_package(self):
        out = api.apply_policy(self.pkg, api.compile_policy("Keep it private."), "bank_a", records=self.records, now=NOW)
        self.assertEqual((out["held"], out["package"]), (True, None))

    def test_unknown_record_is_withheld_when_a_rule_needs_its_metadata(self):
        out = api.apply_policy(self.pkg, api.compile_policy({"kind": "deny", "target": "category", "value": "health",
                                                             "params": {}}), "bank_a", records={}, now=NOW)
        self.assertEqual(out["package"]["source_trust"], [])

    def test_final_scan_fails_closed(self):
        pkg = json.loads(json.dumps(self.pkg))
        pkg["claim_status"][0]["summary"] += " see rec-0001-005"
        with self.assertRaises(policy.DisclosureError):
            api.apply_policy(pkg, api.compile_policy({"kind": "deny", "target": "record_kind", "value": "refund",
                                                      "params": {}}), "bank_a", records=self.records, now=NOW)

    def test_every_fixture_compilation_applies_cleanly_for_both_banks(self):
        for case in api.load_policy_rules():
            rules = [dict(r, params={k: (v[0] if isinstance(v, list) and k == "max_age_days" else v)
                                     for k, v in r["params"].items()}) for r in case["expected_rules"]]
            compiled = api.compile_policy(rules)
            for recipient in ("bank_a", "bank_b"):
                out = api.apply_policy(self.pkg, compiled, recipient, records=self.records, now=NOW)
                if out["held"]:
                    continue
                withheld = {e["record_id"] for e in out["private_review"]["excluded"]}
                text = ids_in(out["package"])
                for rid in withheld:
                    self.assertNotIn(rid, text, (case["id"], recipient))


class CodeParser(unittest.TestCase):
    def test_fixture_and_holdout_have_no_leaks(self):
        from pathlib import Path

        from agents.execution import policy_harness as h

        self.assertEqual(h.evaluate(policy.parse_rule_text)["metrics"]["leaks"], 0)
        path = Path(api.OUT) / "policy_rules_holdout.jsonl"
        holdout = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        self.assertEqual(h.evaluate(policy.parse_rule_text, holdout)["metrics"]["leaks"], 0)


if __name__ == "__main__":
    unittest.main()
