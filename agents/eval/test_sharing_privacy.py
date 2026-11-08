"""Deny-first preview and exact approval. Public synthetic data only.

    uv run --frozen python -m pytest -q agents/eval/test_sharing_privacy.py
"""
from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest
import rfc8785

from agents.eval.sharing_privacy import (
    CLAIM_FIELDS, DISCLOSABLE_FIELDS, PUBLIC_BODY_FIELDS, WITHHELD_GAP, ApprovalError,
    ApprovalStore, HolderAuthenticator, Policy, PolicyHold, TrustedState, body_digest, build_preview)

NOW = datetime(2026, 11, 3, 10, 0, tzinfo=timezone.utc)
KEY = b"synthetic-demo-holder-key-0001"
MAYA = "maya-001"
HOLDER = "holder-maya"


def rec(record_id, kind, amount, at, **extra):
    base = {"schema_version": "1", "event_id": "evt-" + record_id[4:], "record_id": record_id,
            "revision": 1, "issuer_id": "mock-marketplace", "subject_ref": MAYA, "kind": kind,
            "occurred_at": at, "amount_minor": amount, "currency": "GBP",
            "trust": "issuer_verified"}
    base.update(extra)
    return base


def txn(txn_id, amount, at, description, counterparty):
    return {"txn_id": txn_id, "revision": 1, "subject_ref": MAYA, "kind": "bank_txn",
            "account_ref": "acct-maya-001", "booked_at": at, "amount_minor": amount,
            "currency": "GBP", "description": description, "counterparty_name": counterparty,
            "trust": "bank_observed"}


def make_state(policy: Policy | None = None) -> TrustedState:
    records = {r.get("record_id") or r["txn_id"]: r for r in [
        rec("rec-sale", "sale", 240000, "2026-10-20T10:00:00Z", item="second-hand denim jacket"),
        rec("rec-refund", "refund", 18000, "2026-10-21T12:00:00Z", status="completed",
            related_record_id="rec-sale"),
        rec("rec-fee", "fee", 6000, "2026-10-20T10:01:00Z", fee_type="marketplace_commission"),
        rec("rec-payout", "payout", 216000, "2026-10-26T08:00:00Z", destination="bank",
            covers_record_ids=["rec-sale", "rec-refund", "rec-fee", "rec-pharmacy"]),
        rec("rec-pharmacy", "purchase", 4599, "2026-10-22T09:00:00Z", issuer_id="mock-pharmacy",
            merchant_name="Synthetic Pharmacy", item="prescription refill",
            trust="self_supplied"),
        rec("rec-other", "sale", 99900, "2026-10-23T09:00:00Z", subject_ref="other-002",
            item="vintage watch"),
        txn("txn-credit", 216000, "2026-10-26T15:00:00Z", "MKTPLACE PAYOUT 8841",
            "Synthetic Marketplace"),
        txn("txn-rent", -65000, "2026-10-06T19:00:00Z", "RENT OCT", "Synthetic Lettings Ltd"),
    ]}
    requests = {
        "rfc-a": {"request_id": "rfc-a", "parent_request_id": None, "audience": "bank_a",
                  "subject_ref": MAYA, "txn_ids": ["txn-credit"], "period": None,
                  "question_ids": ["q.source_of_funds", "q.breakdown"],
                  "issued_at": "2026-11-02T09:00:00Z", "expires_at": "2026-11-09T09:00:00Z"},
        "rfc-b": {"request_id": "rfc-b", "parent_request_id": None, "audience": "bank_b",
                  "subject_ref": MAYA, "txn_ids": [],
                  "period": {"from": "2026-10-01T00:00:00Z", "to": "2026-11-01T00:00:00Z"},
                  "question_ids": ["q.income_in_period"],
                  "issued_at": "2026-11-02T14:00:00Z", "expires_at": "2026-11-11T14:00:00Z"},
    }
    return TrustedState(records=records, requests=requests,
                        policy=policy or Policy(version="p1", deny_terms=("Synthetic Pharmacy",)),
                        holders={MAYA: HOLDER})


def ref(r):
    return {"ref": r, "revision": 1}


