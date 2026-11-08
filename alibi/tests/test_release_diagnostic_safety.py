"""Public release diagnostics use issuer facts and preserve private revision dependencies."""
import json

import pytest

from agents.data.api import load_case, without_gold
from alibi import api
from alibi.adapter import _World, _run, run_case
from alibi.connectors.case_source import CaseClock, parse_time
from alibi.core.schemas import ClaimStatus, SigningDomain
from alibi.llm.fake import FakeClient


RELEASE_GAP = "balance_release_not_settlement"
NO_PROPOSAL = {"claims": [], "disclosures": [], "rationale": "offline"}


def release_case(change=None):
    case = without_gold(load_case(17))
    release = next(d for d in case["merchant_deliveries"] if d["event"]["kind"] == "balance_release")
    if change == "unrelated":
        release["event"]["message"] = "A support ticket about your seller balance has been opened."
    elif change == "negative":
        release["event"]["message"] = "No funds are available in your seller balance."
    elif change == "conditional":
        release["event"]["message"] = "If the amount is now available in your seller balance, request a withdrawal."
    elif change == "currency":
        release["event"]["currency"] = "EUR"
    elif change == "status":
        release["event"]["status"] = "failed"
    elif change == "untrusted":
        case["merchant_deliveries"].remove(release)
        case["imports"].append({"received_at": release["received_at"], "record": release["event"]})
    return case, release["event"]["record_id"]


@pytest.mark.parametrize("number,gap", [(17, RELEASE_GAP), (18, RELEASE_GAP), (32, "no_merchant_records")])
def test_one_pass_release_uses_observed_evidence_and_fixed_strategy(tmp_path, number, gap):
    client = FakeClient({"*": NO_PROPOSAL})
    predictions = run_case(number, "one_pass", client=client, root=tmp_path / "run")
    assert client.call_count == len(predictions)
    for row in predictions:
        assert row["failure"] is None
        assert row["retrieval"]["lookups"] == 0
        assert all(c["status"] == "unresolved" and c["gap"] == gap for c in row["claims"].values())
        assert all(c["evidence_refs"] == [] for c in row["claims"].values())


@pytest.mark.parametrize("change", ["unrelated", "negative", "conditional", "currency", "status", "untrusted"])
def test_one_pass_rejects_non_release_messages(tmp_path, change):
    case, _ = release_case(change)
    client = FakeClient({"*": NO_PROPOSAL})
    rows = run_case(case, "one_pass", client=client, root=tmp_path / "run")
    assert client.call_count == len(rows)
    for row in rows:
        assert row["failure"] is None
        assert row["retrieval"]["lookups"] == 0
        assert all(c["status"] == "unresolved" and c["gap"] != RELEASE_GAP for c in row["claims"].values())


def correction_invalidates(tmp_path, mode, initially_release):
    case, rid = release_case(None if initially_release else "negative")
    at = parse_time(case["rfcs"][0]["issued_at"], "at")
    world = _World(case, tmp_path / "world")
    client = FakeClient({"*": NO_PROPOSAL})
    try:
        world.seed(CaseClock(case, at))
        rfc = world.store.get_request(case["rfcs"][0]["request_id"])
        result, run = _run(world, mode, rfc, at, client)
        assert all(c.status == ClaimStatus.UNRESOLVED for c in result.claims)
        for claim in result.claims:
            assert (RELEASE_GAP in (claim.gap or "")) == initially_release
            assert rid in {ref.ref_id for ref in claim.examined_refs}
            assert claim.evidence_refs == [] and claim.calculation is None
        if mode == "one_pass":
            assert run.lookups == 0 and run.model_calls == 1
        draft = api.build_review(world.store, rfc.request_id, audience=rfc.audience, now=at)
        approval = api.approve(world.store, draft.draft_id, holder_id="holder_maya", now=at)
        assert draft.body.attachments == []
        assert rid not in json.dumps(draft.body.to_json_dict())
        body = world.store.get_source(rid).to_event().to_json_dict()
        body.update(event_id="public-release-correction-two", revision=2, corrects_revision=1,
                    correction_reason="Public synthetic correction", text=(
                        "No funds are available in your seller balance." if initially_release else
                        "Good news! The amount is now available in your seller balance."))
        payload = {"event": body, "signature": world.keystore.sign(
            body, kid="merchant_mock", domain=SigningDomain.RECORD).to_json_dict()}
        changed = api.ingest_merchant_event(world.store, payload, registry=world.registry, now=at)
        assert {c.claim_id for c in result.claims} <= set(changed.invalidated["claims"])
        assert draft.draft_id in changed.invalidated["drafts"]
        assert approval.approval_id in changed.invalidated["approvals"]
        with pytest.raises(api.ApprovalError):
            api.sign_and_package(world.store, approval.approval_id, keystore=world.keystore, now=at)
        rerun, _ = _run(world, mode, rfc, at, client)
        assert all((RELEASE_GAP in (c.gap or "")) != initially_release for c in rerun.claims)
    finally:
        world.close()


@pytest.mark.parametrize("initially_release", [True, False])
def test_one_pass_release_correction_invalidates_minimal_review(tmp_path, initially_release):
    correction_invalidates(tmp_path, "one_pass", initially_release)
