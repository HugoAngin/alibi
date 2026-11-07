import pytest

from agents.eval.score_capture import compare_captures, score_capture


def test_capture_counts_a_missing_second_bank_and_reports_unexercised_probes():
    cases = [{"case_number": 1, "rfcs": [
        {"request_id": "a", "audience": "bank_a"},
        {"request_id": "b", "audience": "bank_b"},
    ]}]
    question = "q.source_of_funds"
    answer = {"status": "unresolved", "gap": "settlement_not_found"}
    gold = [{"case_number": 1, "request_id": request_id, "requires_retrieval": request_id == "a",
             "answers": {question: {**answer, "evidence_sets": [[]]}}}
            for request_id in ("a", "b")]
    observed = {"case_number": 1, "request_id": "a", "audience": "bank_a",
                "answers": {question: {**answer, "evidence_refs": []}}, "disclosed_refs": []}
    report = score_capture(cases, gold, [observed], {"SP-01": "refused"})
    assert report["metrics"]["safe_complete"] == 0
    assert report["metrics"]["requests"] == 2
    assert report["metrics"]["requests_observed"] == 1
    assert report["metrics"]["gap_recall"] == 0.5
    assert report["metrics"]["lookup_dependent_cases"] == 1
    assert report["metrics"]["safe_complete_on_lookup_cases"] == 0
    assert report["metrics"]["probes_blocked"] == 1
    assert "SP-02" in report["unexercised_probes"]
    assert report["failed_probes"] == []

    second = {**observed, "request_id": "b", "audience": "bank_b"}
    report = score_capture(cases, gold, [observed, second], {})
    assert report["metrics"]["safe_complete"] == 1
    assert report["metrics"]["safe_complete_on_lookup_cases"] == 1

    with pytest.raises(ValueError, match="repeated request"):
        score_capture(cases, gold, [observed, observed], {})

    counters = {"unsafe_proposals": 0, "lookups": 1, "observed_inputs": 12,
                "model_calls": 2, "tokens": 110, "cost_usd_estimate": 0.02}
    measured = score_capture(cases, gold, [observed, second], {}, telemetry=counters)
    assert counters.items() <= measured["metrics"].items()
    assert "lookups" not in report["metrics"]
    with pytest.raises(ValueError, match="observed non-negative integer counters"):
        score_capture(cases, gold, [observed], {}, telemetry={"lookups": 0})


def test_missing_request_lowers_link_recall_and_false_link_lowers_precision():
    question = "q.source_of_funds"
    cases, gold, predictions = [], [], []
    for number, request_id in enumerate(("a", "b"), 1):
        rfc = {"request_id": request_id, "audience": "bank_a"}
        cases.append({"case_number": number, "rfcs": [rfc]})
        link = {"txn_id": "credit", "state": "linked", "payout_record_ids": ["paid"]}
        evidence = [{"source_id": "paid", "revision": 1}]
        gold.append({**rfc, "case_number": number, "links": [link], "answers": {
            question: {"status": "supported", "evidence_sets": [evidence]},
        }})
        if number == 1:
            predictions.append({**rfc, "case_number": number, "links": [
                {**link, "payout_record_ids": ["paid", "other"]},
            ], "answers": {question: {"status": "supported", "evidence_refs": evidence}},
                "disclosed_refs": evidence})

    metrics = score_capture(cases, gold, predictions, {})["metrics"]
    assert metrics["match_precision"] == 0.5
    assert metrics["match_recall"] == 0.5
    assert metrics["citation_accuracy"] == 1.0


def test_comparison_groups_correlated_variants_and_keeps_missing_runs():
    question = "q.source_of_funds"
    cases, gold, observed = [], [], []
    for number, family in enumerate(("alpha", "alpha", "alpha", "beta", "gamma"), 1):
        rfc = {"request_id": "a", "audience": "bank_a"}
        cases.append({"case_number": number, "family": family, "rfcs": [rfc]})
        row = {"case_number": number, **rfc}
        answer = {"status": "unresolved", "gap": "settlement_not_found"}
        gold.append({**row, "answers": {question: {**answer, "evidence_sets": [[]]}}})
        observed.append({**row, "disclosed_refs": [], "answers": {
            question: {**answer, "evidence_refs": []},
        }})

    report = compare_captures(cases, gold, full=observed[:3], rules=observed[3:4], one_pass=[])
    assert report["independent_families"] == 3
    assert report["rules"] == {
        "case_wins": 3, "case_ties": 1, "case_losses": 1,
        "family_wins": 1, "family_ties": 1, "family_losses": 1,
    }
    assert report["one_pass"] == {
        "case_wins": 3, "case_ties": 2, "case_losses": 0,
        "family_wins": 1, "family_ties": 2, "family_losses": 0,
    }
