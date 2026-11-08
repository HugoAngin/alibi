import json
import unittest

from agents.eval.api import score_probe_steps
from agents.execution import api, verifier_probes


class VerifierProbePlan(unittest.TestCase):
    def test_committed_plan_is_current(self):
        committed = json.loads((api.OUT / "verifier_probes.json").read_text(encoding="utf-8"))
        self.assertEqual(committed, json.loads(json.dumps(verifier_probes.plan())))

    def test_plan_scores_with_eval(self):
        plan = api.verifier_probe_plan()
        all_correct = [{**s, "outcome": next(a["expect"] for a in verifier_probes.ATTEMPTS
                                              if a["step_id"] == s["step_id"])} for s in plan]
        result = score_probe_steps(plan, all_correct)
        self.assertEqual(result["incomplete_probes"], [])
        self.assertEqual(result["incorrect_steps"], 0)
        self.assertEqual(len(result["observed"]), 8)

    def test_missing_route_is_incomplete_not_refused(self):
        plan = api.verifier_probe_plan()
        errors = [{**s, "outcome": "error"} for s in plan]
        result = score_probe_steps(plan, errors)
        self.assertEqual(len(result["incomplete_probes"]), 8)
        self.assertEqual(result["observed"], {})


if __name__ == "__main__":
    unittest.main()
