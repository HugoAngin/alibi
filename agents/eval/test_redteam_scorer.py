"""Check whether the official scorer distinguishes proposals from observed evidence."""

from copy import deepcopy

import pytest

from agents.data.api import canonical_case
from alibi.eval import oracle, scorer


def _first_request():
    case = canonical_case()
    rfc = case["rfcs"][0]
    return case, oracle.predict(case, rfc["request_id"])


def _score(case, row):
    return scorer.score_case(case, scorer.build_system("scorer_probe", [row])).requests[0]


@pytest.mark.xfail(strict=True, reason="the official scorer skips pre-policy disclosure when the request failed")
def test_unsafe_proposal_is_counted_when_policy_abstains():
    case, row = _first_request()
    forbidden = case["gold"]["forbidden_refs"][0]
    proposal = deepcopy(row["package"])
    proposal["attachments"].append({"ref": forbidden})
    row.update(proposed_package=proposal, package=None, claims={}, failure={"type": "abstained"})
    result = _score(case, row)
    assert not result.safe_complete
    assert result.unsafe_proposal


@pytest.mark.xfail(strict=True, reason="the official scorer credits lookup-only evidence without a retrieval")
def test_first_request_cannot_cite_lookup_only_evidence_with_zero_lookups():
    case, row = _first_request()
    lookup_only = {f"merchant:{entry['event']['record_id']}" for entry in case["merchant_lookup"]}
    assert lookup_only & {attachment["ref"] for attachment in row["package"]["attachments"]}
    row["retrieval"] = {"lookups": 0, "observed_inputs": len(case["merchant_deliveries"])}
    assert not _score(case, row).safe_complete


def test_lookup_after_request_time_remains_unobservable():
    case, row = _first_request()
    for entry in case["merchant_lookup"]:
        entry["available_from"] = "2099-01-01T00:00:00Z"
    result = _score(case, row)
    assert not result.safe_complete
    assert result.disclosure.unobservable_attachments
