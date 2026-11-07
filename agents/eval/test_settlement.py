import pytest

from agents.data.api import load_cases
from agents.eval.api import fixed_candidate_pool, match_credit, period_totals, reconcile_credit, request_context


@pytest.mark.parametrize("case", [case for case in load_cases() if case["case_number"] <= 40],
                         ids=lambda case: str(case["case_number"]))
def test_public_matching_and_reconciliation(case):
    request = case["rfcs"][0]
    view, lookup = request_context(case, request["request_id"])
    bank, sources, telemetry = fixed_candidate_pool(view, lookup)
    credit = bank[case["holder_claim"]["txn_id"]]
    match = match_credit(credit, sources)
    gold = case["gold"]

    assert {key: match[key] for key in gold["links"][0]} == gold["links"][0]
    breakdown = reconcile_credit(credit, match, sources, case["holder_claim"])
    assert breakdown["breakdown_minor"] == gold["breakdown_minor"]
    assert breakdown["status"] == gold["claim_status"]
    if match["state"] == "linked":
        for ref in match["evidence_refs"]:
            kind, _, identity = ref["source_id"].partition(":")
            if kind == "import":
                assert sources[identity]["source_kind"] == "import"
                assert gold["source_trust"][ref["source_id"]] == "self_supplied"
    if gold["requires_retrieval"]:
        assert telemetry["lookups"] > 0

    for request in case["rfcs"][1:]:
        if "q.sales_refunds_in_period" in request["question_ids"]:
            view, lookup = request_context(case, request["request_id"])
            _, sources, telemetry = fixed_candidate_pool(view, lookup)
            totals = period_totals(request["period"], sources)
            expected = gold["answers"][request["request_id"]]["q.sales_refunds_in_period"]
            assert totals["facts"] == expected["facts"]
            assert telemetry["lookups"] == 0


@pytest.mark.parametrize("case", load_cases("hidden_reference"), ids=lambda case: str(case["case_number"]))
def test_hidden_reference_matching_after_scoped_retrieval(case):
    view, lookup = request_context(case, case["rfcs"][0]["request_id"])
    bank, sources, _ = fixed_candidate_pool(view, lookup)
    for query in case["gold"]["retrieval_path"]:
        for event in lookup(**query):
            current = sources.get(event["record_id"])
            if current is None or event["revision"] > current["revision"]:
                sources[event["record_id"]] = event
    [expected] = case["gold"]["links"]
    link = match_credit(bank[expected["txn_id"]], sources)
    assert {key: link[key] for key in expected} == expected
