"""Adversarial integration checks for the published S1 passport core."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from alibi import api
from alibi.core.ingest import IngestError
from alibi.core.signing import DEMO_KEYS, DemoKeystore, KeyPurpose
from alibi.fixtures import maya


NOW = datetime(2026, 11, 7, 12, tzinfo=timezone.utc)
SUPPORTED = api.ClaimStatus.SUPPORTED


@pytest.fixture
def passport(tmp_path, monkeypatch):
    monkeypatch.delenv("ALIBI_MASTER_KEY", raising=False)
    keys = DemoKeystore(tmp_path / "keys", specs={
        **DEMO_KEYS, "merchant_other": (KeyPurpose.RECORD_SIGNING, "other_market"),
    }).ensure()
    with api.open_store(tmp_path / "data" / "passport.db", master_key=Fernet.generate_key()) as store:
        yield SimpleNamespace(store=store, keys=keys, registry=keys.registry())


def load(passport, variant="supported"):
    return api.load_fixture(passport.store, variant, keystore=passport.keys, now=NOW)


def intake(passport, request, *, accounts=None):
    return api.receive_rfc(passport.store, maya.sign_request(request, passport.keys),
                           authenticated_issuer=request["issuer"], registry=passport.registry,
                           account_refs=accounts, now=NOW)


def investigate(passport, request_id, lookup):
    return api.run_investigation(passport.store, request_id, settlement_lookup=lookup,
                                 registry=passport.registry, now=NOW)


def test_rerunning_investigation_with_changed_rationale_revokes_old_consent(passport, monkeypatch):
    from alibi.core import investigate_rules

    fixture = load(passport)
    request_id = "rfc_a_001"
    lookup = fixture["settlement_lookup"]
    initial = investigate(passport, request_id, lookup)
    draft = api.build_review(passport.store, request_id, now=NOW)
    approval = api.approve(passport.store, draft.draft_id, holder_id="holder_maya", now=NOW)

    answer = investigate_rules._Run.answer

    def revised_answer(run, question):
        claim = answer(run, question)
        return claim.model_copy(update={"rationale": f"Rechecked. {claim.rationale}"})

    monkeypatch.setattr(investigate_rules._Run, "answer", revised_answer)
    revised = investigate(passport, request_id, lookup)
    assert revised.run_id != initial.run_id
    assert [claim.evidence_refs for claim in revised.claims] == [claim.evidence_refs for claim in initial.claims]
    assert [claim.rationale for claim in revised.claims] != [claim.rationale for claim in initial.claims]

    with pytest.raises(api.ApprovalError):
        api.sign_and_package(passport.store, approval.approval_id, keystore=passport.keys, now=NOW)


def test_independent_banks_can_reuse_their_own_external_request_id(passport):
    load(passport)
    bank_a, bank_b = maya.build_fixture("supported")["rfcs"]
    collision = {**bank_b, "request_id": bank_a["request_id"]}
    assert intake(passport, collision).issuer == "bank_b"
    assert passport.store.get_request(bank_a["request_id"]).issuer == "bank_a"


def test_followup_cannot_widen_parent_period(passport):
    load(passport)
    parent = maya.build_fixture("supported")["rfcs"][1]
    followup = {**parent, "request_id": "rfc_b_wider", "parent_request_id": parent["request_id"],
                "period": {**parent["period"], "from": "2026-09-01T00:00:00Z"}}
    with pytest.raises(IngestError):
        intake(passport, followup)


def test_valid_issuer_settlement_more_than_five_days_before_credit_cannot_support_it(passport):
    load(passport)
    data = maya.build_fixture("supported")
    settlement = {**data["lookup_events"][0], "effective_at": "2026-10-20T07:45:00Z",
                  "occurred_at": "2026-10-20T08:00:00Z"}
    signed = maya.sign_event(settlement, passport.keys)
    request_id = data["rfcs"][0]["request_id"]
    result = investigate(passport, request_id, lambda _: signed)
    draft = api.build_review(passport.store, request_id, now=NOW)
    approval = api.approve(passport.store, draft.draft_id, holder_id="holder_maya", now=NOW)
    package = api.sign_and_package(passport.store, approval.approval_id, keystore=passport.keys, now=NOW)
    assert SUPPORTED not in [claim.status for claim in (*result.claims, *package.body.claims)]


def test_redacted_bank_package_cannot_verify_for_a_different_subject(passport):
    fixture = load(passport)
    request = maya.build_fixture("supported")["rfcs"][1]
    request_id = request["request_id"]
    api.record_policy(passport.store, request["subject_ref"], [
        {"type": "deny", "rule_id": "deny_subject", "fields": ["subject_ref"]},
    ], confirmed=True, now=NOW)
    investigate(passport, request_id, fixture["settlement_lookup"])
    draft = api.build_review(passport.store, request_id, now=NOW)
    approval = api.approve(passport.store, draft.draft_id, holder_id="holder_maya", now=NOW)
    package = api.sign_and_package(passport.store, approval.approval_id, keystore=passport.keys, now=NOW)
    assert package.body.attachments and all(a.form == "redacted_extract" for a in package.body.attachments)
    options = {"expected_audience": "bank_b", "expected_request_id": request_id,
               "registry": passport.registry, "now": NOW}
    assert api.verify_package(package, expected_subject=request["subject_ref"], **options).accepted
    assert not api.verify_package(package, expected_subject="subj_other", **options).accepted


def test_pending_settlement_uses_published_gap_code(passport):
    fixture = load(passport, "contradiction")
    original = maya.build_fixture("contradiction")["rfcs"][0]
    request = {**original, "request_id": "rfc_pinned_question", "questions": [
        {**original["questions"][0], "question_id": "q.source_of_funds"},
    ]}
    intake(passport, request, accounts=["acc_maya_bank_a"])
    [claim] = investigate(passport, request["request_id"], fixture["settlement_lookup"]).claims
    assert claim.question_id == "q.source_of_funds" and claim.status != SUPPORTED
    assert claim.gap == "settlement_pending_at_booking"


def test_exact_five_day_boundary_still_supports_the_credit(passport):
    load(passport)
    data = maya.build_fixture("supported")
    booked = datetime.fromisoformat(data["bank_txns"][0]["booked_at"])
    effective = (booked - timedelta(days=5)).isoformat().replace("+00:00", "Z")
    settlement = {**data["lookup_events"][0], "effective_at": effective, "occurred_at": effective}
    result = investigate(passport, data["rfcs"][0]["request_id"], lambda _: maya.sign_event(settlement, passport.keys))
    assert result.claims and all(claim.status == SUPPORTED for claim in result.claims)


def test_other_issuer_cannot_supply_settlement_proof_for_original_payout(passport):
    load(passport)
    fixture = maya.build_fixture("supported")
    settlement = {**fixture["lookup_events"][0], "issuer_id": "other_market"}
    payload = maya.sign_event(settlement, passport.keys, kid="merchant_other")
    api.ingest_merchant_event(passport.store, payload, registry=passport.registry,
                              subject_ref=fixture["subject_ref"], now=NOW)
    result = investigate(passport, fixture["rfcs"][0]["request_id"], lambda _: None)
    assert all(claim.status != SUPPORTED for claim in result.claims)


def test_one_payout_cannot_support_two_distinct_credits_in_separate_requests(passport):
    loaded = load(passport)
    fixture = maya.build_fixture("supported")
    credit = {**fixture["bank_txns"][0], "txn_id": "txn_9002"}
    api.ingest_bank_txns(passport.store, [credit], subject_ref=fixture["subject_ref"],
                         connection_id=maya.CONNECTION_ID, now=NOW)
    first = fixture["rfcs"][0]
    second = {**first, "request_id": "rfc_second_credit", "txn_ids": [credit["txn_id"]]}
    intake(passport, second, accounts=[credit["account_ref"]])
    results = [investigate(passport, request["request_id"], loaded["settlement_lookup"])
               for request in (first, second)]
    assert sum(claim.status == SUPPORTED for result in results for claim in result.claims) <= 1
