"""Consent-matrix refusal codes and verbatim reasons on the public API (track 1c).

Every refusal of ``approve`` and ``sign_and_package`` carries the code and the filled sentence of
agents/execution/out/consent_matrix.md (``alibi.core.approval.REFUSAL_TEMPLATES``), decided in
the matrix order. The controls (C9, C14, C16) still send. All data is the synthetic Maya fixture.
"""

import re
from datetime import timedelta
from types import SimpleNamespace

import pytest

from alibi import api
from alibi.core import approval as core_approval
from alibi.core import investigate_rules
from alibi.core.approval import REFUSAL_TEMPLATES, code_for_reason, refusal_reason

HOLDER = "holder_maya"
RFC_A = "rfc_a_001"
REGENERATED = core_approval.CHANGE_REGENERATED


def reason_pattern(code: str) -> re.Pattern:
    """The matrix template as a regex, each placeholder matching anything (as the execution
    conformance test checks it)."""
    parts = re.split(r"\{[a-z0-9_]+\}", REFUSAL_TEMPLATES[code])
    return re.compile("^" + ".*?".join(re.escape(p) for p in parts) + "$", re.S)


def check(exc: api.ApprovalError, code: str) -> api.ApprovalError:
    assert exc.code == code
    assert exc.reason and reason_pattern(code).match(exc.reason), exc.reason
    assert exc.refusal == {"code": exc.code, "reason": exc.reason}
    return exc


@pytest.fixture
def ctx(tmp_path, keystore, registry, now):
    store = api.open_store(tmp_path / "passport.db")
    yield SimpleNamespace(store=store, keystore=keystore, registry=registry, now=now)
    store.close()


def approved(ctx, variant="supported"):
    """Load, investigate, review and approve RFC A; returns the fixture, draft and approval."""
    fx = api.load_fixture(ctx.store, variant, keystore=ctx.keystore, now=ctx.now)
    api.run_investigation(ctx.store, RFC_A, settlement_lookup=fx["settlement_lookup"],
                          registry=ctx.registry, now=ctx.now)
    draft = api.build_review(ctx.store, RFC_A, now=ctx.now)
    approval = api.approve(ctx.store, draft.draft_id, holder_id=HOLDER, now=ctx.now)
    return SimpleNamespace(fx=fx, draft=draft, approval=approval)


def dispatch(ctx, approval_id, now=None):
    return api.sign_and_package(ctx.store, approval_id, keystore=ctx.keystore, now=now or ctx.now)


def refused(ctx, approval_id, now=None) -> api.ApprovalError:
    with pytest.raises(api.ApprovalError) as info:
        dispatch(ctx, approval_id, now)
    assert ctx.store.list_packages() == [] and ctx.store.list_deliveries() == []
    return info.value


# --------------------------------------------------------------------------- the templates


def test_templates_are_the_execution_consent_matrix_verbatim():
    from agents.execution.approval_gate import CONSENT_MATRIX

    assert set(REFUSAL_TEMPLATES) == set(CONSENT_MATRIX)
    for code, (_rows, template) in CONSENT_MATRIX.items():
        assert REFUSAL_TEMPLATES[code] == template, code


def test_every_filled_template_maps_back_to_its_code():
    fields = {name: f"<{name}>" for name in
              ("request_id", "approved12", "actual12", "change", "approved_audience", "audience",
               "request_audience", "current12", "old", "new", "record_id", "valid_until",
               "expires_at", "approval_id", "approved_request", "package_id")}
    for code in REFUSAL_TEMPLATES:
        assert code_for_reason(refusal_reason(code, **fields)) == code, code
    assert code_for_reason("the approval expired at 2026-11-07T12:15:00Z") is None
    assert code_for_reason(None) is None
    with pytest.raises(ValueError):
        refusal_reason("approval_expired")          # missing field
    with pytest.raises(ValueError):
        refusal_reason("no_such_code")


def test_approval_error_exposes_code_reason_and_refusal():
    exc = api.ApprovalError(["the approval expired at X"], code="approval_expired",
                            valid_until="2026-11-07T12:15:00Z")
    assert exc.reason == "dispatch refused: approval expired at 2026-11-07T12:15:00Z. Review and approve again."
    assert exc.refusal == {"code": "approval_expired", "reason": exc.reason}
    assert str(exc) == "the approval expired at X"
    free = api.ApprovalError(["something else"])
    assert free.refusal == {"code": None, "reason": "something else"}
    # A matrix sentence given as the reason recovers its code.
    stored = api.ApprovalError(["x"], reason=refusal_reason("approval_digest_not_current",
                                                             approved12="a" * 12, current12="b" * 12))
    assert stored.code == "approval_digest_not_current"


# --------------------------------------------------------------------------- dispatch rows


