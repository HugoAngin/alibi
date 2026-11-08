"""End-to-end tests of the public API (``alibi.api``) on tmp paths.

Every step goes through ``alibi.api``. The store's own methods are used only to read results
and to simulate tampering (an edited draft, a forged approval).
"""

import importlib
import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from alibi import api

HOLDER = "holder_maya"
RFC_A, RFC_B = "rfc_a_001", "rfc_b_001"
SUPPORTED = api.ClaimStatus.SUPPORTED
NOT_SUPPORTED = {api.ClaimStatus.CONTRADICTED, api.ClaimStatus.UNRESOLVED}
CONTRACT_FUNCTIONS = {
    "ingest_merchant_event", "ingest_bank_txns", "import_self_supplied", "receive_rfc",
    "run_investigation", "build_review", "record_policy", "approve", "sign_and_package",
    "verify_package", "load_fixture", "open_store",
}


# --------------------------------------------------------------------------- helpers


@pytest.fixture
def ctx(tmp_path, keystore, registry, now):
    store = api.open_store(tmp_path / "passport.db")
    yield SimpleNamespace(store=store, keystore=keystore, registry=registry, now=now)
    store.close()


def load(ctx, variant="supported", *, store=None):
    return api.load_fixture(store or ctx.store, variant, keystore=ctx.keystore, now=ctx.now)


def investigate(ctx, fx, request_id=RFC_A, *, store=None):
    return api.run_investigation(store or ctx.store, request_id,
                                 settlement_lookup=fx["settlement_lookup"],
                                 registry=ctx.registry, now=ctx.now)


def deliver(ctx, fx, request_id=RFC_A, *, store=None):
    """Investigate, review, approve and dispatch one request."""
    store = store or ctx.store
    run = investigate(ctx, fx, request_id, store=store)
    draft = api.build_review(store, request_id, now=ctx.now)
    approval = api.approve(store, draft.draft_id, holder_id=HOLDER, now=ctx.now)
    package = api.sign_and_package(store, approval.approval_id, keystore=ctx.keystore,
                                   now=ctx.now)
    return SimpleNamespace(run=run, draft=draft, approval=approval, package=package)


def verify(ctx, package, audience):
    return api.verify_package(package.to_json_dict(), expected_audience=audience,
                              registry=ctx.registry, now=ctx.now)


def source_rows(store, subject):
    """Every stored source revision of the subject, as (record_id, revision)."""
    return sorted((r.record_id, r.revision) for r in store.list_sources(subject, active_only=False))


def text(model):
    return json.dumps(model.to_json_dict(), ensure_ascii=False)


def refusals(store, target_id, action):
    return [event for event in store.list_audit(target_id=target_id)
            if event.action == action and event.outcome == "refused"]


def outcomes(store, target_id, action):
    return [event.outcome for event in store.list_audit(target_id=target_id)
            if event.action == action]


def item_names(store, subject):
    """Every item description in the subject's orders and refunds."""
    return sorted({item.description
                   for record in store.list_sources(subject, kinds=["order", "refund"])
                   for item in record.items or [] if item.description})


# --------------------------------------------------------------------------- the main flow


