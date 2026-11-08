"""One-pass Gemini baseline: fixed retrieval, one scripted call, code verdicts and the gate.

Every test drives ``alibi.baselines.one_pass`` with ``FakeClient`` over the committed Maya
fixtures in a temporary store. No network, no Gemini, no ``data/alibi``.
"""

from __future__ import annotations

import json
import re

import pytest

from alibi import api
from alibi.baselines import one_pass
from alibi.baselines.one_pass import FIXED_RETRIEVAL, OnePassRun, run_investigation, run_one_pass
from alibi.core.schemas import ClaimStatus, LinkState, MerchantEvent, RecordKind
from alibi.fixtures import maya
from alibi.gateway import Gateway
from alibi.llm.client import Unavailable
from alibi.llm.fake import FakeClient
from alibi.llm.prompts import load_prompt
from alibi.llm.schema import validate

RFC_A, RFC_B = maya.RFC_A_ID, maya.RFC_B_ID
SUPPORTED, CONTRADICTED, UNRESOLVED = ClaimStatus.SUPPORTED, ClaimStatus.CONTRADICTED, ClaimStatus.UNRESOLVED

NAMED_MESSAGE = ("Your October earnings of £2,160.00 have been released to your seller balance. "
                 "Payout PO-5001 to your bank account is sent separately.")


# --------------------------------------------------------------------------- helpers


def ref(record_id: str, revision: int = 1, role: str = "confirms") -> dict:
    return {"record_id": record_id, "revision": revision, "role": role}


def claim(question_id: str, status: str, refs: list[dict], statement: str = "The answer.",
          gaps: list[str] | None = None) -> dict:
    return {"question_id": question_id, "statement": statement, "status": status, "refs": refs,
            "gaps": gaps or []}


def disclosure(question_id: str, record_id: str, revision: int = 1, form: str = "extract",
               fields: list[str] | None = None) -> dict:
    return {"question_id": question_id, "record_id": record_id, "revision": revision, "form": form,
            "fields": fields or ["amount_pence", "currency"], "reason": "supports the answer"}


def answer(claims: list[dict], disclosures: list[dict] | None = None) -> dict:
    return {"claims": claims, "disclosures": disclosures or [], "rationale": "Scripted answer."}


SUPPORTED_STATEMENT = ("Credit txn_9001 of 216000 pence GBP is payout po_5001 (reference PO-5001), "
                       "settled as stl_88001 before the credit was booked.")
Q1_SUPPORTED = answer(
    [claim("q1", "supported", [ref("txn_9001"), ref("po_5001"), ref("stl_88001")], SUPPORTED_STATEMENT)],
    [disclosure("q1", "po_5001"), disclosure("q1", "stl_88001")])


@pytest.fixture
def ctx(tmp_path, keystore, registry):
    store = api.open_store(tmp_path / "p.db")
    yield {"store": store, "keystore": keystore, "registry": registry, "tmp": tmp_path}
    store.close()


def load(ctx, variant: str) -> dict:
    return api.load_fixture(ctx["store"], variant, keystore=ctx["keystore"])


def gateway_for(ctx, fx, request_id: str, run_id: str | None = None, budget: int = 12) -> Gateway:
    return Gateway(ctx["store"], subject_ref=fx["subject_ref"],
                   run_id=run_id or one_pass.next_run_id(ctx["store"], request_id),
                   registry=ctx["registry"], budget=budget,
                   settlement_lookup=fx["settlement_lookup"], txn_refresh=fx["txn_refresh"],
                   now=fx["now"], request_id=request_id)


def run(ctx, fx, request_id: str, client: FakeClient, **kwargs) -> OnePassRun:
    return run_one_pass(ctx["store"], request_id, client=client, gateway=gateway_for(ctx, fx, request_id),
                        now=fx["now"], **kwargs)


