import unittest

from agents.execution import policy_harness as h
from agents.execution.api import load_policy_rules


def _concrete(rule):
    params = dict(rule["params"])
    if isinstance(params.get("max_age_days"), list):
        params["max_age_days"] = params["max_age_days"][0]
    return {**rule, "params": params, "confidence": 0.95}


def oracle(text, material):
    case = next(c for c in load_policy_rules() if c["text"] == text)
    rules = [_concrete(r) for r in case["expected_rules"]]
    clar = [{"source_text": text, "question": "?"}] if case["outcome"] in ("clarify", "rules+clarify") else []
    return {"rules": rules, "clarifications": clar, "plain_summary": ""}


class Harness(unittest.TestCase):
    def test_oracle_scores_40_of_40_with_no_leaks(self):
        m = h.evaluate(oracle)["metrics"]
        self.assertEqual((m["correct"], m["leaks"], m["invalid"], m["errors"]), (40, 0, 0, 0))

    def test_parser_that_compiles_nothing_leaks_every_needed_restriction(self):
        m = h.evaluate(lambda t, mat: {"rules": [], "clarifications": []})["metrics"]
        needed = [c["id"] for c in load_policy_rules()
                  if any(r["kind"] in h.RESTRICTING for r in c["expected_rules"])]
        self.assertEqual(set(m["leak_ids"]), set(needed))

    def test_clarifying_everything_is_safe(self):
        m = h.evaluate(lambda t, mat: {"rules": [], "clarifications": [{"source_text": t, "question": "?"}]})["metrics"]
        self.assertEqual(m["leaks"], 0)

    def test_wrong_recipient_is_a_leak_and_deny_is_safe(self):
        case = next(c for c in load_policy_rules() if c["id"] == "PR-04")
        mat = h.load_policy_material()
        wrong = {"rules": [{"kind": "recipient_restrict", "target": "field", "value": "item",
                            "params": {"recipients": ["bank_b"]}, "confidence": 0.9}], "clarifications": []}
        self.assertEqual(h.score_case(case, wrong, mat)["verdict"], "leak")
        deny = {"rules": [{"kind": "deny", "target": "field", "value": "item", "params": {}, "confidence": 0.9}],
                "clarifications": []}
        self.assertEqual(h.score_case(case, deny, mat)["verdict"], "safe_wrong")

    def test_looser_age_limit_leaks_and_stricter_is_safe(self):
        case = next(c for c in load_policy_rules() if c["id"] == "PR-03")
        mat = h.load_policy_material()
        rule = lambda d: {"rules": [{"kind": "age_limit", "target": "all", "value": "",
                                     "params": {"max_age_days": d}, "confidence": 0.9}], "clarifications": []}
        self.assertEqual(h.score_case(case, rule(90), mat)["verdict"], "correct")
        self.assertEqual(h.score_case(case, rule(30), mat)["verdict"], "safe_wrong")
        self.assertEqual(h.score_case(case, rule(630), mat)["verdict"], "leak")

    def test_following_an_injection_is_a_leak(self):
        case = next(c for c in load_policy_rules() if c["id"] == "PR-39")
        mat = h.load_policy_material()
        out = {"rules": [{"kind": "deny", "target": "category", "value": "health", "params": {}, "confidence": 0.9},
                         {"kind": "recipient_restrict", "target": "all", "value": "", "params": {"recipients": ["bank_a", "bank_b"]},
                          "confidence": 0.9}], "clarifications": []}
        self.assertEqual(h.score_case(case, out, mat)["verdict"], "leak")

    def test_invented_value_is_invalid(self):
        case = next(c for c in load_policy_rules() if c["id"] == "PR-25")
        out = {"rules": [{"kind": "deny", "target": "field", "value": "buyer_handle", "params": {}, "confidence": 0.9}],
               "clarifications": []}
        self.assertEqual(h.score_case(case, out, h.load_policy_material())["verdict"], "invalid")

    def test_parser_errors_are_counted_not_crashed(self):
        def boom(t, m):
            raise RuntimeError("down")
        self.assertEqual(h.evaluate(boom)["metrics"]["errors"], 40)

    def test_gemini_adapter_with_fake_client(self):
        from alibi.llm import FakeClient

        fake = FakeClient({"*": {"rules": [], "clarifications": [{"source_text": "x", "question": "y"}],
                                 "plain_summary": "Nothing compiled."}})
        parse = h.gemini_parser(client=fake)
        m = h.evaluate(parse)["metrics"]
        self.assertEqual((m["errors"], m["leaks"]), (0, 0))
        self.assertEqual(len(fake.calls), 40)


if __name__ == "__main__":
    unittest.main()
