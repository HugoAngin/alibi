"""Approval tests: an approval binds one exact body, recipient, request, policy version and set
of source revisions for a short window, and is used at most once."""

from datetime import timedelta
from types import SimpleNamespace

import pytest

from alibi.core import canon
from alibi.core.approval import (
    DEFAULT_APPROVAL_TTL,
    ApprovalError,
    approve,
    check_approval,
    consume,
    current_revisions,
    require_valid,
)
from alibi.core.package import PackageError, build_body, cited_refs, make_draft, sign_package
from alibi.core.policy import compile_policy, parse_rule_text
from alibi.core.schemas import (
    ApprovalStatus,
    DenyRule,
    DraftStatus,
    EvidenceRecord,
    EvidenceRef,
    PackageBody,
    Policy,
)
from alibi.core.store import Store
from alibi.tests.test_package import (
    HOLDER,
    NOW,
    claim_a,
    claims_b,
    ledger,
    raw_payload,
    rfc_a,
    rfc_b,
    settlement,
)

POLICY = Policy(policy_id="pol_maya", version=1, subject_ref="subj_maya",
                rules=[DenyRule(rule_id="r_no_messages", kinds=["message"])], confirmed=True,
                created_at=NOW)
LATER = NOW + timedelta(minutes=5)


@pytest.fixture
def ev(keystore):
    return ledger(keystore)


@pytest.fixture
def case(ev):
    """RFC A's draft and the holder's approval of it, both at NOW."""
    request, claims = rfc_a(), [claim_a(ev)]
    compiled = compile_policy(POLICY)
    body, review = build_body(request, claims, list(ev.values()), compiled, now=NOW)
    draft = make_draft(request, body, review, policy_version=compiled.policy_version,
                       source_revisions=cited_refs(claims), now=NOW)
    approval = approve(draft, holder_id=HOLDER, now=NOW)
    revisions = {ref.token(): ref.revision for ref in approval.source_revisions}
    return SimpleNamespace(request=request, body=body, draft=draft, approval=approval,
                           revisions=revisions, policy_version=compiled.policy_version)


def check(case, approval=None, **over):
    args = {"body": case.body, "audience": "bank_a", "request_id": "rfc_a_001",
            "policy_version": case.policy_version, "revisions": case.revisions, "now": LATER}
    args.update(over)
    return check_approval(approval or case.approval, **args)


def sign(case, keystore, approval=None, body=None, **over):
    args = {"policy_version": case.policy_version, "revisions": case.revisions, "now": LATER}
    args.update(over)
    return sign_package(body or case.body, approval or case.approval, keystore, **args)


def test_approval_binds_body_recipient_request_policy_and_sources(case):
    approval = case.approval
    assert approval.status == ApprovalStatus.PENDING
    assert approval.body_sha256 == canon.digest(case.body) == case.draft.body_sha256
    assert (approval.audience, approval.request_id) == ("bank_a", "rfc_a_001")
    assert approval.policy_version == "pol_maya@v1"
    assert (approval.holder_id, approval.draft_id) == (HOLDER, case.draft.draft_id)
    assert approval.approved_at == NOW and approval.expires_at == NOW + DEFAULT_APPROVAL_TTL
    assert approval.approval_id == canon.make_id("apr", case.draft.draft_id, HOLDER,
                                                 "2026-11-07T12:00:00Z")
    keys = {ref.key() for ref in approval.source_revisions}
    assert {a.ref.key() for a in case.body.attachments} <= keys
    assert {"record:stl_88001@1", "txn:txn_9001@1"} <= keys
    assert check(case) == []
    require_valid(case.approval, body=case.body, audience="bank_a", request_id="rfc_a_001",
                  policy_version=case.policy_version, revisions=case.revisions, now=LATER)


def test_body_change_invalidates_the_approval(case):
    data = case.body.to_json_dict()
    data["claims"][0]["statement"] = "Edited after the holder approved it."
    problems = check(case, body=PackageBody.model_validate(data))
    assert any("changed after it was approved" in p for p in problems)

    data = case.body.to_json_dict()
    data["attachments"][0]["content"]["amount_pence"] += 1  # a raw dict body is digested as is
    assert any("changed after it was approved" in p for p in check(case, body=data))


