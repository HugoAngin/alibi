"""Offline adversarial checks of the published S3a LLM client and role schemas."""

import json

import pytest

from alibi.llm import BudgetExceeded, CallBudget, GeminiClient, SchemaValidationError, Unavailable
from alibi.llm.client import RawResponse
from alibi.llm.config import FLASH
from alibi.llm.prompts import load_prompt


def client_for(tmp_path, transport, *, cap=3):
    return GeminiClient(transport=transport, cache_dir=tmp_path / "cache",
                        log_path=tmp_path / "audit.jsonl", budget=CallBudget(cap),
                        sleep=lambda _: None)


def challenger_answer(**extra_args):
    return {
        "claim_id": "c1", "proposed_status": "unresolved", "bank_settlement_established": False,
        "payout_reference": "PO-0002-01", "confirming_refs": [], "conflicting_refs": [],
        "missing_evidence": ["Bank settlement record"],
        "next_action": {"type": "lookup_settlement",
                        "args": {"payout_reference": "PO-0002-01", "merchant_id": "mock-marketplace",
                                 **extra_args},
                        "reason": "Check whether the payout settled."},
        "trace": [], "rationale": "The visible message describes a release to a platform balance.",
    }


def policy_answer(*, params=None, confidence=0.98):
    return {"rules": [{"rule_id": "r1", "kind": "deny", "target": "category", "value": "medical",
                       "params": {} if params is None else params, "source_text": "Do not share medical items.",
                       "confidence": confidence}], "clarifications": [],
            "plain_summary": "Medical items will be withheld."}


def generate(client, role, *, budget=None):
    prompt = load_prompt(role)
    return client.generate(FLASH, prompt.system, '{"subject_ref":"maya-synthetic"}', prompt.schema, budget)


@pytest.mark.parametrize(
    ("role", "answer"),
    [
        pytest.param("challenger", challenger_answer(subject_ref="other-synthetic-holder"),
                     id="challenger-foreign-selector"),
        pytest.param("policy_parse", policy_answer(params={"allow_auto_share": True}),
                     id="policy-undeclared-parameter"),
        pytest.param("policy_parse", policy_answer(confidence=float("nan")), id="policy-nonfinite-confidence"),
    ],
)
def test_s3a_rejects_untyped_role_output(tmp_path, role, answer):
    client = client_for(tmp_path, lambda *_: RawResponse(text=json.dumps(answer)))
    with pytest.raises(SchemaValidationError):
        generate(client, role)


@pytest.mark.parametrize(("process_cap", "task_cap"), [(1, 3), (3, 1)], ids=["process", "task"])
def test_s3a_retries_respect_each_transport_attempt_budget(tmp_path, process_cap, task_cap):
    calls = []

    class Busy(Exception):
        code = 503

    def transport(*_):
        calls.append(1)
        raise Busy()

    client = client_for(tmp_path, transport, cap=process_cap)
    with pytest.raises((BudgetExceeded, Unavailable)):
        generate(client, "challenger", budget=CallBudget(task_cap))
    assert len(calls) == 1


def test_s3a_does_not_replay_schema_invalid_cached_challenger_output(tmp_path):
    answer = challenger_answer()
    client = client_for(tmp_path, lambda *_: RawResponse(text=json.dumps(answer)))
    initial = generate(client, "challenger")
    assert initial.data == answer and not initial.cached
    cache_path = client.cache_dir / f"{initial.prompt_sha256}.json"
    cache_entry = json.loads(cache_path.read_text(encoding="utf-8"))
    cache_entry["data"]["next_action"]["type"] = "dump_passport"
    cache_path.write_text(json.dumps(cache_entry), encoding="utf-8")

    try:
        replay = generate(client, "challenger")
    except SchemaValidationError:
        return  # Rejecting the cache entry also keeps the invalid action out of the result.
    assert replay.data == answer


def test_s3a_live_parse_rejects_unknown_action_without_echoing_it_in_audit(tmp_path):
    answer = challenger_answer()
    answer["next_action"]["type"] = "dump_passport_for_other_synthetic_holder"
    client = client_for(tmp_path, lambda *_: RawResponse(text=json.dumps(answer)))
    with pytest.raises(SchemaValidationError):
        generate(client, "challenger")
    audit = client.log_path.read_text(encoding="utf-8")
    assert json.loads(audit)["outcome"] == "schema_invalid"
    assert "dump_passport_for_other_synthetic_holder" not in audit
    assert "maya-synthetic" not in audit
