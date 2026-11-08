"""Public replay must preserve captured evidence and never launch a worker."""

from hashlib import sha256

from agents.eval import public_rehearsal_report as report, run_sealed


def test_public_replay_is_read_only_and_reproduces_capture(monkeypatch):
    def must_not_run(*args, **kwargs):
        raise AssertionError("public report attempted to launch a candidate worker")

    monkeypatch.setattr(run_sealed, "run_case_worker", must_not_run)
    def snapshot():
        return {str(path.relative_to(report.CAPTURE)): sha256(path.read_bytes()).hexdigest()
                for path in report.CAPTURE.rglob("*") if path.is_file()}

    before = snapshot()
    metrics = report.replay()
    assert snapshot() == before
    assert [metrics[key] for key in ("safe_complete", "one_pass_safe_complete", "rules_safe_complete")] == [0, 0, 1]
    assert metrics["prediction_schema_rows"] == 12
    assert metrics["prediction_schema_invalid_rows"] == 0
    assert metrics["policy_control_package_amount_pence_fields"] == 30
    assert metrics["policy_restriction_package_amount_pence_fields"] == 0
    assert metrics["policy_restriction_claim_status_divergences"] == 3
    assert metrics["policy_restriction_rfcs_with_claim_status_divergences"] == 2
    assert metrics["retained_usage_cost_usd_estimate_total"] == 0.281259