def test_supported_flow_signs_delivers_and_verifies_for_bank_a(ctx):
    fx = load(ctx)
    run = investigate(ctx, fx)
    (claim,) = run.claims
    assert claim.status == SUPPORTED
    gold = fx["expected"]["claims"][RFC_A]["q1"]
    assert set(gold["evidence"]) <= {ref.key() for ref in claim.evidence_refs}

    draft = api.build_review(ctx.store, RFC_A, now=ctx.now)
    assert (draft.status, draft.request_id, draft.audience) \
        == (api.DraftStatus.PENDING, RFC_A, "bank_a")
    assert [c.status for c in draft.body.claims] == [SUPPORTED]
    assert draft.review.audience == "bank_a" and draft.review.included

    approval = api.approve(ctx.store, draft.draft_id, holder_id=HOLDER, now=ctx.now)
    assert approval.status == api.ApprovalStatus.PENDING
    assert (approval.body_sha256, approval.audience, approval.request_id) \
        == (draft.body_sha256, "bank_a", RFC_A)
    assert ctx.store.get_draft(draft.draft_id).status == api.DraftStatus.APPROVED

    package = api.sign_and_package(ctx.store, approval.approval_id, keystore=ctx.keystore,
                                   now=ctx.now)
    assert package.body.to_json_dict() == draft.body.to_json_dict()
    consumed = ctx.store.get_approval(approval.approval_id)
    assert (consumed.status, consumed.package_id) \
        == (api.ApprovalStatus.CONSUMED, package.package_id)
    assert ctx.store.get_draft(draft.draft_id).status == api.DraftStatus.PACKAGED
    (delivery,) = ctx.store.list_deliveries()
    assert (delivery.package_id, delivery.audience, delivery.request_id, delivery.status) \
        == (package.package_id, "bank_a", RFC_A, "sent")

    result = verify(ctx, package, "bank_a")
    assert result.accepted and result.errors == []
    # 1. Package authentication, on its own.
    auth = result.authentication
    assert auth.valid and (auth.package_id, auth.audience, auth.kid) \
        == (package.package_id, "bank_a", "alibi_demo")
    assert result.package_sha256 == delivery.package_sha256
    # 2. Per-source trust, separately: signed merchant records verify against the pinned
    #    registry, the bank txn extract is attested only by the package signer.
    by_ref = {source.ref.key(): source for source in result.sources}
    assert set(by_ref) == {attachment.ref.key() for attachment in package.body.attachments}
    settlement, credit = by_ref["record:stl_88001@1"], by_ref["txn:txn_9001@1"]
    assert (settlement.attestation, settlement.signature_valid, settlement.lower_trust) \
        == ("issuer_signature", True, False)
    assert (credit.attestation, credit.signature_valid, credit.lower_trust) \
        == ("package_signer", None, True)
    # 3. Claim status per question, with the arithmetic rechecked from disclosed amounts.
    assert [(c.question_id, c.status, c.calculation_valid) for c in result.claims] \
        == [("q1", SUPPORTED, True)]
    assert [event.outcome for event in ctx.store.list_audit(target_id=package.package_id)
            if event.action == "sign_and_package"] == ["ok"]


def test_bank_b_cannot_verify_or_fetch_bank_a_package(ctx):
    fx = load(ctx)
    a = deliver(ctx, fx)
    result = verify(ctx, a.package, "bank_b")
    assert not result.accepted and not result.authentication.valid
    assert "the package is addressed to bank_a, not bank_b" in result.authentication.reasons
    with pytest.raises(api.NotFound):
        ctx.store.get_package(a.package.package_id, audience="bank_b")
    assert ctx.store.list_packages(audience="bank_b") == []
    assert ctx.store.list_deliveries(audience="bank_b") == []


def test_verify_package_without_a_registry_reads_the_pinned_file_and_writes_nothing(
        ctx, tmp_path, monkeypatch):
    fx = load(ctx)
    package = deliver(ctx, fx).package.to_json_dict()
    pinned = tmp_path / "recipient_keys"
    monkeypatch.setattr(api, "DEFAULT_KEY_DIR", pinned)

    # Nothing pinned yet: rejected, and no key or registry is created.
    missing = api.verify_package(package, expected_audience="bank_a", now=ctx.now)
    assert not missing.accepted and not missing.authentication.valid
    assert "pinned trust registry" in missing.errors[0]
    assert not pinned.exists()

    # The recipient pinned only the public registry: accepted, and the folder is untouched.
    ctx.registry.save(pinned / "trust_registry.json")
    before = (pinned / "trust_registry.json").read_bytes()
    result = api.verify_package(package, expected_audience="bank_a", now=ctx.now)
    assert result.accepted and result.errors == []
    assert sorted(path.name for path in pinned.iterdir()) == ["trust_registry.json"]
    assert (pinned / "trust_registry.json").read_bytes() == before

    # A naive clock is reported as a rejection, not raised.
    naive = api.verify_package(package, expected_audience="bank_a", registry=ctx.registry,
                               now=ctx.now.replace(tzinfo=None))
    assert not naive.accepted
    assert any("timezone-aware" in error for error in naive.errors)


