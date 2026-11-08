"""Public replay must preserve captured evidence and never launch a worker."""

from hashlib import sha256

from agents.eval import public_rehearsal_report as report, run_sealed


def test_public_replay_is_read_only_and_reproduces_capture(monkeypatch):
    def must_not_run(*args, **kwargs):
        raise AssertionError("public report attempted to launch a candidate worker")

    monkeypatch.setattr(run_sealed, "run_case_worker", must_not_run)
    def snapshot():
        return {str(path): sha256(path.read_bytes()).hexdigest()
                for root in (report.CAPTURE, report.PARALLEL_CAPTURE, report.CANDIDATE3_CAPTURE)
                for path in root.rglob("*") if path.is_file()}

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
    assert [metrics[f"parallel_{key}"] for key in
            ("safe_complete", "one_pass_safe_complete", "rules_safe_complete")] == [0, 0, 1]
    assert metrics["parallel_prediction_schema_rows"] == 12
    assert metrics["parallel_prediction_schema_invalid_rows"] == 0
    assert metrics["parallel_rfc_invalid_output_count"] == 2
    assert metrics["parallel_rfc_timeout_count"] == 0
    assert metrics["parallel_scheduler_completed"] == 6
    assert metrics["parallel_scheduler_errors"] == 0
    assert metrics["parallel_scheduler_initial_workers"] == metrics["parallel_scheduler_final_workers"] == 4
    assert [metrics[f"candidate3_{key}"] for key in
            ("safe_complete", "one_pass_safe_complete", "rules_safe_complete")] == [1, 0, 1]
    assert all(metrics[f"candidate3_second_scorer_{mode}_safe_complete"] == 0
               for mode in ("agents", "one_pass", "rules"))
    assert metrics["candidate3_prediction_schema_rows"] == 12
    assert metrics["candidate3_prediction_schema_invalid_rows"] == metrics["candidate3_rfc_failures"] == 0
    assert metrics["candidate3_rfc_timeout_count"] == 0
    assert metrics["candidate3_retained_usage_agents_tokens"] == 108460
    assert metrics["candidate3_retained_usage_agents_unknown_workers"] == 0
    assert metrics["candidate3_claim_divergence_agents_differing_rfcs"] == 0
    assert metrics["candidate3_claim_divergence_agents_unknown_comparison_rfcs"] == 4