def claims_a():
    return [
        {"question_id": "q.source_of_funds", "status": "supported",
         "rationale": "Payout rec-payout settled to the bank before txn-credit was booked.",
         "evidence_refs": [ref("rec-payout"), ref("txn-credit")]},
        {"question_id": "q.breakdown", "status": "supported",
         "rationale": "Sales less refunds and fees equal the credit of 2,160.00 GBP.",
         "evidence_refs": [ref("rec-sale"), ref("rec-refund"), ref("rec-fee")],
         "calculation": {"terms": [{"ref": "rec-sale", "amount_minor": 240000},
                                   {"ref": "rec-refund", "amount_minor": -18000},
                                   {"ref": "rec-fee", "amount_minor": -6000}],
                         "total_minor": 216000, "currency": "GBP"}},
    ]


def text(body) -> str:
    return json.dumps(body).casefold()


def claim(preview, qid):
    return next(c for c in preview.body["claims"] if c["question_id"] == qid)


@pytest.fixture
def auth():
    return HolderAuthenticator(KEY)


@pytest.fixture
def store(auth):
    return ApprovalStore(auth)


def approve(store, auth, state, preview, now=NOW, **kw):
    args = {"token": auth.issue(HOLDER, now=now), "recipient": preview.body["audience"],
            "confirmed_digest": preview.body_digest, "now": now}
    args.update(kw)
    return store.approve(state, preview, **args)


# ---------------------------------------------------------------- public body


def test_body_has_exact_public_fields_and_jcs_digest():
    preview = build_preview(make_state(), "rfc-a", claims_a(), now=NOW)
    body = preview.body
    assert set(body) == set(PUBLIC_BODY_FIELDS)
    assert body["request_id"] == "rfc-a" and body["audience"] == "bank_a"
    assert body["case_id"] == "rfc-a" and body["schema_version"] == "1"
    assert body["scope"] == {"txn_ids": ["txn-credit"], "period": None,
                             "question_ids": ["q.source_of_funds", "q.breakdown"]}
    assert body["issued_at"] == "2026-11-03T10:00:00Z"
    assert body["expires_at"] == "2026-11-09T09:00:00Z"     # capped at the RFC's expiry
    assert preview.body_digest == hashlib.sha256(rfc8785.dumps(body)).hexdigest()
    assert [c["status"] for c in body["claims"]] == ["supported", "supported"]
    assert all(set(c) == set(CLAIM_FIELDS) for c in body["claims"])
    assert claim(preview, "q.breakdown")["calculation"]["total_minor"] == 216000


def test_preview_is_deterministic():
    a = build_preview(make_state(), "rfc-a", claims_a(), now=NOW)
    b = build_preview(make_state(), "rfc-a", claims_a(), now=NOW)
    assert a.body == b.body and a.body_digest == b.body_digest


def test_preview_refuses_mixed_transaction_and_period_scope():
    state = make_state()
    state.requests["rfc-a"]["period"] = state.requests["rfc-b"]["period"]
    with pytest.raises(ApprovalError) as exc:
        build_preview(state, "rfc-a", claims_a(), now=NOW)
    assert exc.value.code == "invalid_scope"


def test_only_allowlisted_record_fields_reach_attachments():
    preview = build_preview(make_state(), "rfc-a", claims_a(), now=NOW)
    atts = {a["ref"]: a for a in preview.body["attachments"]}
    assert set(atts) == {"rec-payout", "txn-credit", "rec-sale", "rec-refund", "rec-fee"}
    for att in atts.values():
        assert set(att["fields"]) <= set(DISCLOSABLE_FIELDS)
        assert att["presentation"] == "redacted_extract"
    blob = text(preview.body)
    for secret in ("denim", "marketplace_commission", "acct-maya-001", "mktplace payout",
                   "synthetic marketplace", "maya-001", "evt-sale", "related_record_id",
                   "covers_record_ids", "rec-pharmacy"):
        assert secret not in blob, secret
    assert atts["txn-credit"]["trust"] == "bank_observed"
    assert atts["rec-payout"]["fields"]["destination"] == "bank"


