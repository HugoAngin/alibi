import base64
import gzip
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from agents.execution import at_rest_probe as p

KEY = hashlib.sha256(b"SYNTHETIC test key").digest()


class Controls(unittest.TestCase):
    def test_all_controls_as_expected(self):
        self.assertEqual(p.controls(), p.EXPECTED_CONTROLS)

    def test_app_store_not_run_until_published(self):
        self.assertEqual(p.probe(p.app_store())["outcome"], "not_run")


class Detection(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_report_is_redacted(self):
        _, canaries = p.planted_case("redact01")
        report = p.probe(p.PlaintextStore(self.root), run_id="redact01")
        text = json.dumps(report)
        self.assertEqual(report["outcome"], "not_blocked")
        for c in canaries:
            self.assertNotIn(c.decode(), text)
        self.assertNotIn("prescription eyewear", text)

    def test_key_under_innocent_name_is_found_by_content(self):
        class Hidden(p.EncryptedStore):
            def persist(self, case):
                super().persist(case)
                (self.root / "config.json").write_text(json.dumps({"x": base64.b64encode(KEY).decode()}))

        report = p.probe(Hidden(self.root, KEY), run_id="hide01")
        self.assertEqual(report["outcome"], "not_blocked")
        self.assertEqual(report["findings"][0]["kind"], "key")
        self.assertEqual(report["findings"][0]["encoding"], "base64")

    def test_utf16_and_gzip_sidecars_are_found(self):
        class Sneaky(p.EncryptedStore):
            def persist(self, case):
                super().persist(case)
                text = json.dumps(case["merchant_deliveries"][0]["event"])
                (self.root / "cache.gz").write_bytes(gzip.compress(text.encode()))
                (self.root / ".tmp16").write_bytes(text.encode("utf-16-le"))

        report = p.probe(Sneaky(self.root, KEY), run_id="side01")
        seen = {(f["path"], f["view"], f["encoding"]) for f in report["findings"]}
        self.assertIn(("cache.gz", "gunzip", "raw"), seen)
        self.assertIn((".tmp16", "file", "utf16le"), seen)

    def test_unreadable_through_app_is_not_run(self):
        class Lossy(p.EncryptedStore):
            def read_all(self):
                return b"nothing useful"

        report = p.probe(Lossy(self.root, KEY), run_id="lossy01")
        self.assertEqual(report["outcome"], "not_run")
        self.assertIn("persistence not established", report["reason"])


if __name__ == "__main__":
    unittest.main()
