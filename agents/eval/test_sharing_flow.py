"""Public fixture integration checks for the local sharing path."""

import json
import secrets
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agents.data.api import canonical_case, known_at, lookup, without_gold
from agents.eval.api import dispatch_approved
from agents.eval.sharing_delivery import (
    ISSUER_RECORD_DOMAIN,
    PURPOSE_ISSUER_RECORD,
    PURPOSE_PACKAGE,
    BankInbox,
    ConflictError,
    TrustRegistry,
    VerificationError,
    body_digest,
    canonical,
    parse_envelope,
    sign_package,
    sign_record,
    verify_detached,
    verify_package,
)
from agents.eval.sharing_privacy import (
    ApprovalError,
    ApprovalStore,
    HolderAuthenticator,
    Policy,
    TrustedState,
    build_preview,
)
from agents.execution.api import probe_ids, summarise

PACKAGE_KEY_ID = "alibi-demo-package"
ISSUER_KEY_ID = "mock-marketplace-record"
CREDENTIALS = {"fixture-a": "bank_a", "fixture-b": "bank_b"}


@pytest.fixture
def maya_case():
    return without_gold(canonical_case())


@pytest.fixture
def dispatch_time():
    # Both published RFCs are open at this point in the synthetic timeline.
    return datetime.fromisoformat("2026-11-04T14:01:00+00:00")


@pytest.fixture
def maya_evidence(maya_case):
    """Prepare the validated inputs for the sharing test from public evidence."""
    request_a, request_b = maya_case["rfcs"]
    seen = known_at(maya_case, request_a["issued_at"])
    merchant = {item["event"]["record_id"]: item["event"] for item in seen["merchant_deliveries"]}
    credit, = (
        item["row"] for item in seen["bank_deliveries"]
        if item["row"]["txn_id"] in request_a["txn_ids"]
    )
    settlement, = (
        event for event in lookup(
            maya_case, request_a["issued_at"], payout_reference=credit["payment_reference"]
        )
        if event["kind"] == "payout"
    )
    assert settlement["record_id"] not in merchant
    assert settlement["destination"] == "bank" and settlement["status"] == "settled"
    assert settlement["settled_at"] <= credit["booked_at"]
    assert settlement["currency"] == credit["currency"] == "GBP"
    assert settlement["amount_minor"] == credit["amount_minor"]

    breakdown = [merchant[record_id] for record_id in settlement["covers_record_ids"]]
    period = request_b["period"]
    period_records = [
        event for event in merchant.values()
        if event["kind"] in {"sale", "refund"}
        and period["from"] <= event["occurred_at"] < period["to"]
    ]
    return credit, settlement, breakdown, period_records


@pytest.fixture
def mock_bank(tmp_path):
    package_key = Ed25519PrivateKey.generate()
    issuer_key = Ed25519PrivateKey.generate()
    registry = TrustRegistry()
    registry.add(PACKAGE_KEY_ID, PURPOSE_PACKAGE, "alibi", package_key.public_key())
    registry.add(
        ISSUER_KEY_ID, PURPOSE_ISSUER_RECORD, "mock-marketplace", issuer_key.public_key()
    )
    return BankInbox(tmp_path, registry, CREDENTIALS), package_key, issuer_key


def claim(question_id, evidence, rationale, *, calculation=None):
    return {
        "question_id": question_id,
        "status": "supported",
        "rationale": rationale,
        "evidence_refs": [
            {"ref": record.get("record_id") or record["txn_id"], "revision": record["revision"]}
            for record in evidence
        ],
        "calculation": calculation,
    }