def test_private_exclusions_stay_out_of_body():
    state = make_state()
    proposals = claims_a()
    proposals[0]["evidence_refs"] += [ref("rec-pharmacy"), ref("txn-rent")]
    preview = build_preview(state, "rfc-a", proposals, now=NOW, holder_removals={"rec-fee"})
    reasons = {e["ref"]: e["reason"] for e in preview.private_review["excluded"]}
    assert reasons == {"rec-pharmacy": "policy_deny_term", "txn-rent": "outside_scope",
                       "rec-fee": "removed_by_holder"}
    assert preview.private_review["counts"]["excluded"] == 3
    blob = text(preview.body)
    for leaked in ("rec-pharmacy", "txn-rent", "rec-fee", "removed_by_holder",
                   "policy_deny_term", "outside_scope", "excluded", "pharmacy", "lettings"):
        assert leaked not in blob, leaked
    for c in preview.body["claims"]:          # both claims cited something withheld
        assert c["status"] == "unresolved" and c["gap"] == WITHHELD_GAP
        assert c["calculation"] is None


# ---------------------------------------------------------------- narrative and metadata


def test_narrative_naming_withheld_merchant_is_replaced():
    proposals = claims_a()
    proposals[0]["rationale"] = "Not related to the pharmacy purchase; payout settled first."
    preview = build_preview(make_state(), "rfc-a", proposals, now=NOW)
    c = claim(preview, "q.source_of_funds")
    assert "pharmacy" not in c["rationale"].casefold()
    assert c["status"] == "supported"                      # evidence itself was fine
    assert {"question_id": "q.source_of_funds", "change": "rationale_replaced",
            "reason": "withheld_material"} in preview.private_review["claim_changes"]


@pytest.mark.parametrize("narrative", [
    "Received after paying £650.00 rent.",                 # amount of an uncited record
    "Unrelated to Synthetic Lettings Ltd.",               # counterparty of an uncited record
    "See txn-rent for context.",                          # identifier of an uncited record
    "Proceeds of the denim jacket sale.",                 # withheld field of an attached record
    "Same buyer as rec-other.",                           # another account's record
    "Booked at 2026-10-26T08:00:00Z exactly.",            # a value the policy generalised
    "Covers rec-ghost too.",                              # identifier nobody disclosed
])
def test_narrative_touching_withheld_material_is_replaced(narrative):
    policy = Policy(version="p1", deny_terms=("Synthetic Pharmacy",),
                    generalise={"occurred_at": "day"})
    proposals = claims_a()
    proposals[0]["rationale"] = narrative
    proposals[1]["gap"] = narrative
    preview = build_preview(make_state(policy), "rfc-a", proposals, now=NOW)
    blob = text(preview.body)
    assert narrative.casefold() not in blob
    for word in ("650.00", "lettings", "txn-rent", "denim", "rec-other", "rec-ghost",
                 "2026-10-26t08:00:00z"):
        assert word not in blob, word


def test_clean_narrative_survives():
    preview = build_preview(make_state(), "rfc-a", claims_a(), now=NOW)
    assert claim(preview, "q.source_of_funds")["rationale"] == claims_a()[0]["rationale"]
    assert claim(preview, "q.breakdown")["rationale"] == claims_a()[1]["rationale"]


def test_unlisted_claim_keys_and_ref_metadata_are_dropped():
    proposals = claims_a()
    proposals[0]["model_notes"] = "also saw the prescription refill"
    proposals[0]["excluded"] = ["rec-pharmacy"]
    proposals[0]["evidence_refs"][0]["note"] = "covers rec-pharmacy"
    preview = build_preview(make_state(), "rfc-a", proposals, now=NOW)
    c = claim(preview, "q.source_of_funds")
    assert set(c) == set(CLAIM_FIELDS)
    assert c["evidence_refs"][0] == {"ref": "rec-payout", "revision": 1}
    blob = text(preview.body)
    assert "prescription" not in blob and "rec-pharmacy" not in blob
    dropped = {ch.get("field") for ch in preview.private_review["claim_changes"]}
    assert {"model_notes", "excluded"} <= dropped


