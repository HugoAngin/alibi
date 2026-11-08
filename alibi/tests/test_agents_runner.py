"""The agents runner on the Maya fixtures with scripted Gemini roles (FakeClient, no network).

Code decides every status: the scripts here try to upgrade, cite unknown records, ask for
unknown lookups and leak private notes, and the claims must come out right anyway.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import pytest

from alibi.agents import runner
from alibi.agents.runner import run_investigation, run_stats
from alibi.api import load_fixture, open_store
from alibi.core.schemas import ClaimStatus, LinkState
from alibi.fixtures.maya import MESSAGE_TEXT
from alibi.llm import client as llm
from alibi.llm.fake import FakeClient

RFC_A, RFC_B = "rfc_a_001", "rfc_b_001"


# --------------------------------------------------------------------------- scripts


def detective(status="unresolved", refs=("txn_9001", "po_5001"), next_check=True, question="q1"):
    return {"alerts": [{"kind": "unexplained_credit", "summary": "Credit txn_9001 has no linked settlement.",
                        "refs": [{"record_id": "txn_9001", "revision": 1}],
                        "suggested_evidence": "Settlement for PO-5001."}],
            "claims": [{"claim_id": "c1", "question_id": question,
                        "statement": "The credit is payout PO-5001. " + MESSAGE_TEXT, "status": status,
                        "refs": [{"record_id": r, "revision": 1, "role": "confirms"} for r in refs],
                        "gaps": ["No settlement record in the material."],
                        "next_checks": [{"type": "lookup_settlement", "args": {"payout_reference": "PO-5001"},
                                         "reason": "Confirm bank settlement."}] if next_check else []}]}


def challenger(status="unresolved", action=("lookup_settlement", {"payout_reference": "PO-5001"}),
               confirming=("po_5001",), conflicting=(), established=False, reference="PO-5001"):
    kind, args = action
    return {"claim_id": "c1", "proposed_status": status, "bank_settlement_established": established,
            "payout_reference": reference,
            "confirming_refs": [{"record_id": r, "revision": 1, "why": "matches"} for r in confirming],
            "conflicting_refs": [{"record_id": r, "revision": 1, "why": "conflicts"} for r in conflicting],
            "missing_evidence": ["settlement"] if status == "unresolved" else [],
            "next_action": {"type": kind, "args": args, "reason": "the one record that decides"},
            "trace": ["Checked po_5001 rev 1."], "rationale": "Released to balance is not settlement."}


def lookup_then(second):
    """Round 1 asks for PO-5001; from round 2 (lookup_results present) answers ``second``."""
    def script(model, system, user, schema):
        claim = json.loads(user)["claim"]
        return second if claim.get("lookup_results") else challenger()
    return script


def curator_all(exclusions=(), gaps=()):
    def script(model, system, user, schema):
        data = json.loads(user)
        questions = {c["question_id"] for c in data["claims"]}
        return {"disclosures": [{"question_id": q, "record_id": s["record_id"], "revision": s["revision"],
                                 "form": "extract", "fields": s["fields"][:2], "reason": "supports the answer"}
                                for q in sorted(questions) for s in data["shareable"]],
                "exclusions": list(exclusions), "gaps": list(gaps), "rationale": "minimum set"}
    return script


def client_for(detective_script=None, challenger_script=None, curator_script=None, matcher_script=None):
    scripts = {"You are the Detective": detective_script if detective_script is not None else detective(),
               "You are the Challenger": challenger_script if challenger_script is not None
               else lookup_then(challenger("supported", ("none", {}), ("po_5001", "stl_88001"), established=True)),
               "You are the Curator": curator_script or curator_all()}
    if matcher_script is not None:
        scripts["You are the Matcher"] = matcher_script
    return FakeClient(scripts)


# --------------------------------------------------------------------------- fixtures


@dataclass
class Ctx:
    store: object
    fx: dict
    registry: object
    runs: list = field(default_factory=list)

    def run(self, client, request_id=RFC_A, **kw):
        kw.setdefault("settlement_lookup", self.fx["settlement_lookup"])
        result = run_investigation(self.store, request_id, client=client, registry=self.registry,
                                   now=self.fx["now"], **kw)
        self.runs.append(result)
        return result


@pytest.fixture
def make(tmp_path, keystore, registry):
    stores = []

    def build(variant="supported"):
        store = open_store(tmp_path / f"{variant}-{len(stores)}.db")
        stores.append(store)
        return Ctx(store, load_fixture(store, variant, keystore=keystore), registry)

    yield build
    for store in stores:
        store.close()


def events(run, **match):
    return [e for e in run.trace if all(getattr(e, k) == v for k, v in match.items())]


def claim_text(claim):
    return " ".join(filter(None, [claim.statement, claim.rationale, claim.gap]))


# --------------------------------------------------------------------------- tests


def test_supported_variant_end_to_end(make):
    ctx = make("supported")
    client = client_for()
    result = ctx.run(client)
    (claim,) = result.claims
    assert result.mode == "agents" and claim.status == ClaimStatus.SUPPORTED
    keys = {r.key() for r in claim.evidence_refs}
    assert {"txn:txn_9001@1", "record:po_5001@1", "record:stl_88001@1", "record:ord_1001@1",
            "record:rfd_2001@1", "record:fee_3001@1"} <= keys
    calc = claim.calculation
    by_kind = {}
    for term in calc.terms:
        by_kind[term.kind] = by_kind.get(term.kind, 0) + term.sign * term.amount_pence
    assert by_kind == {"order": 240000, "refund": -18000, "fee": -6000}
    assert calc.total_pence == 216000 and calc.balanced is True and calc.timing.ok
    (link,) = result.links
    assert link.link_state == LinkState.LINKED and link.record_ref.ref_id == "po_5001" and link.method == "rule"
    lookup = events(result, actor="challenger", tool="lookup_settlement")
    assert lookup and lookup[0].result["via_lookup"] == 0 and "stl_88001@1" in lookup[0].result["new_keys"]
    assert [e.tool for e in events(result, actor="gateway", step="tool_call")].count("lookup_settlement") == 1
    stats = run_stats(result)
    assert stats["lookups"] == 1 and stats["action_budget_used"] == 1 and stats["rounds"] == 2
    assert stats["model_calls"] == 4 and stats["tokens"] == 80 and stats["observed_inputs"] >= 9
    # the detective's claim statement carried the merchant message; the bank-facing claim never does
    assert MESSAGE_TEXT not in claim_text(claim)
    assert ctx.store.list_claims(request_id=RFC_A)[0] == claim
    assert [a.kind.value for a in ctx.store.list_alerts(ctx.fx["subject_ref"])] == ["unexplained_credit"]
    changes = events(result, step="status_change")
    assert all(e.reason for e in changes) and {e.actor for e in changes} >= {"detective", "challenger", "runner"}


def test_contradiction_variant_is_contradicted_in_code_even_if_the_challenger_says_supported(make):
    ctx = make("contradiction")
    result = ctx.run(client_for())
    (claim,) = result.claims
    assert claim.status == ClaimStatus.CONTRADICTED
    assert claim.gap.startswith("settlement_pending_at_booking:")
    assert "record:stl_88001@1" in {r.key() for r in claim.evidence_refs}
    assert MESSAGE_TEXT not in claim_text(claim) and "seller balance" not in claim_text(claim)
    upgrades = [e for e in result.trace if "never upgrades" in (e.reason or "")]
    assert upgrades, "the refused upgrade is traced"
    assert result.links[0].link_state == LinkState.UNMATCHED


def test_unknown_ref_and_unknown_lookup_are_dropped(make):
    ctx = make("supported")
    bad = challenger(confirming=("po_9999", "po_5001"), action=("lookup_settlement", {"payout_reference": "PO-9999"}),
                     reference="PO-9999")
    result = ctx.run(client_for(challenger_script=bad))
    (claim,) = result.claims
    refused = events(result, step="validation", outcome="refused", actor="challenger")
    assert {e.result["record_id"] for e in refused} == {"po_9999", "PO-9999"}
    assert not events(result, actor="gateway", tool="lookup_settlement"), "no lookup ran"
    assert claim.status == ClaimStatus.UNRESOLVED
    assert claim.gap.startswith(("balance_release_not_settlement:", "settlement_not_found:"))
    assert run_stats(result)["action_budget_used"] == 0 and run_stats(result)["rounds"] == 1


def test_lookup_returning_nothing_leaves_the_claim_unresolved(make):
    ctx = make("supported")
    client = client_for(challenger_script=lookup_then(challenger(action=("none", {}))))
    result = ctx.run(client, settlement_lookup=lambda ref: None)
    (claim,) = result.claims
    assert claim.status == ClaimStatus.UNRESOLVED
    assert claim.gap.startswith("balance_release_not_settlement:") and "returned no record" in claim.gap
    second = [c for c in client.calls if c.system.startswith("# Challenger")][1]
    fed = json.loads(second.user)["claim"]["lookup_results"]
    assert fed[0]["outcome"] == "not_found" and fed[0]["returned"] == []
    assert run_stats(result)["lookups"] == 1


def test_action_budget_exhaustion_stops_the_loop(make):
    ctx = make("supported")
    result = ctx.run(client_for(challenger_script=challenger()))   # always asks for a lookup
    stats = run_stats(result)
    assert stats["action_budget_used"] == 2 and stats["rounds"] == 2
    assert len(events(result, actor="gateway", tool="lookup_settlement")) == 2
    assert any(e.result.get("stop_reason") == "repeated_action_no_progress" for e in result.trace)
    (claim,) = result.claims
    assert claim.status == ClaimStatus.UNRESOLVED and claim.gap.startswith("challenger_unresolved:")
    wide = make("supported")
    result2 = wide.run(client_for(challenger_script=challenger()), action_budget=1, max_rounds=5)
    stats2 = run_stats(result2)
    assert stats2["action_budget_used"] == 1 and stats2["rounds"] == 2
    assert events(result2, actor="challenger", outcome="budget_exceeded")


def matcher(decision="linked", chosen="po_5001"):
    return {"entry": {"record_id": "txn_9001", "revision": 1},
            "candidates": [{"record_id": "po_5001", "revision": 1, "relationship": "exact",
                            "signals": ["earlier payout"], "confidence": 0.6},
                           {"record_id": "po_5002", "revision": 1, "relationship": "unrelated",
                            "signals": ["later payout"], "confidence": 0.4}],
            "decision": decision, "chosen_record_id": chosen if decision == "linked" else "",
            "chosen_revision": 1 if decision == "linked" else 0, "rationale": "po_5001 was initiated first"}


def test_ambiguous_variant_matcher_link_is_accepted_only_from_the_pool(make):
    ctx = make("ambiguous")
    result = ctx.run(client_for(matcher_script=matcher(), challenger_script=challenger(action=("none", {}))))
    (link,) = result.links
    assert link.link_state == LinkState.LINKED and link.record_ref.ref_id == "po_5001" and link.method == "agent"
    assert {r.ref_id for r in link.candidates} == {"po_5001", "po_5002"}
    (claim,) = result.claims
    assert claim.status == ClaimStatus.UNRESOLVED
    assert claim.gap.startswith(("balance_release_not_settlement:", "settlement_not_found:"))

    outside = make("ambiguous")
    result2 = outside.run(client_for(matcher_script=matcher(chosen="ord_1001"),
                                     challenger_script=challenger(action=("none", {}))))
    assert result2.links[0].link_state == LinkState.AMBIGUOUS and result2.links[0].record_ref is None
    assert result2.claims[0].gap.startswith("ambiguous_candidates:")
    assert events(result2, actor="matcher", step="validation", outcome="refused")


def test_ambiguous_variant_matcher_abstains_or_is_distrusted(make):
    ctx = make("ambiguous")
    result = ctx.run(client_for(matcher_script=matcher("ambiguous"), challenger_script=challenger(action=("none", {}))))
    assert result.links[0].link_state == LinkState.AMBIGUOUS
    assert result.claims[0].status == ClaimStatus.UNRESOLVED
    assert result.claims[0].gap.startswith("ambiguous_candidates:")

    doubted = make("ambiguous")
    opinions = []

    def opinion(role, input_dict, output):
        opinions.append(role)
        return (False, "both payouts fit") if role == "matcher" else (True, "")

    result2 = doubted.run(client_for(matcher_script=matcher(), challenger_script=challenger(action=("none", {}))),
                          second_opinion=opinion)
    assert "matcher" in opinions
    assert result2.links[0].link_state == LinkState.AMBIGUOUS
    assert result2.claims[0].gap.startswith("ambiguous_candidates:")


def test_ambiguous_variant_resolves_by_lookup_without_a_reference_on_the_credit(make):
    ctx = make("ambiguous")
    result = ctx.run(client_for(matcher_script=matcher("ambiguous")))
    (claim,) = result.claims
    assert result.links[0].link_state == LinkState.LINKED and result.links[0].method == "rule"
    assert claim.status == ClaimStatus.UNRESOLVED and "carries no reference" in claim.gap
    assert claim.calculation is None and claim.evidence_refs == [] and claim.examined_refs
    assert [e.result for e in result.trace if e.step == "calculation"][-1]["balanced"] is True


def test_rfc_b_period_totals_are_computed_in_code(make):
    ctx = make("supported")
    contradicting = {"alerts": [], "claims": [
        {"claim_id": "c1", "question_id": "q1", "statement": "Sales were zero.", "status": "contradicted",
         "refs": [{"record_id": "ord_1001", "revision": 1, "role": "conflicts"}], "gaps": [], "next_checks": []},
        {"claim_id": "c2", "question_id": "q2", "statement": "No refunds.", "status": "contradicted",
         "refs": [], "gaps": [], "next_checks": []}]}
    result = ctx.run(client_for(detective_script=contradicting), request_id=RFC_B)
    q1, q2 = result.claims
    assert (q1.status, q1.calculation.total_pence) == (ClaimStatus.SUPPORTED, 240000)
    assert (q2.status, q2.calculation.total_pence) == (ClaimStatus.SUPPORTED, 18000)
    assert {r.ref_id for r in q2.evidence_refs} == {"rfd_2001"}
    assert len(events(result, step="calculation")) == 2
    decided = [e for e in events(result, step="status_change", actor="runner") if e.result.get("decided_by") == "code"]
    assert [(e.result["from"], e.result["to"]) for e in decided] == [("contradicted", "supported")] * 2
    assert run_stats(result)["model_calls"] == 2    # detective and curator; no challenger for period totals


def test_unavailable_client_degrades_to_unresolved(make):
    ctx = make("supported")
    result = ctx.run(FakeClient({"*": llm.Unavailable("simulated outage")}))
    (claim,) = result.claims
    assert claim.status == ClaimStatus.UNRESOLVED and claim.gap
    assert any(r.startswith("unavailable: detective") for r in result.unavailable)
    assert any(r.startswith("unavailable: challenger") for r in result.unavailable)
    assert events(result, actor="detective", outcome="unavailable")
    assert run_stats(result)["model_calls"] == 0
    period = ctx.run(FakeClient({"*": llm.BudgetExceeded("cap")}), request_id=RFC_B)
    assert [c.status for c in period.claims] == [ClaimStatus.SUPPORTED, ClaimStatus.SUPPORTED]
    assert any(r.startswith("budget_exceeded: detective") for r in period.unavailable)


def test_distrusted_detective_leaves_claims_unresolved(make):
    ctx = make("supported")
    result = ctx.run(client_for(), second_opinion=lambda role, i, o: (role != "detective", "doubt"))
    (claim,) = result.claims
    assert claim.status == ClaimStatus.UNRESOLVED and "second opinion" in claim.gap
    assert events(result, actor="detective", step="note", result={"second_opinion": "disagree"})


def test_curator_exclusions_stay_in_the_trace(make):
    ctx = make("supported")
    secret = "HOLDER NOTE: medical receipt withheld"
    exclusions = [{"record_id": "ord_1002", "revision": 1, "reason": "not_needed_for_any_question", "note": secret}]
    gaps = [{"question_id": "q1", "description": "a record is withheld"}]
    result = ctx.run(client_for(curator_script=curator_all(exclusions, gaps)))
    (claim,) = result.claims
    assert secret not in claim_text(claim) and "a record is withheld" not in claim_text(claim)
    # Nothing was in fact withheld (code keeps every calculation term), so no withholding gap
    # reaches the bank; the Curator's gap text stays holder-side.
    assert runner.GENERIC_CURATOR_GAP not in (claim.gap or "")
    notes = events(result, actor="curator", step="note")
    assert any(secret in json.dumps(e.result) for e in notes)
    assert claim.status == ClaimStatus.SUPPORTED
    # code wins: everything the calculation cites stays even when the curator drops it
    assert "record:ord_1002@1" in {r.key() for r in claim.evidence_refs}


def test_curator_can_narrow_evidence_but_not_below_the_calculation(make):
    ctx = make("supported")

    def narrow(model, system, user, schema):
        return {"disclosures": [{"question_id": "q1", "record_id": "txn_9001", "revision": 1, "form": "full",
                                 "fields": [], "reason": "the credit"}],
                "exclusions": [], "gaps": [], "rationale": "credit only"}

    result = ctx.run(client_for(curator_script=narrow))
    (claim,) = result.claims
    keys = {r.key() for r in claim.evidence_refs}
    assert keys == {r.key() for r in claim.calculation.cited_refs}
    assert "record:stl_88001@1" in keys


def test_shape_distinct_run_ids_and_replay_note(make):
    ctx = make("supported")
    first = ctx.run(client_for())
    second = ctx.run(client_for())
    assert first.mode == second.mode == "agents" and first.run_id != second.run_id
    assert first.run_id.startswith("run_") and first.request_id == RFC_A
    assert first.calls_used >= 3 and first.unavailable == []
    assert len(ctx.store.list_claims(request_id=RFC_A, state=None)) == 2

    class Replay(FakeClient):
        replay = True
        label = "maya_supported"

    replayed = ctx.run(Replay(client_for().scripts))
    note = replayed.trace[0]
    assert note.step == "note" and note.reason.startswith("REPLAY") and note.result == {"replay": True,
                                                                                         "label": "maya_supported"}


def test_run_ids_are_deterministic_and_separate_from_rules_runs(make):
    ctx = make("supported")
    assert runner.next_run_id(ctx.store, RFC_A, audience="bank_a") == runner.next_run_id(ctx.store, RFC_A, audience="bank_a")
    from alibi.core.investigate_rules import next_run_id as rules_next
    assert runner.next_run_id(ctx.store, RFC_A, audience="bank_a") != rules_next(ctx.store, RFC_A, audience="bank_a")
    # run ids carry the bank: another bank reusing the id never shares a counter
    assert runner.next_run_id(ctx.store, RFC_A, audience="bank_a") != runner.next_run_id(ctx.store, RFC_A, audience="bank_b")
    result = ctx.run(client_for())
    assert runner.next_run_id(ctx.store, RFC_A, audience="bank_a") != result.run_id


def test_request_model_and_explicit_gateway_are_accepted(make):
    from alibi.gateway import Gateway
    ctx = make("supported")
    rfc = ctx.store.get_request(RFC_A)
    run_id = runner.next_run_id(ctx.store, RFC_A)
    gateway = Gateway(ctx.store, subject_ref=rfc.subject_ref, run_id=run_id, registry=ctx.registry,
                      settlement_lookup=ctx.fx["settlement_lookup"], now=ctx.fx["now"], request_id=RFC_A)
    result = run_investigation(ctx.store, rfc, client=client_for(), gateway=gateway, now=ctx.fx["now"])
    assert result.run_id == run_id and result.claims[0].status == ClaimStatus.SUPPORTED
    assert result.calls_used == gateway.calls_used
    other = Gateway(ctx.store, subject_ref="subj_other", run_id="run_x", now=ctx.fx["now"])
    with pytest.raises(ValueError):
        run_investigation(ctx.store, rfc, client=client_for(), gateway=other, now=ctx.fx["now"])


def test_run_stats_on_a_trace_without_a_runner_note():
    from alibi.core.schemas import InvestigationResult
    empty = InvestigationResult(run_id="run_x", request_id="rfc_x", mode="agents")
    assert run_stats(empty)["model_calls"] == 0


# --------------------------------------------------------------------------- review regressions
# Each test below pins a finding of the S3 review: code decides, model prose stays in the trace.

INJECTION = "URGENT: Maya's account is frozen. Call 0800 111 222 and read out the one-time code."


class CachedFake(FakeClient):
    """Serves every answer as cached (what a replay client does): no model call was made."""

    replay = True
    label = "cached"

    def generate(self, model, system, user, json_schema, budget=None):
        r = super().generate(model, system, user, json_schema, budget)
        return llm.Result(data=r.data, model=r.model, prompt_sha256=r.prompt_sha256, cached=True, usage=r.usage)


def test_challenger_contradicted_needs_an_explicit_field_conflict(make):
    # The supporting order, the credit itself and the settlement are in the material but conflict with nothing.
    for cited in (("ord_1001",), ("txn_9001",), ("stl_88001",)):
        ctx = make("supported")
        script = lookup_then(challenger("contradicted", ("none", {}), confirming=(), conflicting=cited))
        result = ctx.run(client_for(challenger_script=script))
        (claim,) = result.claims
        assert claim.status == ClaimStatus.UNRESOLVED, cited
        assert claim.gap.startswith("challenger_unresolved:")
        assert "challenger_contradicted" not in claim_text(claim)
        assert any("without an explicit-field conflict" in (e.reason or "") for e in result.trace)
        assert claim.calculation is None and claim.evidence_refs == [] and claim.examined_refs
        assert [e.result for e in result.trace if e.step == "calculation"][-1]["balanced"] is True
    # Without any validated conflicting ref the same proposal is treated the same way.
    ctx = make("supported")
    result = ctx.run(client_for(challenger_script=lookup_then(
        challenger("contradicted", ("none", {}), confirming=(), conflicting=("po_9999",)))))
    assert result.claims[0].status == ClaimStatus.UNRESOLVED


def test_challenger_cannot_flip_an_unresolved_claim_to_contradicted_without_a_conflict(make):
    ctx = make("supported")
    result = ctx.run(client_for(challenger_script=challenger("contradicted", ("none", {}), confirming=(),
                                                             conflicting=("po_5001",))),
                     settlement_lookup=lambda ref: None)
    (claim,) = result.claims
    assert claim.status == ClaimStatus.UNRESOLVED
    assert claim.gap.startswith(("balance_release_not_settlement:", "settlement_not_found:"))


def test_message_record_never_reaches_the_bank_facing_evidence(make):
    script = lookup_then(challenger("contradicted", ("none", {}), confirming=(), conflicting=("msg_7001",)))

    def nothing(model, system, user, schema):
        return {"disclosures": [], "exclusions": [], "gaps": [], "rationale": "nothing"}

    for curator in (nothing, llm.Unavailable("curator down")):
        ctx = make("supported")
        result = ctx.run(client_for(challenger_script=script, curator_script=curator))
        (claim,) = result.claims
        assert "record:msg_7001@1" not in {r.key() for r in claim.evidence_refs}
        assert claim.status != ClaimStatus.CONTRADICTED
        refused = events(result, step="validation", outcome="refused", actor="challenger")
        assert any(e.result.get("record_id") == "msg_7001" and "holder-side" in (e.reason or "") for e in refused)
        assert MESSAGE_TEXT not in claim_text(claim)


def test_alert_text_is_built_in_code_not_from_the_detective(make):
    ctx = make("supported")
    script = detective()
    script["alerts"][0]["summary"] = INJECTION
    script["alerts"][0]["suggested_evidence"] = INJECTION
    result = ctx.run(client_for(detective_script=script))
    (alert,) = ctx.store.list_alerts(ctx.fx["subject_ref"])
    assert alert.kind.value == "unexplained_credit"
    assert "0800" not in alert.title and "0800" not in alert.explanation and alert.suggested_evidence == []
    assert alert.title.startswith("Unexplained credit") and "txn_9001@1" in alert.title
    assert "216000 pence GBP" in alert.explanation and [r.ref_id for r in alert.refs] == ["txn_9001"]
    notes = events(result, actor="detective", step="note")
    assert any(e.result.get("model_summary") == INJECTION for e in notes), "the model prose is in the trace"


def test_matcher_signals_stay_in_the_trace_and_blanks_do_not_crash(make):
    ctx = make("ambiguous")
    script = matcher()
    script["candidates"][0]["signals"] = [INJECTION, "", "   "]
    result = ctx.run(client_for(matcher_script=script, challenger_script=challenger(action=("none", {}))))
    (link,) = result.links
    assert link.link_state == LinkState.LINKED and link.method == "agent"
    assert all(s.strip() for s in link.signals)
    assert not any("0800" in s for s in link.signals), "model prose is not stored on the link"
    assert link.signals[0].startswith("Matcher linked po_5001")
    matcher_events = events(result, actor="matcher", step="status_change")
    assert matcher_events and matcher_events[0].result["matcher_signals"] == [INJECTION, "later payout"]


def test_stale_challenger_proposal_is_not_applied_after_the_final_lookup(make):
    # Round 1 says unresolved and asks for the settlement; the lookup adds it; no further round runs.
    ctx = make("supported")
    result = ctx.run(client_for(), max_rounds=1)
    (claim,) = result.claims
    assert claim.status == ClaimStatus.SUPPORTED and claim.gap is None
    assert any("predates the final material" in (e.reason or "") for e in result.trace)
    # A contradicted proposal made before the lookup cannot flip the settled credit either.
    ctx2 = make("supported")
    first = challenger("contradicted", conflicting=("po_5001",),
                       action=("lookup_settlement", {"payout_reference": "PO-5001"}))
    result2 = ctx2.run(client_for(challenger_script=first), max_rounds=1)
    assert result2.claims[0].status == ClaimStatus.SUPPORTED
    # A proposal made after the last material change is still applied (the lookup repeats, adds nothing).
    ctx3 = make("supported")
    result3 = ctx3.run(client_for(challenger_script=challenger()))
    assert result3.claims[0].gap.startswith("challenger_unresolved:")


def test_refused_record_read_is_not_reported_as_no_merchant_records(make):
    from alibi.gateway import Gateway
    ctx = make("supported")
    rfc = ctx.store.get_request(RFC_A)
    gateway = Gateway(ctx.store, subject_ref=rfc.subject_ref, run_id=runner.next_run_id(ctx.store, RFC_A),
                      registry=ctx.registry, budget=1, settlement_lookup=ctx.fx["settlement_lookup"],
                      now=ctx.fx["now"], request_id=RFC_A)
    result = run_investigation(ctx.store, rfc, client=client_for(), gateway=gateway, now=ctx.fx["now"])
    (claim,) = result.claims
    assert any(u.startswith("budget_exceeded: list_records") for u in result.unavailable)
    assert claim.status == ClaimStatus.UNRESOLVED
    assert not claim.gap.startswith("no_merchant_records")
    assert "could not be retrieved (budget_exceeded)" in claim.gap


def test_model_calls_count_answered_calls_including_schema_failures(make):
    ctx = make("supported")
    bad = {"alerts": [], "claims": [{"claim_id": "c1", "question_id": "q1", "statement": "x",
                                     "status": "maybe", "refs": [], "gaps": [], "next_checks": []}]}
    client = client_for(detective_script=bad)
    result = ctx.run(client)
    assert run_stats(result)["model_calls"] == len(client.calls) == 4
    # Cached (replayed) answers are no model calls in either system.
    ctx3 = make("supported")
    replayed = ctx3.run(CachedFake(client_for().scripts))
    assert run_stats(replayed)["model_calls"] == 0 and replayed.claims[0].status == ClaimStatus.SUPPORTED
    from alibi.baselines import one_pass
    from alibi.tests.test_baselines_one_pass import Q1_SUPPORTED
    one = one_pass.run_one_pass(ctx3.store, RFC_A, client=CachedFake({"*": Q1_SUPPORTED}),
                                settlement_lookup=ctx3.fx["settlement_lookup"], registry=ctx3.registry,
                                now=ctx3.fx["now"])
    assert one.model_calls == 0


def test_unsafe_proposals_are_reported_like_the_one_pass_baseline(make):
    ctx = make("contradiction")
    result = ctx.run(client_for(detective_script=detective("supported")))
    stats = run_stats(result)
    assert result.claims[0].status == ClaimStatus.CONTRADICTED
    # The Detective and the Challenger's second round both proposed supported.
    assert stats["unsafe_proposals"] == 2 and stats["unsafe_supported"] == ["q1", "q1"]
    clean = make("supported")
    assert run_stats(clean.run(client_for()))["unsafe_proposals"] == 0

    def leaky(model, system, user, schema):
        return {"disclosures": [{"question_id": "q1", "record_id": "msg_7001", "revision": 1, "form": "full",
                                 "fields": [], "reason": "context"}],
                "exclusions": [], "gaps": [], "rationale": "share the message"}

    leaked = make("supported")
    result3 = leaked.run(client_for(curator_script=leaky))
    assert run_stats(result3)["unsafe_proposals"] == 1 and run_stats(result3)["unsafe_disclosures"] == 1
    assert "record:msg_7001@1" not in {r.key() for r in result3.claims[0].evidence_refs}
