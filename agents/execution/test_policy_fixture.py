import json
import unittest

from agents.execution import api, policy_fixture


class PolicyFixture(unittest.TestCase):
    def test_committed_files_match_generator(self):
        policy_fixture.validate()
        rows = [json.dumps(c, ensure_ascii=False) for c in policy_fixture.CASES]
        committed = (api.OUT / "policy_rules.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(committed, rows)
        self.assertEqual(api.load_policy_material(), policy_fixture.MATERIAL)

    def test_every_case_names_a_leak(self):
        for c in api.load_policy_rules():
            self.assertTrue(c["leak"]["what"] and c["leak"]["wrong_parse"], c["id"])


if __name__ == "__main__":
    unittest.main()