def test_calculation_citing_withheld_record_is_dropped_and_claim_downgraded():
    proposals = claims_a()
    calc = proposals[1]["calculation"]
    calc["terms"].append({"ref": "rec-pharmacy", "amount_minor": -4599})
    calc["total_minor"] -= 4599
    preview = build_preview(make_state(), "rfc-a", proposals, now=NOW)
    c = claim(preview, "q.breakdown")
    assert c["status"] == "unresolved" and c["calculation"] is None
    assert c["gap"] == WITHHELD_GAP
    assert "45.99" not in text(preview.body) and "4599" not in text(preview.body)


def test_wrong_arithmetic_is_dropped():
    proposals = claims_a()
    proposals[1]["calculation"]["total_minor"] = 220000
    preview = build_preview(make_state(), "rfc-a", proposals, now=NOW)
    c = claim(preview, "q.breakdown")
    assert c["calculation"] is None and c["status"] == "unresolved"


def test_stale_citation_is_not_attached_twice_or_orphaned():
    proposals = claims_a()
    proposals[1]["evidence_refs"].append({"ref": "rec-payout", "revision": 0})
    preview = build_preview(make_state(), "rfc-a", proposals, now=NOW)
    refs = {a["ref"] for a in preview.body["attachments"]}
    cited = {r["ref"] for c in preview.body["claims"] for r in c["evidence_refs"]}
    assert cited <= refs and "rec-payout" in refs
    assert claim(preview, "q.breakdown")["status"] == "unresolved"


def test_out_of_scope_question_dropped_and_unanswered_question_marked_unresolved():
    proposals = [claims_a()[0], {"question_id": "q.lifestyle", "status": "supported",
                                 "rationale": "x", "evidence_refs": [ref("rec-sale")]}]
    preview = build_preview(make_state(), "rfc-a", proposals, now=NOW)
    assert [c["question_id"] for c in preview.body["claims"]] == ["q.source_of_funds",
                                                                  "q.breakdown"]
    assert claim(preview, "q.breakdown")["status"] == "unresolved"
    assert "q.lifestyle" not in text(preview.body)
    assert "rec-sale" not in {a["ref"] for a in preview.body["attachments"]}


def test_foreign_and_unknown_records_never_attached():
    proposals = claims_a()
    proposals[0]["evidence_refs"] += [ref("rec-other"), ref("rec-ghost")]
    preview = build_preview(make_state(), "rfc-a", proposals, now=NOW)
    reasons = {e["ref"]: e["reason"] for e in preview.private_review["excluded"]}
    assert reasons["rec-other"] == "foreign_subject"
    assert reasons["rec-ghost"] == "unknown_record"
    assert "vintage" not in text(preview.body) and "other-002" not in text(preview.body)


def test_period_request_withholds_records_outside_period():
    state = make_state()
    state.records["rec-late"] = rec("rec-late", "sale", 5000, "2026-11-01T00:00:00Z")
    proposals = [{"question_id": "q.income_in_period", "status": "supported",
                  "rationale": "Marketplace sales and refunds in October.",
                  "evidence_refs": [ref("rec-sale"), ref("rec-refund"), ref("rec-late")]}]
    preview = build_preview(state, "rfc-b", proposals, now=NOW)
    reasons = {e["ref"]: e["reason"] for e in preview.private_review["excluded"]}
    assert reasons == {"rec-late": "outside_period"}       # [from, to) is half-open


def test_generalised_and_unsupported_restrictions():
    policy = Policy(version="p1", generalise={"occurred_at": "day", "amount_minor": "band"},
                    deny_fields=frozenset({"destination"}))
    preview = build_preview(make_state(policy), "rfc-a", claims_a()[:1], now=NOW)
    payout = next(a for a in preview.body["attachments"] if a["ref"] == "rec-payout")
    assert payout["fields"]["occurred_at"] == "2026-10-26"
    assert "amount_minor" not in payout["fields"]           # cannot band: withheld instead
    assert "destination" not in payout["fields"]
    assert {"ref": "rec-payout", "field": "occurred_at", "to": "day"} in \
        preview.private_review["generalised"]


