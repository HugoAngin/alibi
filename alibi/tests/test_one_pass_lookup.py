"""One-pass fixed retrieval looks up a payout reference that an issuer-verified message names,
even when the payout is lookup-only (public cases, Maya fixtures, FakeClient, no network)."""

from __future__ import annotations

from agents.data.api import load_case
from alibi.adapter import run_case
from alibi.core.investigate_rules import MAX_REFERENCE_LOOKUPS
from alibi.core.schemas import ClaimStatus
from alibi.llm.fake import FakeClient
from alibi.tests.test_baselines_one_pass import (  # noqa: F401 - ctx is a fixture
    RFC_A,
    ctx,
    load,
    name_payout_in_message,
    notes,
    run,
)

NO_PROPOSAL = {"claims": [], "disclosures": [], "rationale": "x"}


def _case(n: int, tmp_path) -> dict[str, dict]:
    return {p["request_id"]: p for p in run_case(n, "one_pass", client=FakeClient({"*": NO_PROPOSAL}),
                                                root=tmp_path / f"case_{n}")}


def test_public_case1_source_and_breakdown_resolve_from_the_named_withdrawal(tmp_path):
    preds = _case(1, tmp_path)
    a = preds["rfc-0001-01"]
    assert a["failure"] is None and a["retrieval"]["lookups"] == 1
    source, breakdown = a["claims"]["q.source_of_funds"], a["claims"]["q.breakdown"]
    assert source["status"] == "supported"
    assert source["facts"] == {"payout_references": ["PO-0001-01"], "settled_at": ["2026-10-27T16:00:00Z"]}
    assert breakdown["status"] == "supported"
    assert breakdown["facts"] == {"breakdown_minor": {"sales": 240000, "refunds": 18000, "fees": 6000,
                                                      "net": 216000}}
    period = preds["rfc-0001-02"]["claims"]["q.sales_refunds_in_period"]
    assert period["status"] == "supported"


def test_public_case2_pending_withdrawal_stays_unresolved(tmp_path):
    a = _case(2, tmp_path)["rfc-0002-01"]
    assert a["retrieval"]["lookups"] == 1
    for qid in ("q.source_of_funds", "q.breakdown"):
        assert a["claims"][qid]["status"] == "unresolved"
        assert a["claims"][qid]["gap"].startswith("settlement_pending_at_booking")


def test_public_injection_cases_gain_no_trust_from_message_text(tmp_path):
    """Cases 51-60: the lookups stay bounded and every source verdict equals public gold."""
    for n in range(51, 61):
        gold = load_case(n)["gold"]["answers"]
        for rid, pred in _case(n, tmp_path).items():
            assert pred["retrieval"]["lookups"] <= MAX_REFERENCE_LOOKUPS, n
            for qid, claim in pred["claims"].items():
                if qid == "q.source_of_funds":
                    assert claim["status"] == gold[rid][qid]["status"], (n, rid)


def test_written_references_are_capped_and_dates_or_amounts_are_never_looked_up(ctx):
    fx = load(ctx, "supported")
    name_payout_in_message(ctx, fx, "On 2026-11-03 £2,160.00 was released. Refs ZZ-0001, ZZ-0002 "
                                    "and ZZ-0003 were requested.")
    out = run(ctx, fx, RFC_A, FakeClient({"*": NO_PROPOSAL}))
    asked = [e.args["payment_ref"] for e in notes(out.result, tool="lookup_settlement") if e.args]
    assert out.lookups == MAX_REFERENCE_LOOKUPS == 2
    assert sorted(asked) == ["ZZ-0001", "ZZ-0002"]
    (claim,) = out.result.claims
    assert claim.status == ClaimStatus.UNRESOLVED   # unknown references grant nothing