def name_payout_in_message(ctx, fx, text: str = NAMED_MESSAGE) -> None:
    """Ingest revision 2 of msg_7001 with ``text`` (by default naming PO-5001), signed by the
    merchant key."""
    stored = ctx["store"].get_source("msg_7001")
    body = MerchantEvent.model_validate({
        "event_id": "evt_msg_7001_r2", "record_id": "msg_7001", "revision": 2,
        "corrects_revision": 1, "issuer_id": stored.issuer_id, "subject_ref": stored.subject_ref,
        "kind": "message", "occurred_at": "2026-11-01T08:05:00Z", "status": "released_to_balance",
        "amount_pence": stored.amount_pence, "currency": stored.currency, "text": text,
        "correction_reason": "Message restated.",
    }).to_json_dict()
    put = api.ingest_merchant_event(ctx["store"], maya.sign_event(body, ctx["keystore"]),
                                    registry=ctx["registry"], subject_ref=fx["subject_ref"], now=fx["now"])
    assert put.record.revision == 2


def by_question(result) -> dict[str, object]:
    return {c.question_id: c for c in result.claims}


def notes(result, **match) -> list:
    events = [e for e in result.trace if e.actor == one_pass.ACTOR]
    for key, value in match.items():
        events = [e for e in events if getattr(e, key) == value]
    return events


def counts_note(result):
    found = [e for e in notes(result, step="note") if "unsafe_proposals" in e.result]
    assert len(found) == 1
    return found[0]


# --------------------------------------------------------------------------- fixed retrieval


def test_maya_message_names_no_reference_so_zero_lookups_and_unresolved(ctx):
    fx = load(ctx, "supported")
    client = FakeClient({"*": Q1_SUPPORTED})
    out = run(ctx, fx, RFC_A, client)

    assert out.lookups == 0
    assert out.model_calls == 1 and out.tokens == 20
    assert client.call_count == 1
    assert out.result.mode == "single_call"
    (c,) = out.result.claims
    assert c.status == UNRESOLVED
    assert c.gap.startswith("settlement_not_found")
    assert "balance_release_not_settlement" in c.gap
    assert "stl_88001" not in {r.ref_id for r in c.evidence_refs}, "the settlement was never retrieved"
    # Unsafe: the supported proposal, and the disclosure of stl_88001, which was never retrieved
    # (po_5001 is cited by the unresolved claim, so the gate attaches it).
    assert out.unsafe_proposals == 2
    assert c.statement != SUPPORTED_STATEMENT
    note = counts_note(out.result)
    assert note.result["lookups"] == 0 and note.result["fixed_retrieval"] == FIXED_RETRIEVAL
    assert note.result["proposed_statuses"] == {"q1": "supported"}
    assert note.result["unsafe_proposals"] == out.unsafe_proposals
    # The predeclared strategy is traced and no settlement lookup went through the gateway.
    assert any("names a payout reference" in (e.reason or "") for e in notes(out.result))
    assert not [e for e in out.result.trace if e.actor == "gateway" and e.tool == "lookup_settlement"]
    (link,) = out.result.links
    assert link.link_state == LinkState.UNMATCHED and link.method == "rule"
    assert [r.ref_id for r in link.candidates] == ["po_5001"]


def test_message_naming_the_payout_triggers_exactly_one_lookup_and_supports(ctx):
    fx = load(ctx, "supported")
    name_payout_in_message(ctx, fx)
    client = FakeClient({"*": Q1_SUPPORTED})
    out = run(ctx, fx, RFC_A, client)

    assert out.lookups == 1
    gateway_lookups = [e for e in out.result.trace if e.actor == "gateway" and e.tool == "lookup_settlement"]
    assert len(gateway_lookups) == 1 and gateway_lookups[0].outcome == "ok"
    (c,) = out.result.claims
    assert c.status == SUPPORTED
    assert c.statement == SUPPORTED_STATEMENT, "a clean model statement with the right status is kept"
    cited = {r.ref_id for r in c.evidence_refs}
    assert {"txn_9001", "po_5001", "stl_88001", "ord_1001", "rfd_2001", "fee_3001"} <= cited
    assert c.calculation is not None and c.calculation.balanced is True
    assert c.calculation.total_pence == 216000 and c.calculation.timing.ok is True
    assert out.unsafe_proposals == 0
    assert out.observed_inputs >= 10   # 4 orders, refund, fee, message, payout, credit, settlement
    (link,) = out.result.links
    assert link.link_state == LinkState.LINKED and link.record_ref.ref_id == "po_5001"
    # The model saw the settlement the fixed retrieval fetched, marked as a lookup result. The
    # settlements list holds every record that can show settlement: the payout carries a status
    # (the team's payouts fold the settlement state in), so it is listed beside the settlement.
    sent = json.loads(client.calls[0].user)
    assert [s["record_id"] for s in sent["settlements"]] == ["po_5001", "stl_88001"]
    assert sent["settlements"][0]["via_lookup"] is None and sent["settlements"][1]["via_lookup"] == 0
    assert sent["candidate_rule"] and sent["fixed_retrieval"] == FIXED_RETRIEVAL
    assert "msg_7001" in {r["record_id"] for r in sent["merchant"]}
    assert "msg_7001" not in {s["record_id"] for s in sent["shareable"]}