def test_editing_the_approved_draft_makes_dispatch_refuse(ctx):
    fx = load(ctx)
    investigate(ctx, fx)
    draft = api.build_review(ctx.store, RFC_A, now=ctx.now)
    approval = api.approve(ctx.store, draft.draft_id, holder_id=HOLDER, now=ctx.now)

    edited = ctx.store.get_draft(draft.draft_id).to_json_dict()
    edited["body"]["claims"][0]["statement"] = "Edited after approval: a different explanation."
    ctx.store.save_draft(api.Draft.model_validate(edited))

    with pytest.raises(api.ApprovalError) as refused:
        api.sign_and_package(ctx.store, approval.approval_id, keystore=ctx.keystore, now=ctx.now)
    assert "the package body changed after it was approved" in refused.value.problems
    assert ctx.store.list_packages() == [] and ctx.store.list_deliveries() == []
    assert ctx.store.get_approval(approval.approval_id).status == api.ApprovalStatus.PENDING
    assert refusals(ctx.store, approval.approval_id, "sign_and_package")

    # The holder reviews the current draft again and approves it afresh: that one is sent.
    later = ctx.now + timedelta(minutes=1)
    fresh = api.build_review(ctx.store, RFC_A, now=later)
    assert fresh.draft_id != draft.draft_id
    renewed = api.approve(ctx.store, fresh.draft_id, holder_id=HOLDER, now=later)
    package = api.sign_and_package(ctx.store, renewed.approval_id, keystore=ctx.keystore,
                                   now=later)
    assert package.body.to_json_dict() == fresh.body.to_json_dict()
    assert verify(ctx, package, "bank_a").accepted


def test_rfc_b_reuses_the_passport_with_its_own_package_and_fresh_approval(ctx):
    fx = load(ctx)
    subject = fx["subject_ref"]
    a = deliver(ctx, fx, RFC_A)
    held = source_rows(ctx.store, subject)
    held_ids = {record_id for record_id, _ in held}

    with pytest.raises(api.PackageError, match="run_investigation"):
        api.build_review(ctx.store, RFC_B, now=ctx.now)
    run_b = investigate(ctx, fx, RFC_B)
    gold = fx["expected"]["claims"][RFC_B]
    assert [(c.question_id, c.status, c.calculation.total_pence) for c in run_b.claims] \
        == [("q1", SUPPORTED, gold["q1"]["total_pence"]),
            ("q2", SUPPORTED, gold["q2"]["total_pence"])]
    draft_b = api.build_review(ctx.store, RFC_B, now=ctx.now)
    assert source_rows(ctx.store, subject) == held          # nothing re-imported or rebuilt
    cited = {ref.ref_id for c in run_b.claims for ref in c.evidence_refs if ref.ref_type == "record"}
    assert cited and cited <= held_ids

    # Reusing RFC A's approval for RFC B fails. It is spent, so a retry only returns RFC A's
    # own package, which bank_b cannot accept...
    spent = api.sign_and_package(ctx.store, a.approval.approval_id, keystore=ctx.keystore,
                                 now=ctx.now)
    assert spent.to_json_dict() == a.package.to_json_dict()
    assert not verify(ctx, spent, "bank_b").accepted
    # ...and the same approval binding pointed at RFC B's draft is refused.
    forged = {**a.approval.to_json_dict(), "approval_id": "apr_reused_for_rfc_b",
              "draft_id": draft_b.draft_id}
    ctx.store.save_approval(api.Approval.model_validate(forged))
    with pytest.raises(api.ApprovalError) as refused:
        api.sign_and_package(ctx.store, "apr_reused_for_rfc_b", keystore=ctx.keystore,
                             now=ctx.now)
    problems = refused.value.problems
    assert "the package body changed after it was approved" in problems
    assert any("addressed to bank_b, not the approved recipient bank_a" in p for p in problems)
    assert any("answers request rfc_b_001, not the approved request rfc_a_001" in p
               for p in problems)
    assert ctx.store.list_packages(request_id=RFC_B) == []

    # A fresh approval of RFC B's own draft gives a different, recipient-bound package.
    approval_b = api.approve(ctx.store, draft_b.draft_id, holder_id=HOLDER, now=ctx.now)
    package_b = api.sign_and_package(ctx.store, approval_b.approval_id, keystore=ctx.keystore,
                                     now=ctx.now)
    assert approval_b.approval_id != a.approval.approval_id
    assert package_b.package_id != a.package.package_id
    assert (package_b.body.request_id, package_b.body.audience) == (RFC_B, "bank_b")
    assert (package_b.body.claims[0].question_id, package_b.body.claims[0].status) \
        == ("q1", SUPPORTED)          # q2: see test_rfc_b_delivers_both_supported_totals
    assert verify(ctx, package_b, "bank_b").accepted
    assert not verify(ctx, package_b, "bank_a").accepted
    assert {att.ref.ref_id for att in package_b.body.attachments} <= held_ids
    assert sorted((d.audience, d.request_id) for d in ctx.store.list_deliveries()) \
        == [("bank_a", RFC_A), ("bank_b", RFC_B)]
    assert source_rows(ctx.store, subject) == held


