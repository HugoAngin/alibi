"""Synthetic capture summaries: incomplete usage cannot become a zero-cost total."""

import json

from agents.eval.capture_reporting import summarize_capture
from alibi.eval.scorer import System


def system(name="agents", cases=(1,)):
    return System(name, None, {}, {(case, "rfc"): {"claims": {}, "package_claims": []}
                                  for case in cases}, {})


def worker(root, case, *, mode="agents", cost=0.1, failed=False, empty=False, uncertain=False):
    path = root / "capture-workers" / f"{mode}-{case}"
    path.mkdir(parents=True)
    (path / "metadata.json").write_text(json.dumps({"system": mode, "case_number": case,
                                                  "failures": ["timeout"] if failed else []}))
    (path / "llm_log.jsonl").write_text("" if empty else "{}\n")
    (path / "usage.json").write_text(json.dumps({"totals": {
        "model_calls": 0 if empty else 1, "tokens": 0 if empty else 10,
        "cost_usd_estimate": cost, "priced_rows_cost_usd_estimate": 0.04,
        "rows_with_potential_unmeasured_attempts": int(uncertain)}}))
    return path


def test_complete_then_missing_worker_has_only_partial_totals(tmp_path):
    worker(tmp_path, 1)
    result = summarize_capture(tmp_path, [system()])["usage_by_system"]["agents"]
    assert result["complete"] and result["totals"]["cost_usd_estimate"] == 0.1
    result = summarize_capture(tmp_path, [system(cases=(1, 2))])["usage_by_system"]["agents"]
    assert not result["complete"] and result["totals"]["cost_usd_estimate"] is None
    assert result["partial_known_totals"]["cost_usd_estimate"] == 0.1
    assert result["absent_logs"] == result["unknown_workers"] == 1


def test_timeout_and_retry_keep_partial_priced_usage(tmp_path):
    path = worker(tmp_path, 1, failed=True)
    usage = json.loads((path / "usage.json").read_text())
    usage["partial_totals"] = usage["totals"]
    usage["totals"] = {key: None for key in ("model_calls", "tokens", "cost_usd_estimate")}
    usage["usage_error"] = "worker did not complete"
    (path / "usage.json").write_text(json.dumps(usage))
    worker(tmp_path, 2, uncertain=True, cost=None)
    result = summarize_capture(tmp_path, [system(cases=(1, 2))])["usage_by_system"]["agents"]
    assert result["unknown_workers"] == 2 and result["failed_workers"] == 1
    assert result["totals"] == {"model_calls": None, "tokens": None, "cost_usd_estimate": None}
    assert round(result["partial_known_totals"]["cost_usd_estimate"], 4) == 0.14


def test_only_successful_rules_can_have_complete_empty_log(tmp_path):
    worker(tmp_path, 1, mode="rules", cost=0, empty=True)
    worker(tmp_path, 1, cost=0, empty=True)
    result = summarize_capture(tmp_path, [system("rules"), system()])["usage_by_system"]
    assert result["rules"]["complete"] and result["rules"]["totals"]["tokens"] == 0
    assert not result["agents"]["complete"]


def test_wrong_case_usage_is_not_a_known_partial_total(tmp_path):
    path = worker(tmp_path, 1)
    usage = json.loads((path / "usage.json").read_text())
    usage["by_case"] = [{"case_number": 2, "log_rows": 1}]
    (path / "usage.json").write_text(json.dumps(usage))
    result = summarize_capture(tmp_path, [system()])["usage_by_system"]["agents"]
    assert result["scope_mismatches"] == 1 and not result["complete"]
    assert result["partial_known_totals"]["cost_usd_estimate"] is None


def test_policy_divergence_counts_rfcs_and_missing_facts_are_unknown(tmp_path):
    candidate = system()
    candidate.predictions[(1, "rfc")] = {
        "claims": {"q": {"status": "supported", "facts": {"amount": 1}, "evidence_refs": ["a", "b"]}},
        "package_claims": [{"question_id": "q", "status": "unresolved", "evidence_refs": ["b", "a"],
                            "changed": "the calculation would show fields the policy withholds"}]}
    result = summarize_capture(tmp_path, [candidate])["claim_divergence_by_system"]["agents"]
    assert result["differing_rfcs"] == result["policy_filtering_rfcs"] == 1
    assert result["details"][0]["differing_fields"] == ["status"]
    assert result["details"][0]["unknown_fields"] == ["facts"]
    candidate.predictions[(1, "rfc")]["package_claims"][0].update(facts={"amount": 2}, changed=None)
    result = summarize_capture(tmp_path, [candidate])["claim_divergence_by_system"]["agents"]
    assert result["details"][0]["differing_fields"] == ["facts", "status"]
    assert result["unknown_reason_rfcs"] == 1
