import json
import unittest

from agents.execution import api, injection_fixture


class InjectionFixture(unittest.TestCase):
    def test_committed_file_matches_generator(self):
        injection_fixture.validate()
        rows = [json.dumps(c, ensure_ascii=False) for c in injection_fixture.CASES]
        committed = (api.OUT / "injections.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(committed, rows)

    def test_probe_ids_exist(self):
        known = set(api.probe_ids())
        for c in api.load_injections():
            for p in c["probes"]:
                self.assertIn(p, known, c["id"])

    def test_covers_every_channel(self):
        self.assertEqual({c["channel"] for c in api.load_injections()}, injection_fixture.CHANNELS)


if __name__ == "__main__":
    unittest.main()
