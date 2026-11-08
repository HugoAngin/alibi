"""Synthetic authorization, byte preservation and resume/replay tests; no model calls."""

from datetime import datetime, timedelta, timezone
import json

import pytest

from agents.eval import resume_capture as resume
from agents.eval.sealed_worker import failure_rows
from alibi.eval import common


def fixture(tmp_path):
    capture = tmp_path / "capture"
    capture.mkdir()
    cases = [{"case_number": number, "rfcs": [{"request_id": f"r-{number}"}]} for number in range(1, 45)]
    jobs = [(mode, case) for case in cases for mode in resume.MODES]
    rows = {mode: [] for mode in resume.MODES}
    for index, (mode, case) in enumerate(jobs):
        row = failure_rows(case, mode, "error", "isolated candidate did not complete")[0]
        if index < 62:
            row["failure"] = resume.UNSTARTED.copy()
        elif index < 65:
            row["failure"] = resume.PERMISSION.copy()
        elif index % 2:
            row["failure"] = None
        rows[mode].append(row)
    for mode in resume.MODES:
        with (capture / f"{mode}.jsonl").open("wb") as handle:
            for row in rows[mode]:
                handle.write((json.dumps(row, separators=(",  ", ": ")) + "\n").encode())
    common.write_jsonl(capture / "probes.jsonl", [])
    common.write_json(capture / "probes.jsonl.receipt.json", resume.probe_receipt(resume.CANDIDATE, []))
    return capture, cases


def test_selects_exact_authorized_groups_and_preserves_original_bytes(tmp_path):
    capture, cases = fixture(tmp_path)
    before = {mode: (capture / f"{mode}.jsonl").read_bytes() for mode in resume.MODES}
    raw, groups, selected = resume.select_groups(capture, cases)
    assert {key: len(value) for key, value in selected.items()} == dict(unstarted=62, permission=3, preserved=67)
    replacements = {key: [{**groups[key][0], "failure": None}] for key in selected["unstarted"]}
    output = capture / "resume"
    output.mkdir()
    resume.combine(output, capture, raw, replacements, selected["preserved"] | selected["permission"])
    for mode in resume.MODES:
        assert (capture / f"{mode}.jsonl").read_bytes() == before[mode]
        result = (output / f"{mode}.jsonl").read_bytes().splitlines(keepends=True)
        kept = [line for line in result if (mode, json.loads(line)["case_number"]) not in replacements]
        expected = [line for line in raw[mode] if (mode, json.loads(line)["case_number"]) not in replacements]
        assert kept == expected
    assert any(row.get("failure") for key in selected["preserved"] for row in groups[key])


def test_rejects_arbitrary_failed_group_and_proof_mismatch(tmp_path):
    capture, cases = fixture(tmp_path)
    rows = common.read_jsonl(capture / "agents.jsonl")
    rows[0]["failure"] = {"type": "error", "detail": "another error"}
    common.write_jsonl(capture / "agents.jsonl", rows)
    with pytest.raises(ValueError, match="classification"):
        resume.select_groups(capture, cases)
    proof = tmp_path / "proof.json"
    payload = dict(candidate_commit=resume.CANDIDATE, public_only=True,
                   permission_error_reproduced=True, fix_verified=True)
    common.write_json(proof, payload)
    assert resume._proof(proof, resume._digest(proof)) == resume._digest(proof)
    with pytest.raises(ValueError, match="SHA256"):
        resume._proof(proof, "0" * 64)
    payload["fix_verified"] = False
    common.write_json(proof, payload)
    with pytest.raises(ValueError, match="establish"):
        resume._proof(proof, resume._digest(proof))
    assert resume._proof(None, None) is None