def test_rfc_b_delivers_both_supported_totals(ctx):
    fx = load(ctx)
    b = deliver(ctx, fx, RFC_B)
    assert [(c.question_id, c.status) for c in b.draft.body.claims] \
        == [("q1", SUPPORTED), ("q2", SUPPORTED)]
    result = verify(ctx, b.package, "bank_b")
    assert result.accepted
    assert [(c.question_id, c.status, c.calculation_valid) for c in result.claims] \
        == [("q1", SUPPORTED, True), ("q2", SUPPORTED, True)]


def test_a_dispatch_retry_returns_the_identical_package_and_one_delivery(ctx):
    fx = load(ctx)
    a = deliver(ctx, fx)
    retry = api.sign_and_package(ctx.store, a.approval.approval_id, keystore=ctx.keystore,
                                 now=ctx.now + timedelta(minutes=5))
    assert retry.to_json_dict() == a.package.to_json_dict()
    assert len(ctx.store.list_packages(request_id=RFC_A)) == 1
    assert len(ctx.store.list_deliveries(package_id=a.package.package_id)) == 1
    assert [event.outcome for event in ctx.store.list_audit(target_id=a.package.package_id)
            if event.action == "sign_and_package"] == ["ok", "duplicate"]


def test_approving_the_same_draft_again_returns_the_existing_approval(ctx):
    fx = load(ctx)
    investigate(ctx, fx)
    draft = api.build_review(ctx.store, RFC_A, now=ctx.now)
    approval = api.approve(ctx.store, draft.draft_id, holder_id=HOLDER, now=ctx.now)
    again = api.approve(ctx.store, draft.draft_id, holder_id=HOLDER,
                        now=ctx.now + timedelta(minutes=1))
    assert again == approval
    assert len(ctx.store.list_approvals(request_id=RFC_A)) == 1
    assert outcomes(ctx.store, approval.approval_id, "approve") == ["ok", "duplicate"]
    # Nobody else can take over the approved draft.
    with pytest.raises(api.ApprovalError, match="only a pending draft can be approved"):
        api.approve(ctx.store, draft.draft_id, holder_id="holder_other", now=ctx.now)
    package = api.sign_and_package(ctx.store, again.approval_id, keystore=ctx.keystore,
                                   now=ctx.now)
    assert verify(ctx, package, "bank_a").accepted


def quick_start(ctx, path: Path):
    """The CONTRACTS.md quick start on the passport at ``path``, closed afterwards."""
    with api.open_store(path) as store:
        fx = load(ctx, store=store)
        flow = deliver(ctx, fx, store=store)
        flow.accepted = verify(ctx, flow.package, "bank_a").accepted
        flow.deliveries = store.list_deliveries()
        flow.audit = store.list_audit()
    return flow


def test_rerunning_the_quick_start_on_one_passport_returns_the_same_package(
        tmp_path, keystore, registry, now):
    ctx = SimpleNamespace(keystore=keystore, registry=registry, now=now)
    path = tmp_path / "passport.db"
    first = quick_start(ctx, path)
    second = quick_start(ctx, path)       # a restarted process: same passport, same clock
    assert first.accepted and second.accepted
    assert second.draft.draft_id == first.draft.draft_id
    assert second.draft.status == api.DraftStatus.PACKAGED
    assert second.approval.approval_id == first.approval.approval_id
    assert second.approval.status == api.ApprovalStatus.CONSUMED
    assert second.package.to_json_dict() == first.package.to_json_dict()
    assert len(second.deliveries) == 1
    retries = [(e.action, e.outcome) for e in second.audit if e.outcome == "duplicate"
               and e.action in ("approve", "sign_and_package")]
    assert retries == [("approve", "duplicate"), ("sign_and_package", "duplicate")]