@pytest.fixture
def sharing_state(maya_case, maya_evidence, mock_bank):
    _, _, issuer_key = mock_bank
    credit, settlement, breakdown, period_records = maya_evidence
    request_a, request_b = maya_case["rfcs"]
    known = known_at(maya_case, request_a["issued_at"])
    records = {}
    for event in [item["event"] for item in known["merchant_deliveries"]] + [settlement]:
        signed = sign_record(event, issuer_key, ISSUER_KEY_ID)
        assert verify_detached(
            ISSUER_RECORD_DOMAIN, event, signed["signature"], issuer_key.public_key()
        )
        records[event["record_id"]] = {**event, "trust": "issuer_verified"}
    for item in known["bank_deliveries"]:
        row = item["row"]
        records[row["txn_id"]] = {
            **row, "subject_ref": maya_case["subject_ref"], "kind": "bank_txn",
            "trust": "bank_observed",
        }

    terms = [
        {
            "ref": event["record_id"],
            "amount_minor": event["amount_minor"] * (1 if event["kind"] == "sale" else -1),
        }
        for event in breakdown
    ]
    total = sum(term["amount_minor"] for term in terms)
    assert total == credit["amount_minor"] == 216000
    assert sum(event["amount_minor"] for event in period_records if event["kind"] == "sale") == 240000
    assert sum(event["amount_minor"] for event in period_records if event["kind"] == "refund") == 18000

    state = TrustedState(
        records,
        {rfc["request_id"]: rfc for rfc in (request_a, request_b)},
        Policy("maya-policy-v1", deny_kinds=frozenset({"purchase"}), deny_fields=frozenset({"item"})),
        {maya_case["subject_ref"]: "maya-holder"},
    )
    claims_a = [
        claim(request_a["question_ids"][0], [settlement, credit], "The bank settlement matches the credit."),
        claim(request_a["question_ids"][1], breakdown, "The net is sales less refunds and fees.",
              calculation={"terms": terms, "total_minor": total, "currency": "GBP"}),
    ]
    claims_b = [
        claim(request_b["question_ids"][0], period_records,
              "The records show 240000 pence in sales and 18000 pence in refunds.")
    ]
    return state, request_a, claims_a, request_b, claims_b