def test_unresolved_rule_holds_preview_and_recipient_restriction_blocks():
    held = Policy(version="p1", unresolved_rules=("hide anything embarrassing",))
    with pytest.raises(PolicyHold) as exc:
        build_preview(make_state(held), "rfc-a", claims_a(), now=NOW)
    assert exc.value.code == "unresolved_rules"
    only_b = Policy(version="p1", allowed_recipients=frozenset({"bank_b"}))
    with pytest.raises(PolicyHold) as exc:
        build_preview(make_state(only_b), "rfc-a", claims_a(), now=NOW)
    assert exc.value.code == "recipient_not_allowed"


# ---------------------------------------------------------------- approval


def test_exact_approval_validates_against_current_state(store, auth):
    state = make_state()
    preview = build_preview(state, "rfc-a", claims_a(), now=NOW)
    record = approve(store, auth, state, preview)
    assert record.body_digest == preview.body_digest
    assert record.recipient == "bank_a" and record.request_id == "rfc-a"
    assert record.policy_version == "p1" and record.holder_id == HOLDER
    assert ("txn-credit", 1) in record.source_revisions
    assert record.expires_at == "2026-11-03T10:15:00Z"
    later = NOW + timedelta(minutes=5)
    assert store.validate(record.approval_id, preview.body, state, now=later) == record
    roundtrip = json.loads(rfc8785.dumps(preview.body))      # JCS reorders keys; still valid
    assert store.validate(record.approval_id, roundtrip, state, now=later) == record


def expect(code, fn, *a, **kw):
    with pytest.raises(ApprovalError) as exc:
        fn(*a, **kw)
    assert exc.value.code == code


def test_any_body_edit_invalidates(store, auth):
    state = make_state()
    preview = build_preview(state, "rfc-a", claims_a(), now=NOW)
    record = approve(store, auth, state, preview)
    tampered = copy.deepcopy(preview.body)
    tampered["claims"][0]["rationale"] = "Edited after approval."
    expect("body_changed", store.validate, record.approval_id, tampered, state, now=NOW)
    edited = build_preview(state, "rfc-a", claims_a(), now=NOW, holder_removals={"rec-fee"})
    expect("body_changed", store.validate, record.approval_id, edited.body, state, now=NOW)


def test_approval_needs_matching_digest_recipient_and_holder(store, auth):
    state = make_state()
    preview = build_preview(state, "rfc-a", claims_a(), now=NOW)
    expect("digest_mismatch", approve, store, auth, state, preview, confirmed_digest="0" * 64)
    expect("recipient_mismatch", approve, store, auth, state, preview, recipient="bank_b")
    expect("holder_mismatch", approve, store, auth, state, preview,
           token=auth.issue("holder-someone-else", now=NOW))
    forged = HolderAuthenticator(b"attacker-key-not-the-real-one").issue(HOLDER, now=NOW)
    expect("holder_unauthenticated", approve, store, auth, state, preview, token=forged)
    stale_session = auth.issue(HOLDER, now=NOW - timedelta(hours=2))
    expect("holder_unauthenticated", approve, store, auth, state, preview, token=stale_session)
    expect("unknown_approval", store.validate, "apr-made-up", preview.body, state, now=NOW)


def test_policy_change_invalidates(store, auth):
    state = make_state()
    preview = build_preview(state, "rfc-a", claims_a(), now=NOW)
    record = approve(store, auth, state, preview)
    state.policy = Policy(version="p2", deny_terms=("Synthetic Pharmacy",))
    expect("policy_changed", store.validate, record.approval_id, preview.body, state, now=NOW)


def test_policy_change_requires_a_new_preview_before_approval(store, auth):
    state = make_state()
    preview = build_preview(state, "rfc-a", claims_a(), now=NOW)
    state.policy = Policy(version="p2", generalise={"occurred_at": "day"})
    expect("policy_changed", approve, store, auth, state, preview)
    revised = build_preview(state, "rfc-a", claims_a(), now=NOW)
    assert revised.body_digest != preview.body_digest
    assert approve(store, auth, state, revised).body_digest == revised.body_digest