def test_a_correction_after_approval_invalidates_it_and_dispatch_refuses(ctx):
    fx = load(ctx, "pending_correction")
    run = investigate(ctx, fx)
    assert run.claims[0].status == SUPPORTED
    draft = api.build_review(ctx.store, RFC_A, now=ctx.now)
    approval = api.approve(ctx.store, draft.draft_id, holder_id=HOLDER, now=ctx.now)

    (correction,) = fx["corrections"]
    put = api.ingest_merchant_event(ctx.store, correction, registry=ctx.registry, now=ctx.now)
    assert (put.status, put.record.record_id, put.record.revision) == ("corrected", "stl_88001", 2)
    assert approval.approval_id in put.invalidated["approvals"]
    assert draft.draft_id in put.invalidated["drafts"]
    assert run.claims[0].claim_id in put.invalidated["claims"]
    assert ctx.store.get_approval(approval.approval_id).status == api.ApprovalStatus.INVALIDATED

    with pytest.raises(api.ApprovalError) as refused:
        api.sign_and_package(ctx.store, approval.approval_id, keystore=ctx.keystore, now=ctx.now)
    problems = refused.value.problems
    assert any(p.startswith("the approval was invalidated") for p in problems)
    assert any("record:stl_88001 changed after approval" in p for p in problems)
    assert ctx.store.list_packages() == [] and ctx.store.list_deliveries() == []
    assert refusals(ctx.store, approval.approval_id, "sign_and_package")
    assert [r.revision for r in ctx.store.source_history("stl_88001")] == [1, 2]
    # The invalidated draft cannot be approved again, and an unknown approval is refused.
    with pytest.raises(api.ApprovalError, match="only a pending draft can be approved"):
        api.approve(ctx.store, draft.draft_id, holder_id=HOLDER, now=ctx.now)
    assert refusals(ctx.store, draft.draft_id, "approve")
    with pytest.raises(api.ApprovalError, match="no approval"):
        api.sign_and_package(ctx.store, "apr_unknown", keystore=ctx.keystore, now=ctx.now)

    # The stale investigation cannot be packaged; a rerun sees the pending settlement.
    with pytest.raises(api.PackageError, match="out of date"):
        api.build_review(ctx.store, RFC_A, now=ctx.now)
    rerun = investigate(ctx, fx)
    assert rerun.claims[0].status in NOT_SUPPORTED
    assert "record:stl_88001@2" in {ref.key() for ref in rerun.claims[0].evidence_refs}


# --------------------------------------------------------------------------- policy


def test_record_policy_dont_share_item_names_removes_them_from_the_next_draft(ctx):
    fx = load(ctx)
    subject = fx["subject_ref"]
    investigate(ctx, fx)
    names = sorted({item.description
                    for record in ctx.store.list_sources(subject, kinds=["order", "refund"])
                    for item in record.items or [] if item.description})
    first = api.build_review(ctx.store, RFC_A, now=ctx.now)
    assert names and any(name in text(first.body) for name in names)
    approval = api.approve(ctx.store, first.draft_id, holder_id=HOLDER, now=ctx.now)

    policy = api.record_policy(ctx.store, subject, "don't share item names", confirmed=True,
                               now=ctx.now)
    (rule,) = policy.rules
    assert (policy.version, rule.type, rule.fields, policy.confirmed) \
        == (1, "deny", ["items.description"], True)
    assert ctx.store.latest_policy(subject) == policy
    # The earlier draft and its pending approval were made without this policy.
    assert ctx.store.get_draft(first.draft_id).status == api.DraftStatus.INVALIDATED
    assert ctx.store.get_approval(approval.approval_id).status == api.ApprovalStatus.INVALIDATED
    with pytest.raises(api.ApprovalError):
        api.sign_and_package(ctx.store, approval.approval_id, keystore=ctx.keystore, now=ctx.now)

    second = api.build_review(ctx.store, RFC_A, now=ctx.now)
    assert second.policy_version == policy.version_tag()
    assert not any(name in text(second.body) for name in names)
    assert any(entry.field == "items.description" for entry in second.review.withheld_fields)
    assert [c.status for c in second.body.claims] == [SUPPORTED]   # the amounts still show
    # Recording the same rule again keeps the current version.
    assert api.record_policy(ctx.store, subject, ["don't share item names"], confirmed=True,
                             now=ctx.now) == policy