def test_c1_unknown_approval_and_unapproved_request_are_approval_missing(ctx):
    case = approved(ctx)
    exc = check(refused(ctx, "apr_doesnotexist0000000000"), "approval_missing")
    assert "no approval 'apr_doesnotexist0000000000'" in exc.problems
    # A request id that was never approved: the reason names the request (C1, C12, C13).
    fx = case.fx
    api.run_investigation(ctx.store, "rfc_b_001", settlement_lookup=fx["settlement_lookup"],
                          registry=ctx.registry, now=ctx.now)
    api.build_review(ctx.store, "rfc_b_001", now=ctx.now)
    exc = check(refused(ctx, "rfc_b_001"), "approval_missing")
    assert exc.reason == "dispatch refused: no holder approval recorded for request rfc_b_001."
    # A refused dispatch is audited against the id given.
    assert [e.outcome for e in ctx.store.list_audit(target_id="rfc_b_001")
            if e.action == "sign_and_package"] == ["refused"]


def test_c7_policy_change_after_approval_names_both_versions(ctx):
    case = approved(ctx)
    old = case.approval.policy_version
    policy = api.record_policy(ctx.store, case.fx["subject_ref"],
                               [api.parse_rule_text("don't share item names").rule],
                               confirmed=True, now=ctx.now)
    new = policy.version_tag()
    exc = check(refused(ctx, case.approval.approval_id), "policy_version_changed")
    assert exc.reason == refusal_reason("policy_version_changed", old=old, new=new)
    assert old in exc.reason and new in exc.reason and old != new
    # The old draft cannot be approved either: C7 on approve, with the same sentence.
    with pytest.raises(api.ApprovalError) as info:
        api.approve(ctx.store, case.draft.draft_id, holder_id=HOLDER, now=ctx.now)
    assert check(info.value, "policy_version_changed").reason == exc.reason


def test_c8_cited_correction_names_record_and_revisions(ctx):
    case = approved(ctx, "pending_correction")
    assert case.fx["corrections"]
    for event in case.fx["corrections"]:
        api.ingest_merchant_event(ctx.store, event, registry=ctx.registry, now=ctx.now)
    exc = check(refused(ctx, case.approval.approval_id), "source_revision_changed")
    assert exc.reason == refusal_reason("source_revision_changed", record_id="stl_88001",
                                        old=1, new=2)
    assert any("record:stl_88001 changed after approval" in p for p in exc.problems)
    # Approving the stale draft again is refused with the same row.
    with pytest.raises(api.ApprovalError) as info:
        api.approve(ctx.store, case.draft.draft_id, holder_id=HOLDER, now=ctx.now)
    assert check(info.value, "source_revision_changed").reason == exc.reason
    assert any(p.startswith("only a pending draft can be approved") for p in info.value.problems)


def test_c9_uncited_new_record_still_sends(ctx):
    case = approved(ctx)
    subject = case.fx["subject_ref"]
    api.import_self_supplied(ctx.store, {
        "kind": "receipt", "amount_pence": 100, "currency": "GBP",
        "occurred_at": "2026-11-05T13:20:00Z", "status": "paid",
        "schema_version": "alibi.merchant_event.v1", "subject_ref": subject,
        "text": "SYNTHETIC unrelated receipt", "items": []}, subject_ref=subject, now=ctx.now)
    assert dispatch(ctx, case.approval.approval_id).package_id


def test_c10_expired_approval_names_valid_until(ctx):
    case = approved(ctx)
    late = case.approval.expires_at + timedelta(seconds=1)
    exc = check(refused(ctx, case.approval.approval_id, now=late), "approval_expired")
    assert exc.reason == refusal_reason("approval_expired",
                                        valid_until=api.format_utc(case.approval.expires_at))
    assert "2026-11-07T12:15:00Z" in exc.reason


def test_c11_expired_request_names_request_and_expiry(ctx):
    case = approved(ctx)
    rfc = ctx.store.get_request(RFC_A)
    exc = check(refused(ctx, case.approval.approval_id, now=rfc.expires_at), "request_expired")
    assert exc.reason == refusal_reason("request_expired", request_id=RFC_A,
                                        expires_at=api.format_utc(rfc.expires_at))
    # Request expiry comes first in the matrix order, before the approval's own window.
    assert any("the approval expired" in p for p in exc.problems)


def test_c2_removed_item_keeps_the_change_note(ctx):
    case = approved(ctx)
    removable = next(a.ref.token() for a in case.draft.body.attachments)
    second = api.build_review(ctx.store, RFC_A, now=ctx.now, removed=[removable])
    exc = check(refused(ctx, case.approval.approval_id), "approval_digest_mismatch")
    assert f"({removable} removed)" in exc.reason
    assert f"sha256:{case.approval.body_sha256[:12]}" in exc.reason
    assert f"sha256:{second.body_sha256[:12]}" in exc.reason
    with pytest.raises(api.ApprovalError) as info:
        api.approve(ctx.store, case.draft.draft_id, holder_id=HOLDER, now=ctx.now)
    check(info.value, "approval_digest_not_current")


