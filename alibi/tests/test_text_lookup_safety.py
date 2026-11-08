"""Public issuer-text retrieval preserves private revision dependencies and scope."""
import json

import pytest

from agents.data.api import load_case, without_gold
from alibi import api
from alibi.adapter import _World, _run
from alibi.connectors.case_source import CaseClock, parse_time
from alibi.core.ingest import import_self_supplied
from alibi.core.schemas import ClaimStatus, SigningDomain
from alibi.gateway import Gateway, ScopeError


PAYOUT = "rec-0041-020"
CLUE = "rec-0041-022"


@pytest.mark.parametrize("pending, corrected", [(True, PAYOUT), (True, CLUE), (False, CLUE)])
def test_text_dependency_correction_invalidates_claim_and_consent(tmp_path, pending, corrected):
    case = without_gold(load_case(41))
    if pending:
        event = next(d["event"] for d in case["merchant_lookup"]
                     if d["event"]["record_id"] == PAYOUT)
        event.update(status="pending", settled_at=None)
    at = parse_time(case["rfcs"][0]["issued_at"], "at")
    world = _World(case, tmp_path / "world")
    try:
        world.seed(CaseClock(case, at))
        rfc = world.store.get_request(case["rfcs"][0]["request_id"])
        result, _ = _run(world, "rules", rfc, at, None)
        want = ClaimStatus.UNRESOLVED if pending else ClaimStatus.SUPPORTED
        assert all(c.status == want for c in result.claims)
        assert all(CLUE in {ref.ref_id for ref in c.examined_refs} for c in result.claims)
        if pending:
            assert all(PAYOUT in {ref.ref_id for ref in c.examined_refs} for c in result.claims)
            for c in result.claims:
                assert "had not settled when" not in (c.gap_detail or "")
                assert "do not establish settlement" in (c.gap_detail or "")
                assert c.evidence_refs == [] and c.calculation is None
        draft = api.build_review(world.store, rfc.request_id, audience=rfc.audience, now=at)
        approval = api.approve(world.store, draft.draft_id, holder_id="holder_maya", now=at)
        assert CLUE not in json.dumps(draft.body.to_json_dict())
        if pending:
            assert draft.body.attachments == []
        body = world.store.get_source(corrected).to_event().to_json_dict()
        body.update(event_id="public-text-correction-two", revision=2, corrects_revision=1,
                    correction_reason="Public synthetic correction")
        if corrected == PAYOUT:
            body.update(status="settled", effective_at=body["occurred_at"])
        else:
            body["text"] = "The previous withdrawal reference was incorrect; please check it."
        payload = {"event": body, "signature": world.keystore.sign(
            body, kid="merchant_mock", domain=SigningDomain.RECORD).to_json_dict()}
        put = api.ingest_merchant_event(world.store, payload, registry=world.registry, now=at)
        assert {c.claim_id for c in result.claims} <= set(put.invalidated["claims"])
        assert draft.draft_id in put.invalidated["drafts"]
        assert approval.approval_id in put.invalidated["approvals"]
        with pytest.raises(api.ApprovalError):
            api.sign_and_package(world.store, approval.approval_id, keystore=world.keystore, now=at)
    finally:
        world.close()


def test_unsigned_text_cannot_open_new_lookup_scope(tmp_path):
    case = without_gold(load_case(41))
    at = parse_time(case["rfcs"][0]["issued_at"], "at")
    world = _World(case, tmp_path / "scope")
    try:
        world.seed(CaseClock(case, at))
        import_self_supplied(world.store, {"record_id": "unsigned-clue", "kind": "message",
                              "text": "Please look up PO-UNSIGNED-991.",
                              "occurred_at": case["rfcs"][0]["issued_at"]},
                              subject_ref=world.subject, now=at)
        calls = []
        gateway = Gateway(world.store, subject_ref=world.subject, run_id="scope-control",
                          registry=world.registry, now=at,
                          settlement_lookup=lambda ref: calls.append(ref))
        with pytest.raises(ScopeError):
            gateway.lookup_settlement("PO-UNSIGNED-991")
        assert calls == []
    finally:
        world.close()