def test_runs_only_65_authorized_groups_once_and_retains_all_denominators(tmp_path, monkeypatch):
    capture, cases = fixture(tmp_path)
    _raw, groups, selected = resume.select_groups(capture, cases)
    selected["eligible"] = selected["unstarted"] | selected["permission"]
    output = capture / "resume"
    output.mkdir()
    seen = []

    def worker(case, mode, **kwargs):
        key = (mode, case["case_number"])
        seen.append(key)
        assert key in selected["eligible"]
        assert kwargs["capture_root"] == capture
        assert len(kwargs["denied_scratch"]) == 3
        assert kwargs["scratch_parent"] not in kwargs["denied_scratch"]
        assert "gold" not in case
        return failure_rows(case, mode, "timeout", "candidate failure stays final"), {}

    monkeypatch.setattr(resume, "run_isolated", worker)
    replacements, summary, skipped = resume._capture(output, capture, cases, selected, groups,
        private_root=tmp_path / "private", credential=tmp_path / "credential",
        deadline=datetime.now(timezone.utc) + timedelta(minutes=1))
    assert len(seen) == len(set(seen)) == len(replacements) == 65
    assert set(seen) == selected["eligible"]
    assert not skipped
    assert summary["aggregate"]["completed"] == 65


def test_deadline_keeps_original_permission_failures_without_attempts(tmp_path, monkeypatch):
    capture, cases = fixture(tmp_path)
    _raw, groups, selected = resume.select_groups(capture, cases)
    selected["eligible"] = selected["unstarted"] | selected["permission"]
    output = capture / "resume"
    output.mkdir()
    monkeypatch.setattr(resume, "run_isolated", lambda *_a, **_kw: pytest.fail("deadline launched worker"))
    replacements, summary, skipped = resume._capture(output, capture, cases, selected, groups,
        private_root=tmp_path / "private", credential=tmp_path / "credential",
        deadline=datetime.now(timezone.utc) - timedelta(seconds=1))
    assert summary["aggregate"]["not_started"] == 65
    assert len(skipped) == 65
    assert all(replacements[key] == groups[key] for key in selected["permission"])
    assert all(replacements[key][0]["failure"]["type"] == "unavailable" for key in selected["unstarted"])


def test_existing_resume_replays_saved_metrics_without_workers_or_gold(tmp_path, monkeypatch, capsys):
    capture, _cases = fixture(tmp_path)
    common.write_json(capture / "run.json", {"synthetic": True})
    output = capture / "resume"
    output.mkdir()
    original_hash = resume._digest(capture / "run.json")
    common.write_json(output / "resume-lock.json", {"original_run_sha256": original_hash})
    common.write_json(output / "metrics.json", {"cases": 32, "resume_attempted": 65})
    common.write_json(output / "run.json", {"input": {"original_run_sha256": original_hash},
                                            "files": resume._retained_files(output)})
    monkeypatch.setattr(resume, "_original", lambda _: ({}, {}))
    monkeypatch.setattr(resume, "run_isolated", lambda *_a, **_kw: pytest.fail("replay launched worker"))
    monkeypatch.setattr(common, "read_gold_file", lambda *_a: pytest.fail("replay read gold"))
    resume.main(["--capture", str(capture)])
    assert json.loads(capsys.readouterr().out)["resume_attempted"] == 65
    (output / "metrics.json").write_text("{}")
    with pytest.raises(ValueError, match="changed"):
        resume.main(["--capture", str(capture)])


def test_incomplete_resume_never_relaunches(tmp_path, monkeypatch):
    capture, _cases = fixture(tmp_path)
    common.write_json(capture / "run.json", {})
    (capture / "resume").mkdir()
    monkeypatch.setattr(resume, "_original", lambda _: ({}, {}))
    with pytest.raises(RuntimeError, match="never relaunch"):
        resume.main(["--capture", str(capture)])


