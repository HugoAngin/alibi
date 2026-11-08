"""Checkpoint tests use public synthetic fixtures and never call a model."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from threading import Event

import pytest

from agents.data.api import canonical_case
from agents.eval import capture_checkpoint, isolated_candidate, run_sealed
from agents.eval.sealed_worker import failure_rows
from alibi.eval import common, rules_reference


def snapshot_fixture(tmp_path, monkeypatch):
    source = tmp_path / "capture"
    source.mkdir()
    first = canonical_case()
    second = deepcopy(first)
    second["case_number"] = 2
    gold = {case["case_number"]: case["gold"] for case in (first, second)}
    cases = [{key: value for key, value in case.items() if key != "gold"} for case in (first, second)]
    for mode in run_sealed.MODES:
        rows = rules_reference.predictions_for([first]) if mode == "rules" else []
        common.write_jsonl(source / f"{mode}.jsonl", rows)
    common.write_jsonl(source / "probes.jsonl", [])
    gold_path = tmp_path / "synthetic-gold"
    gold_path.write_text("synthetic test gold, supplied by stub reader")
    monkeypatch.setattr(common, "read_gold_file", lambda path: gold)
    kwargs = dict(gold_path=gold_path, adversarial_ids={2}, signature={"candidate": "a" * 40},
                  scheduling={"aggregate": {"total": 6, "attempted": 1, "completed": 1}, "systems": {}},
                  completed_workers={"rules-1"})
    return source, cases, kwargs


def test_checkpoint_full_denominators_and_saved_only_replay(tmp_path, monkeypatch, capsys):
    source, cases, kwargs = snapshot_fixture(tmp_path, monkeypatch)
    original = deepcopy(cases)
    checkpoint = capture_checkpoint.create_checkpoint(source, cases, **kwargs)
    assert cases == original and all("gold" not in case for case in cases)
    scores = common.load_json(checkpoint / "scores.json")
    assert scores["checkpoint"]["missing_requests"] == {"agents": 4, "one_pass": 4, "rules": 2}
    for mode in run_sealed.MODES:
        assert scores["official"][mode]["cases"] == 1
        assert scores["adversarial"][mode]["cases"] == 1
        assert len(common.read_jsonl(checkpoint / f"{mode}.jsonl")) == 4
        assert scores["retained_capture"]["usage_by_system"][mode]["totals"]["cost_usd_estimate"] is None
    assert (checkpoint / "report.md").read_text().startswith("INCOMPLETE checkpoint")
    assert checkpoint.stat().st_mode & 0o777 == 0o700
    assert (checkpoint / capture_checkpoint.MARKER).exists()
    kwargs["gold_path"].unlink()
    for mode in run_sealed.MODES:
        (source / f"{mode}.jsonl").unlink()

    def forbidden(*_args, **_kwargs):
        pytest.fail("saved-only replay read gold or launched a worker")

    monkeypatch.setattr(common, "read_gold_file", forbidden)
    monkeypatch.setattr(isolated_candidate, "run_isolated", forbidden)
    capture_checkpoint.main(["--capture", str(checkpoint)])
    metrics = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert metrics["checkpoint_incomplete"] == 1
    assert metrics["checkpoint_full_denominator"] == 1
    assert metrics["official_agents_cases"] == 1
    assert metrics["adversarial_agents_cases"] == 1
    with (checkpoint / "rules.jsonl").open("a") as handle:
        handle.write("\n")
    with pytest.raises(ValueError, match="changed"):
        capture_checkpoint.replay_checkpoint(checkpoint)


def test_checkpoint_is_exclusive_and_incomplete_snapshot_cannot_replay(tmp_path, monkeypatch):
    source, cases, kwargs = snapshot_fixture(tmp_path, monkeypatch)

    def interrupted(*_args, **_kwargs):
        raise RuntimeError("synthetic interruption")

    monkeypatch.setattr(run_sealed, "score_saved", interrupted)
    with pytest.raises(RuntimeError, match="interruption"):
        capture_checkpoint.create_checkpoint(source, cases, **kwargs)
    assert not (source / "checkpoint" / capture_checkpoint.MARKER).exists()
    with pytest.raises(ValueError, match="incomplete"):
        capture_checkpoint.replay_checkpoint(source / "checkpoint")
    with pytest.raises(FileExistsError):
        capture_checkpoint.create_checkpoint(source, cases, **kwargs)


@pytest.mark.parametrize("due", [True, False])
def test_coordinator_checkpoint_once_only_when_due(tmp_path, monkeypatch, due):
    output = tmp_path / "capture"
    output.mkdir()
    calls = []
    now = datetime.now(timezone.utc)
    isolation = dict(candidate="candidate", private_root=tmp_path / "private", credential=tmp_path / "credential",
                     workers=1, deadline=now + timedelta(minutes=5),
                     checkpoint_at=now + timedelta(minutes=-1 if due else 1),
                     gold_path=tmp_path / "gold-never-read", adversarial_ids=set())

    def worker(case, mode, **kwargs):
        assert "gold_path" not in kwargs and "adversarial_ids" not in kwargs
        return failure_rows(case, mode, "timeout", "synthetic"), {}

    def snapshot(*args, **kwargs):
        calls.append(kwargs)
        assert all("gold" not in case for case in args[1])
        assert kwargs["signature"] == {"checkpoint_at": isolation["checkpoint_at"].isoformat()}

    case = {key: value for key, value in canonical_case().items() if key != "gold"}
    monkeypatch.setattr(isolated_candidate, "run_isolated", worker)
    monkeypatch.setattr(capture_checkpoint, "create_checkpoint", snapshot)
    run_sealed._parallel_capture(output, [case], isolation,
                                 {"checkpoint_at": isolation["checkpoint_at"].isoformat()})
    assert len(calls) == int(due)


def test_checkpoint_fires_while_every_worker_is_in_flight(tmp_path, monkeypatch):
    from agents.eval import parallel_capture

    output = tmp_path / "capture"
    output.mkdir()
    release = Event()
    real_wait = parallel_capture.wait
    state = {"waits": 0, "checkpoints": 0}
    now = datetime.now(timezone.utc)
    isolation = dict(candidate="candidate", private_root=tmp_path / "private", credential=tmp_path / "credential",
                     workers=3, deadline=now + timedelta(minutes=5), checkpoint_at=now - timedelta(seconds=1),
                     gold_path=tmp_path / "gold-never-read", adversarial_ids=set())

    def wait(futures, *, timeout, return_when):
        state["waits"] += 1
        if state["waits"] == 1:
            assert timeout <= 1
            return set(), set(futures)
        return real_wait(futures, timeout=timeout, return_when=return_when)

    def worker(case, mode, **kwargs):
        assert release.wait(2)
        return failure_rows(case, mode, "timeout", "synthetic"), {}

    def snapshot(*args, **kwargs):
        state["checkpoints"] += 1
        assert not kwargs["completed_workers"]
        assert kwargs["scheduling"]["aggregate"]["completed"] == 0
        assert kwargs["scheduling"]["aggregate"]["attempted"] == 3
        assert all(not (output / f"{mode}.jsonl").read_text() for mode in run_sealed.MODES)
        release.set()

    monkeypatch.setattr(parallel_capture, "wait", wait)
    monkeypatch.setattr(isolated_candidate, "run_isolated", worker)
    monkeypatch.setattr(capture_checkpoint, "create_checkpoint", snapshot)
    run_sealed._parallel_capture(output, [canonical_case()], isolation, {})
    assert state["checkpoints"] == 1
