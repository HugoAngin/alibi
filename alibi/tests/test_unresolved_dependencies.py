"""Public fixtures: minimal unresolved disclosures still invalidate on examined changes."""

import json
from types import SimpleNamespace

import pytest

from alibi import api
from alibi.core.answered import answered_calculation, answered_refs, examined_refs
from alibi.core.schemas import ClaimStatus, DerivedState, SigningDomain
from alibi.core.store import dependency_refs


RFC = "rfc_a_001"


@pytest.fixture
def unresolved(tmp_path, keystore, registry, now):
    path = tmp_path / "passport.db"
    store = api.open_store(path)
    fx = api.load_fixture(store, "pending_correction", keystore=keystore, now=now)
    body = dict(fx["corrections"][0]["event"])
    body["occurred_at"] = "2026-11-03T08:00:00Z"  # pending before the requested credit
    body.pop("effective_at", None)
    correction = {"event": body, "signature": keystore.sign(
        body, kid="merchant_demo", domain=SigningDomain.RECORD).to_json_dict()}
    api.ingest_merchant_event(store, correction, registry=registry, now=now)
    result = api.run_investigation(store, RFC, audience="bank_a", registry=registry,
                                   settlement_lookup=fx["settlement_lookup"], now=now)
    (claim,) = result.claims
    assert claim.status == ClaimStatus.UNRESOLVED
    yield SimpleNamespace(store=store, path=path, claim=claim, result=result, fx=fx,
                          keystore=keystore, registry=registry, now=now)
    store.close()


def revise(ctx):
    body = ctx.store.get_source("stl_88001").to_event().to_json_dict()
    body.update(event_id="public-dependency-correction-3", revision=3, corrects_revision=2,
                correction_reason="Public synthetic correction", status="settled")
    payload = {"event": body, "signature": ctx.keystore.sign(
        body, kid="merchant_demo", domain=SigningDomain.RECORD).to_json_dict()}
    return api.ingest_merchant_event(ctx.store, payload, registry=ctx.registry, now=ctx.now)


def review(ctx):
    draft = api.build_review(ctx.store, RFC, audience="bank_a", now=ctx.now)
    approval = api.approve(ctx.store, draft.draft_id, holder_id="holder_maya", now=ctx.now)
    return draft, approval


def test_empty_package_keeps_private_dependencies_and_scrubs_examined_identifiers(unresolved):
    ctx = unresolved
    assert ctx.claim.evidence_refs == [] and ctx.claim.calculation is None
    keys = {ref.key() for ref in ctx.claim.examined_refs}
    assert "record:stl_88001@2" in keys and "txn:txn_9001@1" in keys
    assert dependency_refs(ctx.claim) == ctx.claim.examined_refs
    draft, approval = review(ctx)
    assert {ref.key() for ref in draft.source_revisions} == keys
    assert {ref.key() for ref in approval.source_revisions} == keys
    assert draft.body.attachments == []
    for claim in draft.body.claims:
        assert claim.evidence_refs == [] and claim.calculation is None
    text = json.dumps(draft.body.to_json_dict())
    assert "examined_refs" not in text
    assert all(value not in text for value in ("stl_88001", "po_5001", "PO-5001"))
    package = api.sign_and_package(ctx.store, approval.approval_id, keystore=ctx.keystore, now=ctx.now)
    assert package.body.attachments == []
    assert api.verify_package(package.to_json_dict(), expected_audience="bank_a",
                              registry=ctx.registry, now=ctx.now).accepted


def test_examined_correction_invalidates_claim_draft_and_approval_after_restart(unresolved):
    ctx = unresolved
    draft, approval = review(ctx)
    ctx.store.close()
    ctx.store = api.open_store(ctx.path)
    stored = ctx.store.list_claims(request_id=RFC, audience="bank_a")[0]
    assert stored.examined_refs == ctx.claim.examined_refs
    put = revise(ctx)
    assert ctx.claim.claim_id in put.invalidated["claims"]
    assert draft.draft_id in put.invalidated["drafts"]
    assert approval.approval_id in put.invalidated["approvals"]
    assert ctx.store.list_claims(request_id=RFC, audience="bank_a") == []
    assert ctx.store.get_draft(draft.draft_id).status == api.DraftStatus.INVALIDATED
    assert ctx.store.get_approval(approval.approval_id).status == api.ApprovalStatus.INVALIDATED
    with pytest.raises(api.ApprovalError):
        api.sign_and_package(ctx.store, approval.approval_id, keystore=ctx.keystore, now=ctx.now)
    with pytest.raises(api.PackageError, match="out of date"):
        api.build_review(ctx.store, RFC, audience="bank_a", now=ctx.now)
    assert ctx.store.list_packages() == [] and ctx.store.list_deliveries() == []


def test_late_save_with_old_examined_revision_is_invalidated(unresolved):
    ctx = unresolved
    revise(ctx)
    late = ctx.claim.model_copy(update={"claim_id": "late-unresolved", "run_id": "late-run"})
    ctx.store.save_claim(late)
    stored = next(c for c in ctx.store.list_claims(state=None) if c.claim_id == late.claim_id)
    assert stored.state == DerivedState.INVALIDATED


@pytest.mark.parametrize("build_new_review", [False, True])
def test_changed_private_dependencies_revoke_same_body_consent(unresolved, build_new_review):
    ctx = unresolved
    draft, approval = review(ctx)
    # Another run examines one additional source while leaving recipient-facing answers alone.
    subject = ctx.store.get_request(RFC).subject_ref
    extra = next(record.ref() for record in ctx.store.list_sources(subject)
                 if record.ref() not in ctx.claim.examined_refs)
    newer = ctx.claim.model_copy(update={"claim_id": "newer-unresolved", "run_id": "newer-run",
                                         "examined_refs": [*ctx.claim.examined_refs, extra]})
    ctx.store.save_claim(newer)
    if build_new_review:
        rebuilt = api.build_review(ctx.store, RFC, audience="bank_a", now=ctx.now)
        assert rebuilt.body_sha256 == draft.body_sha256
        assert rebuilt.draft_id != draft.draft_id
        assert extra in rebuilt.source_revisions
        assert ctx.store.get_approval(approval.approval_id).status == api.ApprovalStatus.INVALIDATED
    with pytest.raises(api.ApprovalError):
        api.sign_and_package(ctx.store, approval.approval_id, keystore=ctx.keystore, now=ctx.now)
    assert ctx.store.list_packages() == [] and ctx.store.list_deliveries() == []


def test_supported_and_contradicted_helpers_keep_disclosed_evidence(unresolved):
    refs = unresolved.claim.examined_refs
    for status in (ClaimStatus.SUPPORTED, ClaimStatus.CONTRADICTED):
        assert answered_refs(status, refs) == refs
        assert examined_refs(status, refs, None) == []
        assert answered_calculation(status, None) is None


def test_breakdown_carries_source_dependencies_before_disclosure_filter(unresolved):
    from alibi.core.investigate_rules import _Run
    from types import SimpleNamespace
    refs = unresolved.claim.examined_refs
    base = SimpleNamespace(status=ClaimStatus.UNRESOLVED, calculation=None, evidence=refs,
                           rationale="Missing settlement", reason="Missing settlement",
                           detail="Missing settlement", gap="settlement_not_found")
    fake = SimpleNamespace(credit_outcome=lambda _: base, bank={})
    outcome = _Run.breakdown_outcome(fake, "txn_9001")
    assert outcome.evidence == refs
    assert examined_refs(outcome.status, outcome.evidence, outcome.calculation) == refs
