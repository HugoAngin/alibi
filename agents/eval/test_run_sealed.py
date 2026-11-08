"""Exercise the runner and its capture-once replay without opening the lockbox."""

import json

import pytest

from agents.data.api import canonical_case
from agents.eval import run_sealed
from alibi.eval import common, rules_reference


def test_public_dry_run_replays_without_launching_workers(tmp_path, capsys, monkeypatch):
    output = tmp_path / "results"
    args = ["--dry-run", "--case", "1", "--case", "2", "--out", str(output)]
    run_sealed.main(args)
    first = json.loads(capsys.readouterr().out)
    report = common.load_json(output / "scores.json")
    assert first["cases"] == 2 and first["safe_complete"] == 0
    assert report["mode"] == "public_rehearsal"
    assert report["official"]["rules"]["model_calls"] == 0
    assert report["official"]["one_pass"]["model_calls"] == report["official"]["one_pass"]["requests"]
    assert "stubbed model" in (output / "deck-table.md").read_text()

    def must_not_run(*_args, **_kwargs):
        pytest.fail("replay started an adapter worker")

    monkeypatch.setattr(run_sealed, "run_case_worker", must_not_run)
    run_sealed.main(args)
    assert json.loads(capsys.readouterr().out) == first
    with (output / "rules.jsonl").open("a") as result:
        result.write("\n")
    with pytest.raises(RuntimeError, match="retained capture or its inputs changed"):
        run_sealed.main(args)


def test_both_scorers_read_recipient_visible_results(tmp_path):
    case = canonical_case()
    rows = rules_reference.predictions_for([case])
    for filename in run_sealed.PREDICTION_FILES.values():
        common.write_jsonl(tmp_path / filename, rows)
    common.write_jsonl(tmp_path / run_sealed.PROBE_FILE, [])
    run_sealed.score_saved(tmp_path, [case], [], dry=True)
    report = common.load_json(tmp_path / "scores.json")
    assert report["official"]["rules"]["requests_safe"] > 0
    assert report["independent"]["rules"]["metrics"]["requests_observed"] == len(case["rfcs"])


def test_closed_lockbox_stops_before_private_paths_are_read(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(run_sealed, "is_open", lambda: False)
    with pytest.raises(SystemExit):
        run_sealed.main(["--out", str(tmp_path / "unused"), "--manifest", "missing-file"])
    assert "authorized tools/results.py" in capsys.readouterr().err
    assert not (tmp_path / "unused").exists()


def test_capture_lock_stops_a_second_run_in_a_new_directory(tmp_path):
    lock = tmp_path / "capture.json"
    case = canonical_case()
    options = {"dry": True, "timeout": 15, "blocked": (), "probes": None, "lock": lock}
    run_sealed._capture_once(tmp_path / "first", [case], {"manifest": "synthetic"}, **options)
    with pytest.raises(RuntimeError, match="already started"):
        run_sealed._capture_once(tmp_path / "second", [case], {"manifest": "synthetic"}, **options)
    assert not (tmp_path / "second").exists()