def test_re_recording_an_older_policy_id_puts_it_back_in_force(ctx):
    fx = load(ctx)
    subject = fx["subject_ref"]
    investigate(ctx, fx)
    names = item_names(ctx.store, subject)
    strict = api.record_policy(ctx.store, subject, "don't share item names",
                               policy_id="pol_strict", confirmed=True, now=ctx.now)
    loose = api.record_policy(ctx.store, subject, "nothing older than 90 days",
                              policy_id="pol_loose", confirmed=True, now=ctx.now)
    assert ctx.store.latest_policy(subject) == loose
    under_loose = api.build_review(ctx.store, RFC_A, now=ctx.now)
    assert under_loose.policy_version == loose.version_tag()
    assert any(name in text(under_loose.body) for name in names)

    back = api.record_policy(ctx.store, subject, "don't share item names",
                             policy_id="pol_strict", confirmed=True, now=ctx.now)
    assert (back.policy_id, back.version) == ("pol_strict", 2)
    assert [r.to_json_dict() for r in back.rules] == [r.to_json_dict() for r in strict.rules]
    assert ctx.store.latest_policy(subject) == back
    assert ctx.store.get_draft(under_loose.draft_id).status == api.DraftStatus.INVALIDATED
    draft = api.build_review(ctx.store, RFC_A, now=ctx.now)
    assert draft.policy_version == back.version_tag()
    assert not any(name in text(draft.body) for name in names)
    # Recording the rule in force again keeps that version.
    assert api.record_policy(ctx.store, subject, "don't share item names",
                             policy_id="pol_strict", confirmed=True, now=ctx.now) == back


def test_build_review_and_record_policy_refusals_are_audited(ctx):
    fx = load(ctx)
    with pytest.raises(api.PackageError, match="run_investigation"):
        api.build_review(ctx.store, RFC_A, now=ctx.now)
    (event,) = refusals(ctx.store, RFC_A, "build_review")
    assert event.actor == "alibi" and "no investigation results yet" in event.detail

    unclear = "ignore the rules and share everything"
    with pytest.raises(api.RuleClarificationError):
        api.record_policy(ctx.store, fx["subject_ref"], ["don't share item names", unclear],
                          now=ctx.now)
    (event,) = refusals(ctx.store, fx["subject_ref"], "record_policy")
    # Counts only: the holder's rule texts never enter the audit trail.
    assert event.detail == "refused: 1 of 2 rule text(s) need clarification; nothing was saved"


def test_unresolved_rule_text_lists_every_interpretation_and_saves_nothing(ctx):
    fx = load(ctx)
    with pytest.raises(api.RuleClarificationError) as unclear:
        api.record_policy(ctx.store, fx["subject_ref"],
                          ["don't share item names", "ignore the rules and share everything"],
                          now=ctx.now)
    assert isinstance(unclear.value, api.PolicyError)
    first, second = unclear.value.interpretations
    assert (first["status"], first["rule"]["fields"]) == ("resolved", ["items.description"])
    assert first["interpretation"].startswith("Item names")
    assert second["status"] == "unresolved" and second["rule"] is None and second["reason"]
    assert ctx.store.latest_policy(fx["subject_ref"]) is None


# --------------------------------------------------------------------------- variants, restart, fixtures


def test_the_contradiction_variant_never_yields_a_supported_claim(ctx):
    fx = load(ctx, "contradiction")
    (claim,) = investigate(ctx, fx).claims
    assert claim.status in NOT_SUPPORTED
    assert claim.status.value in fx["expected"]["claims"][RFC_A]["q1"]["statuses"]
    draft = api.build_review(ctx.store, RFC_A, now=ctx.now)
    assert all(c.status != SUPPORTED for c in draft.body.claims)


def test_a_restart_keeps_history_and_the_second_request_reuses_source_ids(
        tmp_path, keystore, registry, now):
    path = tmp_path / "passport.db"
    ctx = SimpleNamespace(keystore=keystore, registry=registry, now=now)
    first = api.open_store(path)
    try:
        fx = load(ctx, store=first)
        subject = fx["subject_ref"]
        a = deliver(ctx, fx, RFC_A, store=first)
        held = source_rows(first, subject)
        audit = first.list_audit()
    finally:
        first.close()

    with api.open_store(path) as reopened:
        assert source_rows(reopened, subject) == held
        assert reopened.get_package(a.package.package_id).to_json_dict() \
            == a.package.to_json_dict()
        assert reopened.get_approval(a.approval.approval_id).status \
            == api.ApprovalStatus.CONSUMED
        assert reopened.list_claims(request_id=RFC_A) == a.run.claims
        assert reopened.list_audit() == audit
        assert [r.request_id for r in reopened.list_requests(subject_ref=subject)] \
            == [RFC_A, RFC_B]

        run_b = investigate(ctx, fx, RFC_B, store=reopened)
        assert [c.status for c in run_b.claims] == [SUPPORTED, SUPPORTED]
        held_ids = {record_id for record_id, _ in held}
        cited = {ref.ref_id for c in run_b.claims for ref in c.evidence_refs}
        assert cited and cited <= held_ids
        draft_b = api.build_review(reopened, RFC_B, now=now)
        assert {att.ref.ref_id for att in draft_b.body.attachments} <= held_ids
        assert source_rows(reopened, subject) == held


