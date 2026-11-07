"""The probe harness: every catalogue probe is registered and not run; attempts map to blocked
or not blocked; the summary comes from agents.execution.api.summarise; results feed the scorer."""
from __future__ import annotations

import pytest

from agents.execution.api import load_probes
from alibi.eval import probes, scorer
from alibi.eval.probes import Harness, ProbeResult, Target


def test_every_probe_is_registered_and_not_run():
    harness = Harness()
    catalogue = load_probes()
    assert set(harness.probes) == {p["id"] for p in catalogue} and len(catalogue) == 47
    assert harness.registered() == []
    results = harness.run()
    assert len(results) == 47 and {r.outcome for r in results} == {"not_run"}
    assert all(r.passed is None for r in results)
    summary = harness.summary(results)
    assert summary["probes_attempted"] == 45 and summary["probes_blocked"] == 0
    assert summary["controls_attempted"] == 2 and summary["controls_allowed"] == 0
    assert len(summary["not_run"]) == 47 and summary["errors"] == []
    assert len(summary["failed"]) == 47 and "SP-01" in summary["core_failed"]


def test_attempts_map_to_outcomes():
    harness = Harness(Target(base_url="http://127.0.0.1:1"))
    seen: list[str] = []

    @harness.register("SP-01")
    def no_approval(target: Target) -> bool:
        seen.append(target.base_url)
        return True

    @harness.register("SP-30")
    def self_supplied_control(target: Target) -> bool:
        return False

    @harness.register("SP-02")
    def edited_body(target: Target) -> ProbeResult:
        return ProbeResult("x", "blocked", "approval_digest_mismatch",
                           audit=["dispatch refused approval_id=apr-1"])

    @harness.register("SP-03")
    def broken(target: Target) -> bool:
        raise RuntimeError("connection refused")

    @harness.register("SP-04")
    def wrong_type(target: Target):
        return "maybe"

    assert harness.registered() == ["SP-01", "SP-02", "SP-03", "SP-04", "SP-30"]
    results = {r.probe_id: r for r in harness.run()}
    assert seen == ["http://127.0.0.1:1"]
    assert results["SP-01"].outcome == "blocked" and results["SP-01"].passed is True
    assert results["SP-30"].outcome == "not_blocked" and results["SP-30"].passed is True
    assert results["SP-02"].outcome == "blocked" and results["SP-02"].probe_id == "SP-02"
    assert results["SP-02"].kind == "refusal" and results["SP-02"].audit == ["dispatch refused approval_id=apr-1"]
    assert results["SP-03"].outcome == "error" and "RuntimeError" in results["SP-03"].detail
    assert results["SP-04"].outcome == "error" and results["SP-04"].passed is None
    assert results["SP-05"].outcome == "not_run"
    outcomes = harness.outcomes(results.values())
    assert outcomes == {"SP-01": "refused", "SP-02": "refused", "SP-30": "accepted"}
    summary = harness.summary(list(results.values()))
    assert summary["probes_blocked"] == 2 and summary["probes_attempted"] == 45
    assert summary["controls_allowed"] == 1 and summary["controls_attempted"] == 2
    assert summary["errors"] == ["SP-03", "SP-04"] and "SP-03" in summary["failed"]
    assert "SP-01" not in summary["failed"] and "SP-30" not in summary["failed"]


def test_control_probe_that_is_blocked_fails():
    harness = Harness()
    harness.register("SP-32")(lambda target: True)
    results = harness.run(["SP-32"])
    assert results[0].passed is False
    assert "SP-32" in harness.summary(results)["failed"]


def test_registry_guards():
    harness = Harness()
    with pytest.raises(KeyError):
        harness.register("SP-99")
    harness.register("SP-01")(lambda target: True)
    with pytest.raises(ValueError):
        harness.register("SP-01")(lambda target: False)
    with pytest.raises(KeyError):
        harness.run(["SP-98"])
    with pytest.raises(ValueError):
        ProbeResult("SP-01", "maybe")


def test_results_file_feeds_the_scorer(tmp_path):
    harness = Harness()
    harness.register("SP-01")(lambda target: True)
    harness.register("SP-30")(lambda target: False)
    harness.register("SP-18")(lambda target: False)
    results = harness.run()
    path = tmp_path / "probes.jsonl"
    assert harness.write_results(results, path) == 47
    outcomes = scorer.load_probe_results(path)
    assert outcomes == {"SP-01": "refused", "SP-30": "accepted", "SP-18": "accepted"}
    metrics = scorer.probe_metrics(outcomes)
    assert metrics["probes_blocked"] == 1 and metrics["probes_attempted"] == 45
    assert metrics["controls_allowed"] == 1 and "SP-18" in metrics["failed"]
    table = harness.table(results)
    assert "| SP-18 | refusal | yes | not_blocked | no |" in table


def test_cli_lists_without_running(capsys):
    calls: list[str] = []
    probes.HARNESS.attempts["SP-47"] = lambda target: calls.append("ran") or True
    try:
        assert probes.main(["--list"]) == 0
    finally:
        probes.HARNESS.attempts["SP-47"] = None
    out = capsys.readouterr().out
    assert calls == [] and "registered attempts: 1/47" in out
    assert "| SP-47 | refusal |  | not_run |  | attempt registered |" in out
    assert "probes_blocked 0 / probes_attempted 45" in out
