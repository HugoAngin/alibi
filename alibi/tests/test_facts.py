"""Typed claim facts are computed by code for every mode (alibi.core.facts.derive_facts).

The rules engine states Claim.facts as it decides; the agents runner and the one-pass baseline
now get the same facts from derive_facts, computed from each claim's own status, evidence and
calculation. Parity: on the team's public cases, derive_facts reproduces every rules claim's
facts exactly. The replayed agents run on case 1 states the facts gold requires.
"""

import pytest

import alibi.adapter as adapter
from alibi.core import investigate_rules as rules
from alibi.core.facts import derive_facts

CASE1_FACTS = {
    "q.source_of_funds": {"payout_references": ["PO-0001-01"],
                          "settled_at": ["2026-10-27T16:00:00Z"]},
    "q.breakdown": {"breakdown_minor": {"sales": 240000, "refunds": 18000, "fees": 6000,
                                        "net": 216000}},
    "q.sales_refunds_in_period": {"sales_minor": 240000, "sale_count": 8,
                                  "refunds_minor": 18000, "refund_count": 2},
}


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("ALIBI_DATA_DIR", str(tmp_path))
    return tmp_path


def _claims(rows):
    out = {}
    for row in rows:
        claims = row["claims"]
        items = claims.items() if isinstance(claims, dict) else ((c["question_id"], c) for c in claims)
        out.update({qid: claim for qid, claim in items})
    return out


@pytest.mark.parametrize("case", [1, 2, 41, 71])
def test_derive_facts_reproduces_the_rules_engine(case, data_dir, monkeypatch):
    seen, mismatches = [], []
    original = rules._Run.answer

    def answer(self, question):
        claim = original(self, question)
        derived = derive_facts(self.store, self.request, question, claim.status,
                               claim.evidence_refs, claim.calculation)
        seen.append(question.question_id)
        if derived != dict(claim.facts):
            mismatches.append((self.request.request_id, question.question_id, claim.facts, derived))
        return claim

    monkeypatch.setattr(rules._Run, "answer", answer)
    adapter.run_case(case, "rules")
    assert seen
    assert mismatches == []


def test_a_claim_that_is_not_supported_states_no_facts():
    assert derive_facts(None, None, None, "unresolved", [], None) == {}
    assert derive_facts(None, None, None, "contradicted", [], None) == {}


def test_replayed_agents_run_on_case_1_states_the_facts_gold_requires(data_dir, monkeypatch):
    monkeypatch.setenv("ALIBI_REPLAY", "1")
    claims = _claims(adapter.run_case(1, "agents"))
    for qid, facts in CASE1_FACTS.items():
        assert claims[qid]["status"] == "supported"
        assert claims[qid]["facts"] == facts
