"""Check independent draft inputs, shape and provider call accounting."""

import json
from types import SimpleNamespace

import pytest

from agents.data.api import canonical_case, load_case, without_gold
from agents.eval.api import predict_baseline, propose_baseline
from alibi.eval.scorer import build_system
from alibi.llm.client import CallBudget, RawResponse
from alibi.llm.config import FLASH


def test_rules_public_control_has_both_requests_and_no_model_calls():
    case = without_gold(canonical_case())
    [bank_a, bank_b] = [request["request_id"] for request in case["rfcs"]]
    first, second = predict_baseline(case, "rules")
    assert first["system"] == "rules" and first["case_number"] == 1
    assert [first["request_id"], second["request_id"]] == [bank_a, bank_b]
    assert first["cost"]["model_calls"] == first["cost"]["tokens"] == 0
    breakdown = first["proposed_package"]["claims"]["q.breakdown"]
    assert breakdown["facts"]["breakdown_minor"] == {"sales": 240000, "refunds": 18000, "fees": 6000, "net": 216000}
    assert first["links"] == [{"txn_id": "txn-0001-004", "state": "linked", "payout_record_ids": ["rec-0001-021"]}]
    assert first["retrieval"]["lookups"] > 0 and second["retrieval"]["lookups"] == 0
    source = first["proposed_package"]["claims"]["q.source_of_funds"]
    assert source["evidence_refs"] == ["bank:txn-0001-004", "merchant:rec-0001-021"]
    assert first["claims"] == {} and first["package"] is None
    assert first["failure"]["type"] == "unavailable"
    assert not build_system("rules", [first, second]).invalid


def test_pending_payout_has_no_supporting_breakdown_or_citations():
    first, *_ = predict_baseline(without_gold(load_case(2)), "rules")
    claims = first["proposed_package"]["claims"]
    assert claims["q.breakdown"]["status"] == "unresolved"
    assert claims["q.breakdown"]["gap"] == "settlement_pending_at_booking"
    assert claims["q.breakdown"]["facts"] == {} and claims["q.breakdown"]["evidence_refs"] == []


def test_second_platform_rules_maps_deliveries_and_lookup_only_statement():
    case = without_gold(load_case(73))
    [row] = predict_baseline(case, "rules")
    assert row["links"] == [{"txn_id": "txn-0073-004", "state": "linked",
                             "payout_record_ids": ["rec-0073-013"]}]
    claims = row["proposed_package"]["claims"]
    assert "merchant:rec-0073-013" in claims["q.source_of_funds"]["evidence_refs"]
    assert claims["q.breakdown"]["facts"]["breakdown_minor"]["net"] > 0
    assert row["retrieval"]["lookups"] > 0

    case["merchant_lookup"][0]["available_from"] = "2100-01-01T00:00:00Z"
    [before_lookup] = predict_baseline(case, "rules")
    assert before_lookup["links"][0]["state"] == "unmatched"


def test_one_pass_uses_same_fixed_evidence_without_future_or_gold(tmp_path):
    case = without_gold(canonical_case())
    request = case["rfcs"][0]
    request_id = request["request_id"]
    case["title"] = "evaluator-only-label"
    case["merchant_deliveries"].append({"received_at": "2100-01-01T00:00:00Z",
                                       "event": {"record_id": "future-record"}})
    [txn_id] = request["txn_ids"]
    active = next(entry for entry in case["bank_deliveries"] if entry["row"]["txn_id"] == txn_id)
    case["bank_deliveries"].append({**active, "row": {**active["row"], "revision": 2}})
    rules = propose_baseline(case, request_id, "rules")
    source = rules["pre_policy_proposal"]["answers"]["q.source_of_funds"]
    assert {"source_id": f"bank:{txn_id}", "revision": 2} in source["evidence_refs"]
    calls = []

    def transport(model, system, user, schema, thinking):
        evidence = json.loads(user)
        calls.append(evidence)
        assert model == FLASH and thinking == "low"
        assert "untrusted data" in system
        assert "future-record" not in user and "evaluator-only-label" not in user
        assert "gold" not in evidence and "merchant_lookup" not in evidence
        assert evidence["rfc"]["request_id"] == request_id
        assert set(evidence["bank"]) == set(request["txn_ids"])
        assert evidence["bank"][txn_id]["revision"] == 2
        assert "rec-0001-021" in evidence["merchant"]
        assert schema["additionalProperties"] is False
        return RawResponse(json.dumps(rules["pre_policy_proposal"]), SimpleNamespace(total_token_count=37))

    result = propose_baseline(case, request_id, "one_pass", transport=transport,
                              log_path=tmp_path / "log.jsonl", budget=CallBudget(2))
    assert len(calls) == result["model_calls"] == 1 and result["tokens"] == 37
    assert result["failure"] is None and result["pre_policy_proposal"] == rules["pre_policy_proposal"]
    assert (result["lookups"], result["observed_inputs"]) == (rules["lookups"], rules["observed_inputs"])


