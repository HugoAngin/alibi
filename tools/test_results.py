import json
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock

import lockbox
import results

try:
    import pandas as pd
except ImportError:
    pd = None

NEEDS_PANDAS = "needs pandas: run with uv run --frozen python -m unittest"


class Metrics(unittest.TestCase):
    def test_reads_the_last_line(self):
        out = "loading...\n{\"ignored\": 1}\nfitting\n{\"sharpe\": 1.5, \"name\": \"x\", \"ok\": true}\n"
        self.assertEqual(results.metrics_from(out), {"sharpe": 1.5})

    def test_rejects_output_without_metrics(self):
        for out in ["", "done\n", "{\"name\": \"x\"}\n", "[1, 2]\n"]:
            with self.assertRaises(ValueError):
                results.metrics_from(out)

    def test_compare(self):
        self.assertEqual(results.compare({"a": 1.0, "b": float("nan")},
                                         {"a": 1.0 + 1e-12, "b": float("nan")}, 1e-6, 1e-9), [])
        problems = results.compare({"a": 1.0, "c": 2.0}, {"a": 1.1}, 1e-6, 1e-9)
        self.assertEqual(len(problems), 2)

    def test_ids_are_stable_and_distinct(self):
        record = {"role": "research", "split": "validation", "command": "x", "commit": "c",
                  "metrics": {"sharpe": 1}, "created": "t"}
        self.assertEqual(results.record_id(record), results.record_id(dict(record)))
        self.assertNotEqual(results.record_id(record), results.record_id({**record, "created": "u"}))


class LockboxLooks(unittest.TestCase):
    def test_only_eval_and_only_once(self):
        self.assertIn("only eval", results.lockbox_problem("research", [], None))
        self.assertIsNone(results.lockbox_problem("eval", [], None))
        earlier = [{"id": "abc", "split": "lockbox"}]
        self.assertIn("already scored", results.lockbox_problem("eval", earlier, None))
        self.assertIsNone(results.lockbox_problem("eval", earlier, "bug in costs, fixed"))

    def test_child_processes_see_the_lockbox_only_when_asked(self):
        with mock.patch.dict(results.os.environ, {lockbox.OPEN_FLAG: "1"}):
            self.assertNotIn(lockbox.OPEN_FLAG, results.child_env(False))
            self.assertEqual(results.child_env(True)[lockbox.OPEN_FLAG], "1")


class Report(unittest.TestCase):
    def test_rows_show_verdicts(self):
        records = [{"id": "a1", "role": "research", "split": "validation", "variants": 12,
                    "metrics": {"sharpe": 1.23456}, "commit": "0123456789", "command": "run"},
                   {"id": "b2", "role": "eval", "split": "lockbox", "variants": 12,
                    "metrics": {"sharpe": 0.4}, "commit": "abcdef0123", "command": "run"}]
        rows = results.report_rows(records, {"a1": {"verdict": "pass", "by": "eval"}})
        self.assertEqual(rows[0][2], "sharpe 1.235")
        self.assertEqual(rows[0][4], "pass (eval)")
        self.assertEqual(rows[1][4], "not verified")


class Lockbox(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.splits = Path(self.folder.name) / "splits.json"
        self.patches = [mock.patch.object(lockbox, "SPLITS", self.splits),
                        mock.patch.object(lockbox, "role", return_value="research")]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in self.patches:
            patch.stop()
        self.folder.cleanup()

    def set_start(self, value):
        self.splits.write_text(json.dumps({"lockbox_from": value}))

    def test_times_in_many_shapes(self):
        utc = datetime(2024, 7, 1, tzinfo=timezone.utc)
        self.assertEqual(lockbox.to_utc("2024-07-01T00:00:00Z"), utc)
        self.assertEqual(lockbox.to_utc("2024-07-01"), utc)
        self.assertEqual(lockbox.to_utc(date(2024, 7, 1)), utc)
        self.assertEqual(lockbox.to_utc("2024-07-01T02:00:00+02:00"), utc)
        self.assertEqual(lockbox.to_utc(42), 42)

    def test_no_split_hides_nothing(self):
        lockbox.check("2030-01-01")

    def test_check_blocks_the_lockbox(self):
        self.set_start("2024-07-01")
        lockbox.check(["2024-06-30T23:59:59Z", "2024-01-01"])
        with self.assertRaises(PermissionError):
            lockbox.check(["2024-06-30", "2024-07-01"])

    def test_numeric_split(self):
        self.set_start(9000)
        lockbox.check(range(9000))
        with self.assertRaises(PermissionError):
            lockbox.check(9000)

    def test_open_only_for_eval_with_the_flag(self):
        self.set_start("2024-07-01")
        with mock.patch.dict(lockbox.os.environ, {lockbox.OPEN_FLAG: "1"}):
            with self.assertRaises(PermissionError):
                lockbox.check("2024-08-01")
            with mock.patch.object(lockbox, "role", return_value="eval"):
                lockbox.check("2024-08-01")
        with mock.patch.object(lockbox, "role", return_value="eval"):
            with self.assertRaises(PermissionError):
                lockbox.check("2024-08-01")

    @unittest.skipIf(pd is None, NEEDS_PANDAS)
    def test_visible_drops_lockbox_rows(self):
        self.set_start("2024-07-01T00:00:00Z")
        frame = pd.DataFrame({"timestamp": pd.date_range("2024-06-28", periods=6, freq="D"),
                              "price": range(6)})
        self.assertEqual(list(lockbox.visible(frame, "timestamp")["price"]), [0, 1, 2])
        # Midnight on 1 July in London is 23:00 UTC on 30 June, so it is still before the lockbox.
        aware = frame.assign(timestamp=frame["timestamp"].dt.tz_localize("Europe/London"))
        self.assertEqual(len(lockbox.visible(aware, "timestamp")), 4)
        self.assertEqual(len(lockbox.visible(frame.set_index("timestamp"))), 3)
        lockbox.check(lockbox.visible(frame, "timestamp")["timestamp"])
        with self.assertRaises(PermissionError):
            lockbox.check(frame["timestamp"])

    @unittest.skipIf(pd is None, NEEDS_PANDAS)
    def test_visible_with_numeric_steps(self):
        self.set_start(4)
        frame = pd.DataFrame({"step": range(6), "x": range(6)})
        self.assertEqual(list(lockbox.visible(frame, "step")["step"]), [0, 1, 2, 3])


if __name__ == "__main__":
    unittest.main()
