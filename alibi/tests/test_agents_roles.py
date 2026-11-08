"""Role wrappers: prompt in, validated typed output out, typed RoleError on every failure.

No network: every call goes through ``alibi.llm.fake.FakeClient``.
"""

from __future__ import annotations

import json

import pytest

from alibi.agents import roles
from alibi.agents.material import Material, challenger_input, curator_input, matcher_input
from alibi.agents.models import ChallengerOutput, CuratorOutput, MatcherOutput
from alibi.agents.roles import RoleError
from alibi.api import load_fixture, open_store
from alibi.core.schemas import RecordKind
from alibi.llm import client as llm
from alibi.llm.config import FLASH, FRONTIER
from alibi.llm.fake import FakeClient

CHALLENGER = {
    "claim_id": "c1", "proposed_status": "unresolved", "bank_settlement_established": False,
    "payout_reference": "PO-5001",
    "confirming_refs": [{"record_id": "po_5001", "revision": 1, "why": "reference"}],
    "conflicting_refs": [], "missing_evidence": ["settlement"],
    "next_action": {"type": "lookup_settlement", "args": {"payout_reference": "PO-5001"}, "reason": "confirm"},
    "trace": ["checked po_5001 rev 1"], "rationale": "released to balance only",
}


@pytest.fixture
def fx(tmp_path, keystore):
    store = open_store(tmp_path / "p.db")
    fixture = load_fixture(store, "supported", keystore=keystore)
    material = Material(fixture["subject_ref"])
    for record in store.list_sources(fixture["subject_ref"]):
        material.add_record(record)
    txn = store.get_bank_txn("txn_9001")
    material.add_txn(txn)
    yield {"store": store, "material": material, "txn": txn, "rfc": store.get_request("rfc_a_001")}
    store.close()


def challenger_input_for(fx, claim=None):
    m = fx["material"]
    return challenger_input(m, claim or {"claim_id": "c1", "status": "unresolved"}, [fx["txn"]],
                            [r for r in m.records() if r.kind != RecordKind.SETTLEMENT],
                            m.records(kinds=[RecordKind.SETTLEMENT]), round_index=0, actions_left=3)


def test_challenger_runs_on_frontier_and_returns_typed_output(fx):
    client = FakeClient({"You are the Challenger": CHALLENGER})
    rr = roles.run_challenger(client, fx["material"], challenger_input_for(fx))
    assert isinstance(rr.output, ChallengerOutput)
    assert rr.model_id == FRONTIER and client.calls[0].model == FRONTIER
    assert rr.failures == [] and rr.undeclared == [] and rr.distrusted is False
    assert rr.output.next_action.type == "lookup_settlement"
    assert rr.raw["payout_reference"] == "PO-5001" and rr.result.cached is False and rr.tokens == 20
    user = json.loads(client.calls[0].user)
    assert user["gateway"]["actions_left"] == 3 and user["bank"][0]["record_id"] == "txn_9001"


def test_matcher_runs_on_flash_and_model_can_be_overridden(fx):
    m, txn = fx["material"], fx["txn"]
    answer = {"entry": {"record_id": "txn_9001", "revision": 1}, "candidates": [], "decision": "ambiguous",
              "chosen_record_id": "", "chosen_revision": 0, "rationale": "two fit"}
    client = FakeClient({"You are the Matcher": answer})
    payouts = m.records(kinds=[RecordKind.PAYOUT])
    rr = roles.run_matcher(client, m, matcher_input(m, txn, payouts, [], rule="rule"))
    assert isinstance(rr.output, MatcherOutput) and rr.model_id == FLASH
    rr2 = roles.run_matcher(client, m, matcher_input(m, txn, payouts, [], rule="rule"), model="other-model")
    assert rr2.model_id == "other-model" and client.calls[-1].model == "other-model"


def test_unknown_references_are_dropped_and_reported(fx):
    answer = dict(CHALLENGER, confirming_refs=[{"record_id": "po_9999", "revision": 1, "why": "x"},
                                               {"record_id": "po_5001", "revision": 1, "why": "y"}],
                  next_action={"type": "lookup_settlement", "args": {"payout_reference": "PO-9999"}, "reason": "r"},
                  payout_reference="PO-9999")
    rr = roles.run_challenger(FakeClient({"*": answer}), fx["material"], challenger_input_for(fx))
    assert [r.record_id for r in rr.output.confirming_refs] == ["po_5001"]
    assert rr.output.next_action.type == "none" and rr.output.payout_reference == ""
    paths = {f.path for f in rr.failures}
    assert paths == {"confirming_refs[0]", "payout_reference", "next_action"}
    assert all(f.role == "challenger" for f in rr.failures)


def test_undeclared_fields_are_reported_not_used(fx):
    answer = dict(CHALLENGER, verdict="trust me")
    rr = roles.run_challenger(FakeClient({"*": answer}, validate=False), fx["material"],
                             challenger_input_for(fx))   # the closed schema stops it at a validating client
    assert rr.undeclared == ["verdict"] and not hasattr(rr.output, "verdict")


@pytest.mark.parametrize("exc, kind", [
    (llm.Unavailable("outage"), "unavailable"),
    (llm.BudgetExceeded("cap"), "budget_exceeded"),
])
def test_llm_errors_become_typed_role_errors(fx, exc, kind):
    with pytest.raises(RoleError) as info:
        roles.run_challenger(FakeClient({"*": exc}), fx["material"], challenger_input_for(fx))
    assert info.value.kind == kind and info.value.role == "challenger"
    assert info.value.outcome == kind


