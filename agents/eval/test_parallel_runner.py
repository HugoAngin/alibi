"""Synthetic capture/replay checks; no model calls or private cases."""

from datetime import datetime, timedelta, timezone
import json
from threading import Barrier, Lock

import pytest

from agents.data.api import canonical_case
from agents.eval import isolated_candidate, run_sealed
from agents.eval.sealed_worker import failure_rows
from alibi.eval import common


def capture_args(tmp_path, monkeypatch, *, workers=4):
    monkeypatch.setattr(run_sealed.client, "load_api_key", lambda *_: "stub")
    private = tmp_path / "private"
    private.mkdir()
    credential = tmp_path / "credential"
    credential.write_text("unused")
    isolation = dict(candidate="candidate", private_root=private, credential=credential,
                     workers=workers, deadline=datetime.now(timezone.utc) + timedelta(minutes=1))
    second = canonical_case()
    second["case_number"] = 2
    return dict(output=tmp_path / "capture", cases=[canonical_case(), second],
                signature={"commit": "candidate"}, dry=False, timeout=90,
                blocked=(), probes=None, isolation=isolation)


def worker_rows(case, mode):
    rows = failure_rows(case, mode, "error", "stub")
    for row in rows:
        row["failure"] = None
    return rows


@pytest.mark.parametrize('deny_siblings', [False, True])
def test_parallel_capture_lanes_integrity_and_replay(tmp_path, monkeypatch, capsys, deny_siblings):
    args = capture_args(tmp_path, monkeypatch)
    if deny_siblings:
        args['isolation']['capture_root'] = args['output'].parent
    barrier = Barrier(4)
    guard = Lock()
    calls = []

    def worker(case, mode, **kwargs):
        with guard:
            calls.append((mode, case["case_number"], kwargs))
            index = len(calls)
        if index <= 4:
            barrier.wait(timeout=5)
        output = kwargs["output"]
        output.mkdir(parents=True)
        common.write_json(output / "usage.json", {"known": False})
        (output / "llm_log.jsonl").write_text("retained\n")
        return worker_rows(case, mode), {"rate_limited": index == 1}

    monkeypatch.setattr(isolated_candidate, "run_isolated", worker)
    run_sealed._capture_once(**args)
    assert len(calls) == 6
    first_lanes = {kwargs["scratch_parent"] for _, _, kwargs in calls[:4]}
    assert len(first_lanes) == 4
    for _, _, kwargs in calls:
        assert set(kwargs["denied_scratch"]) == first_lanes - {kwargs["scratch_parent"]}
        assert kwargs["capture_root"] == (args['output'].parent if deny_siblings else args['output'])
        assert kwargs["deadline"] == args["isolation"]["deadline"]
    assert all(not root.exists() for root in first_lanes)
    summary = common.load_json(args["output"] / "scheduling.json")
    assert summary["aggregate"]["attempted"] == 6
    assert summary["aggregate"]["final_workers"] == 2
    assert all(counts == dict(attempted=2, completed=2, failed=0, not_started=0)
               for counts in summary["systems"].values())
    record = common.load_json(args["output"] / "run.json")
    assert len(record["extra_files"]) == 14
    assert "case_number" not in capsys.readouterr().out

    def never(*args, **kwargs):
        pytest.fail("replay must not launch workers")

    monkeypatch.setattr(isolated_candidate, "run_isolated", never)
    args["isolation"]["deadline"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    run_sealed._capture_once(**args)
    next((args["output"] / "capture-workers").rglob("llm_log.jsonl")).write_text("changed")
    with pytest.raises(RuntimeError, match="worker evidence changed"):
        run_sealed._capture_once(**args)


def test_worker_exception_preserves_all_request_denominators(tmp_path, monkeypatch):
    args = capture_args(tmp_path, monkeypatch, workers=1)

    def broken(*_args, **_kwargs):
        raise RuntimeError("sensitive case text must not escape")

    monkeypatch.setattr(isolated_candidate, "run_isolated", broken)
    run_sealed._capture_once(**args)
    for filename in run_sealed.PREDICTION_FILES.values():
        rows = common.read_jsonl(args["output"] / filename)
        assert len(rows) == sum(len(case["rfcs"]) for case in args["cases"])
        assert all(row["failure"]["type"] in ("error", "unavailable") for row in rows)
        for row in rows:
            if row["failure"]["type"] == "unavailable":
                assert "capture_integrity" in row["failure"]["detail"]
        assert "sensitive" not in json.dumps(rows)
    summary = common.load_json(args["output"] / "scheduling.json")
    assert summary["aggregate"]["errors"] == 1
    assert summary["aggregate"]["not_started"] == 5
    assert summary["aggregate"]["aborted"]
    assert summary["aggregate"]["abort_reason"] == "RuntimeError"
    assert sum(counts["failed"] for counts in summary["systems"].values()) == 1


def test_deadline_skips_queued_jobs_with_failure_rows(tmp_path, monkeypatch):
    from agents.eval import parallel_capture
    args = capture_args(tmp_path, monkeypatch, workers=1)
    now = datetime.now(timezone.utc)
    clock = [now]
    args["isolation"]["deadline"] = now + timedelta(seconds=10)
    monkeypatch.setattr(parallel_capture, "_now", lambda: clock[0])

    def worker(case, mode, **_kwargs):
        clock[0] = now + timedelta(seconds=11)
        return worker_rows(case, mode), {}

    monkeypatch.setattr(isolated_candidate, "run_isolated", worker)
    run_sealed._capture_once(**args)
    summary = common.load_json(args["output"] / "scheduling.json")
    assert summary["aggregate"]["attempted"] == 1
    assert summary["aggregate"]["not_started"] == 5
    for filename in run_sealed.PREDICTION_FILES.values():
        rows = common.read_jsonl(args["output"] / filename)
        assert len(rows) == sum(len(case["rfcs"]) for case in args["cases"])
    rows = common.read_jsonl(args["output"] / "rules.jsonl")
    assert all(row["failure"]["type"] == "unavailable" for row in rows)


def test_expired_capture_does_not_create_directory_or_lock(tmp_path, monkeypatch):
    args = capture_args(tmp_path, monkeypatch)
    args["isolation"]["deadline"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    args["lock"] = tmp_path / "capture-lock"
    with pytest.raises(ValueError, match="deadline"):
        run_sealed._capture_once(**args)
    assert not args["output"].exists()
    assert not args["lock"].exists()


def test_deadline_requires_timezone():
    import argparse
    with pytest.raises(argparse.ArgumentTypeError, match="timezone"):
        run_sealed._aware_deadline("2026-11-08T11:50:00")
    assert run_sealed._aware_deadline("2026-11-08T11:50:00Z").utcoffset() == timedelta(0)


def test_pinned_candidate_requires_tag_and_clean_evaluator(monkeypatch):
    responses = iter(["", "candidate\n", "candidate\n", "evaluator\n"])
    monkeypatch.setattr(run_sealed.subprocess, "check_output", lambda *_a, **_kw: next(responses))
    assert run_sealed._pinned_candidate("sealed-candidate") == ("candidate", "evaluator")
    responses = iter(["", "wrong", "candidate"])
    with pytest.raises(ValueError, match="sealed-candidate tag"):
        run_sealed._pinned_candidate("wrong")
    responses = iter(["?? untracked.py"])
    with pytest.raises(ValueError, match="clean"):
        run_sealed._pinned_candidate("sealed-candidate")


@pytest.mark.parametrize("tag", ["sealed-candidate-2", "sealed-candidate-3", "sealed-candidate-4", "sealed-candidate-5"])
def test_refrozen_candidate_is_bound_to_its_own_approved_tag(monkeypatch, tag):
    commands = []
    responses = iter(["", "candidate2", "candidate2", "evaluator"])

    def git(command, **_kwargs):
        commands.append(command)
        return next(responses)

    monkeypatch.setattr(run_sealed.subprocess, "check_output", git)
    assert run_sealed._pinned_candidate(tag) == ("candidate2", "evaluator")
    assert commands[2][-1] == f"refs/tags/{tag}^{{commit}}"


def test_second_look_preserves_the_original_lock_and_binds_its_config(tmp_path):
    original = tmp_path / "original"
    original.mkdir()
    lock = tmp_path / "capture-original.json"
    common.write_json(lock, {"run": str(original)})
    manifest = {"set_sha256": "pack", "model_config": {"sha256": "config"}}
    original_manifest = tmp_path / "original-manifest.json"
    common.write_json(original_manifest, manifest)
    marker = original / "run.json"
    common.write_json(marker, {"input": {"commit": "58bf0d4a199f5ad3833fa9bde751d31140219975",
                                         "manifest": run_sealed._digest(original_manifest)}})

    second_lock, original_hash = run_sealed._second_look_lock(lock, original_manifest, manifest)
    assert second_lock == tmp_path / "capture-original-candidate-5.json"
    assert original_hash == run_sealed._digest(marker)
    assert common.load_json(lock) == {"run": str(original)}
    assert not second_lock.exists()
    with pytest.raises(ValueError, match="original capture, pack and model configuration"):
        run_sealed._second_look_lock(lock, original_manifest, {**manifest, "model_config": {"sha256": "other"}})


@pytest.mark.parametrize("tag", ["sealed-candidate-4", "sealed-candidate-5"])
def test_second_candidate_cannot_use_the_original_capture_route(capsys, tag):
    with pytest.raises(SystemExit) as error:
        run_sealed.main(["--candidate-ref", tag])
    assert error.value.code == 2
    assert "require --post-fix-second-look" in capsys.readouterr().err


def test_public_cli_signature_binds_candidate_and_evaluator_separately(tmp_path, monkeypatch):
    private = tmp_path / "private"
    private.mkdir()
    captured = {}
    monkeypatch.setenv("ALIBI_EVAL_ENV_FILE", str(tmp_path / "credential"))
    monkeypatch.setattr(run_sealed, "is_open", lambda: False)
    monkeypatch.setattr(run_sealed, "_pinned_candidate", lambda _: ("candidate", "evaluator"))
    monkeypatch.setattr(run_sealed.seal, "git_commit", lambda _: ("evaluator", False))
    monkeypatch.setattr(run_sealed, "load_case", lambda _: canonical_case())
    monkeypatch.setattr(run_sealed, "score_saved", lambda *_a, **_kw: {})

    def capture(_output, _cases, signature, **kwargs):
        captured.update(signature=signature, isolation=kwargs["isolation"])

    monkeypatch.setattr(run_sealed, "_capture_once", capture)
    run_sealed.main(["--public-live", "--case", "1", "--candidate-ref", "sealed-candidate",
                     "--workers", "3", "--deadline", "2026-11-08T11:50:00+00:00",
                     "--private-root", str(private), "--out", str(tmp_path / "capture")])
    signature = captured["signature"]
    assert signature["commit"] == signature["candidate"] == "candidate"
    assert signature["evaluator_commit"] == "evaluator"
    assert signature["workers"] == 3
    assert signature["deadline"] == "2026-11-08T11:50:00+00:00"
    assert signature["limits"] == isolated_candidate.LIMITS
    assert signature["inherited_llm_cap"] is False