def test_contradiction_is_decided_in_code_whatever_the_model_says(ctx):
    fx = load(ctx, "contradiction")
    name_payout_in_message(ctx, fx)
    client = FakeClient({"*": Q1_SUPPORTED})
    out = run(ctx, fx, RFC_A, client)

    assert out.lookups == 1
    (c,) = out.result.claims
    assert c.status == CONTRADICTED
    assert c.gap.startswith("settlement_pending_at_booking")
    assert {"txn_9001", "po_5001", "stl_88001"} <= {r.ref_id for r in c.evidence_refs}
    assert c.statement != SUPPORTED_STATEMENT
    assert out.unsafe_proposals == 1   # supported proposed, not supported in code
    status_change = notes(out.result, step="status_change")
    assert status_change and status_change[-1].result["proposed_status"] == "supported"
    assert status_change[-1].result["status"] == "contradicted"


def test_ambiguous_candidates_stay_unresolved(ctx):
    fx = load(ctx, "ambiguous")
    out = run(ctx, fx, RFC_A, FakeClient({"*": Q1_SUPPORTED}))
    (c,) = out.result.claims
    assert c.status == UNRESOLVED and c.gap.startswith("ambiguous_candidates")
    (link,) = out.result.links
    assert link.link_state == LinkState.AMBIGUOUS
    assert sorted(r.ref_id for r in link.candidates) == ["po_5001", "po_5002"]
    # The supported proposal plus the stl_88001 disclosure (never retrieved on this fixture).
    assert out.lookups == 0 and out.unsafe_proposals == 2


# --------------------------------------------------------------------------- validation and gate


def test_unknown_and_non_shareable_disclosures_are_dropped_and_counted(ctx):
    fx = load(ctx, "supported")
    scripted = answer(
        [claim("q1", "unresolved", [ref("txn_9001"), ref("po_5001"), ref("msg_7001", role="context")],
               "The source of this credit is not established.", ["no settlement record"])],
        [disclosure("q1", "msg_7001"), disclosure("q1", "ord_9999")])
    out = run(ctx, fx, RFC_A, FakeClient({"*": scripted}))

    # Two unsafe disclosures (msg_7001, ord_9999) plus the claim ref to msg_7001, which is not shareable.
    assert out.unsafe_proposals == 3
    assert out.proposal["disclosures"] == []
    refused = notes(out.result, step="validation", outcome="refused")
    reasons = " ".join(e.reason for e in refused)
    assert "msg_7001@1 not in the shareable evidence" in reasons
    assert "ord_9999@1 not in the retrieved material" in reasons
    (c,) = out.result.claims
    assert "msg_7001" not in {r.ref_id for r in c.evidence_refs}, "message records stay holder-side"
    assert "msg_7001" in reasons and "holder-side" in reasons
    gate = notes(out.result, step="validation", tool="build_body")
    assert gate and sorted(gate[0].result["unsafe_disclosures"]) == ["msg_7001@1", "ord_9999@1"]