def test_different_audience_invalidates_the_approval(case):
    problems = check(case, audience="bank_b")
    assert any("recipient bank_a, not bank_b" in p for p in problems)


def test_policy_version_change_invalidates_the_approval(case):
    newer = compile_policy(POLICY.model_copy(update={"version": 2})).policy_version
    problems = check(case, policy_version=newer)
    assert problems == ["the disclosure policy changed after approval (approved pol_maya@v1, "
                        "now pol_maya@v2)"]


def test_source_revision_change_invalidates_the_approval(case):
    corrected = {**case.revisions, "record:stl_88001": 2}
    assert check(case, revisions=corrected) == [
        "source record:stl_88001 changed after approval (approved revision 1, now revision 2)"]
    missing = {**case.revisions, "record:stl_88001": None}
    assert check(case, revisions=missing) == [
        "source record:stl_88001 is missing (approved revision 1)"]
    del missing["record:stl_88001"]
    assert check(case, revisions=missing) == [
        "source record:stl_88001 is missing (approved revision 1)"]


def test_expiry_invalidates_the_approval(case):
    assert check(case, now=NOW + DEFAULT_APPROVAL_TTL - timedelta(seconds=1)) == []
    assert check(case, now=NOW + DEFAULT_APPROVAL_TTL) == [
        "the approval expired at 2026-11-07T12:15:00Z"]
    assert check(case, now=NOW - timedelta(seconds=1)) == [
        "the approval is not valid before 2026-11-07T12:00:00Z"]


def test_approval_cannot_be_reused_after_consumption(case, keystore):
    package = sign(case, keystore)
    used = consume(case.approval, package_id=package.package_id)
    assert used.status == ApprovalStatus.CONSUMED and used.package_id == package.package_id

    problems = check(case, approval=used)
    assert problems == [f"the approval was already used for package {package.package_id}; "
                        "approve the package again"]
    with pytest.raises(ApprovalError):
        sign(case, keystore, approval=used)
    with pytest.raises(ApprovalError):
        consume(used, package_id=package.package_id)


def test_invalidated_approval_is_refused(case):
    stale = case.approval.model_copy(update={
        "status": ApprovalStatus.INVALIDATED,
        "invalidated_reason": "record:stl_88001 superseded by revision 2"})
    assert check(case, approval=stale) == [
        "the approval was invalidated: record:stl_88001 superseded by revision 2"]


def test_only_a_pending_draft_can_be_approved(case):
    for status in (DraftStatus.APPROVED, DraftStatus.PACKAGED, DraftStatus.INVALIDATED):
        with pytest.raises(ApprovalError) as info:
            approve(case.draft.model_copy(update={"status": status}), holder_id=HOLDER, now=NOW)
        assert info.value.problems == [
            f"only a pending draft can be approved; this draft is {status.value}"]
    tampered = case.draft.model_copy(update={"body_sha256": "0" * 64})
    with pytest.raises(ApprovalError) as info:
        approve(tampered, holder_id=HOLDER, now=NOW)
    assert "the draft body does not match its recorded digest" in info.value.problems
    with pytest.raises(ApprovalError) as info:
        approve(case.draft, holder_id=HOLDER, now=case.body.expires_at)
    assert "the package body has already expired" in info.value.problems


def test_sign_package_refuses_an_invalid_approval(case, keystore):
    newer = compile_policy(POLICY.model_copy(update={"version": 2})).policy_version
    with pytest.raises(ApprovalError) as info:
        sign(case, keystore, policy_version=newer, revisions={**case.revisions,
                                                              "record:po_5001": 2})
    problems = info.value.problems
    assert any("disclosure policy changed" in p for p in problems)
    assert any("record:po_5001 changed after approval" in p for p in problems)
    with pytest.raises(ApprovalError) as info:
        sign(case, keystore, now=NOW + timedelta(hours=1))
    assert any("expired" in p for p in info.value.problems)


