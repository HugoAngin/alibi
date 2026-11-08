import json
import unittest

from agents.eval.api import score_probe_steps
from agents.execution import api, verifier_probes


class VerifierProbePlan(unittest.TestCase):
    def test_committed_plan_is_current(self):
        committed = json.loads((api.OUT / "verifier_probes.json").read_text(encoding="utf-8"))
        self.assertEqual(committed, json.loads(json.dumps(verifier_probes.plan())))

    def test_identical_retry_is_an_expected_acceptance(self):
        rows = {r["step_id"]: r for r in api.verifier_probe_plan()}
        self.assertEqual(rows["sp31-identical-retry"]["expected_outcome"], "accepted")
        self.assertNotIn("expected_outcome", rows["sp31-reuse-id"])

    def test_missing_route_is_incomplete_not_refused(self):
        plan = api.verifier_probe_plan()
        result = score_probe_steps(plan, [{**s, "outcome": "error"} for s in plan])
        self.assertEqual(len(result["incomplete_probes"]), 8)
        self.assertEqual(result["observed"], {})


class ReferenceRun(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = verifier_probes.run()

    def test_all_eight_probes_pass_with_planned_codes(self):
        m = self.result["metrics"]
        self.assertEqual((m["verifier_ref_passed"], m["verifier_ref_incorrect_steps"], m["verifier_ref_error_steps"],
                          m["verifier_ref_evidence_problems"]), (8, 0, 0, 0))
        for o in self.result["observations"]:
            if o["outcome"] == "refused":
                step = next(a for a in verifier_probes.ATTEMPTS if a["step_id"] == o["step_id"])
                self.assertIn(o["code"], step["codes"], o["step_id"])

    def test_refused_bytes_are_not_stored(self):
        self.assertTrue(self.result["checks"]["refusals_stored_nothing"])

    def test_scorer_catches_a_verifier_that_accepts_tampering(self):
        bad = [dict(o) for o in self.result["observations"]]
        next(o for o in bad if o["step_id"] == "sp25-body-edit")["outcome"] = "accepted"
        score = score_probe_steps(api.verifier_probe_plan(), bad)
        self.assertEqual(score["observed"]["SP-25"], "accepted")
        self.assertEqual(score["incorrect_steps"], 1)

    def test_run_is_deterministic(self):
        again = verifier_probes.run()
        self.assertEqual(again["observations"], self.result["observations"])


if __name__ == "__main__":
    unittest.main()