def test_public_sharing_probes(sharing_state, mock_bank, dispatch_time):
    state, request_a, claims_a, request_b, claims_b = sharing_state
    bank, package_key, issuer_key = mock_bank
    authenticator = HolderAuthenticator(secrets.token_bytes(32))
    approvals = ApprovalStore(authenticator)
    holder_token = authenticator.issue("maya-holder", now=dispatch_time)
    outcomes = {}

    def approve(preview):
        return approvals.approve(
            state, preview, token=holder_token, recipient=preview.body["audience"],
            confirmed_digest=preview.body_digest, now=dispatch_time,
        )

    def dispatch(preview, approval_id, *, recipient=None, now=dispatch_time, retry_bytes=None):
        return dispatch_approved(
            preview.body, approval_id, approvals=approvals, state=state,
            recipient=recipient or preview.body["audience"], inbox=bank,
            signing_key=package_key, key_id=PACKAGE_KEY_ID, now=now, retry_bytes=retry_bytes,
        )

    def refused(probe, exc_type, code, attempt):
        with pytest.raises(exc_type) as caught:
            attempt()
        if code is not None:
            assert caught.value.code == code
        outcomes[probe] = "refused"

    preview_a = build_preview(state, request_a["request_id"], claims_a, now=dispatch_time)
    preview_b = build_preview(state, request_b["request_id"], claims_b, now=dispatch_time)
    for preview, request in [(preview_a, request_a), (preview_b, request_b)]:
        assert [item["question_id"] for item in preview.body["claims"]] == request["question_ids"]
        assert all(item["status"] == "supported" for item in preview.body["claims"])

    refused("SP-01", ApprovalError, "approval_missing", lambda: dispatch(preview_a, None))
    assert not bank.list_receipts("fixture-a")

    approval_a = approve(preview_a)
    payout_ref = next(att["ref"] for att in preview_a.body["attachments"] if att["kind"] == "payout")
    edited = build_preview(
        state, request_a["request_id"], claims_a, now=dispatch_time, holder_removals=[payout_ref]
    )
    assert any(item["ref"] == payout_ref for item in edited.private_review["excluded"])
    assert payout_ref.encode() not in canonical(edited.body)
    refused("SP-02", ApprovalError, "body_changed", lambda: dispatch(edited, approval_a.approval_id))
    refused("SP-03", ApprovalError, "recipient_mismatch",
            lambda: dispatch(preview_a, approval_a.approval_id, recipient="bank_b"))
    refused("SP-05", ApprovalError, "approval_expired",
            lambda: dispatch(preview_a, approval_a.approval_id, now=dispatch_time + timedelta(minutes=16)))

    source = preview_a.body["attachments"][0]["ref"]
    original_record = state.records[source]
    state.records[source] = {**original_record, "revision": original_record["revision"] + 1}
    refused("SP-06", ApprovalError, "source_changed", lambda: dispatch(preview_a, approval_a.approval_id))
    state.records[source] = original_record

    original_policy = state.policy
    state.policy = replace(original_policy, version="maya-policy-v2")
    refused("SP-07", ApprovalError, "policy_changed", lambda: dispatch(preview_a, approval_a.approval_id))
    state.policy = original_policy
    assert not bank.list_receipts("fixture-a") and not bank.list_receipts("fixture-b")

    # A second bank gets a new consent decision for its separately scoped package.
    with pytest.raises(ApprovalError):
        dispatch(preview_b, approval_a.approval_id)
    approval_b = approve(preview_b)
    assert approval_b.approval_id != approval_a.approval_id
    assert preview_a.body["package_id"] != preview_b.body["package_id"]

    raw_a, first = dispatch(preview_a, approval_a.approval_id)
    raw_b, second = dispatch(preview_b, approval_b.approval_id)
    assert first.created and second.created
    assert first.receipt.request_id == request_a["request_id"]
    assert second.receipt.request_id == request_b["request_id"]
    assert canonical(parse_envelope(raw_a)["body"]) == canonical(preview_a.body)
    assert canonical(parse_envelope(raw_b)["body"]) == canonical(preview_b.body)
    assert not any(b"prescription eyewear" in raw or b"private_review" in raw for raw in (raw_a, raw_b))
    assert {item["kind"] for item in preview_b.body["attachments"]} == {"sale", "refund"}
    assert {item["ref"] for item in preview_b.body["attachments"]} <= {
        item["ref"] for item in preview_a.body["attachments"]
    }

    bank_a = bank.get("fixture-a", first.receipt.package_id)
    assert bank_a.verification["package_signature"] == "valid"
    assert all(claim["status"] == "supported" for claim in bank_a.verification["claims"])
    assert {att["trust"] for att in bank_a.verification["attachments"]} == {
        "issuer_verified", "bank_observed"
    }
    issuer_extracts = [
        att for att in bank_a.verification["attachments"] if att["trust"] == "issuer_verified"
    ]
    assert issuer_extracts
    assert all(att["source_signature"] == "absent" and att["attested_by"] == "package_signer"
               for att in issuer_extracts)
    outcomes["SP-32"] = "accepted"

    malformed = parse_envelope(raw_a)
    malformed["signature"]["signature"] = "A"
    with pytest.raises(VerificationError, match="bad_encoding"):
        verify_package(canonical(malformed), recipient="bank_a", registry=bank.registry, now=dispatch_time)

    for section, field in [
        ("claims", "evidence_refs"), ("claims", "status"), ("attachments", "trust")
    ]:
        malformed = parse_envelope(raw_a)
        malformed["body"][section][0][field] = [{}] if field == "evidence_refs" else {}
        with pytest.raises(VerificationError, match="schema"):
            verify_package(canonical(malformed), recipient="bank_a", registry=bank.registry, now=dispatch_time)

    assert [r.package_id for r in bank.list_receipts("fixture-a")] == [first.receipt.package_id]
    assert [r.package_id for r in bank.list_receipts("fixture-b")] == [second.receipt.package_id]
    outcomes["SP-17"] = "refused"
    refused("SP-18", KeyError, None, lambda: bank.get("fixture-a", second.receipt.package_id))
    with pytest.raises(KeyError):
        bank.get("fixture-b", first.receipt.package_id)

    tampered = parse_envelope(raw_a)
    tampered["body"]["claims"][0]["rationale"] = "Attacker altered the package."
    refused("SP-25", VerificationError, "bad_signature",
            lambda: bank.deliver("bank_a", canonical(tampered), now=dispatch_time))
    refused("SP-26", VerificationError, "wrong_recipient",
            lambda: bank.deliver("bank_b", raw_a, now=dispatch_time))
    refused("SP-27", VerificationError, "expired",
            lambda: verify_package(raw_a, recipient="bank_a", registry=bank.registry,
                                   now=dispatch_time + timedelta(days=8)))
    assert bank.get("fixture-a", first.receipt.package_id).raw == raw_a

    unknown = sign_package(preview_a.body, package_key, "unregistered-key",
                           approved_body_digest=preview_a.body_digest)
    refused("SP-28", VerificationError, "unknown_key",
            lambda: bank.deliver("bank_a", unknown, now=dispatch_time))
    wrong_purpose = sign_package(preview_a.body, issuer_key, ISSUER_KEY_ID,
                                 approved_body_digest=preview_a.body_digest)
    refused("SP-28", VerificationError, "wrong_key_purpose",
            lambda: bank.deliver("bank_a", wrong_purpose, now=dispatch_time))

    claimed = deepcopy(preview_a.body)
    issuer_attachment = next(att for att in claimed["attachments"] if att["trust"] == "issuer_verified")
    issuer_attachment["presentation"] = "full_record"
    issuer_record = state.records[issuer_attachment["ref"]]
    forged = sign_record(issuer_record, Ed25519PrivateKey.generate(), ISSUER_KEY_ID)
    claimed_bytes = sign_package(claimed, package_key, PACKAGE_KEY_ID,
                                 approved_body_digest=body_digest(claimed))
    refused("SP-29", VerificationError, "source_signature_invalid",
            lambda: bank.deliver("bank_a", claimed_bytes, now=dispatch_time, issuer_records=[forged]))

    replay_bytes, replay = dispatch(preview_a, approval_a.approval_id, retry_bytes=raw_a)
    assert replay_bytes == raw_a and replay.receipt == first.receipt and not replay.created

    # A conflicting, correctly signed body under an existing ID tests the receiver's
    # idempotency rule independently of holder-side package ID generation.
    changed_body = deepcopy(preview_a.body)
    changed_body["claims"][0]["rationale"] = "A different, validly signed explanation."
    conflict_bytes = sign_package(
        changed_body, package_key, PACKAGE_KEY_ID, approved_body_digest=body_digest(changed_body)
    )
    refused("SP-31", ConflictError, "package_id_conflict",
            lambda: bank.deliver("bank_a", conflict_bytes, now=dispatch_time))
    assert bank.list_receipts("fixture-a") == [first.receipt]

    revised_approval = approve(edited)
    revised_raw, revised = dispatch(edited, revised_approval.approval_id)
    assert revised.created
    assert parse_envelope(revised_raw)["body"] == edited.body

    follow_up_id = request_a["request_id"] + "-follow-up"
    state.requests[follow_up_id] = {**request_a, "request_id": follow_up_id,
                                   "parent_request_id": request_a["request_id"]}
    follow_up = build_preview(state, follow_up_id, claims_a, now=dispatch_time)
    refused("SP-04", ApprovalError, "body_changed",
            lambda: dispatch(follow_up, approval_a.approval_id))
    _, follow_up_delivery = dispatch(follow_up, approve(follow_up).approval_id)
    assert follow_up_delivery.created and follow_up_delivery.receipt.request_id == follow_up_id

    # Trusted ingest classifies a holder-supplied copy as self_supplied. The bank
    # must accept the approved extract without treating it as an issuer proof.
    state.records[payout_ref] = {**state.records[payout_ref], "trust": "self_supplied"}
    holder_claim = claim(request_a["question_ids"][0], [state.records[payout_ref]],
                         "The holder supplied this record.")
    holder_claim.update(status="unresolved", gap="Independent settlement evidence is missing.")
    holder_preview = build_preview(state, request_a["request_id"], [holder_claim], now=dispatch_time)
    _, holder_delivery = dispatch(holder_preview, approve(holder_preview).approval_id)
    assert holder_delivery.created
    verified_import, = bank.get("fixture-a", holder_delivery.receipt.package_id).verification["attachments"]
    assert verified_import["trust"] == "self_supplied"
    assert verified_import["source_signature"] == "absent"
    outcomes["SP-30"] = "accepted"

    report = summarise(outcomes)
    unexercised = sorted(set(probe_ids()) - outcomes.keys())
    assert report["failed"] == unexercised
    assert report["probes_blocked"] == sum(result == "refused" for result in outcomes.values())
    assert report["controls_allowed"] == 2
    print(json.dumps({"observed": outcomes, "unexercised": unexercised}, sort_keys=True))
