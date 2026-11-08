"""One-pass question classification matches the rules engine (public cases and Maya fixtures).

One-pass used the keyword-only ``classify_question``, so the pinned ``q.breakdown`` came out
``unmapped`` and ``q.sales_refunds_in_period`` came out ``several_totals``; both were answered
unresolved whatever the records showed. It now uses ``investigate_rules.classify``, a breakdown
follows the source verdict, and several documented totals are summed per kind. Separately,
``classify_question`` no longer reads "explain how this credit was calculated" as a
source-of-funds question. No network, no Gemini: ``FakeClient`` only.
"""

from __future__ import annotations

from agents.data.api import load_cases
from alibi.adapter import run_case
from alibi.baselines.one_pass import run_one_pass
from alibi.connectors.case_source import map_rfc
from alibi.core.investigate_rules import (
    BREAKDOWN,
    PINNED_QUESTIONS,
    SOURCE_OF_FUNDS,
    classify,
    classify_question,
)
from alibi.core.schemas import ClaimStatus, Question, RequestForContext
from alibi.fixtures import maya
from alibi.llm.fake import FakeClient
from alibi.tests.test_baselines_one_pass import (  # noqa: F401 - ctx is a fixture
    RFC_A,
    ctx,
    gateway_for,
    load,
    name_payout_in_message,
    notes,
    run,
)

SUPPORTED, CONTRADICTED, UNRESOLVED = (ClaimStatus.SUPPORTED, ClaimStatus.CONTRADICTED,
                                       ClaimStatus.UNRESOLVED)
NO_PROPOSAL = {"claims": [], "disclosures": [], "rationale": "x"}
PINNED_A = [{"question_id": "q.source_of_funds",
             "text": "What is the origin of the incoming payment booked on 3 November?"},
            {"question_id": "q.breakdown", "text": "Please show how this amount was calculated."}]


def _rfc_a() -> RequestForContext:
    return RequestForContext.model_validate(maya.load_variant("supported")["rfcs"][0])


# --------------------------------------------------------------------------- classify_question


def test_breakdown_wording_with_explain_or_why_is_not_a_source_question():
    a = _rfc_a()
    for text in ("Please explain how this credit was calculated.",
                 "Explain the breakdown of the £2,160 payment.",
                 "Why does the credit not reconcile to your sales, refunds and fees?",
                 "Explain how txn_9001 was worked out."):
        assert classify_question(text, a).category == "unmapped", text   # keyword view
        assert classify(Question(question_id="q9", text=text), a).category == BREAKDOWN, text
    for text in ("Why did £2,160 arrive at your account on 3 November?",   # "arrive at" is no breakdown cue
                 "Why did £2,160 arrive in your account on 3 November?",
                 "What is the origin of the payment and how was it calculated?",
                 "Where did this credit come from, and how is it made up?",
                 "Explain where this credit came from.",
                 "Please provide supporting documents for the £2,160 deposit (txn_9001)."):
        assert classify_question(text, a).category == SOURCE_OF_FUNDS, text


def test_every_public_question_keeps_its_category():
    """Public cases 1-80: the keyword view still finds every source question, and the full
    classification gives each pinned id its pinned category."""
    seen = 0
    for case in load_cases():
        for raw in case["rfcs"]:
            rfc = map_rfc(raw).body
            rfc = rfc if isinstance(rfc, RequestForContext) else RequestForContext.model_validate(rfc)
            for q in rfc.questions:
                seen += 1
                assert classify(q, rfc).category == PINNED_QUESTIONS[q.question_id], (case["case_number"], q.text)
                if q.question_id == "q.source_of_funds":
                    assert classify_question(q.text, rfc).category == SOURCE_OF_FUNDS, q.text
    assert seen == 174


# --------------------------------------------------------------------------- one-pass, public Case 1