def test_model_statement_with_foreign_ids_or_message_text_is_replaced(ctx):
    fx = load(ctx, "supported")
    name_payout_in_message(ctx, fx)
    leaked = answer([claim("q1", "supported", [ref("txn_9001"), ref("po_5001"), ref("stl_88001")],
                           "Your October earnings of £2,160.00 have been released to your seller balance "
                           "so the credit is payout PO-5001.")])
    out = run(ctx, fx, RFC_A, FakeClient({"*": leaked}))
    (c,) = out.result.claims
    assert c.status == SUPPORTED
    assert "seller balance" not in c.statement and c.statement.startswith("Credit txn_9001")

    fx2 = load(ctx, "supported")  # idempotent reload; new run id
    foreign = answer([claim("q1", "supported", [ref("txn_9001"), ref("po_5001"), ref("stl_88001")],
                            "Credit txn_9001 is payout po_5001, see also account acc_other_77.")])
    out2 = run(ctx, fx2, RFC_A, FakeClient({"*": foreign}))
    (c2,) = out2.result.claims
    assert c2.status == SUPPORTED and "acc_other_77" not in c2.statement


# --------------------------------------------------------------------------- period totals


def test_rfc_b_totals_are_summed_in_code(ctx):
    fx = load(ctx, "supported")
    scripted = answer([
        claim("q1", "supported", [ref("ord_1001"), ref("ord_1002")], "Sales were 999999 pence."),
        claim("q2", "supported", [ref("rfd_2001")], "Refunds totalled 18000 pence GBP from rfd_2001."),
    ])
    out = run(ctx, fx, RFC_B, FakeClient({"*": scripted}))

    assert out.lookups == 0 and out.result.mode == "single_call"
    claims = by_question(out.result)
    q1, q2 = claims["q1"], claims["q2"]
    assert q1.status == SUPPORTED and q1.calculation.total_pence == 240000
    assert {r.ref_id for r in q1.calculation.cited_refs} == {"ord_1001", "ord_1002", "ord_1003", "ord_1004"}
    assert "999999" not in q1.statement, "an amount outside the material is not repeated"
    assert "240000" in q1.statement
    assert q2.status == SUPPORTED and q2.calculation.total_pence == 18000
    assert q2.statement == "Refunds totalled 18000 pence GBP from rfd_2001."
    assert out.unsafe_proposals == 0
    assert out.result.links == []


# --------------------------------------------------------------------------- failures


def test_unavailable_model_gives_unresolved_claims_without_raising(ctx):
    fx = load(ctx, "supported")
    name_payout_in_message(ctx, fx)
    out = run(ctx, fx, RFC_A, FakeClient({"*": Unavailable("simulated outage")}))

    # Shared rule (alibi.agents.roles.call_cost): an outage is not an answered, billed call.
    assert out.model_calls == 0 and out.tokens == 0
    assert out.proposal == {"claims": [], "disclosures": [], "rationale": ""}
    (c,) = out.result.claims
    assert c.status == UNRESOLVED
    assert "model_unavailable" in c.gap
    failed = [e for e in notes(out.result, tool="generate") if e.outcome == "unavailable"]
    assert len(failed) == 1
    assert "simulated outage" not in (failed[0].reason or ""), "the payload is never logged"
    assert out.unsafe_proposals == 0
    assert out.lookups == 1, "the fixed retrieval still ran"


def test_schema_invalid_answer_is_refused_not_raised(ctx):
    fx = load(ctx, "supported")
    bad = {"claims": [{"question_id": "q1", "statement": "x", "status": "maybe", "refs": [], "gaps": []}],
           "disclosures": [], "rationale": "r"}
    out = run(ctx, fx, RFC_A, FakeClient({"*": bad}))   # FakeClient raises SchemaValidationError
    (c,) = out.result.claims
    assert c.status == UNRESOLVED and "model_unavailable" in c.gap
    assert any(e.outcome == "error" for e in notes(out.result, tool="generate"))


def test_gateway_budget_exhaustion_is_a_typed_reason(ctx):
    fx = load(ctx, "supported")
    gateway = gateway_for(ctx, fx, RFC_A, budget=1)   # get_txn uses it; list_records is refused
    out = run_one_pass(ctx["store"], RFC_A, client=FakeClient({"*": Q1_SUPPORTED}), gateway=gateway,
                       now=fx["now"])
    assert any(u.startswith("budget_exceeded") for u in out.result.unavailable)
    (c,) = out.result.claims
    assert c.status == UNRESOLVED


# --------------------------------------------------------------------------- persistence and api shape