def test_preview_digest_binds_the_server_side_body_before_approval(store, auth):
    state = make_state()
    preview = build_preview(state, "rfc-a", claims_a(), now=NOW)
    preview.body["claims"][0]["rationale"] = "Edited after preview."
    expect("digest_mismatch", approve, store, auth, state, preview,
           confirmed_digest=body_digest(preview.body))


def test_denied_issuer_metadata_does_not_reach_the_public_body():
    state = make_state()
    state.policy = Policy(version="p1", deny_fields=frozenset({"issuer_id"}))
    preview = build_preview(state, "rfc-a", claims_a(), now=NOW)
    assert "mock-marketplace" not in text(preview.body)
    assert all(att["issuer_id"] is None for att in preview.body["attachments"])
    assert any(item["reason"] == "policy_deny_structural"
               for item in preview.private_review["excluded"])


@pytest.mark.parametrize("changed", ["rec-payout", "txn-credit"])
def test_relevant_source_revision_invalidates(store, auth, changed):
    state = make_state()
    preview = build_preview(state, "rfc-a", claims_a(), now=NOW)
    record = approve(store, auth, state, preview)
    state.records[changed] = {**state.records[changed], "revision": 2, "status": "pending"}
    expect("source_changed", store.validate, record.approval_id, preview.body, state, now=NOW)


def test_correction_to_withheld_cited_record_invalidates(store, auth):
    state = make_state()
    proposals = claims_a()
    proposals[0]["evidence_refs"].append(ref("rec-pharmacy"))
    preview = build_preview(state, "rfc-a", proposals, now=NOW)
    assert "rec-pharmacy" not in text(preview.body)
    record = approve(store, auth, state, preview)
    state.records["rec-pharmacy"] = {**state.records["rec-pharmacy"], "revision": 2}
    expect("source_changed", store.validate, record.approval_id, preview.body, state, now=NOW)


def test_recipient_or_holder_change_in_trusted_state_invalidates(store, auth):
    state = make_state()
    preview = build_preview(state, "rfc-a", claims_a(), now=NOW)
    record = approve(store, auth, state, preview)
    state.requests["rfc-a"] = {**state.requests["rfc-a"], "audience": "bank_b"}
    expect("recipient_mismatch", store.validate, record.approval_id, preview.body, state, now=NOW)
    state = make_state()
    state.holders[MAYA] = "holder-new"
    expect("holder_mismatch", store.validate, record.approval_id, preview.body, state, now=NOW)


def test_expiry_invalidates(store, auth):
    state = make_state()
    preview = build_preview(state, "rfc-a", claims_a(), now=NOW)
    record = approve(store, auth, state, preview)
    expect("approval_expired", store.validate, record.approval_id, preview.body, state,
           now=NOW + timedelta(minutes=15))
    expect("request_expired", store.validate, record.approval_id, preview.body, state,
           now=datetime(2026, 11, 9, 9, 0, tzinfo=timezone.utc))


def test_newly_denied_term_blocks_dispatch_even_if_version_unchanged(store, auth):
    state = make_state()
    preview = build_preview(state, "rfc-a", claims_a(), now=NOW)
    record = approve(store, auth, state, preview)
    state.policy = Policy(version="p1", deny_kinds=frozenset({"fee"}))
    expect("policy_changed", store.validate, record.approval_id, preview.body, state, now=NOW)


def test_first_approval_cannot_authorise_second_recipient_package(store, auth):
    state = make_state()
    first = build_preview(state, "rfc-a", claims_a(), now=NOW)
    record_a = approve(store, auth, state, first)
    proposals_b = [{"question_id": "q.income_in_period", "status": "supported",
                    "rationale": "Marketplace sales and refunds in October.",
                    "evidence_refs": [ref("rec-sale"), ref("rec-refund")]}]
    second = build_preview(state, "rfc-b", proposals_b, now=NOW)
    assert second.body["package_id"] != first.body["package_id"]
    assert second.body["audience"] == "bank_b" and second.body["request_id"] == "rfc-b"
    expect("body_changed", store.validate, record_a.approval_id, second.body, state, now=NOW)
    record_b = approve(store, auth, state, second)
    assert record_b.approval_id != record_a.approval_id
    assert store.validate(record_b.approval_id, second.body, state, now=NOW) == record_b
    assert "txn-credit" not in text(second.body) and "payout" not in text(second.body)