def test_one_pass_does_not_retry_provider_503(tmp_path):
    class RetryableError(Exception):
        code = 503

    case = without_gold(canonical_case())
    calls = []

    def transport(*args):
        calls.append(args)
        raise RetryableError

    result = propose_baseline(case, case["rfcs"][0]["request_id"], "one_pass", transport=transport,
                              log_path=tmp_path / "log.jsonl", budget=CallBudget(2))
    assert len(calls) == result["model_calls"] == 1
    assert result["failure"] == "source_unavailable"
    assert result["tokens"] is None and result["pre_policy_proposal"] is None


def test_failed_prediction_preserves_each_request_and_unknown_tokens(tmp_path):
    case = without_gold(canonical_case())
    rows = predict_baseline(case, "one_pass", transport=lambda *_: RawResponse("{}"),
                            log_path=tmp_path / "log.jsonl", budget=CallBudget(2))
    requests = {rfc["request_id"] for rfc in case["rfcs"]}
    assert {row["request_id"] for row in rows} == requests
    for row in rows:
        assert row["claims"] == {} and row["failure"]["type"] == "invalid_output"
        assert row["cost"]["model_calls"] == 1 and "tokens" not in row["cost"]
        assert not row["usage_measured"]
    assert not build_system("one_pass", rows).invalid


@pytest.mark.parametrize("wrong_recipient", [False, True])
def test_shared_gate_supplies_final_claims_and_must_keep_original_recipient(wrong_recipient):
    case = without_gold(canonical_case())
    calls = []

    def gate(evidence, draft):
        rfc = evidence["rfc"]
        calls.append(rfc["request_id"])
        claims = {qid: {**answer, "evidence_refs": [ref["source_id"] for ref in answer["evidence_refs"]]}
                  for qid, answer in draft["answers"].items()}
        draft["answers"].clear()
        return {"claims": claims, "package": {"request_id": rfc["request_id"],
                                               "audience": "other" if wrong_recipient else rfc["audience"],
                                               "attachments": []}}

    rows = predict_baseline(case, "rules", gate=gate)
    assert calls == [rfc["request_id"] for rfc in case["rfcs"]]
    assert all(row["proposed_package"]["claims"] for row in rows)
    assert not build_system("rules", rows).invalid
    if wrong_recipient:
        assert all(row["package"] is None and row["failure"]["type"] == "invalid_output" for row in rows)
    else:
        assert all(row["package"] and row["failure"] is None for row in rows)


def test_inputs_with_gold_or_unauthorized_sealed_numbers_are_rejected(monkeypatch):
    case = canonical_case()
    with pytest.raises(ValueError, match="without gold"):
        predict_baseline(case, "rules")
    sealed = {**without_gold(case), "case_number": 1000}
    monkeypatch.setattr("agents.eval.baselines.is_open", lambda: False)
    with pytest.raises(PermissionError, match="lockbox run"):
        predict_baseline(sealed, "rules")
