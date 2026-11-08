"""The rules reference reproduces gold on the clean family, falls short on hidden_reference, and
reports per-family results. Expected counts are derived from the loaded case set."""
from __future__ import annotations

import json

from alibi.eval import rules_reference, scorer
from alibi.eval.common import GAP_CODES, load_dev_cases

CASES = load_dev_cases()
ROWS = rules_reference.predictions_for(CASES)
SCORE = scorer.run(CASES, [scorer.build_system("rules_reference", ROWS)])[0]

N = len(CASES)
N_REQUESTS = sum(len(c["rfcs"]) for c in CASES)
FAMILIES = sorted({c["family"] for c in CASES})
N_CLEAN = sum(1 for c in CASES if c["family"] == "clean")


def row(number: int, request_id: str) -> dict:
    return next(r for r in ROWS if r["case_number"] == number and r["request_id"] == request_id)


def test_reproduces_gold_on_the_clean_family():
    clean = [c for c in CASES if c["family"] == "clean"]
    assert clean
    for case in clean:
        gold = case["gold"]
        for rfc in case["rfcs"]:
            pred = row(case["case_number"], rfc["request_id"])
            assert pred["failure"] is None
            assert pred["links"][0]["state"] == gold["links"][0]["state"]
            assert set(pred["links"][0]["payout_record_ids"]) == set(gold["links"][0]["payout_record_ids"])
            for qid, answer in gold["answers"][rfc["request_id"]].items():
                claim = pred["claims"][qid]
                assert claim["status"] == answer["status"], (case["case_number"], qid)
                assert claim["gap"] == answer["gap"], (case["case_number"], qid)
                assert claim["facts"] == answer["facts"], (case["case_number"], qid)
                assert set(claim["evidence_refs"]) == set(answer["evidence_sets"][0]), (case["case_number"], qid)
    clean_scores = [c for c in SCORE.cases if c.family == "clean"]
    assert all(c.safe_complete for c in clean_scores) and len(clean_scores) == N_CLEAN


def test_per_family_table_lists_every_family():
    table = scorer.family_table([SCORE])
    assert {"demo", "clean", "hidden_reference"} <= set(FAMILIES)
    for family in FAMILIES:
        assert f"| {family} |" in table
    assert f"| clean | {N_CLEAN} | {N_CLEAN}/{N_CLEAN} (1.000) |" in table
    assert SCORE.metrics["cases"] == N and SCORE.metrics["families"] == len(FAMILIES)


def test_does_not_score_100_percent_overall():
    """The fixed strategy reaches gold on the clean family but not on hidden_reference, whose
    lookup-only payouts are not named in any pushed message. That gap is Alibi's headroom."""
    clean = [c for c in SCORE.cases if c.family == "clean"]
    assert clean and all(c.safe_complete for c in clean)
    missed_hidden = [c.case_number for c in SCORE.cases
                     if c.family == "hidden_reference" and not c.safe_complete]
    assert SCORE.metrics["safe_complete"] < SCORE.metrics["cases"], (
        f"rules reference reaches gold on all {SCORE.metrics['cases']} cases; "
        f"hidden_reference cases missed: {missed_hidden}")


def test_emits_every_pinned_gap_code():
    seen = {claim["gap"] for r in ROWS for claim in r["claims"].values() if claim["gap"]}
    assert seen == set(GAP_CODES)


def test_fixed_retrieval_strategy_and_counts():
    one = row(1, "rfc-0001-01")
    assert one["retrieval"]["strategy"] == rules_reference.STRATEGY
    assert one["retrieval"]["lookups"] == 1 and one["cost"]["tool_calls"] == 1
    assert one["retrieval"]["observed_inputs"] > 20 and one["cost"]["model_calls"] == 0
    assert row(3, "rfc-0003-01")["retrieval"]["lookups"] == 0
    assert row(17, "rfc-0017-01")["retrieval"]["lookups"] == 0  # the release names no reference
    lookups = sum(r["retrieval"]["lookups"] for r in ROWS)
    assert 0 < lookups < N_REQUESTS  # at most one lookup per request, and not every request has one
    assert SCORE.metrics["lookups"] == lookups and SCORE.metrics["model_calls"] == 0


def test_contradiction_and_pending_cases():
    two = row(2, "rfc-0002-01")
    assert two["links"][0]["state"] == "unmatched"
    assert two["claims"]["q.source_of_funds"] == {**two["claims"]["q.source_of_funds"],
                                                   "status": "unresolved",
                                                   "gap": "settlement_pending_at_booking",
                                                   "evidence_refs": []}
    assert two["package"]["attachments"] == [] and two["package"]["empty"] is True
    assert any(e["ref"] == "merchant:rec-0002-021" for e in two["exclusions"])
    eight = row(8, "rfc-0008-01")
    assert eight["claims"]["q.breakdown"]["status"] == "contradicted"
    assert eight["claims"]["q.source_of_funds"]["status"] == "supported"
    period = row(1, "rfc-0001-02")["claims"]["q.sales_refunds_in_period"]
    assert period["facts"] == {"sales_minor": 240000, "refunds_minor": 18000, "sale_count": 8, "refund_count": 2}


def test_references_in_messages():
    assert rules_reference.references_in("Withdrawal PO-0001-01 has been requested.", set()) == ["PO-0001-01"]
    assert rules_reference.references_in("Released as REL-0001-01, see PO-0001-01 and PO-0001-01.",
                                         {"REL-0001-01"}) == ["PO-0001-01"]
    assert rules_reference.references_in("Nothing here. 2026-10-26 is a date.", set()) == []


def test_cli_writes_predictions_and_prints_the_results_line(tmp_path, capsys):
    out = tmp_path / "rules.jsonl"
    assert rules_reference.main(["--out", str(out), "--families", "clean"]) == 0
    captured = capsys.readouterr()
    clean_requests = sum(len(c["rfcs"]) for c in CASES if c["family"] == "clean")
    assert f"wrote {clean_requests} predictions for {N_CLEAN} cases" in captured.err
    lines = [line for line in captured.out.splitlines() if line.strip()]
    last = json.loads(lines[-1])
    assert list(last) == list(scorer.RESULT_KEYS) and last["safe_complete"] == N_CLEAN
    assert f"| clean | {N_CLEAN} | {N_CLEAN}/{N_CLEAN} (1.000) |" in captured.out
    assert len(out.read_text(encoding="utf-8").splitlines()) == 1 + clean_requests  # header plus predictions
