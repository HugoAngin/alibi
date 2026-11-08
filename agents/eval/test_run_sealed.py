"""Exercise the runner and its capture-once replay without opening the lockbox."""

import json

import pytest

from agents.data.api import canonical_case
from agents.eval import run_sealed
from agents.eval.probe_capture import probe_receipt, probe_receipt_path
from alibi.eval import common, rules_reference


@pytest.fixture
def candidate_probes(tmp_path):
    path = tmp_path / "candidate-probes.jsonl"
    rows = [{"probe_id": probe_id, "outcome": "not_run"} for probe_id in run_sealed.probe_ids()]
    common.write_jsonl(path, rows)
    common.write_json(probe_receipt_path(path), probe_receipt("candidate", rows))
    return path


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
    run_sealed.score_saved(tmp_path, [case], [], kind="public_rehearsal")
    report = common.load_json(tmp_path / "scores.json")
    assert report["official"]["rules"]["requests_safe"] > 0
    assert report["independent"]["rules"]["metrics"]["requests_observed"] == len(case["rfcs"])


def test_live_probe_report_keeps_the_catalogue_when_no_outcomes_are_observed(tmp_path):
    case = canonical_case()
    for filename in run_sealed.PREDICTION_FILES.values():
        common.write_jsonl(tmp_path / filename, [])
    ids = run_sealed.probe_ids()
    common.write_jsonl(tmp_path / run_sealed.PROBE_FILE, [
        {"probe_id": probe_id, "outcome": "error" if i == 0 else "not_run"}
        for i, probe_id in enumerate(ids)
    ])

    run_sealed.score_saved(tmp_path, [case], [], kind="public_live")
    report = common.load_json(tmp_path / "scores.json")
    assert report["probe_coverage"] == {"catalogue": len(ids), "observed": 0,
                                        "not_run": len(ids) - 1, "error": 1}
    official = report["official"]["agents"]
    assert official["probes_blocked"] == official["controls_allowed"] == 0
    assert official["probes_attempted"] + official["controls_attempted"] == len(ids)
    assert f"coverage was 0/{len(ids)}" in (tmp_path / "deck-table.md").read_text()


def test_public_live_uses_real_worker_mode_and_denies_private_and_capture_paths(tmp_path, monkeypatch):
    private_root, output = tmp_path / "private", tmp_path / "capture"
    private_root.mkdir()
    monkeypatch.setattr(run_sealed, "is_open", lambda: False)
    monkeypatch.setattr(run_sealed.seal, "git_commit", lambda _: ("commit", False))
    monkeypatch.setattr(run_sealed, "verify_adapter", lambda paths: paths or pytest.fail("missing isolation"))
    monkeypatch.setattr(run_sealed.client, "load_api_key", lambda: "synthetic-credential")
    calls = []

    def worker(case, mode, *, dry_run, timeout, blocked):
        assert case["case_number"] == 1 and not dry_run
        assert private_root in blocked and output in blocked
        calls.append(mode)
        return rules_reference.predictions_for([case])

    monkeypatch.setattr(run_sealed, "run_case_worker", worker)
    run_sealed.main(["--public-live", "--case", "1", "--case", "1",
                     "--private-root", str(private_root), "--out", str(output)])
    assert calls == list(run_sealed.MODES)
    assert common.load_json(output / "scores.json")["mode"] == "public_live"
    assert common.load_json(output / "run.json")["input"]["commit"] == "commit"
    assert (output / "deck-table.md").read_text().startswith("# Public live adapter check")


def test_missing_provider_key_does_not_start_a_capture(tmp_path, monkeypatch, candidate_probes):
    output, lock = tmp_path / "capture", tmp_path / "capture-lock.json"

    def missing_key():
        raise run_sealed.client.MissingApiKey("credential unavailable")

    monkeypatch.setattr(run_sealed.client, "load_api_key", missing_key)
    with pytest.raises(run_sealed.client.MissingApiKey, match="credential unavailable"):
        run_sealed._capture_once(output, [canonical_case()], {"commit": "candidate"}, dry=False, timeout=15,
                                 blocked=(tmp_path,), probes=candidate_probes, lock=lock)
    assert not lock.exists() and not output.exists()


@pytest.mark.parametrize("problem", ["missing receipt", "wrong commit", "changed outcome"])
def test_frozen_capture_rejects_unbound_probes_before_lock(tmp_path, candidate_probes, problem):
    output, lock = tmp_path / "capture", tmp_path / "capture-lock.json"
    receipt = probe_receipt_path(candidate_probes)
    if problem == "missing receipt":
        receipt.unlink()
    elif problem == "wrong commit":
        common.write_json(receipt, probe_receipt("earlier", common.read_jsonl(candidate_probes)))
    else:
        rows = common.read_jsonl(candidate_probes)
        rows[0]["outcome"] = "blocked"
        common.write_jsonl(candidate_probes, rows)
    with pytest.raises(ValueError, match="probe evidence|--candidate-evidence"):
        run_sealed._capture_once(output, [canonical_case()], {"commit": "candidate"}, dry=False, timeout=15,
                                 blocked=(), probes=candidate_probes, lock=lock)
    assert not lock.exists() and not output.exists()


def test_frozen_replay_validates_retained_receipt_without_original(tmp_path, monkeypatch, candidate_probes):
    output, lock = tmp_path / "capture", tmp_path / "capture-lock.json"
    monkeypatch.setattr(run_sealed, "verify_adapter", lambda _: None)
    monkeypatch.setattr(run_sealed.client, "load_api_key", lambda: "synthetic-credential")
    monkeypatch.setattr(run_sealed, "run_case_worker",
                        lambda case, *args, **kwargs: rules_reference.predictions_for([case]))
    signature = {"commit": "candidate"}
    options = {"dry": False, "timeout": 15, "blocked": (), "probes": candidate_probes, "lock": lock}
    run_sealed._capture_once(output, [canonical_case()], signature, **options)
    retained = output / run_sealed.PROBE_RECEIPT
    assert common.load_json(retained) == common.load_json(probe_receipt_path(candidate_probes))
    candidate_probes.unlink()
    probe_receipt_path(candidate_probes).unlink()
    run_sealed._capture_once(output, [canonical_case()], signature, **options)
    retained.write_text("{}", encoding="utf-8")
    with pytest.raises(RuntimeError, match="retained capture or its inputs changed"):
        run_sealed._capture_once(output, [canonical_case()], signature, **options)


def test_broken_adapter_import_does_not_start_frozen_capture(tmp_path, monkeypatch, candidate_probes):
    output, lock = tmp_path / "capture", tmp_path / "capture-lock.json"
    monkeypatch.setattr(run_sealed.client, "load_api_key", lambda: "synthetic-credential")

    def broken_adapter(paths):
        assert paths == (tmp_path,)
        raise RuntimeError("cannot import adapter")

    monkeypatch.setattr(run_sealed, "verify_adapter", broken_adapter)
    with pytest.raises(RuntimeError, match="cannot import adapter"):
        run_sealed._capture_once(output, [canonical_case()], {"commit": "candidate"}, dry=False, timeout=15,
                                 blocked=(tmp_path,), probes=candidate_probes, lock=lock)
    assert not lock.exists() and not output.exists()


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