def test_build_review_uses_the_run_recorded_last_whatever_its_clock(ctx):
    fx = load(ctx, "pending_correction")
    later = ctx.now + timedelta(days=1)
    first = api.run_investigation(ctx.store, RFC_A, settlement_lookup=fx["settlement_lookup"],
                                  registry=ctx.registry, now=later)
    (correction,) = fx["corrections"]
    api.ingest_merchant_event(ctx.store, correction, registry=ctx.registry, now=ctx.now)
    rerun = investigate(ctx, fx)                     # on the earlier fixture clock
    assert rerun.claims[0].created_at < first.claims[0].created_at
    assert rerun.claims[0].status in NOT_SUPPORTED

    draft = api.build_review(ctx.store, RFC_A, now=ctx.now)
    assert [c.status for c in draft.body.claims] == [rerun.claims[0].status]
    assert "record:stl_88001@2" in {ref.key() for ref in draft.source_revisions}


def test_load_fixture_defaults_to_the_fixture_clock(ctx, tmp_path):
    fx = api.load_fixture(ctx.store, keystore=ctx.keystore)      # no now: the fixture clock
    assert fx["now"] == ctx.now
    assert fx["request_ids"] == [RFC_A, RFC_B]
    expiry = ctx.store.get_request(RFC_A).expires_at

    # A clock at RFC A's expiry is refused before anything reaches the passport.
    with api.open_store(tmp_path / "late.db") as late:
        with pytest.raises(api.IngestError, match=RFC_A) as refused:
            api.load_fixture(late, keystore=ctx.keystore, now=expiry)
        assert "nothing was loaded" in str(refused.value)
        assert late.list_sources(fx["subject_ref"], active_only=False) == []
        assert late.list_bank_txns(fx["subject_ref"], active_only=False) == []
        assert late.list_requests() == [] and late.list_audit() == []


def test_replaying_load_fixture_is_idempotent(ctx):
    fx = load(ctx)
    subject = fx["subject_ref"]
    rows = source_rows(ctx.store, subject)
    txns = ctx.store.list_bank_txns(subject, active_only=False)
    requests = ctx.store.list_requests()
    assert set(fx["statuses"].values()) == {"inserted"}
    assert fx["request_ids"] == [RFC_A, RFC_B]
    assert fx["txn_ids"] == list(fx["expected"]["links"])
    # Lookup-only settlements stay outside the passport until an investigation retrieves them.
    assert "stl_88001" not in {record_id for record_id, _ in rows}

    again = load(ctx)
    assert set(again["statuses"].values()) == {"duplicate"}
    assert (again["record_ids"], again["txn_ids"], again["request_ids"]) \
        == (fx["record_ids"], fx["txn_ids"], fx["request_ids"])
    assert source_rows(ctx.store, subject) == rows
    assert ctx.store.list_bank_txns(subject, active_only=False) == txns
    assert ctx.store.list_requests() == requests


def test_the_retail_fixture_imports_a_self_supplied_receipt(ctx):
    fx = load(ctx, "retail_purchase")
    (record_id,) = fx["record_ids"]
    record = ctx.store.get_source(record_id)
    assert record_id.startswith("self_")
    assert record.source_trust == api.SourceTrust.SELF_SUPPLIED and record.issuer_signature is None
    assert fx["expected"]["source_trust"][f"record:{record_id}"] == record.source_trust.value
    assert fx["txn_ids"] == list(fx["expected"]["links"]) and fx["request_ids"] == []


@pytest.mark.parametrize("mode", ["agents", "single_call"])
def test_other_investigation_modes_are_not_built(ctx, mode):
    load(ctx)
    with pytest.raises(NotImplementedError):
        api.run_investigation(ctx.store, RFC_A, mode=mode, registry=ctx.registry, now=ctx.now)
    assert ctx.store.list_claims(request_id=RFC_A, state=None) == []


