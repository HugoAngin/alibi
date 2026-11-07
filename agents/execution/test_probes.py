import unittest

from agents.execution import api, probes

REQUIRED = {"id", "area", "actor", "attack", "boundary", "expected", "audit", "kind", "core", "demo"}


class ProbeCatalogue(unittest.TestCase):
    def test_fields_and_unique_ids(self):
        ps = api.load_probes()
        self.assertEqual(len({p["id"] for p in ps}), len(ps))
        for p in ps:
            self.assertEqual(set(p), REQUIRED, p["id"])
            self.assertIn(p["kind"], ("refusal", "control"))

    def test_spec_adversarial_suite_is_covered(self):
        # SPEC Evaluation: bad keys, altered bytes, injection, cross-recipient access,
        # wrong audience, expiry, stale consent.
        ids = set(api.probe_ids())
        for needed in ("SP-28", "SP-25", "SP-38", "SP-18", "SP-26", "SP-27", "SP-02"):
            self.assertIn(needed, ids)
        self.assertGreaterEqual(len(api.core_probes()), 12)

    def test_rendered_markdown_is_current(self):
        self.assertEqual(probes.PROBES_MD.read_text(encoding="utf-8"), probes.render())


class Summarise(unittest.TestCase):
    def test_all_pass(self):
        outcomes = {
            p["id"]: ("refused" if p["kind"] == "refusal" else "accepted") for p in api.load_probes()
        }
        s = api.summarise(outcomes)
        self.assertEqual(s["probes_blocked"], s["probes_attempted"])
        self.assertEqual(s["controls_allowed"], s["controls_attempted"])
        self.assertEqual(s["failed"], [])

    def test_missing_counts_as_failed(self):
        s = api.summarise({"SP-01": "refused", "SP-30": "refused"})
        self.assertEqual(s["probes_blocked"], 1)
        self.assertEqual(s["controls_allowed"], 0)
        self.assertIn("SP-30", s["failed"])
        self.assertIn("SP-02", s["failed"])
        self.assertNotIn("SP-01", s["failed"])

    def test_rejects_unknown(self):
        with self.assertRaises(ValueError):
            api.summarise({"SP-99": "refused"})
        with self.assertRaises(ValueError):
            api.summarise({"SP-01": "maybe"})


if __name__ == "__main__":
    unittest.main()