def test_full_resume_lifecycle_verifies_commitments_then_scores_once(tmp_path, monkeypatch, capsys):
    capture, cases = fixture(tmp_path)
    private = tmp_path / "private"
    inputs = private / "cases"
    inputs.mkdir(parents=True)
    gold_path = private / "gold.jsonl"
    gold_path.write_text("synthetic gold provided by stub")
    common.write_jsonl(inputs / "adversarial.jsonl", [{"case_number": n} for n in range(33, 45)])
    manifest = private / "manifest.json"
    common.write_json(manifest, {"purpose": "sealed", "candidate": {"commit": resume.CANDIDATE}, "set_sha256": "set"})
    config = private / "model.json"
    config.write_text("{}")
    common.write_json(private / "capture-set.json", {"run": str(capture)})
    signature = dict(commit=resume.CANDIDATE, candidate=resume.CANDIDATE, workers=4,
                     limits=resume.LIMITS, deadline=resume.DEADLINE.isoformat(), manifest=resume._digest(manifest))
    aggregate = dict(total=132, attempted=70, completed=67, errors=3, not_started=62,
                     aborted=True, abort_reason="PermissionError")
    common.write_json(capture / "scheduling.json", {"aggregate": aggregate, "systems": {}})
    common.write_json(capture / "progress.json", {"aggregate": aggregate})
    names = [*resume.PREDICTION_FILES.values(), "probes.jsonl", "probes.jsonl.receipt.json"]
    common.write_json(capture / "run.json", {"input": signature,
        "files": {name: resume._digest(capture / name) for name in names},
        "extra_files": resume._extra_capture_files(capture)})
    original_bytes = {name: (capture / name).read_bytes() for name in names}
    proof = tmp_path / "public-proof.json"
    common.write_json(proof, dict(candidate_commit=resume.CANDIDATE, public_only=True,
                                 permission_error_reproduced=True, fix_verified=True))
    credential = tmp_path / "synthetic-credential"
    credential.write_text("unused")
    monkeypatch.setenv("ALIBI_EVAL_ENV_FILE", str(credential))
    monkeypatch.setattr(resume, "is_open", lambda: True)
    monkeypatch.setattr(resume, "_pinned_candidate", lambda _: (resume.CANDIDATE, "e" * 40))
    monkeypatch.setattr(resume.seal, "verify_manifest", lambda *_: ([], []))
    monkeypatch.setattr(common, "load_sealed_cases", lambda *_a, **_kw: cases)
    monkeypatch.setattr(resume, "case_payload", lambda case: case)
    monkeypatch.setattr(resume.client, "load_api_key", lambda *_: None)
    state = {"gold_reads": 0, "captures": 0}

    class Clock:
        @staticmethod
        def now(_timezone):
            return resume.DEADLINE - timedelta(minutes=20)

    monkeypatch.setattr(resume, "datetime", Clock)

    def capture_stub(output, original, cases, selected, groups, **kwargs):
        assert state["gold_reads"] == 0
        state["captures"] += 1
        replacements = {key: [{**groups[key][0], "failure": None}] for key in selected["eligible"]}
        assert len(replacements) == 65 and not selected["preserved"] & replacements.keys()
        return replacements, {"aggregate": dict(attempted=65, completed=65, errors=0, not_started=0,
            final_workers=4, aborted=False, deadline_reached=False), "systems": {}}, set()

    def read_gold(_path):
        assert state["captures"] == 1
        state["gold_reads"] += 1
        return {case["case_number"]: {} for case in cases}

    def score(output, main, attacks, **kwargs):
        assert len(main) == 32 and len(attacks) == 12
        for name in ("report.md", "deck-table.md"):
            (output / name).write_text("synthetic report")
        common.write_json(output / "scores.json", {"synthetic": True})
        return {"cases": 32}

    monkeypatch.setattr(resume, "_capture", capture_stub)
    monkeypatch.setattr(common, "read_gold_file", read_gold)
    monkeypatch.setattr(resume, "score_saved", score)
    argv = ["--capture", str(capture), "--cases", str(inputs), "--gold", str(gold_path),
            "--manifest", str(manifest), "--model-config", str(config), "--private-root", str(private),
            "--permission-proof", str(proof), "--permission-proof-sha256", resume._digest(proof)]
    resume.main(argv)
    first = json.loads(capsys.readouterr().out)
    assert first["resume_attempted"] == 65
    assert all((capture / name).read_bytes() == data for name, data in original_bytes.items())
    combined = common.load_json(capture / "resume/scheduling.json")
    assert combined["aggregate"]["attempted"] == combined["aggregate"]["completed"] == 132
    assert combined["aggregate"]["errors"] == combined["aggregate"]["not_started"] == 0
    resume.main(argv)
    assert json.loads(capsys.readouterr().out) == first
    assert state == {"gold_reads": 1, "captures": 1}