def test_a_second_request_cannot_reuse_the_first_approval(case, ev, keystore):
    request_b = rfc_b()
    body_b, _ = build_body(request_b, claims_b(ev), list(ev.values()), POLICY, now=NOW)
    problems = check(case, body=body_b, audience="bank_b", request_id="rfc_b_001")
    assert any("changed after it was approved" in p for p in problems)
    assert any("recipient bank_a, not bank_b" in p for p in problems)
    assert any("request rfc_a_001, not rfc_b_001" in p for p in problems)
    with pytest.raises(ApprovalError):
        sign(case, keystore, body=body_b)


def test_withheld_cited_source_still_binds_the_approval(ev):
    # The answer counts the refund, which the policy keeps out of the body: a correction to it
    # must still invalidate the approval, so make_draft insists on the cited revisions.
    request, claims = rfc_a(), [claim_a(ev)]
    compiled = compile_policy([parse_rule_text("don't share refunds").rule])
    body, review = build_body(request, claims, list(ev.values()), compiled, now=NOW)
    assert "record:rfd_2001@1" not in {a.ref.key() for a in body.attachments}
    with pytest.raises(TypeError):
        make_draft(request, body, review, policy_version=compiled.policy_version, now=NOW)
    with pytest.raises(PackageError):
        make_draft(request, body, review, policy_version=compiled.policy_version,
                   source_revisions=None, now=NOW)

    draft = make_draft(request, body, review, policy_version=compiled.policy_version,
                       source_revisions=cited_refs(claims), now=NOW)
    approval = approve(draft, holder_id=HOLDER, now=NOW)
    assert "record:rfd_2001@1" in {ref.key() for ref in approval.source_revisions}
    revisions = {ref.token(): ref.revision for ref in approval.source_revisions}
    problems = check_approval(approval, body=body, audience="bank_a", request_id="rfc_a_001",
                              policy_version=compiled.policy_version,
                              revisions={**revisions, "record:rfd_2001": 2}, now=LATER)
    assert problems == ["source record:rfd_2001 changed after approval (approved revision 1, "
                        "now revision 2)"]


def test_approval_must_cover_every_source_the_body_cites(case):
    narrow = case.approval.model_copy(update={"source_revisions": []})
    problems = check(case, approval=narrow)
    assert problems and all(p.startswith("the approval does not cover source") for p in problems)
    assert "the approval does not cover source record:stl_88001@1" in problems


def test_current_revisions_follow_the_store_and_catch_a_correction(case, keystore, ev, tmp_path):
    with Store(tmp_path / "db" / "passport.db") as store:
        for item in ev.values():
            if isinstance(item, EvidenceRecord):
                store.put_source(item, raw_payload=raw_payload(item))
        store.put_bank_txns([ev["txn_9001"]], connector="bank_feed")

        revisions = current_revisions(store, case.approval.source_revisions)
        assert revisions == case.revisions
        assert check(case, revisions=revisions) == []

        correction = settlement(keystore, revision=2, status="pending", effective_at=None,
                                occurred_at="2026-11-04T09:00:00Z", corrects_revision=1,
                                correction_reason="The transfer is still pending.")
        assert store.put_source(correction, raw_payload=raw_payload(correction)).status \
            == "corrected"
        revisions = current_revisions(store, case.approval.source_revisions)
        assert revisions["record:stl_88001"] == 2
        assert any("record:stl_88001 changed after approval" in p
                   for p in check(case, revisions=revisions))
        with pytest.raises(ApprovalError):
            require_valid(case.approval, body=case.body, audience="bank_a",
                          request_id="rfc_a_001", policy_version=case.policy_version,
                          revisions=revisions, now=LATER)
        unknown = EvidenceRef(ref_type="record", ref_id="ord_9999", revision=1)
        assert current_revisions(store, [unknown]) == {"record:ord_9999": None}
