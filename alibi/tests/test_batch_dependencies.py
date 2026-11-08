"""Public synthetic batches retain stale-source and exclusive-allocation safeguards."""
import copy
import json
from datetime import timedelta

import pytest

from agents.data.api import load_case, without_gold
from alibi import api
from alibi.adapter import _World, _run, run_case
from alibi.connectors.case_source import CaseClock, parse_time
from alibi.core.schemas import ClaimStatus, SigningDomain


def case9():
    return without_gold(load_case(9))


def payouts(case):
    return [d["event"] for d in case["merchant_deliveries"] if d["event"]["kind"] == "payout"]


@pytest.mark.parametrize("problem", ["wrong_sum", "missing_component"])
def test_examined_batch_component_correction_invalidates_empty_review(tmp_path, problem):
    case = case9()
    covered = payouts(case)[0]["covers_record_ids"]
    target = covered[0]
    if problem == "wrong_sum":
        next(d["event"] for d in case["merchant_deliveries"]
             if d["event"]["record_id"] == target)["amount_minor"] += 1
    else:
        case["merchant_deliveries"] = [d for d in case["merchant_deliveries"]
                                        if d["event"]["record_id"] != covered[-1]]
    at = parse_time(case["rfcs"][0]["issued_at"], "at")
    world = _World(case, tmp_path / problem)
    try:
        world.seed(CaseClock(case, at))
        rfc = world.store.get_request(case["rfcs"][0]["request_id"])
        result, _ = _run(world, "rules", rfc, at, None)
        assert all(c.status == ClaimStatus.UNRESOLVED for c in result.claims)
        assert all(target in {r.ref_id for r in c.examined_refs} for c in result.claims)
        draft = api.build_review(world.store, rfc.request_id, audience=rfc.audience, now=at)
        approval = api.approve(world.store, draft.draft_id, holder_id="holder_maya", now=at)
        assert draft.body.attachments == []
        assert all(c.evidence_refs == [] and c.calculation is None for c in draft.body.claims)
        body = world.store.get_source(target).to_event().to_json_dict()
        body.update(event_id="public-batch-revision-two", revision=2, corrects_revision=1,
                    correction_reason="Synthetic component correction")
        body["amount_pence"] += 2
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


def with_secondary_credit(*, separate_requests):
    case = case9()
    primary = case["rfcs"][0]
    original_id = primary["txn_ids"][0]
    delivery = copy.deepcopy(next(d for d in case["bank_deliveries"]
                                  if d["row"]["txn_id"] == original_id))
    member = payouts(case)[1]
    row = delivery["row"]
    row.update(txn_id="txn-public-secondary", amount_minor=member["amount_minor"],
               payment_reference=member["payout_reference"], description="Individual payout")
    case["bank_deliveries"].append(delivery)
    if separate_requests:
        later = copy.deepcopy(primary)
        later.update(request_id="rfc-public-secondary", audience="bank_b",
                     txn_ids=[row["txn_id"]])
        later["issued_at"] = (parse_time(primary["issued_at"], "at") + timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
        case["rfcs"] = [primary, later]
    else:
        primary["txn_ids"].append(row["txn_id"])
        case["rfcs"] = [primary]
    return case, original_id


def test_secondary_batch_payout_cannot_fund_another_credit_in_same_request(tmp_path):
    case, _ = with_secondary_credit(separate_requests=False)
    (prediction,) = run_case(case, "rules", root=tmp_path)
    assert all(c["status"] == "unresolved" and c["gap"] == "payout_already_linked"
               for c in prediction["claims"].values())
    assert prediction["package"]["attachments"] == []


def test_secondary_batch_payout_cannot_fund_later_other_bank_credit(tmp_path):
    case, earlier_id = with_secondary_credit(separate_requests=True)
    first, second = run_case(case, "rules", root=tmp_path)
    assert all(c["status"] == "supported" for c in first["claims"].values())
    assert all(c["status"] == "unresolved" and c["gap"] == "payout_already_linked"
               for c in second["claims"].values())
    assert second["package"]["attachments"] == []
    assert earlier_id not in json.dumps(second["package"])


def test_agent_link_alternatives_do_not_reserve_unchosen_payouts():
    from types import SimpleNamespace
    from alibi.core.investigate_rules import _allocated_payouts
    from alibi.core.schemas import LinkState
    first, alternate = SimpleNamespace(ref_id="first"), SimpleNamespace(ref_id="alternate")
    link = SimpleNamespace(link_state=LinkState.LINKED, record_ref=first,
                           candidates=[first, alternate], method="agent")
    assert _allocated_payouts(link) == {"first"}
    link.method = "reference"
    assert _allocated_payouts(link) == {"first", "alternate"}
    link.link_state = LinkState.AMBIGUOUS
    assert _allocated_payouts(link) == set()
