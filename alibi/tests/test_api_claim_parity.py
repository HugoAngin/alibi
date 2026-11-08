"""The public ``api.run_investigation`` forwards the holder's untrusted claim in the one-pass
modes as it does in rules mode, and a validated payout and settlement establish the source of
funds when a covered component record is missing, without claiming a breakdown."""
from __future__ import annotations

import pytest

from agents.data import api as data_api
from alibi import adapter, api
from alibi.core.schemas import ClaimStatus
from alibi.llm.fake import FakeClient

RFC_A, TXN = "rfc-0001-01", "txn-0001-004"


def _control() -> dict:
    return data_api.without_gold(data_api.load_case(1))


def _single_credit(number: int = 8) -> tuple[dict, str, str]:
    """A public single-credit case that the offline one-pass engine can answer; its holder
    claim is replaced by each test, so its gold expectation is never used."""
    case = data_api.without_gold(data_api.load_case(number))
    rfc = next(r for r in case["rfcs"] if len(r["txn_ids"]) == 1)
    return case, rfc["request_id"], rfc["txn_ids"][0]


def _through_api(api_mode, seen):
    """Replace the adapter's dispatch with a direct ``api.run_investigation`` call on the same
    seeded store, recording each result, so the API path itself is what answers."""
    fake = _client()   # always offline: the adapter's rules mode passes no client

    def run(world, mode, rfc, now, client):
        common = dict(registry=world.registry, now=now, **world.connectors())
        result = api.run_investigation(world.store, rfc.request_id, audience=rfc.audience,
                                       mode=api_mode, client=fake,
                                       holder_claim=world.case.get("holder_claim"), **common)
        seen[rfc.request_id] = {c.question_id: c for c in result.claims}
        return result, None
    return run


def _client():
    return FakeClient({"*": {"claims": [], "disclosures": [], "rationale": "No proposal."}})


@pytest.mark.parametrize("api_mode", ["one_pass", "single_call", "rules"])
def test_api_forwards_the_untrusted_stated_breakdown(api_mode, tmp_path, monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(adapter, "_run", _through_api(api_mode, seen))
    case, request_id, txn_id = _single_credit()
    case["holder_claim"] = None
    adapter.run_case(case, "rules", root=tmp_path / "truth", client=_client())
    truth = seen[request_id]["q.breakdown"]
    assert truth.status == ClaimStatus.SUPPORTED
    minor = dict(truth.facts["breakdown_minor"])
    wrong = {"sales": minor["sales"] + 100, "refunds": minor["refunds"], "fees": minor["fees"],
             "net": minor["net"]}

    seen.clear()
    case["holder_claim"] = {"txn_id": txn_id, "stated_minor": wrong}
    adapter.run_case(case, "rules", root=tmp_path / "wrong", client=_client())
    claim = seen[request_id]["q.breakdown"]
    assert claim.status == ClaimStatus.CONTRADICTED
    assert claim.facts == {"breakdown_minor": minor}
    assert {r.key() for r in claim.evidence_refs} == {r.key() for r in truth.evidence_refs}
    assert seen[request_id]["q.source_of_funds"].status == ClaimStatus.SUPPORTED

    seen.clear()
    case["holder_claim"] = {"txn_id": txn_id, "stated_minor": minor}
    adapter.run_case(case, "rules", root=tmp_path / "same", client=_client())
    assert seen[request_id]["q.breakdown"].status == ClaimStatus.SUPPORTED


def _missing_component(case: dict, *, amount_delta: int = 0, self_supplied: bool = False) -> str:
    """Drop one fee the case's payout covers from every merchant feed, so the payout cites a
    record that never arrived; optionally change the payout's amount."""
    payouts = [e["event"] for e in case["merchant_lookup"] + case["merchant_deliveries"]
               if e["event"].get("kind") == "payout" and e["event"].get("covers_record_ids")]
    covered = set(payouts[0]["covers_record_ids"])
    fee = next(e["event"]["record_id"] for e in case["merchant_deliveries"]
               if e["event"]["record_id"] in covered and e["event"]["kind"] == "fee")
    case["merchant_deliveries"] = [e for e in case["merchant_deliveries"]
                                   if e["event"]["record_id"] != fee]
    for payout in payouts:
        payout["amount_minor"] += amount_delta
    return fee


def _rules(case, root):
    predictions = adapter.run_case(case, "rules", root=root)
    return next(p for p in predictions if p["request_id"] == RFC_A)["claims"]


def test_validated_payout_establishes_the_source_without_a_breakdown(tmp_path):
    case = _control()
    fee = _missing_component(case)
    claims = _rules(case, tmp_path)
    source, breakdown = claims["q.source_of_funds"], claims["q.breakdown"]
    assert source["status"] == "supported"
    assert source["calculation"] is None          # no component arithmetic is claimed
    assert set(source["evidence_refs"]) == {"bank:txn-0001-004", "merchant:rec-0001-021"}
    assert source["facts"]["payout_references"] == ["PO-0001-01"]
    assert breakdown["status"] == "unresolved"
    assert breakdown["gap"] == "missing_cited_record"
    assert breakdown["evidence_refs"] == [] and breakdown["facts"] == {}
    assert fee not in " ".join(source["evidence_refs"])


def test_missing_component_with_a_different_payout_amount_stays_unresolved(tmp_path):
    case = _control()
    _missing_component(case, amount_delta=500)
    claims = _rules(case, tmp_path)
    assert claims["q.source_of_funds"]["status"] == "unresolved"
    assert claims["q.breakdown"]["status"] == "unresolved"