def test_one_pass_public_case1_answers_breakdown_and_period_as_their_own_questions(tmp_path):
    preds = {p["request_id"]: p for p in run_case(1, "one_pass", client=FakeClient({"*": NO_PROPOSAL}),
                                                     root=tmp_path / "case_1")}
    assert all(p["failure"] is None for p in preds.values())
    period = preds["rfc-0001-02"]["claims"]["q.sales_refunds_in_period"]
    assert period["status"] == "supported"
    assert period["facts"] == {"sales_minor": 240000, "sale_count": 8,
                               "refunds_minor": 18000, "refund_count": 2}
    source = preds["rfc-0001-01"]["claims"]["q.source_of_funds"]
    breakdown = preds["rfc-0001-01"]["claims"]["q.breakdown"]
    # The breakdown follows the source verdict; it is never supported on its own.
    if source["status"] == "supported":
        assert breakdown["status"] == "supported"
    else:
        assert breakdown["status"] == "unresolved" and breakdown["facts"] == {}
        assert breakdown["gap"].split(":")[0] == source["gap"].split(":")[0]


# --------------------------------------------------------------------------- one-pass, Maya fixtures


def _pinned_run(ctx, fixture: str):
    fx = load(ctx, fixture)
    name_payout_in_message(ctx, fx)
    rfc = ctx["store"].get_request(RFC_A)
    rfc = rfc.model_copy(update={"questions": [Question.model_validate(q) for q in PINNED_A]})
    out = run_one_pass(ctx["store"], rfc, client=FakeClient({"*": NO_PROPOSAL}),
                       gateway=gateway_for(ctx, fx, RFC_A), now=fx["now"])
    return {c.question_id: c for c in out.result.claims}, out


def test_one_pass_breakdown_reconciles_a_supported_credit(ctx):
    claims, out = _pinned_run(ctx, "supported")
    source, breakdown = claims["q.source_of_funds"], claims["q.breakdown"]
    assert source.status == SUPPORTED
    assert breakdown.status == SUPPORTED and breakdown.gap is None
    assert breakdown.facts == {"breakdown_minor": {"sales": 240000, "refunds": 18000,
                                                   "fees": 6000, "net": 216000}}
    assert breakdown.calculation is not None and breakdown.calculation.balanced is True
    assert {"txn_9001", "po_5001", "ord_1001", "rfd_2001", "fee_3001"} <= {r.ref_id for r in breakdown.evidence_refs}
    # The source verdict is computed once: one link, one candidate-rule note.
    assert len(out.result.links) == 1
    assert len([e for e in notes(out.result, step="validation") if e.tool == "settlement_candidates"]) == 1
    categories = [e.result["category"] for e in notes(out.result, step="note") if "category" in (e.result or {})]
    assert categories == [SOURCE_OF_FUNDS, BREAKDOWN]


def test_one_pass_breakdown_of_a_contradicted_credit_is_unresolved_and_cites_nothing(ctx):
    claims, _ = _pinned_run(ctx, "contradiction")
    source, breakdown = claims["q.source_of_funds"], claims["q.breakdown"]
    assert source.status == CONTRADICTED
    assert breakdown.status == UNRESOLVED
    assert breakdown.gap.startswith("settlement_pending_at_booking")
    assert breakdown.evidence_refs == [] and breakdown.calculation is None and breakdown.facts == {}


def test_one_pass_documented_totals_state_each_kind(ctx):
    fx = load(ctx, "supported")
    rfc_b = ctx["store"].get_request(maya.RFC_B_ID)
    rfc_b = rfc_b.model_copy(update={"questions": [Question(
        question_id="q.sales_refunds_in_period",
        text="Please provide documented marketplace sales and refunds between 01 October 2026 and "
             "31 October 2026.")]})
    out = run_one_pass(ctx["store"], rfc_b, client=FakeClient({"*": NO_PROPOSAL}),
                       gateway=gateway_for(ctx, fx, maya.RFC_B_ID), now=fx["now"])
    (claim,) = out.result.claims
    assert claim.status == SUPPORTED
    assert claim.facts == {"sales_minor": 240000, "sale_count": 4, "refunds_minor": 18000, "refund_count": 1}
    assert "sales total 240000 pence GBP across 4" in claim.statement
    assert "refunds total 18000 pence GBP across 1" in claim.statement