# --------------------------------------------------------------------------- C3: the run behind the draft


def rerun(ctx, fx, monkeypatch, *, revise: bool):
    """Run the investigation again; with ``revise`` every answer's rationale changes."""
    if revise:
        answer = investigate_rules._Run.answer

        def revised(run, question):
            claim = answer(run, question)
            return claim.model_copy(update={"rationale": f"Rechecked. {claim.rationale}"})

        monkeypatch.setattr(investigate_rules._Run, "answer", revised)
    return api.run_investigation(ctx.store, RFC_A, settlement_lookup=fx["settlement_lookup"],
                                 registry=ctx.registry, now=ctx.now)


def test_c3_rerun_with_changed_rationale_revokes_the_approval(ctx, monkeypatch):
    case = approved(ctx)
    before = ctx.store.list_claims(request_id=RFC_A)
    revised = rerun(ctx, case.fx, monkeypatch, revise=True)
    assert [c.evidence_refs for c in revised.claims] == [c.evidence_refs for c in before]
    assert [c.rationale for c in revised.claims] != [c.rationale for c in before]
    # No new preview was built, yet the earlier approval no longer dispatches.
    exc = check(refused(ctx, case.approval.approval_id), "approval_digest_mismatch")
    assert exc.reason.endswith(f"the body changed after approval{REGENERATED}. Review and "
                               "approve the revised body.")
    assert f"sha256:{case.approval.body_sha256[:12]}" in exc.reason
    # The regenerated preview carries the new rationale, approves and sends.
    draft = api.build_review(ctx.store, RFC_A, now=ctx.now)
    assert draft.body_sha256 != case.draft.body_sha256
    assert f"sha256:{draft.body_sha256[:12]}" in exc.reason
    assert all(c.rationale.startswith("Rechecked.") for c in draft.body.claims)
    fresh = api.approve(ctx.store, draft.draft_id, holder_id=HOLDER, now=ctx.now)
    package = dispatch(ctx, fresh.approval_id)
    assert package.body_sha256 == draft.body_sha256
    # C14: the retry returns the identical package with one delivery, after the rerun too.
    assert dispatch(ctx, fresh.approval_id).to_json_dict() == package.to_json_dict()
    assert len(ctx.store.list_deliveries(package_id=package.package_id)) == 1


def test_c3_rerun_with_identical_answers_still_sends(ctx, monkeypatch):
    case = approved(ctx)
    again = rerun(ctx, case.fx, monkeypatch, revise=False)
    assert again.claims and ctx.store.list_claims(request_id=RFC_A)[-1].run_id == again.run_id
    package = dispatch(ctx, case.approval.approval_id)
    assert package.body_sha256 == case.approval.body_sha256


def test_c3_rerun_after_delivery_keeps_the_retry_idempotent(ctx, monkeypatch):
    case = approved(ctx)
    package = dispatch(ctx, case.approval.approval_id)
    rerun(ctx, case.fx, monkeypatch, revise=True)
    assert dispatch(ctx, case.approval.approval_id).to_json_dict() == package.to_json_dict()
    assert len(ctx.store.list_deliveries()) == 1


# --------------------------------------------------------------------------- approve rows


def test_approve_refuses_an_expired_request_with_c11(ctx):
    fx = api.load_fixture(ctx.store, "supported", keystore=ctx.keystore, now=ctx.now)
    api.run_investigation(ctx.store, RFC_A, settlement_lookup=fx["settlement_lookup"],
                          registry=ctx.registry, now=ctx.now)
    draft = api.build_review(ctx.store, RFC_A, now=ctx.now)
    rfc = ctx.store.get_request(RFC_A)
    with pytest.raises(api.ApprovalError) as info:
        api.approve(ctx.store, draft.draft_id, holder_id=HOLDER, now=rfc.expires_at)
    exc = check(info.value, "request_expired")
    assert exc.reason == refusal_reason("request_expired", request_id=RFC_A,
                                        expires_at=api.format_utc(rfc.expires_at))
    assert ctx.store.get_draft(draft.draft_id).status == api.DraftStatus.PENDING


def test_refusals_outside_the_matrix_have_no_code(ctx):
    approved(ctx)
    with pytest.raises(api.ApprovalError) as info:
        api.approve(ctx.store, "drf_unknown", holder_id=HOLDER, now=ctx.now)
    assert info.value.refusal == {"code": None, "reason": "no draft 'drf_unknown'"}
