"""Synthetic sentinel content cannot pass the aggregate export allowlist."""

import json
from pathlib import Path
import socket
import subprocess

import pytest

from agents.eval import frozen_summary as export


SECRET = "FORBIDDEN_HIDDEN_CASE_AND_REQUEST"


def synthetic():
    metrics = {"cases": 2, "safe_complete": 1, "cost_usd_estimate": None,
               "request_id": SECRET, SECRET: 999, "facts": {"amount": SECRET}}
    scores = {"mode": "frozen", "official": {"agents": metrics, SECRET: metrics},
              "independent": {"agents": {"metrics": metrics, "details": SECRET}},
              "adversarial": {"agents": metrics},
              "headline": {"systems": {"agents": {**metrics, "family_bootstrap_95": [SECRET, 1],
                            "forbidden_disclosure": {"failures": 0, "observed": 2, "case_number": SECRET}}},
                           "details": SECRET},
              "retained_capture": {
                  "usage_by_system": {"agents": {"unknown_workers": 1, "complete": False,
                    "totals": {"tokens": None}, "partial_known_totals": {"tokens": 22, SECRET: 99},
                    "source_path": SECRET}},
                  "claim_divergence_by_system": {"agents": {"differing_rfcs": 1,
                    "details": [{"case_number": 123, "request_id": SECRET}]}}, "scope": SECRET}}
    run = {"input": {"candidate": "a" * 40, "evaluator_commit": "b" * 40, "manifest": "c" * 64,
                     "workers": 4, "deadline": SECRET, "model_config": SECRET, SECRET: 99}}
    schedule = {"aggregate": {"completed": 3, "aborted": True, "abort_reason": SECRET},
                "systems": {"agents": {"failed": 1, "request_id": SECRET}}}
    return scores, run, schedule


def test_allowlist_excludes_nested_content_and_preserves_unknowns():
    result = export.build_summary(*synthetic())
    encoded = json.dumps(result)
    assert SECRET not in encoded and "request_id" not in encoded and "case_number" not in encoded
    assert result["official"]["agents"]["cost_usd_estimate"] is None
    assert result["retained_usage"]["agents"]["partial_known_totals"]["tokens"] == 22
    assert result["claim_divergence"]["agents"] == {"differing_rfcs": 1}
    assert result["capture"]["aggregate"] == {"aborted": True, "completed": 3}
    assert result["signature"]["digests"]["manifest"] == "c" * 64


def test_cli_reads_only_three_artifacts_without_mutation(tmp_path, monkeypatch, capsys):
    def forbidden_call(*args, **kwargs):
        raise AssertionError("aggregate export must not launch a process or use the network")
    monkeypatch.setattr(subprocess, "Popen", forbidden_call)
    monkeypatch.setattr(socket, "create_connection", forbidden_call)
    capture = tmp_path / "capture"
    capture.mkdir()
    for name, value in zip(("scores", "run", "scheduling"), synthetic()):
        (capture / f"{name}.json").write_text(json.dumps(value))
    (capture / "gold.json").write_text(SECRET)
    before = {p.name: p.read_bytes() for p in capture.iterdir()}
    read_text = Path.read_text
    allowed = {"scores.json", "run.json", "scheduling.json"}
    def guarded_read(path, *args, **kwargs):
        assert path.parent == capture and path.name in allowed
        return read_text(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", guarded_read)
    artifact = tmp_path / "aggregate.json"
    export.main(["--capture", str(capture), "--out", str(artifact)])
    assert {p.name: p.read_bytes() for p in capture.iterdir()} == before
    assert SECRET.encode() not in artifact.read_bytes()
    stdout = capsys.readouterr().out
    assert SECRET not in stdout
    assert json.loads(stdout.splitlines()[-1])["official_agents_safe_complete"] == 1
    with pytest.raises(FileExistsError):
        export.main(["--capture", str(capture), "--out", str(artifact)])


def test_reject_output_inside_capture_before_reading(tmp_path):
    with pytest.raises(SystemExit):
        export.main(["--capture", str(tmp_path), "--out", str(tmp_path / "scores.json")])


def test_resume_discloses_separate_attempts_without_exporting_details():
    scores, run, schedule = synthetic()
    run["input"].update(original_run_sha256="d" * 64, permission_proof_sha256="e" * 64)
    schedule["original"] = {"aggregate": {"attempted": 70, "completed": 67, "errors": 3,
        "not_started": 62, "abort_reason": SECRET}, "systems": {"rules": {"attempted": 23, "failed": 16}}}
    schedule["resume"] = {"aggregate": {"attempted": 65, "completed": 65, "errors": 0},
                          "systems": {"rules": {"attempted": 21, "completed": 20, "failed": 1}},
                          "details": SECRET}
    result = export.build_summary(scores, run, schedule)
    assert SECRET not in json.dumps(result)
    disclosure = result["interrupted_capture"]
    assert disclosure["original"]["aggregate"]["attempted"] == 70
    assert disclosure["continuation"]["aggregate"]["attempted"] == 65
    assert disclosure["original_run_sha256"] == "d" * 64
    assert "67 original returned groups" in result["interpretation_and_caveats"][-1]


def test_resume_missing_scheduling_fails_closed():
    scores, run, schedule = synthetic()
    run["input"]["original_run_sha256"] = "d" * 64
    with pytest.raises(ValueError, match="requires original"):
        export.build_summary(scores, run, schedule)


def test_resume_export_cannot_write_into_original_capture(tmp_path):
    capture = tmp_path / "original"
    resume = capture / "resume"
    resume.mkdir(parents=True)
    (resume / "resume-lock.json").write_text("{}")
    with pytest.raises(SystemExit):
        export.main(["--capture", str(resume), "--out", str(capture / "new.json")])
    assert not (capture / "new.json").exists()
