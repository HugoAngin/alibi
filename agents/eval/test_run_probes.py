"""Check the bridge from raw eval observations to official scorer outcomes."""

import pytest

from agents.eval import run_probes
from agents.eval.probe_capture import probe_receipt_path
from alibi.eval import common
from alibi.eval.scorer import load_probe_results, probe_metrics


def test_runner_preserves_control_polarity_and_unrun_catalogue(monkeypatch, tmp_path):
    seen = []

    def observe(probe_id, base_urls, tokens):
        seen.append(probe_id)
        assert tokens == {"holder": "synthetic-private-token"}
        assert base_urls["routes"] == {"common": "/common", "override": "/override"}
        if probe_id == "SP-02":
            raise RuntimeError("synthetic-private-token")
        return {"outcome": "not_blocked" if probe_id == "SP-32" else "blocked", "exchanges": [
            {"method": "POST", "status": 200, "response_body": "synthetic-private-token"},
        ]}

    monkeypatch.setattr(run_probes, "attempt", observe)
    selected = ["SP-01", "SP-02", "SP-30", "SP-32", "SP-47"]
    config = {"base_urls": {"routes": {"common": "/common"}}, "tokens": {"holder": "synthetic-private-token"},
              "probes": {probe_id: {"base_urls": {"routes": {"override": "/override"}}} for probe_id in selected}}
    harness = run_probes.build_probe_harness(config, selected)
    results = harness.run()
    assert len(results) == 47 and seen == selected[:-1]

    path = tmp_path / "probes.jsonl"
    assert harness.write_results(results, path) == 47
    assert "synthetic-private-token" not in path.read_text()
    assert "POST HTTP 200" in path.read_text()
    outcomes = load_probe_results(path)
    assert outcomes == {"SP-01": "refused", "SP-30": "accepted", "SP-32": "refused"}
    metrics = probe_metrics(outcomes)
    assert (metrics["probes_blocked"], metrics["probes_attempted"]) == (1, 45)
    assert (metrics["controls_allowed"], metrics["controls_attempted"]) == (1, 2)
    assert harness.summary(results)["errors"] == ["SP-02"]
    assert len(harness.summary(results)["not_run"]) == 43


def test_unconfigured_runner_is_all_not_run():
    harness = run_probes.build_probe_harness({})
    results = harness.run()
    assert len(results) == 47
    assert all(result.outcome == "not_run" and result.detail for result in results)


def test_candidate_capture_records_catalogue_commit_and_digest(tmp_path, monkeypatch):
    config, path = tmp_path / "config.json", tmp_path / "probes.jsonl"
    common.write_json(config, {})
    monkeypatch.setattr(run_probes.seal, "git_commit", lambda _: ("candidate", False))
    monkeypatch.setattr(run_probes, "attempt", lambda *_: {"outcome": "blocked", "exchanges": []})
    args = ["--config", str(config), "--results", str(path), "--candidate-evidence"]
    run_probes.main(args)
    rows = common.read_jsonl(path)
    assert len(rows) == 47
    assert load_probe_results(path)["SP-01"] == "refused"
    assert common.load_json(probe_receipt_path(path)) == {
        "candidate_commit": "candidate", "outcomes_sha256": common.canonical_sha256(rows),
    }
    with pytest.raises(SystemExit):
        run_probes.main(args)
    with pytest.raises(SystemExit):
        run_probes.main(args[:-1])


def test_candidate_capture_does_not_publish_after_checkout_moves(tmp_path, monkeypatch):
    config, path = tmp_path / "config.json", tmp_path / "probes.jsonl"
    common.write_json(config, {})
    revisions = iter([("candidate", False), ("different", False)])
    monkeypatch.setattr(run_probes.seal, "git_commit", lambda _: next(revisions))
    with pytest.raises(SystemExit):
        run_probes.main(["--config", str(config), "--results", str(path), "--candidate-evidence"])
    assert not path.exists() and not probe_receipt_path(path).exists()