def test_schema_failures_become_role_errors(fx):
    bad = dict(CHALLENGER, proposed_status="maybe")
    with pytest.raises(RoleError) as info:
        roles.run_challenger(FakeClient({"*": bad}), fx["material"], challenger_input_for(fx))
    assert info.value.kind == "schema" and info.value.outcome == "error"
    # the client's own validation skipped: the pydantic model still refuses
    with pytest.raises(RoleError) as info2:
        roles.run_challenger(FakeClient({"*": bad}, validate=False), fx["material"], challenger_input_for(fx))
    assert info2.value.kind == "schema"
    with pytest.raises(RoleError) as info3:
        roles.run_challenger(FakeClient({"*": ["not", "an", "object"]}, validate=False), fx["material"],
                             challenger_input_for(fx))
    assert info3.value.kind == "schema"


def test_task_budget_is_passed_to_the_client(fx):
    budget = llm.CallBudget(1, name="test")
    client = FakeClient({"*": CHALLENGER})
    roles.run_challenger(client, fx["material"], challenger_input_for(fx), budget=budget)
    with pytest.raises(RoleError) as info:
        roles.run_challenger(client, fx["material"], challenger_input_for(fx), budget=budget)
    assert info.value.kind == "budget_exceeded"


def test_second_opinion_marks_the_result_distrusted(fx):
    seen = []

    def opinion(role, input_dict, output):
        seen.append((role, type(output).__name__))
        return False, "the settlement is not in the material"

    rr = roles.run_challenger(FakeClient({"*": CHALLENGER}), fx["material"], challenger_input_for(fx),
                              second_opinion=opinion)
    assert rr.distrusted is True and "settlement" in rr.opinion_note
    assert seen == [("challenger", "ChallengerOutput")]

    agree = roles.run_challenger(FakeClient({"*": CHALLENGER}), fx["material"], challenger_input_for(fx),
                                 second_opinion=lambda *_: (True, "fine"))
    assert agree.distrusted is False and agree.opinion_note == "fine"

    def broken(role, input_dict, output):
        raise RuntimeError("gemma down")

    tolerant = roles.run_challenger(FakeClient({"*": CHALLENGER}), fx["material"], challenger_input_for(fx),
                                    second_opinion=broken)
    assert tolerant.distrusted is False and "unavailable" in tolerant.opinion_note


def test_curator_is_limited_to_the_shareable_set(fx):
    m, rfc = fx["material"], fx["rfc"]
    shareable_items = [m.get("po_5001"), fx["txn"]]
    answer = {"disclosures": [
                  {"question_id": "q1", "record_id": "po_5001", "revision": 1, "form": "extract",
                   "fields": ["amount_pence"], "reason": "payout"},
                  {"question_id": "q1", "record_id": "msg_7001", "revision": 1, "form": "full", "fields": [],
                   "reason": "message"}],
              "exclusions": [{"record_id": "ord_1001", "revision": 1, "reason": "not_needed_for_any_question",
                              "note": "private"}],
              "gaps": [], "rationale": "minimum"}
    client = FakeClient({"You are the Curator": answer})
    rr = roles.run_curator(client, m, curator_input(m, rfc, [], shareable_items, {"version": None, "rules": []}),
                           shareable={("po_5001", 1), ("txn_9001", 1)})
    assert isinstance(rr.output, CuratorOutput) and rr.model_id == FLASH
    assert [d.record_id for d in rr.output.disclosures] == ["po_5001"]
    assert [f.reason for f in rr.failures] == ["not in the shareable evidence"]
    assert [e.record_id for e in rr.output.exclusions] == ["ord_1001"]
    user = json.loads(client.calls[0].user)
    assert all("fields" in item for item in user["shareable"])
    assert not any("message" in item for item in user["shareable"])


def test_render_input_is_readable_json():
    text = roles.render_input({"b": 1, "a": "£2,160.00", "when": {"x": None}})
    assert json.loads(text) == {"b": 1, "a": "£2,160.00", "when": {"x": None}}
    assert "£" in text and "\n" in text


def test_unknown_role_is_a_role_error(fx):
    with pytest.raises(RoleError) as info:
        roles.run_role("oracle", FakeClient({"*": {}}), fx["material"], {})
    assert info.value.kind == "error"


def test_role_error_carries_the_call_cost(fx):
    # The model answered and the answer failed the schema: one billed call, no usable output.
    with pytest.raises(RoleError) as schema_failure:
        roles.run_challenger(FakeClient({"*": {**CHALLENGER, "proposed_status": "maybe"}}), fx["material"],
                             challenger_input_for(fx))
    assert schema_failure.value.kind == "schema" and schema_failure.value.model_calls == 1
    # An outage is not an answered call.
    with pytest.raises(RoleError) as outage:
        roles.run_challenger(FakeClient({"*": llm.Unavailable("down")}), fx["material"], challenger_input_for(fx))
    assert outage.value.model_calls == 0 and outage.value.tokens == 0
    # A cached result costs nothing under the shared rule; a live one costs one call and its tokens.
    rr = roles.run_challenger(FakeClient({"*": CHALLENGER}), fx["material"], challenger_input_for(fx))
    assert (rr.model_calls, rr.tokens) == (1, 20) == roles.call_cost(rr.result)
    cached = llm.Result(data=rr.raw, model=rr.model_id, prompt_sha256="x", cached=True, usage=rr.result.usage)
    assert roles.call_cost(cached) == (0, 20) and roles.call_cost(None) == (0, 0)