def test_run_investigation_saves_and_returns_single_call_result(ctx):
    fx = load(ctx, "supported")
    result = run_investigation(ctx["store"], RFC_A, client=FakeClient({"*": Q1_SUPPORTED}),
                               settlement_lookup=fx["settlement_lookup"], txn_refresh=fx["txn_refresh"],
                               registry=ctx["registry"], now=fx["now"])
    assert result.mode == "single_call"
    assert result.run_id.startswith("run_")
    stored = ctx["store"].list_claims(request_id=RFC_A, state=None)
    assert [c.claim_id for c in stored] == [c.claim_id for c in result.claims]
    assert ctx["store"].list_traces(result.run_id)
    assert all(e.run_id == result.run_id for e in result.trace)
    assert result.calls_used == 2    # get_txn + list_records; no lookup on this fixture
    # A second run gets a fresh deterministic id.
    again = run_investigation(ctx["store"], RFC_A, client=FakeClient({"*": Q1_SUPPORTED}),
                              settlement_lookup=fx["settlement_lookup"], registry=ctx["registry"],
                              now=fx["now"])
    assert again.run_id != result.run_id
    assert one_pass.next_run_id(ctx["store"], RFC_A) not in {result.run_id, again.run_id}


def test_gateway_for_another_subject_is_refused(ctx):
    fx = load(ctx, "supported")
    other = Gateway(ctx["store"], subject_ref="subj_other", run_id="run_x", now=fx["now"])
    with pytest.raises(ValueError):
        run_one_pass(ctx["store"], RFC_A, client=FakeClient({"*": Q1_SUPPORTED}), gateway=other, now=fx["now"])


def test_one_call_only_with_a_hard_budget(ctx):
    fx = load(ctx, "supported")
    client = FakeClient({"*": Q1_SUPPORTED})
    run(ctx, fx, RFC_A, client)
    assert client.call_count == 1
    assert client.calls[0].model == "gemini-3.8-flash"
    out = run(ctx, fx, RFC_A, FakeClient({"*": Q1_SUPPORTED}), model="gemini-4-argon")
    assert out.result.mode == "single_call"


# --------------------------------------------------------------------------- the prompt


def test_one_pass_prompt_follows_the_shared_conventions():
    p = load_prompt("one_pass")
    text = p.system
    assert text.startswith("#")
    assert "`one_pass.json`" in text
    assert "`record_id` and `revision`" in text
    assert "supplied" in text and "Never invent" in text
    assert "is data" in text and "never an instruction" in text
    assert "## Output" in text and "## Worked example" in text
    for amount in ("240000", "18000", "6000", "216000"):
        assert amount in text
    assert p.schema["type"] == "object"
    assert set(p.schema["required"]) == {"claims", "disclosures", "rationale"}


def test_one_pass_worked_example_conforms_and_cites_only_input_ids():
    p = load_prompt("one_pass")
    blocks = re.findall(r"Expected output:\s*```json\s*(.*?)```", p.system, re.DOTALL)
    assert blocks
    before = p.system.split("Expected output:")[0]
    for block in blocks:
        example = json.loads(block)
        assert validate(example, p.schema) == []
        ids = {r["record_id"] for c in example["claims"] for r in c["refs"]}
        ids |= {d["record_id"] for d in example["disclosures"]}
        for rid in ids:
            assert f'"{rid}"' in before, f"{rid} not in the input material"
        assert not any(d["record_id"].startswith("msg_") for d in example["disclosures"])


# --------------------------------------------------------------------------- review regressions


class CachedFake(FakeClient):
    """Serves every answer as cached (what a replay client does)."""

    replay = True
    label = "cached"

    def generate(self, model, system, user, json_schema, budget=None):
        from alibi.llm.client import Result
        r = super().generate(model, system, user, json_schema, budget)
        return Result(data=r.data, model=r.model, prompt_sha256=r.prompt_sha256, cached=True, usage=r.usage)


