"""Public synthetic scorer audit. AUDIT_PATCHED=1 validates the suggested change in memory."""
import os
import sys
import types
from pathlib import Path
import pytest
from alibi.eval import scorer as original
scorer = original
if os.environ.get("AUDIT_PATCHED"):
    scorer = types.ModuleType("alibi.eval.scorer_audit_patch")
    sys.modules[scorer.__name__] = scorer
    exec(compile(Path("proposed_scorer.txt").read_text(), "proposed_scorer.txt", "exec"), scorer.__dict__)

A,B,C,D = ["merchant:"+x for x in "abcd"]
Q1,Q2 = "q.source_of_funds", "q.breakdown"

def evaluate(sets, citations, forbidden=(), extra=(), reverse=False):
    if reverse:
        sets = {q:list(reversed(v)) for q,v in sets.items()}
    answers = {q:{"status":"supported", "gap":None, "facts":{}, "evidence_sets":v} for q,v in sets.items()}
    claims = {q:{"status":"supported", "gap":None, "facts":{}, "evidence_refs":v} for q,v in citations.items()}
    refs = {x for groups in sets.values() for group in groups for x in group} | set(extra)
    rfc = {"request_id":"synthetic-rfc", "audience":"bank_a", "issued_at":"2026-11-01T00:00:00Z"}
    case = {"case_number":1,"family":"synthetic-audit","variant":"tiny","title":"Tiny public synthetic case",
            "merchant_deliveries":[{"received_at":"2026-10-31T00:00:00Z", "event":{"record_id":r.split(":")[1],"revision":1}} for r in refs],
            "merchant_lookup":[],"imports":[],"bank_deliveries":[],"rfcs":[rfc],
            "gold":{"answers":{rfc["request_id"]:answers},"forbidden_refs":list(forbidden)}}
    pred = {"case_number":1,"request_id":rfc["request_id"],"claims":claims,
            "package":{"request_id":rfc["request_id"],"audience":"bank_a","attachments":[{"ref":r,"fields":["revision"]} for r in extra]},"failure":None}
    system = scorer.build_system("synthetic", [pred])
    assert not system.invalid
    return scorer.score_case(case, system)

@pytest.mark.parametrize("reverse", [False, True])
def test_valid_equal_sized_alternative_plus_other_question(reverse):
    result = evaluate({Q1:[[A,B],[C,D]],Q2:[[A,B]]}, {Q1:[C,D],Q2:[A,B]}, reverse=reverse)
    assert result.safe_complete, result.requests[0].all_reasons

@pytest.mark.parametrize("reverse", [False, True])
def test_valid_short_alternative_plus_other_question(reverse):
    result = evaluate({Q1:[[A],[B,C]],Q2:[[B,C]]}, {Q1:[A],Q2:[B,C]}, reverse=reverse)
    assert result.safe_complete, result.requests[0].all_reasons

def test_single_question_redundant_alternatives_still_fail():
    result = evaluate({Q1:[[A],[B]]}, {Q1:[A,B]})
    assert not result.safe_complete
    assert result.requests[0].disclosure.unnecessary_items == 1

def test_unjustified_extra_still_fails():
    result = evaluate({Q1:[[A]]}, {Q1:[A]}, extra=[D])
    assert not result.safe_complete
    assert result.requests[0].disclosure.unnecessary == [D]

def test_forbidden_still_fails():
    result = evaluate({Q1:[[A,B],[C,D]],Q2:[[A,B]]}, {Q1:[C,D],Q2:[A,B]}, forbidden=[D])
    assert not result.safe_complete
    assert result.requests[0].disclosure.forbidden_refs == [D]

def test_incomplete_question_citations_still_fail():
    result = evaluate({Q1:[[A,B],[C,D]],Q2:[[A,B]]}, {Q1:[C],Q2:[A,B]}, extra=[D])
    assert not result.safe_complete
    assert not result.requests[0].questions[0].citation_ok

def test_one_question_either_alternative_passes():
    for refs in ([A,B],[C,D]):
        assert evaluate({Q1:[[A,B],[C,D]]}, {Q1:refs}).safe_complete