# ---------------------------------------------------------------- public dev case 1


def test_canonical_case_two_recipients_from_one_passport(store, auth):
    from agents.data.api import canonical_case, lookup, without_gold

    case = without_gold(canonical_case())
    rfc_a, rfc_b = case["rfcs"]
    records = {}
    for d in case["merchant_deliveries"]:
        ev = d["event"]
        if ev["record_id"] not in records or ev["revision"] > records[ev["record_id"]]["revision"]:
            # Fixture assumption: mock-marketplace events arrive signed, so issuer_verified.
            records[ev["record_id"]] = {**ev, "trust": "issuer_verified"}
    for d in case["bank_deliveries"]:
        row = d["row"]
        records[row["txn_id"]] = {**row, "kind": "bank_txn", "subject_ref": case["subject_ref"],
                                  "trust": "bank_observed"}
    requests = {r["request_id"]: r for r in case["rfcs"]}
    state = TrustedState(records=records, requests=requests, policy=Policy(version="p1"),
                         holders={case["subject_ref"]: HOLDER})
    credit = rfc_a["txn_ids"][0]
    merchant = [r for r in records.values() if r["kind"] in ("sale", "refund", "fee")]
    payout, = (
        record for record in lookup(
            case, rfc_a["issued_at"], payout_reference=records[credit]["payment_reference"]
        ) if record["kind"] == "payout"
    )
    records[payout["record_id"]] = {**payout, "trust": "issuer_verified"}
    sign = {"sale": 1, "refund": -1, "fee": -1}
    terms = [{"ref": r["record_id"], "amount_minor": sign[r["kind"]] * r["amount_minor"]}
             for r in merchant]
    proposals_a = [
        {"question_id": "q.source_of_funds", "status": "supported",
         "rationale": f"Payout {payout['record_id']} to the bank matches {credit}.",
         "evidence_refs": [ref(payout["record_id"]), ref(credit)]},
        {"question_id": "q.breakdown", "status": "supported",
         "rationale": "Sales less refunds and fees equal the credit.",
         "evidence_refs": [ref(r["record_id"]) for r in merchant],
         "calculation": {"terms": terms, "total_minor": sum(t["amount_minor"] for t in terms),
                         "currency": "GBP"}},
    ]
    now = datetime(2026, 11, 4, 15, 0, tzinfo=timezone.utc)
    pa = build_preview(state, rfc_a["request_id"], proposals_a, now=now)
    assert claim(pa, "q.breakdown")["calculation"]["total_minor"] == 216000
    assert [c["status"] for c in pa.body["claims"]] == ["supported", "supported"]

    proposals_b = [{"question_id": rfc_b["question_ids"][0], "status": "supported",
                    "rationale": "Marketplace sales and refunds in the period.",
                    "evidence_refs": [ref(r["record_id"]) for r in merchant
                                      if r["kind"] in ("sale", "refund")]}]
    pb = build_preview(state, rfc_b["request_id"], proposals_b, now=now)
    for p in (pa, pb):                       # no other bank rows, no item text, no account ref
        blob = text(p.body)
        for withheld in ("rent oct", "grocery", "streaming", "clothing", "acct-", "maya-001"):
            assert withheld not in blob, withheld
    assert pb.body["claims"][0]["status"] == "supported"
    assert pa.body["audience"] == "bank_a" and pb.body["audience"] == "bank_b"
    assert pa.body["package_id"] != pb.body["package_id"]

    ra = approve(store, auth, state, pa, now=now)
    rb = approve(store, auth, state, pb, now=now)
    assert store.validate(ra.approval_id, pa.body, state, now=now) == ra
    assert store.validate(rb.approval_id, pb.body, state, now=now) == rb
    expect("body_changed", store.validate, ra.approval_id, pb.body, state, now=now)
    expect("body_changed", store.validate, rb.approval_id, pa.body, state, now=now)