def test_agents_lead_api_reexports_alibi_api():
    lead = importlib.import_module("agents.lead.api")
    assert list(lead.__all__) == list(api.__all__)
    assert len(set(api.__all__)) == len(api.__all__)
    assert CONTRACT_FUNCTIONS <= set(api.__all__)
    from alibi.app.policy_chain import parse_rule_text as chain
    for name in api.__all__:
        if name == "parse_rule_text":
            # the app's parser is the policy chain (Gemini, execution's compiler, core), a RuleParse
            assert getattr(lead, name) is chain
            continue
        assert getattr(lead, name) is getattr(api, name)


# --------------------------------------------------------------------------- consent matrix C2 and C6


def test_editing_the_review_after_approval_supersedes_it_and_dispatch_refuses(ctx):
    """C2: Maya approves, removes an item, and the old approval must not sign the old body."""
    import json

    fx = load(ctx, "supported")
    investigate(ctx, fx)
    first = api.build_review(ctx.store, RFC_A, now=ctx.now)
    removable = next(a.ref.token() for a in first.body.attachments)
    approval = api.approve(ctx.store, first.draft_id, holder_id=HOLDER, now=ctx.now)
    second = api.build_review(ctx.store, RFC_A, now=ctx.now, removed=[removable])
    assert second.draft_id != first.draft_id and second.body_sha256 != first.body_sha256
    assert ctx.store.get_draft(first.draft_id).status == api.DraftStatus.INVALIDATED
    assert ctx.store.get_approval(approval.approval_id).status == api.ApprovalStatus.INVALIDATED
    with pytest.raises(api.ApprovalError) as refused:
        api.sign_and_package(ctx.store, approval.approval_id, keystore=ctx.keystore, now=ctx.now)
    assert refused.value.code == "approval_digest_mismatch"
    assert refused.value.reason.startswith("dispatch refused: approved digest sha256:")
    assert ctx.store.list_packages() == [] and ctx.store.list_deliveries() == []
    # C6: the superseded draft cannot be approved either.
    with pytest.raises(api.ApprovalError) as stale:
        api.approve(ctx.store, first.draft_id, holder_id=HOLDER, now=ctx.now)
    assert stale.value.code == "approval_digest_not_current"
    # The new draft approves and signs, and the removed item is not in the package.
    fresh = api.approve(ctx.store, second.draft_id, holder_id=HOLDER, now=ctx.now)
    package = api.sign_and_package(ctx.store, fresh.approval_id, keystore=ctx.keystore, now=ctx.now)
    assert removable.split(":", 1)[1] not in json.dumps(package.to_json_dict())


def test_rebuilding_an_identical_review_keeps_the_approval(ctx):
    """Re-rendering the review page (same body, later clock) must not invalidate anything."""
    from datetime import timedelta

    fx = load(ctx, "supported")
    investigate(ctx, fx)
    first = api.build_review(ctx.store, RFC_A, now=ctx.now)
    approval = api.approve(ctx.store, first.draft_id, holder_id=HOLDER, now=ctx.now)
    again = api.build_review(ctx.store, RFC_A, now=ctx.now + timedelta(minutes=1))
    assert again.draft_id == first.draft_id
    assert ctx.store.get_approval(approval.approval_id).status == api.ApprovalStatus.PENDING
    assert api.sign_and_package(ctx.store, approval.approval_id, keystore=ctx.keystore,
                                now=ctx.now + timedelta(minutes=2)).package_id


def test_rebuilding_a_body_that_was_invalidated_earlier_gets_a_fresh_pending_draft(ctx):
    """Remove an item, then restore it at the same clock: the restored body equals the first
    draft, which _supersede invalidated, so the rebuild must mint a new draft id instead of
    overwriting an invalidated row (StoreError before the fix)."""
    fx = load(ctx)
    investigate(ctx, fx)
    first = api.build_review(ctx.store, "rfc_a_001", now=ctx.now)
    removed = api.build_review(ctx.store, "rfc_a_001", now=ctx.now, removed=["record:rfd_2001"])
    assert removed.draft_id != first.draft_id
    assert api.DraftStatus(ctx.store.get_draft(first.draft_id).status) == api.DraftStatus.INVALIDATED
    restored = api.build_review(ctx.store, "rfc_a_001", now=ctx.now)
    assert restored.draft_id not in {first.draft_id, removed.draft_id}
    assert restored.body_sha256 == first.body_sha256
    assert api.DraftStatus(restored.status) == api.DraftStatus.PENDING
    assert api.DraftStatus(ctx.store.get_draft(removed.draft_id).status) == api.DraftStatus.INVALIDATED
