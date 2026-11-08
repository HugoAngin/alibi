"""Proposed regression tests (eval, public fixture only). Deterministic: no reliance on the
random order of content-hash draft ids."""
from datetime import timedelta
from types import SimpleNamespace

import pytest

from alibi import api

HOLDER, RFC_A, RFC_B = "holder_maya", "rfc_a_001", "rfc_b_001"


@pytest.fixture
def ctx(tmp_path, keystore, registry, now):
    store = api.open_store(tmp_path / "passport.db")
    yield SimpleNamespace(store=store, keystore=keystore, registry=registry, now=now)
    store.close()


def _shared(ctx):
    fx = api.load_fixture(ctx.store, "supported", keystore=ctx.keystore, now=ctx.now)
    body = {**ctx.store.get_request(RFC_B).to_json_dict(), "request_id": RFC_A}
    api.receive_rfc(ctx.store, body, authenticated_issuer="bank_b", registry=ctx.registry,
                    now=ctx.now)
    return fx


def _run(ctx, fx, bank):
    api.run_investigation(ctx.store, RFC_A, audience=bank, settlement_lookup=fx["settlement_lookup"],
                          registry=ctx.registry, now=ctx.now)


def _send(ctx, bank, at):
    draft = api.build_review(ctx.store, RFC_A, audience=bank, now=at)
    approval = api.approve(ctx.store, draft.draft_id, holder_id=HOLDER, now=at)
    return api.sign_and_package(ctx.store, approval.approval_id, keystore=ctx.keystore, now=at)


def test_another_banks_later_draft_is_never_this_banks_current_draft(ctx):
    """bank_a's packaged draft is newer than bank_b's under the same request id. bank_b's
    dispatch compares against bank_b's own latest draft, not bank_a's (C2 is per bank)."""
    fx = _shared(ctx)
    _run(ctx, fx, "bank_a")
    _send(ctx, "bank_a", ctx.now + timedelta(seconds=1))
    _run(ctx, fx, "bank_b")
    package = _send(ctx, "bank_b", ctx.now)
    assert package.body.audience == "bank_b"


def test_one_banks_review_does_not_revoke_another_banks_approval(ctx):
    """Building bank_b's review supersedes only bank_b's drafts and approvals (C6/C2)."""
    fx = _shared(ctx)
    _run(ctx, fx, "bank_a")
    draft_a = api.build_review(ctx.store, RFC_A, audience="bank_a", now=ctx.now)
    approval_a = api.approve(ctx.store, draft_a.draft_id, holder_id=HOLDER, now=ctx.now)
    _run(ctx, fx, "bank_b")
    api.build_review(ctx.store, RFC_A, audience="bank_b", now=ctx.now)
    assert api.DraftStatus(ctx.store.get_draft(draft_a.draft_id).status) == api.DraftStatus.APPROVED
    assert api.ApprovalStatus(ctx.store.get_approval(approval_a.approval_id).status) \
        == api.ApprovalStatus.PENDING
    package = api.sign_and_package(ctx.store, approval_a.approval_id, keystore=ctx.keystore,
                                   now=ctx.now)
    assert package.body.audience == "bank_a"


def test_a_revised_draft_at_the_same_clock_is_the_current_one(ctx):
    """Same bank, frozen clock: after a package, a revised review built at the same instant is
    the request's current draft, whatever its content-hash id sorts as."""
    fx = api.load_fixture(ctx.store, "supported", keystore=ctx.keystore, now=ctx.now)
    api.run_investigation(ctx.store, RFC_B, audience="bank_b",
                          settlement_lookup=fx["settlement_lookup"], registry=ctx.registry,
                          now=ctx.now)
    first = api.build_review(ctx.store, RFC_B, audience="bank_b", now=ctx.now)
    approval = api.approve(ctx.store, first.draft_id, holder_id=HOLDER, now=ctx.now)
    api.sign_and_package(ctx.store, approval.approval_id, keystore=ctx.keystore, now=ctx.now)
    removable = sorted(e.ref.token() for e in first.review.included)
    revised = api.build_review(ctx.store, RFC_B, audience="bank_b", now=ctx.now,
                               removed=removable[-1:])
    assert revised.draft_id != first.draft_id
    approval2 = api.approve(ctx.store, revised.draft_id, holder_id=HOLDER, now=ctx.now)
    api.sign_and_package(ctx.store, approval2.approval_id, keystore=ctx.keystore, now=ctx.now)


def test_same_clock_drafts_keep_build_order(now):
    old = SimpleNamespace(status=api.DraftStatus.PACKAGED, created_at=now, draft_id="z-old")
    new = SimpleNamespace(status=api.DraftStatus.APPROVED, created_at=now, draft_id="a-new")
    class Store:
        def list_drafts(self, *, request_id, audience):
            assert request_id == "same-id" and audience == "bank_a"
            return [old, new]
    assert api._live_drafts(Store(), "same-id", "bank_a") == [old, new]