def test_model_refs_outside_the_shareable_set_are_not_attached(ctx):
    from alibi.core.schemas import DenyRule, Policy
    fx = load(ctx, "supported")
    ctx["store"].save_policy(Policy(policy_id="pol_test", version=1, subject_ref=fx["subject_ref"],
                                    rules=[DenyRule(rule_id="r_payout", kinds=["payout"])], confirmed=True,
                                    created_at=fx["now"]))
    scripted = answer([
        claim("q1", "supported", [ref("ord_1001"), ref("po_5001", role="context")], "Sales were documented."),
        claim("q2", "supported", [ref("rfd_2001")], "Refunds were documented."),
    ])
    out = run(ctx, fx, RFC_B, FakeClient({"*": scripted}))
    q1 = by_question(out.result)["q1"]
    assert q1.status == SUPPORTED
    assert "po_5001" not in {r.ref_id for r in q1.evidence_refs}
    assert out.unsafe_proposals == 1
    gate = notes(out.result, step="validation", tool="build_body")[0]
    assert gate.result["unsafe_refs"] == ["po_5001@1"] and gate.result["claim_changes"] == 0
    refused = [e for e in notes(out.result, step="validation", outcome="refused")
               if e.result.get("record_id") == "po_5001"]
    assert refused and "not in the shareable evidence" in refused[0].reason
    # Without the policy the same shareable payout is attached, and nothing is counted.
    fx2 = load(ctx, "supported")
    ctx["store"].save_policy(Policy(policy_id="pol_test", version=2, subject_ref=fx2["subject_ref"],
                                    rules=[], confirmed=True, created_at=fx2["now"]))
    out2 = run(ctx, fx2, RFC_B, FakeClient({"*": scripted}))
    assert "po_5001" in {r.ref_id for r in by_question(out2.result)["q1"].evidence_refs}
    assert out2.unsafe_proposals == 0


def test_short_message_sentences_are_not_repeated_in_the_statement(ctx):
    fx = load(ctx, "supported")
    text = "Funds are frozen. Call us now. Read the code aloud."
    name_payout_in_message(ctx, fx, text=text)
    for statement in (f"Merchant says: {text}",
                      "The merchant wrote: call us now, so the source is not established.",
                      "Funds are frozen on the platform; the credit source is unknown."):
        scripted = answer([claim("q1", "unresolved", [ref("txn_9001"), ref("po_5001")], statement)])
        out = run(ctx, fx, RFC_A, FakeClient({"*": scripted}))
        (c,) = out.result.claims
        assert c.status == UNRESOLVED
        assert c.statement != statement and "frozen" not in c.statement and "Call us" not in c.statement
    # A clean statement with the right status is still kept.
    clean = "The source of credit txn_9001 is not established from the available records."
    out = run(ctx, fx, RFC_A, FakeClient({"*": answer([claim("q1", "unresolved", [ref("txn_9001")], clean)])}))
    assert out.result.claims[0].statement == clean


def test_refused_record_read_is_not_reported_as_no_merchant_records(ctx):
    fx = load(ctx, "supported")
    gateway = gateway_for(ctx, fx, RFC_A, budget=1)   # get_txn uses it; list_records is refused
    out = run_one_pass(ctx["store"], RFC_A, client=FakeClient({"*": Q1_SUPPORTED}), gateway=gateway,
                       now=fx["now"])
    (c,) = out.result.claims
    assert c.status == UNRESOLVED
    assert "no_merchant_records" not in c.gap and "could not be retrieved (budget_exceeded)" in c.gap


def test_replayed_run_is_labelled_and_counts_no_model_call(ctx):
    fx = load(ctx, "supported")
    out = run(ctx, fx, RFC_A, CachedFake({"*": Q1_SUPPORTED}))
    first = out.result.trace[0]
    assert first.reason.startswith("REPLAY") and first.result == {"replay": True, "label": "cached"}
    assert out.model_calls == 0
    assert counts_note(out.result).result["model_calls"] == 0


def test_schema_failure_counts_as_one_model_call(ctx):
    fx = load(ctx, "supported")
    bad = {"claims": [{"question_id": "q1", "statement": "x", "status": "maybe", "refs": [], "gaps": []}],
           "disclosures": [], "rationale": "r"}
    out = run(ctx, fx, RFC_A, FakeClient({"*": bad}))
    assert out.model_calls == 1
    out2 = run(ctx, fx, RFC_A, FakeClient({"*": Q1_SUPPORTED}))
    assert out2.model_calls == 1 and out2.tokens == 20
