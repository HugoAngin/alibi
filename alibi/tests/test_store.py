"""Store tests: idempotency, revisions and invalidation, encryption at rest, master keys."""

import os
import sqlite3
import stat
import threading
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.fernet import Fernet

from alibi.core import canon
from alibi.core import store as store_module
from alibi.core.schemas import (
    AgeRule,
    Alert,
    AlertKind,
    Approval,
    ApprovalStatus,
    Attachment,
    AuditEvent,
    BankTxn,
    CalcTerm,
    Calculation,
    Claim,
    ClaimStatus,
    ContextPackage,
    Delivery,
    DenyRule,
    DerivedState,
    Draft,
    DraftStatus,
    EvidenceRecord,
    EvidenceRef,
    Gap,
    Link,
    LinkState,
    MerchantEvent,
    PackageBody,
    PackageClaim,
    PackageScope,
    Policy,
    Question,
    RecordKind,
    RequestForContext,
    Review,
    SignatureEnvelope,
    SigningDomain,
    SourceTrust,
    TraceEvent,
)
from alibi.core.store import (
    IdempotencyConflict,
    NamespaceError,
    NotFound,
    RevisionConflict,
    Store,
    StoreError,
    dependency_tokens,
    load_master_key,
)

SUBJ = "subj_maya"
NOW = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)
RECEIVED = datetime(2026, 11, 3, 8, 0, tzinfo=timezone.utc)
NO_INVALIDATIONS = {"links": [], "claims": [], "drafts": [], "approvals": []}


# --------------------------------------------------------------------------- builders


def rref(record_id, revision=1):
    return EvidenceRef(ref_type="record", ref_id=record_id, revision=revision)


def tref(txn_id, revision=1):
    return EvidenceRef(ref_type="txn", ref_id=txn_id, revision=revision)


def event(record_id="po_5001", revision=1, **over) -> MerchantEvent:
    data = {
        "event_id": f"evt_{record_id}_r{revision}", "record_id": record_id, "revision": revision,
        "issuer_id": "threadline", "subject_ref": SUBJ, "kind": "payout",
        "occurred_at": "2026-11-02T09:00:00Z", "amount_pence": 216000, "currency": "GBP",
        "payment_ref": "PO-5001", "status": "initiated",
    }
    if revision > 1:
        data["corrects_revision"] = revision - 1
    data.update(over)
    return MerchantEvent.model_validate(data)


def settlement(revision=1, **over) -> MerchantEvent:
    fields = {"kind": "settlement", "settlement_ref": "STL-88001", "status": "settled",
              "occurred_at": "2026-11-03T07:45:00Z", "effective_at": "2026-11-03T07:45:00Z"}
    if revision > 1:
        fields.update(status="pending", effective_at=None, occurred_at="2026-11-04T09:00:00Z",
                      correction_reason="Transfer was still pending at the bank")
    fields.update(over)
    return event("stl_88001", revision, **fields)


def self_supplied(ev, *, connector="self_supplied_import", received_at=RECEIVED, prefix=True):
    """A self-supplied record. Its record id moves into the ``self_`` namespace unless
    ``prefix`` is false (used to show the store refusing an issuer-namespace id)."""
    if prefix and not ev.record_id.startswith("self_"):
        ev = ev.model_copy(update={"record_id": f"self_{ev.record_id}"})
    raw = {"event": ev.to_json_dict()}
    rec = EvidenceRecord.from_event(ev, source_trust=SourceTrust.SELF_SUPPLIED, connector=connector,
                                    received_at=received_at, payload_sha256=canon.digest(raw))
    return rec, raw


def signed(ev, keystore, *, connector="merchant_webhook", received_at=RECEIVED):
    body = ev.to_json_dict()
    sig = keystore.sign(body, kid="merchant_demo", domain="alibi:record:v1")
    raw = {"event": body, "signature": sig.to_json_dict()}
    rec = EvidenceRecord.from_event(ev, source_trust=SourceTrust.ISSUER_VERIFIED, connector=connector,
                                    received_at=received_at, payload_sha256=canon.digest(raw),
                                    issuer_signature=sig, signed_body=body)
    return rec, raw


def txn(txn_id="txn_9001", revision=1, *, retrieved_at="2026-11-03T11:00:00Z", **over) -> BankTxn:
    data = {
        "txn_id": txn_id, "revision": revision, "subject_ref": SUBJ,
        "account_ref": "acc_maya_bank_a", "amount_pence": 216000, "currency": "GBP",
        "booked_at": "2026-11-03T10:00:00Z", "description": "THREADLINE PO-5001",
        "refs": ["PO-5001", "STL-88001"],
        "provenance": {"connector": "bank_feed", "connection_id": "conn_bank_a_maya",
                       "retrieved_at": retrieved_at, "source_version": "v1"},
    }
    data.update(over)
    return BankTxn.model_validate(data)


def link(link_id, txn_id, record_id, revision=1):
    return Link(link_id=link_id, txn_ref=tref(txn_id), link_state=LinkState.LINKED,
                record_ref=rref(record_id, revision), candidates=[rref(record_id, revision)],
                method="reference", created_at=NOW)


def claim(claim_id, *refs):
    return Claim(claim_id=claim_id, request_id="rfc_a_001", audience="bank_a", question_id="q1",
                 statement="The credit is the PO-5001 payout.", status=ClaimStatus.SUPPORTED,
                 rationale="References and amounts agree.", evidence_refs=list(refs), created_at=NOW)


def body(*refs, request_id="rfc_a_001", audience="bank_a"):
    return PackageBody(
        request_id=request_id, audience=audience, case_ref="case_maya_1",
        scope=PackageScope(txn_ids=["txn_9001"]),
        questions=[Question(question_id="q1", text="What is the source of this credit?")],
        claims=[PackageClaim(question_id="q1", statement="The source is not yet established.",
                             status=ClaimStatus.UNRESOLVED, rationale="Settlement evidence is pending.",
                             gap="No settlement record yet.")],
        attachments=[Attachment(attachment_id=f"att_{i}", ref=ref, form="redacted_extract",
                                source_trust=SourceTrust.ISSUER_VERIFIED, issuer_id="threadline",
                                content={"ref_id": ref.ref_id}) for i, ref in enumerate(refs)],
        expires_at=NOW + timedelta(days=7))


def draft(draft_id, *refs, status=DraftStatus.PENDING):
    b = body()
    return Draft(draft_id=draft_id, request_id="rfc_a_001", audience="bank_a", body=b,
                 body_sha256=canon.digest(b), review=Review(request_id="rfc_a_001", audience="bank_a"),
                 policy_version="pol_maya@v1", source_revisions=list(refs), status=status,
                 created_at=NOW)


def approval(approval_id, *refs, status=ApprovalStatus.PENDING, package_id=None):
    return Approval(approval_id=approval_id, request_id="rfc_a_001", audience="bank_a",
                    body_sha256="0" * 64, policy_version="pol_maya@v1", source_revisions=list(refs),
                    holder_id="holder_maya", approved_at=NOW, expires_at=NOW + timedelta(minutes=15),
                    status=status, package_id=package_id)


def package(package_id, *refs, audience="bank_a"):
    b = body(*refs, audience=audience)
    return ContextPackage(package_id=package_id, issued_at=NOW, body=b, body_sha256=canon.digest(b),
                          signature=SignatureEnvelope(kid="alibi_demo", domain=SigningDomain.PACKAGE,
                                                      sig="ab" * 64))


def rfc(**over):
    data = {
        "issuer": "bank_a", "request_id": "rfc_a_001", "subject_ref": SUBJ, "audience": "bank_a",
        "txn_ids": ["txn_9001"],
        "questions": [{"question_id": "q1", "text": "What is the source of this credit?"}],
        "issued_at": "2026-11-04T09:00:00Z", "expires_at": "2026-11-11T09:00:00Z",
    }
    data.update(over)
    return RequestForContext.model_validate(data)


def policy(version=1, **over):
    data = {"policy_id": "pol_maya", "version": version, "subject_ref": SUBJ,
            "rules": [DenyRule(rule_id="r1", fields=["items.description"])], "confirmed": True,
            "created_at": NOW}
    data.update(over)
    return Policy.model_validate(data)


def alert(alert_id="alr_1"):
    return Alert(alert_id=alert_id, subject_ref=SUBJ, kind=AlertKind.UNEXPLAINED_CREDIT,
                 title="Unexplained credit", explanation="A credit has no linked source yet.",
                 refs=[tref("txn_9001")], created_at=NOW)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "db" / "passport.db")
    yield s
    s.close()


# --------------------------------------------------------------------------- merchant evidence


def test_replayed_event_is_a_duplicate_and_leaves_one_active_record(store, keystore):
    rec, raw = signed(event(), keystore)
    first = store.put_source(rec, raw_payload=raw)
    assert first.status == "inserted" and first.active and first.record == rec
    retry, _ = signed(event(), keystore, received_at=RECEIVED + timedelta(minutes=5))
    again = store.put_source(retry, raw_payload=raw)
    assert again.status == "duplicate" and again.active
    assert again.record == rec  # the stored row, with the original received_at
    assert again.invalidated == NO_INVALIDATIONS
    assert store.list_sources(SUBJ) == [rec]
    assert store.source_history("po_5001") == [rec]
    assert store.get_raw_payload("po_5001", 1) == raw


def test_same_event_id_with_different_bytes_raises_idempotency_conflict(store, keystore):
    rec, raw = signed(event(), keystore)
    store.put_source(rec, raw_payload=raw)
    tampered, tampered_raw = signed(event(amount_pence=216500), keystore)  # same event_id
    with pytest.raises(IdempotencyConflict):
        store.put_source(tampered, raw_payload=tampered_raw)
    with pytest.raises(StoreError, match="payload_sha256"):
        store.put_source(tampered, raw_payload=raw)  # digest does not match the bytes
    assert store.get_source("po_5001") == rec
    assert len(store.source_history("po_5001")) == 1


def test_same_record_via_another_event_is_a_duplicate_and_the_event_key_is_kept(store, keystore):
    rec, raw = signed(event(), keystore)
    store.put_source(rec, raw_payload=raw)
    looked_up, lookup_raw = signed(event(event_id="evt_lookup_1"), keystore, connector="merchant_lookup")
    dup = store.put_source(looked_up, raw_payload=lookup_raw)
    assert dup.status == "duplicate" and dup.record == rec
    # The lookup event key is now remembered: different bytes under it conflict.
    changed, changed_raw = signed(event(revision=2, event_id="evt_lookup_1", status="completed"),
                                  keystore, connector="merchant_lookup")
    with pytest.raises(IdempotencyConflict):
        store.put_source(changed, raw_payload=changed_raw)


def test_correction_keeps_history_and_invalidates_only_its_dependents(store, keystore):
    rec1, raw1 = signed(settlement(1), keystore)
    assert store.put_source(rec1, raw_payload=raw1).status == "inserted"
    stl, other = rref("stl_88001"), rref("po_5002")
    store.save_link(link("lnk_stl", "txn_9001", "stl_88001"))
    store.save_link(link("lnk_other", "txn_9002", "po_5002"))
    store.save_claim(claim("clm_stl", stl))
    store.save_claim(claim("clm_other", other))
    store.save_draft(draft("drf_stl", stl))
    store.save_draft(draft("drf_approved", stl, status=DraftStatus.APPROVED))
    store.save_draft(draft("drf_other", other))
    store.save_draft(draft("drf_done", stl, status=DraftStatus.PACKAGED))
    store.save_approval(approval("apr_stl", stl))
    store.save_approval(approval("apr_other", other))
    store.save_approval(approval("apr_used", stl, status=ApprovalStatus.CONSUMED, package_id="pkg_sent"))
    sent = package("pkg_sent", stl)
    assert store.save_package(sent, approval_id="apr_used") == "inserted"

    rec2, raw2 = signed(settlement(2), keystore, received_at=RECEIVED + timedelta(days=1))
    result = store.put_source(rec2, raw_payload=raw2)

    assert result.status == "corrected" and result.active and result.record == rec2
    assert result.invalidated == {"links": ["lnk_stl"], "claims": ["clm_stl"],
                                  "drafts": ["drf_stl", "drf_approved"], "approvals": ["apr_stl"]}
    assert store.source_history("stl_88001") == [rec1, rec2]
    assert store.get_source("stl_88001") == rec2
    assert store.get_source("stl_88001", 1) == rec1
    assert store.list_sources(SUBJ) == [rec2]
    assert store.list_sources(SUBJ, active_only=False) == [rec1, rec2]

    assert [x.link_id for x in store.list_links()] == ["lnk_other"]
    gone = store.list_links(state="invalidated")
    assert [x.link_id for x in gone] == ["lnk_stl"]
    assert gone[0].invalidated_reason == "record:stl_88001 superseded by revision 2"
    assert [c.claim_id for c in store.list_claims()] == ["clm_other"]
    assert store.list_claims(state=DerivedState.INVALIDATED)[0].claim_id == "clm_stl"
    assert store.get_draft("drf_stl").status == DraftStatus.INVALIDATED
    assert store.get_draft("drf_stl").invalidated_reason.startswith("record:stl_88001")
    assert store.get_draft("drf_other").status == DraftStatus.PENDING
    assert store.get_draft("drf_done").status == DraftStatus.PACKAGED
    assert store.get_approval("apr_stl").status == ApprovalStatus.INVALIDATED
    assert store.get_approval("apr_other").status == ApprovalStatus.PENDING
    assert store.get_approval("apr_used").status == ApprovalStatus.CONSUMED
    # The delivered package is never altered; the supersession is audited instead.
    assert store.get_package("pkg_sent") == sent
    audit = store.list_audit(target_id="pkg_sent")
    assert [(a.action, a.actor) for a in audit] == [("evidence_superseded", "store")]
    assert audit[0].at == rec2.received_at


def test_stale_revision_is_kept_as_history_but_never_becomes_active(store, keystore):
    rec2, raw2 = signed(settlement(2), keystore)
    assert store.put_source(rec2, raw_payload=raw2).status == "inserted"
    store.save_link(link("lnk_stl", "txn_9001", "stl_88001", revision=2))
    rec1, raw1 = signed(settlement(1), keystore, received_at=RECEIVED + timedelta(hours=1))
    result = store.put_source(rec1, raw_payload=raw1)
    assert result.status == "stale" and not result.active
    assert result.invalidated == NO_INVALIDATIONS
    assert store.get_source("stl_88001") == rec2
    assert [r.revision for r in store.source_history("stl_88001")] == [1, 2]
    assert [x.link_id for x in store.list_links()] == ["lnk_stl"]
    again = store.put_source(rec1, raw_payload=raw1)  # replaying it changes nothing
    assert again.status == "duplicate" and not again.active
    assert store.get_source("stl_88001").revision == 2


def test_same_revision_with_different_content_under_a_new_event_raises(store, keystore):
    rec, raw = signed(event(), keystore)
    store.put_source(rec, raw_payload=raw)
    altered, altered_raw = signed(event(event_id="evt_po_5001_again", amount_pence=216500), keystore)
    with pytest.raises(RevisionConflict):
        store.put_source(altered, raw_payload=altered_raw)
    same, same_raw = signed(event(event_id="evt_po_5001_resend"), keystore)
    assert store.put_source(same, raw_payload=same_raw).status == "duplicate"
    assert store.get_source("po_5001") == rec


def test_revisions_cannot_change_issuer_subject_or_kind(store, keystore):
    rec, raw = signed(event(), keystore)
    store.put_source(rec, raw_payload=raw)
    # Each match names the one changed field, so a wrong field in the message fails the test.
    other_issuer, other_raw = signed(event(revision=2, issuer_id="other_market"), keystore)
    with pytest.raises(RevisionConflict, match=r"changes issuer_id;"):
        store.put_source(other_issuer, raw_payload=other_raw)
    other_subject, subject_raw = signed(event(revision=2, subject_ref="subj_other"), keystore)
    with pytest.raises(RevisionConflict, match=r"changes subject_ref;"):
        store.put_source(other_subject, raw_payload=subject_raw)
    other_kind, kind_raw = signed(event(revision=2, kind="settlement", settlement_ref="STL-1"),
                                  keystore)
    with pytest.raises(RevisionConflict, match=r"changes kind;"):
        store.put_source(other_kind, raw_payload=kind_raw)
    assert store.source_history("po_5001") == [rec]


def test_self_supplied_evidence_has_its_own_namespace(store, keystore):
    # A self-supplied import cannot occupy an issuer's record id, before or after the issuer.
    squat, squat_raw = self_supplied(settlement(1), prefix=False)
    with pytest.raises(NamespaceError, match="must start with 'self_'"):
        store.put_source(squat, raw_payload=squat_raw)
    rec, raw = signed(settlement(1), keystore)
    assert store.put_source(rec, raw_payload=raw).status == "inserted"
    forged, forged_raw = self_supplied(settlement(2), prefix=False)
    with pytest.raises(NamespaceError):
        store.put_source(forged, raw_payload=forged_raw)
    # Issuer evidence may not use the reserved prefix either.
    fake, fake_raw = signed(event("self_po_1"), keystore)
    with pytest.raises(NamespaceError, match="reserved for self-supplied"):
        store.put_source(fake, raw_payload=fake_raw)
    own, own_raw = self_supplied(settlement(1))
    assert own.record_id == "self_stl_88001"
    assert store.put_source(own, raw_payload=own_raw).status == "inserted"
    assert store.get_source("stl_88001") == rec
    assert store.get_source("stl_88001").source_trust == SourceTrust.ISSUER_VERIFIED


def test_event_keys_are_scoped_to_the_issuer(store, keystore):
    mine, mine_raw = signed(event(event_id="evt_1"), keystore)
    theirs, theirs_raw = signed(event("po_7001", event_id="evt_1", issuer_id="other_market"),
                                keystore)
    assert store.put_source(mine, raw_payload=mine_raw).status == "inserted"
    assert store.put_source(theirs, raw_payload=theirs_raw).status == "inserted"
    tampered, tampered_raw = signed(event("po_7001", event_id="evt_1", issuer_id="other_market",
                                          amount_pence=1), keystore)
    with pytest.raises(IdempotencyConflict, match="other_market"):
        store.put_source(tampered, raw_payload=tampered_raw)


def test_list_sources_filters_by_subject_and_kind(store):
    events = [
        event("ord_1001", kind="order", amount_pence=60000, payment_ref=None, status="completed",
              occurred_at="2026-10-05T10:00:00Z"),
        event(),
        event("msg_7001", kind="message", amount_pence=None, currency=None, payment_ref=None,
              status=None, occurred_at="2026-11-02T12:00:00Z",
              text="Funds released to your seller balance."),
        event("po_9001", subject_ref="subj_other"),
    ]
    for ev in events:
        rec, raw = self_supplied(ev)
        store.put_source(rec, raw_payload=raw)
    assert [r.record_id for r in store.list_sources(SUBJ)] == [
        "self_ord_1001", "self_po_5001", "self_msg_7001"]
    assert [r.record_id for r in store.list_sources(SUBJ, kinds=["payout", RecordKind.ORDER])] == [
        "self_ord_1001", "self_po_5001"]
    assert [r.record_id for r in store.list_sources(SUBJ, kinds="message")] == ["self_msg_7001"]
    assert [r.record_id for r in store.list_sources("subj_other")] == ["self_po_9001"]
    with pytest.raises(NotFound):
        store.get_source("po_missing")
    assert store.source_history("po_missing") == []


def test_claims_depend_on_their_calculation_terms(store, keystore):
    terms = [CalcTerm(ref=rref("ord_1001"), kind="order", sign=1, amount_pence=240000, currency="GBP"),
             CalcTerm(ref=rref("rfd_2001"), kind="refund", sign=-1, amount_pence=18000, currency="GBP"),
             CalcTerm(ref=rref("fee_3001"), kind="fee", sign=-1, amount_pence=6000, currency="GBP")]
    calc = Calculation(method="net_payout", currency="GBP", terms=terms, total_pence=216000,
                       target_ref=tref("txn_9001"), target_pence=216000, difference_pence=0,
                       balanced=True)
    net = Claim(claim_id="clm_net", request_id="rfc_a_001", audience="bank_a", question_id="q1",
                statement="Sales less refunds and fees equal the credit.",
                status=ClaimStatus.SUPPORTED, rationale="Computed in code from cited amounts.",
                evidence_refs=[tref("txn_9001")], calculation=calc, created_at=NOW)
    assert dependency_tokens(net) == ["record:fee_3001", "record:ord_1001", "record:rfd_2001",
                                      "txn:txn_9001"]
    store.save_claim(net)
    fee1, fee1_raw = signed(event("fee_3001", kind="fee", amount_pence=6000,
                                  payment_ref=None, status="completed"), keystore)
    store.put_source(fee1, raw_payload=fee1_raw)
    fee2, fee2_raw = signed(event("fee_3001", 2, kind="fee", amount_pence=6500,
                                  payment_ref=None, status="completed"), keystore)
    assert store.put_source(fee2, raw_payload=fee2_raw).invalidated["claims"] == ["clm_net"]
    assert store.list_claims() == []


# --------------------------------------------------------------------------- bank feed


def test_bank_txn_replay_conflict_correction_and_stale(store):
    first = store.put_bank_txns([txn()], connector="bank_feed", cursor="cur_1")
    assert [r.status for r in first] == ["inserted"] and first[0].record == txn()
    # A later fetch of the same revision (new retrieval time) is a duplicate.
    again = store.put_bank_txns([txn(retrieved_at="2026-11-04T11:00:00Z")], connector="bank_feed")
    assert again[0].status == "duplicate" and again[0].record == txn()
    with pytest.raises(IdempotencyConflict):
        store.put_bank_txns([txn(amount_pence=216001)], connector="bank_feed")
    with pytest.raises(RevisionConflict):
        store.put_bank_txns([txn(amount_pence=216001)], connector="other_feed")
    with pytest.raises(RevisionConflict, match=r"changes account_ref;"):
        store.put_bank_txns([txn(revision=2, account_ref="acc_someone_else")], connector="bank_feed")
    with pytest.raises(RevisionConflict, match=r"changes subject_ref;"):
        store.put_bank_txns([txn(revision=2, subject_ref="subj_other")], connector="bank_feed")

    store.save_link(link("lnk_txn", "txn_9001", "po_5001"))
    store.save_link(link("lnk_other", "txn_9002", "po_5002"))
    corrected = store.put_bank_txns([txn(revision=2, refs=["PO-5001"])], connector="bank_feed")
    assert corrected[0].status == "corrected" and corrected[0].active
    assert corrected[0].invalidated == {**NO_INVALIDATIONS, "links": ["lnk_txn"]}
    assert store.get_bank_txn("txn_9001").revision == 2
    assert [t.revision for t in store.txn_history("txn_9001")] == [1, 2]
    assert [x.link_id for x in store.list_links()] == ["lnk_other"]

    store.put_bank_txns([txn("txn_9003", 2, description="CARD REFUND")], connector="bank_feed")
    stale = store.put_bank_txns([txn("txn_9003", 1, description="CARD REFUND")], connector="bank_feed")
    assert stale[0].status == "stale" and not stale[0].active
    assert store.get_bank_txn("txn_9003").revision == 2
    assert store.get_bank_txn("txn_9003", 1).revision == 1
    assert [(t.txn_id, t.revision) for t in store.list_bank_txns(SUBJ)] == [
        ("txn_9001", 2), ("txn_9003", 2)]
    assert len(store.list_bank_txns(SUBJ, active_only=False)) == 4
    assert store.get_cursor("bank_feed", "conn_bank_a_maya") == "cur_1"


def test_bank_batch_and_cursor_are_atomic(store):
    store.put_bank_txns([txn()], connector="bank_feed", cursor="cur_1")
    batch = [txn("txn_9002", description="THREADLINE PO-5002", refs=["PO-5002"]),
             txn(amount_pence=999)]  # conflicts with the stored txn_9001 revision 1
    with pytest.raises(IdempotencyConflict):
        store.put_bank_txns(batch, connector="bank_feed", cursor="cur_2")
    with pytest.raises(NotFound):
        store.get_bank_txn("txn_9002")
    assert store.get_cursor("bank_feed", "conn_bank_a_maya") == "cur_1"
    assert [t.txn_id for t in store.list_bank_txns(SUBJ)] == ["txn_9001"]
    assert store.get_cursor("other_feed", "conn_bank_a_maya") is None


# --------------------------------------------------------------------------- encryption and keys


def test_evidence_is_encrypted_at_rest(tmp_path, keystore):
    db = tmp_path / "db" / "passport.db"
    order = event("ord_1001", kind="order", status="completed", items=[{
        "description": "Vintage denim jacket", "category": "outerwear", "quantity": 1,
        "unit_amount_pence": 216000}])
    rec, raw = signed(order, keystore)
    with Store(db) as store:
        store.put_source(rec, raw_payload=raw)
        store.put_bank_txns([txn()], connector="bank_feed", cursor="cursor-secret-7")
        store.save_claim(claim("clm_1", rref("ord_1001")))
        assert store.get_source("ord_1001").items[0].description == "Vintage denim jacket"
    on_disk = b"".join(p.read_bytes() for p in db.parent.iterdir() if p.name.startswith(db.name))
    assert b"ord_1001" in on_disk  # ids stay in clear columns
    for secret in [b"PO-5001", b"Vintage denim jacket", b"216000", b"STL-88001", b"THREADLINE",
                   b"amount_pence", b"cursor-secret-7"]:
        assert secret not in on_disk
    key = (db.parent / "master.key").read_bytes().strip()
    assert key not in on_disk


def test_master_key_from_env_is_used_and_no_key_file_is_written(tmp_path, monkeypatch):
    key = Fernet.generate_key()
    monkeypatch.setenv("ALIBI_MASTER_KEY", key.decode())
    db = tmp_path / "db" / "passport.db"
    rec, raw = self_supplied(event())
    with Store(db) as store:
        store.put_source(rec, raw_payload=raw)
    assert not (tmp_path / "db" / "master.key").exists()
    assert load_master_key(tmp_path / "unused.key") == key
    assert not (tmp_path / "unused.key").exists()
    # An explicit key wins over the environment.
    with pytest.raises(StoreError, match="does not match"):
        Store(db, master_key=Fernet.generate_key())
    monkeypatch.delenv("ALIBI_MASTER_KEY")
    with Store(db, master_key=key) as store:
        assert store.get_source(rec.record_id) == rec
    # With no env key and no key file, an existing database is refused, not re-keyed.
    with pytest.raises(StoreError, match="no master key"):
        Store(db)
    assert not (tmp_path / "db" / "master.key").exists()


def test_key_file_is_generated_outside_the_database_and_reused(tmp_path):
    db = tmp_path / "db" / "passport.db"
    rec, raw = self_supplied(event())
    with Store(db) as store:
        store.put_source(rec, raw_payload=raw)
    key_file = tmp_path / "db" / "master.key"
    key = key_file.read_bytes().strip()
    Fernet(key)  # a valid Fernet key
    assert key not in db.read_bytes()
    with Store(db) as store:
        assert store.get_source(rec.record_id) == rec
    assert key_file.read_bytes().strip() == key
    custom = tmp_path / "keys" / "custom.key"
    with Store(tmp_path / "db2" / "passport.db", key_path=custom):
        pass
    assert custom.exists() and not (tmp_path / "db2" / "master.key").exists()
    if os.name != "nt":  # POSIX permission bits are not enforced on Windows
        assert stat.S_IMODE(key_file.stat().st_mode) == 0o600


def test_wrong_master_key_fails_clearly(tmp_path):
    db = tmp_path / "passport.db"
    right = Fernet.generate_key()
    with Store(db, master_key=right) as store:
        store.save_request(rfc())
    with pytest.raises(StoreError, match="master key does not match"):
        Store(db, master_key=Fernet.generate_key())
    with Store(db, master_key=right) as store:
        assert store.get_request("rfc_a_001") == rfc()


def test_invalid_master_keys_raise_without_echoing_them(tmp_path, monkeypatch):
    monkeypatch.setenv("ALIBI_MASTER_KEY", "not-a-key-s3cr3t-value")
    with pytest.raises(StoreError) as info:
        Store(tmp_path / "passport.db")
    assert "ALIBI_MASTER_KEY" in str(info.value) and "s3cr3t" not in str(info.value)
    with pytest.raises(StoreError) as info:
        load_master_key(tmp_path / "x.key")
    assert "s3cr3t" not in str(info.value)
    monkeypatch.delenv("ALIBI_MASTER_KEY")
    with pytest.raises(StoreError) as info:
        Store(tmp_path / "other.db", master_key=b"short-s3cr3t")
    assert "s3cr3t" not in str(info.value)


def test_everything_survives_a_restart(tmp_path, keystore):
    db = tmp_path / "db" / "passport.db"
    rec, raw = signed(settlement(1), keystore)
    stl = rref("stl_88001")
    lnk, clm, alr = link("lnk_1", "txn_9001", "stl_88001"), claim("clm_1", stl), alert()
    pol, req, drf, apr = policy(), rfc(), draft("drf_1", stl), approval("apr_1", stl)
    pkg = package("pkg_1", stl)
    dlv = Delivery(delivery_id="dlv_1", package_id="pkg_1", request_id="rfc_a_001",
                   audience="bank_a", package_sha256=canon.digest(pkg), delivered_at=NOW)
    aud = AuditEvent(audit_id="aud_1", at=NOW, actor="holder_maya", action="approve",
                     target_id="pkg_1", outcome="ok", detail="Approved for bank_a")
    trc = [TraceEvent(trace_id=f"trc_{i}", run_id="run_1", seq=i, at=NOW, actor="rules",
                      step="tool_call", tool="get_txn", args={"txn_id": "txn_9001"},
                      result={"revision": 1}, refs=[tref("txn_9001")]) for i in range(2)]
    with Store(db) as store:
        store.put_source(rec, raw_payload=raw)
        store.put_bank_txns([txn()], connector="bank_feed", cursor="cur_1")
        store.save_link(lnk)
        store.save_claim(clm)
        store.save_alert(alr)
        store.save_policy(pol)
        store.save_request(req)
        store.save_draft(drf)
        store.save_approval(apr)
        store.save_package(pkg, approval_id="apr_1")
        store.save_delivery(dlv)
        store.append_audit(aud)
        store.append_traces(trc)
    with Store(db) as store:
        assert store.get_source("stl_88001") == rec
        assert store.get_raw_payload("stl_88001", 1) == raw
        assert store.get_bank_txn("txn_9001") == txn()
        assert store.get_cursor("bank_feed", "conn_bank_a_maya") == "cur_1"
        assert store.list_links(txn_id="txn_9001") == [lnk]
        assert store.list_claims(request_id="rfc_a_001") == [clm]
        assert store.list_alerts(SUBJ) == [alr]
        assert store.latest_policy(SUBJ) == pol
        assert store.get_request("rfc_a_001") == req
        assert store.list_requests(subject_ref=SUBJ, audience="bank_a") == [req]
        assert store.get_draft("drf_1") == drf and store.list_drafts(request_id="rfc_a_001") == [drf]
        assert store.get_approval("apr_1") == apr
        assert store.list_approvals(status=ApprovalStatus.PENDING) == [apr]
        assert store.get_package("pkg_1") == pkg and store.list_packages(audience="bank_a") == [pkg]
        assert store.list_deliveries(package_id="pkg_1") == [dlv]
        assert store.list_audit(target_id="pkg_1") == [aud]
        assert store.list_traces("run_1") == trc
        # Dependency tokens persist too: a correction after the restart still invalidates.
        rec2, raw2 = signed(settlement(2), keystore)
        assert store.put_source(rec2, raw_payload=raw2).invalidated == {
            "links": ["lnk_1"], "claims": ["clm_1"], "drafts": ["drf_1"], "approvals": ["apr_1"]}


# --------------------------------------------------------------------------- requests, packages, policies


def test_request_idempotency(store):
    assert store.save_request(rfc()) == "inserted"
    assert store.save_request(rfc()) == "duplicate"
    with pytest.raises(IdempotencyConflict):
        store.save_request(rfc(questions=[{"question_id": "q1", "text": "Who sent this credit?"}]))
    assert store.get_request("rfc_a_001") == rfc()
    period = {"from": "2026-10-01T00:00:00Z", "to": "2026-11-01T00:00:00Z"}
    bank_b = rfc(request_id="rfc_b_001", issuer="bank_b", audience="bank_b", txn_ids=None, period=period)
    assert store.save_request(bank_b) == "inserted"
    assert store.list_requests(audience="bank_b") == [bank_b]
    assert [r.request_id for r in store.list_requests(subject_ref=SUBJ)] == ["rfc_a_001", "rfc_b_001"]
    with pytest.raises(NotFound):
        store.get_request("rfc_missing")


def test_package_idempotency_and_recipient_scope(store):
    pkg = package("pkg_1", rref("po_5001"))
    assert store.save_package(pkg, approval_id="apr_1") == "inserted"
    assert store.save_package(pkg, approval_id="apr_1") == "duplicate"
    reissued = pkg.model_copy(update={"issued_at": NOW + timedelta(minutes=1)})
    with pytest.raises(IdempotencyConflict):
        store.save_package(reissued, approval_id="apr_1")
    with pytest.raises(IdempotencyConflict):
        store.save_package(pkg, approval_id="apr_2")
    assert store.get_package("pkg_1") == pkg
    assert store.get_package("pkg_1", audience="bank_a") == pkg
    with pytest.raises(NotFound):
        store.get_package("pkg_1", audience="bank_b")
    assert store.list_packages(audience="bank_b") == []
    assert store.list_packages(request_id="rfc_a_001") == [pkg]


def test_policy_versions_are_append_only(store):
    assert store.save_policy(policy(1)) == "inserted"
    assert store.save_policy(policy(1)) == "duplicate"
    with pytest.raises(RevisionConflict):
        store.save_policy(policy(1, confirmed=False))
    v2 = policy(2, rules=[DenyRule(rule_id="r1", fields=["items.description"]),
                          AgeRule(rule_id="r2", max_age_days=90)])
    assert store.save_policy(v2) == "inserted"
    assert store.get_policy("pol_maya") == v2
    assert store.get_policy("pol_maya", 1) == policy(1)
    assert store.latest_policy(SUBJ) == v2
    assert store.latest_policy("subj_nobody") is None
    with pytest.raises(RevisionConflict):
        store.save_policy(policy(3, subject_ref="subj_other"))
    with pytest.raises(NotFound):
        store.get_policy("pol_maya", 9)


# --------------------------------------------------------------------------- helpers and plumbing


def test_status_helpers_update_encrypted_rows(store):
    store.save_draft(draft("drf_1", rref("po_5001")))
    updated = store.update_draft_status("drf_1", DraftStatus.APPROVED)
    assert updated.status == DraftStatus.APPROVED == store.get_draft("drf_1").status
    store.update_draft_status("drf_1", "invalidated", reason="Holder withdrew the draft")
    assert store.get_draft("drf_1").invalidated_reason == "Holder withdrew the draft"
    with pytest.raises(NotFound):
        store.update_draft_status("drf_missing", "approved")
    with pytest.raises(StoreError):
        store.update_draft_status("drf_1", "shipped")

    apr = approval("apr_1", rref("po_5001"))
    store.save_approval(apr)
    store.update_approval(apr.model_copy(update={"status": ApprovalStatus.CONSUMED,
                                                 "package_id": "pkg_1"}))
    assert store.get_approval("apr_1").package_id == "pkg_1"
    assert store.list_approvals(status="pending") == []
    assert [a.approval_id for a in store.list_approvals(request_id="rfc_a_001",
                                                        status=ApprovalStatus.CONSUMED)] == ["apr_1"]
    with pytest.raises(NotFound):
        store.update_approval(approval("apr_missing"))

    store.save_alert(alert())
    assert store.set_alert_state("alr_1", "resolved").state == "resolved"
    assert store.list_alerts(SUBJ, state="open") == []
    assert [a.alert_id for a in store.list_alerts(SUBJ, state="resolved")] == ["alr_1"]
    with pytest.raises(StoreError):
        store.set_alert_state("alr_1", "closed")
    with pytest.raises(NotFound):
        store.set_alert_state("alr_missing", "resolved")


def test_audit_and_traces_are_append_only(store):
    aud = AuditEvent(audit_id="aud_1", at=NOW, actor="holder_maya", action="approve",
                     target_id="apr_1", outcome="ok")
    store.append_audit(aud)
    store.append_audit(aud)  # an identical retry is a no-op
    assert store.list_audit() == [aud]
    with pytest.raises(IdempotencyConflict):
        store.append_audit(aud.model_copy(update={"outcome": "refused"}))
    traces = [TraceEvent(trace_id=f"trc_{i}", run_id="run_1", seq=i, at=NOW, actor="rules",
                         step="note") for i in (1, 0)]
    store.append_traces(traces)
    store.append_traces(traces)
    assert [t.seq for t in store.list_traces("run_1")] == [0, 1]
    assert store.list_traces("run_other") == []


def test_transaction_is_reentrant_and_rolls_back(store):
    rec, raw = self_supplied(event())
    with pytest.raises(RuntimeError):
        with store.transaction():
            store.put_source(rec, raw_payload=raw)
            store.save_request(rfc())
            raise RuntimeError("abort")
    with pytest.raises(NotFound):
        store.get_source(rec.record_id)
    assert store.list_requests() == []
    # A failing nested block undoes only its own writes; the outer block still commits.
    with store.transaction():
        store.save_request(rfc())
        with pytest.raises(IdempotencyConflict):
            with store.transaction():
                store.put_source(rec, raw_payload=raw)
                store.save_request(rfc(questions=[{"question_id": "q1", "text": "Different?"}]))
    assert store.get_request("rfc_a_001") == rfc()
    with pytest.raises(NotFound):
        store.get_source(rec.record_id)


def test_concurrent_writers_are_serialised(store):
    errors = []

    def work(i):
        try:
            store.put_bank_txns([txn(f"txn_{i:04d}")], connector="bank_feed", cursor=f"cur_{i}")
            store.save_link(link(f"lnk_{i}", f"txn_{i:04d}", "po_5001"))
        except Exception as exc:  # surfaced by the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert len(store.list_bank_txns(SUBJ)) == 8
    assert len(store.list_links()) == 8


def test_in_memory_store_needs_no_key_file():
    with Store(":memory:") as store:
        assert store.key_path is None
        store.save_request(rfc())
        assert store.get_request("rfc_a_001") == rfc()
    with pytest.raises(StoreError, match="closed"):
        store.get_request("rfc_a_001")


# --------------------------------------------------------------------------- review regressions


def test_late_saves_citing_superseded_revisions_are_stored_invalidated(store, keystore):
    rec1, raw1 = signed(settlement(1), keystore)
    store.put_source(rec1, raw_payload=raw1)
    rec2, raw2 = signed(settlement(2), keystore, received_at=RECEIVED + timedelta(days=1))
    assert store.put_source(rec2, raw_payload=raw2).status == "corrected"
    old = rref("stl_88001", 1)
    # An investigation that read revision 1 saves its results after the correction landed.
    store.save_link(link("lnk_late", "txn_9001", "stl_88001", revision=1))
    store.save_claim(claim("clm_late", old))
    store.save_draft(draft("drf_late", old))
    store.save_approval(approval("apr_late", old))
    assert store.list_links() == [] and store.list_claims() == []
    reason = "cites record:stl_88001@1 but revision 2 is now active"
    assert store.list_links(state="invalidated")[0].invalidated_reason == reason
    assert store.get_draft("drf_late").status == DraftStatus.INVALIDATED
    assert store.get_approval("apr_late").status == ApprovalStatus.INVALIDATED
    assert store.get_approval("apr_late").invalidated_reason == reason
    store.save_claim(claim("clm_now", rref("stl_88001", 2)))  # citing the active revision is fine
    assert [c.claim_id for c in store.list_claims()] == ["clm_now"]


def test_terminal_states_are_never_reverted(store, keystore):
    rec1, raw1 = signed(settlement(1), keystore)
    store.put_source(rec1, raw_payload=raw1)
    stl = rref("stl_88001")
    lnk, clm = link("lnk_1", "txn_9001", "stl_88001"), claim("clm_1", stl)
    drf, apr = draft("drf_1", stl), approval("apr_1", stl)
    store.save_link(lnk)
    store.save_claim(clm)
    store.save_draft(drf)
    store.save_approval(apr)
    rec2, raw2 = signed(settlement(2), keystore)
    store.put_source(rec2, raw_payload=raw2)
    # A retry that re-saves the stale in-memory objects cannot resurrect them.
    for save, obj in [(store.save_link, lnk), (store.save_claim, clm), (store.save_draft, drf),
                      (store.save_approval, apr)]:
        with pytest.raises(StoreError, match="is invalidated and cannot change"):
            save(obj)
    with pytest.raises(StoreError, match="cannot change"):
        store.update_draft_status("drf_1", DraftStatus.APPROVED)
    with pytest.raises(StoreError, match="cannot change"):
        store.update_approval(apr)
    assert store.get_draft("drf_1").status == DraftStatus.INVALIDATED
    assert store.get_approval("apr_1").status == ApprovalStatus.INVALIDATED
    store.save_claim(store.list_claims(state="invalidated")[0])  # an identical re-save is allowed
    # Consumed approvals and packaged drafts are terminal too.
    used = approval("apr_used", rref("stl_88001", 2), status=ApprovalStatus.CONSUMED,
                    package_id="pkg_1")
    store.save_approval(used)
    with pytest.raises(StoreError, match="is consumed"):
        store.update_approval(used.model_copy(update={"status": ApprovalStatus.PENDING,
                                                      "package_id": None}))
    store.save_draft(draft("drf_done", rref("stl_88001", 2), status=DraftStatus.PACKAGED))
    with pytest.raises(StoreError, match="is packaged"):
        store.update_draft_status("drf_done", DraftStatus.PENDING)


def test_delivery_entries_only_change_status_and_receipt(store):
    dlv = Delivery(delivery_id="dlv_1", package_id="pkg_1", request_id="rfc_a_001",
                   audience="bank_a", package_sha256="1" * 64, delivered_at=NOW)
    store.save_delivery(dlv)
    acked = dlv.model_copy(update={"status": "acknowledged", "receipt_id": "rcp_1"})
    store.save_delivery(acked)
    assert store.list_deliveries(audience="bank_a") == [acked]
    for change in [{"package_id": "pkg_2"}, {"audience": "bank_b"}, {"package_sha256": "2" * 64},
                   {"delivered_at": NOW + timedelta(hours=1)}]:
        with pytest.raises(IdempotencyConflict):
            store.save_delivery(acked.model_copy(update=change))
    assert store.list_deliveries(audience="bank_b") == []
    assert store.list_deliveries(package_id="pkg_1") == [acked]


def test_rows_edited_without_the_key_fail_integrity_checks(tmp_path, keystore):
    db = tmp_path / "db" / "passport.db"
    with Store(db) as store:
        rec1, raw1 = signed(settlement(1), keystore)
        store.put_source(rec1, raw_payload=raw1)
        rec2, raw2 = signed(settlement(2), keystore)
        store.put_source(rec2, raw_payload=raw2)
        store.save_approval(approval("apr_a", rref("stl_88001", 2)))
        store.save_approval(approval("apr_b", rref("stl_88001", 2)).model_copy(
            update={"holder_id": "holder_other"}))
    con = sqlite3.connect(db)
    con.execute("UPDATE sources SET active = 0 WHERE record_id = 'stl_88001' AND revision = 2")
    con.execute("UPDATE sources SET active = 1 WHERE record_id = 'stl_88001' AND revision = 1")
    blob_b = con.execute("SELECT payload FROM approvals WHERE approval_id = 'apr_b'").fetchone()[0]
    con.execute("UPDATE approvals SET payload = ? WHERE approval_id = 'apr_a'", (blob_b,))
    con.commit()
    con.close()
    with Store(db) as store:
        with pytest.raises(StoreError, match="integrity check failed"):
            store.get_source("stl_88001")
        with pytest.raises(StoreError, match="integrity check failed"):
            store.list_sources(SUBJ)
        assert store.get_source("stl_88001", 2).status == "pending"  # pinned reads still work
        with pytest.raises(StoreError, match="integrity check failed"):
            store.get_approval("apr_a")
        assert store.get_approval("apr_b").holder_id == "holder_other"


def test_required_scope_arguments_fail_closed(store):
    store.save_alert(alert())
    store.append_traces([TraceEvent(trace_id="trc_1", run_id="run_1", seq=0, at=NOW,
                                    actor="rules", step="note")])
    for call in (lambda: store.list_alerts(None), lambda: store.list_traces(None),
                 lambda: store.list_sources(None), lambda: store.list_bank_txns(None),
                 lambda: store.latest_policy(None)):
        with pytest.raises(StoreError):
            call()
    assert [a.alert_id for a in store.list_alerts(SUBJ)] == ["alr_1"]
    assert [t.trace_id for t in store.list_traces("run_1")] == ["trc_1"]


def test_key_file_creation_is_atomic_and_empty_leftovers_are_handled(tmp_path, monkeypatch):
    db = tmp_path / "db" / "passport.db"
    key_file = tmp_path / "db" / "master.key"

    def disk_full(fd):
        raise OSError("disk full")

    with monkeypatch.context() as patch:
        patch.setattr(store_module.os, "fsync", disk_full)
        with pytest.raises(StoreError, match="cannot use master key file"):
            Store(db)
    assert not key_file.exists()  # no partial key is ever published
    assert [p.name for p in key_file.parent.iterdir() if p.name.endswith(".tmp")] == []
    with Store(db) as store:
        store.save_request(rfc())
    key = key_file.read_bytes().strip()
    # A creator that loses the race to another opener returns the published key.
    assert store_module._create_key_file(key_file) == key
    # An empty leftover holds no key, so it is replaced while no data depends on it...
    fresh = tmp_path / "fresh" / "master.key"
    fresh.parent.mkdir()
    fresh.write_bytes(b"")
    with Store(tmp_path / "fresh" / "passport.db") as store:
        store.save_request(rfc())
    Fernet(fresh.read_bytes().strip())
    # ...but for a database that already holds data it is an error, never a silent re-key.
    key_file.write_bytes(b"")
    with pytest.raises(StoreError, match="is empty"):
        Store(db)


def test_malformed_arguments_raise_typed_errors(store, tmp_path):
    rec, raw = self_supplied(event())
    store.put_source(rec, raw_payload=raw)
    for call in (lambda: store.get_source(rec.record_id, 2**70),
                 lambda: store.get_bank_txn("txn_9001", 2**70),
                 lambda: store.get_raw_payload(rec.record_id, 2**70),
                 lambda: store.get_policy("pol_maya", 2**70)):
        with pytest.raises(NotFound):
            call()
    with pytest.raises(StoreError, match="integers"):
        store.get_source(rec.record_id, "1")
    with pytest.raises(StoreError, match="kinds"):
        store.list_sources(SUBJ, kinds=5)
    deep = current = {}
    for _ in range(5000):
        current["a"] = {}
        current = current["a"]
    other, _ = self_supplied(event("po_5002"))
    with pytest.raises(StoreError):
        store.put_source(other, raw_payload=deep)
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("x")
    with pytest.raises(StoreError, match="cannot create the passport directory"):
        Store(blocker / "passport.db")


def test_cursors_are_kept_per_connection(store):
    a = txn("txn_a1")
    c = txn("txn_c1", account_ref="acc_maya_bank_c", provenance={
        "connector": "bank_feed", "connection_id": "conn_bank_c_maya",
        "retrieved_at": "2026-11-03T11:00:00Z", "source_version": "v1"})
    store.put_bank_txns([a], connector="bank_feed", cursor="acctA-page-10")
    store.put_bank_txns([c], connector="bank_feed", cursor="acctC-page-2")
    assert store.get_cursor("bank_feed", "conn_bank_a_maya") == "acctA-page-10"
    assert store.get_cursor("bank_feed", "conn_bank_c_maya") == "acctC-page-2"
    with pytest.raises(StoreError, match="one connection"):
        store.put_bank_txns([a, c], connector="bank_feed", cursor="mixed")
    with pytest.raises(StoreError, match="one connection"):
        store.put_bank_txns([], connector="bank_feed", cursor="orphan")
    with pytest.raises(StoreError, match="its connection_id"):
        store.put_bank_txns([c], connector="bank_feed", cursor="x", connection_id="conn_bank_a_maya")
    store.put_bank_txns([], connector="bank_feed", cursor="acctA-page-11",
                        connection_id="conn_bank_a_maya")
    assert store.get_cursor("bank_feed", "conn_bank_a_maya") == "acctA-page-11"
    assert store.get_cursor("bank_feed", "conn_bank_c_maya") == "acctC-page-2"


ALLOWED_CLEAR_COLUMNS = {
    "meta": {"key", "value"},
    "sources": {"record_id", "revision", "subject_ref", "connector", "event_id", "active",
                "payload", "raw"},
    "source_events": {"connector", "issuer_id", "event_id", "fingerprint", "record_id", "revision"},
    "bank_txns": {"txn_id", "revision", "subject_ref", "connector", "active", "payload"},
    "cursors": {"connector", "connection_id", "payload"},
    "links": {"link_id", "txn_id", "state", "payload"},
    "claims": {"claim_id", "request_id", "state", "payload"},
    "alerts": {"alert_id", "subject_ref", "state", "payload"},
    "policies": {"policy_id", "version", "subject_ref", "payload"},
    "requests": {"issuer", "request_id", "subject_ref", "audience", "payload"},
    "drafts": {"draft_id", "request_id", "status", "payload"},
    "approvals": {"approval_id", "request_id", "status", "payload"},
    "packages": {"package_id", "approval_id", "request_id", "audience", "payload"},
    "deliveries": {"delivery_id", "package_id", "request_id", "audience", "payload"},
    "audit": {"audit_id", "target_id", "payload"},
    "traces": {"trace_id", "run_id", "seq", "payload"},
    "deps": {"obj_type", "obj_id", "token"},
}
ENCRYPTED_COLUMNS = {"payload", "raw", "value"}


def test_clear_columns_are_allowlisted_and_hold_no_content(tmp_path, keystore):
    db = tmp_path / "db" / "passport.db"
    canary = "CANARY-7Q4Z"
    amounts = {216000, 60000, 18000, 6000, 4599}
    order = event("ord_1001", kind="order", status="completed", amount_pence=60000,
                  text=f"{canary} receipt text", items=[{
                      "description": f"{canary} jacket", "category": "outerwear",
                      "quantity": 1, "unit_amount_pence": 60000}])
    rec, raw = signed(order, keystore)
    pkg_body = body(rref("ord_1001")).model_copy(update={"claims": [PackageClaim(
        question_id="q1", statement=f"{canary} statement", status=ClaimStatus.UNRESOLVED,
        rationale=f"{canary} rationale", gap=f"{canary} gap")]})
    pkg = ContextPackage(package_id="pkg_1", issued_at=NOW, body=pkg_body,
                         body_sha256=canon.digest(pkg_body),
                         signature=SignatureEnvelope(kid="alibi_demo", domain=SigningDomain.PACKAGE,
                                                     sig="ab" * 64))
    review = Review(request_id="rfc_a_001", audience="bank_a",
                    gaps=[Gap(question_id="q1", text=f"{canary} private gap")])
    with Store(db) as store:
        store.put_source(rec, raw_payload=raw)
        store.put_bank_txns([txn(description=f"{canary} THREADLINE", amount_pence=18000)],
                            connector="bank_feed", cursor=f"{canary}-cursor")
        store.save_link(link("lnk_1", "txn_9001", "ord_1001").model_copy(
            update={"signals": [f"{canary} signal"]}))
        store.save_claim(claim("clm_1", rref("ord_1001")).model_copy(
            update={"statement": f"{canary} claim"}))
        store.save_alert(alert().model_copy(update={"explanation": f"{canary} alert"}))
        store.save_policy(policy(rules=[DenyRule(rule_id="r1", fields=["items.description"],
                                                 source_text=f"{canary} rule")]))
        store.save_request(rfc(questions=[{"question_id": "q1", "text": f"{canary} question"}]))
        store.save_draft(draft("drf_1", rref("ord_1001")).model_copy(update={"review": review}))
        store.save_approval(approval("apr_1", rref("ord_1001")).model_copy(
            update={"holder_id": f"holder-{canary}"}))
        store.save_package(pkg, approval_id="apr_1")
        store.save_delivery(Delivery(delivery_id="dlv_1", package_id="pkg_1", request_id="rfc_a_001",
                                     audience="bank_a", package_sha256=canon.digest(pkg),
                                     delivered_at=NOW))
        store.append_audit(AuditEvent(audit_id="aud_1", at=NOW, actor="holder_maya",
                                      action="approve", outcome="ok", detail=f"{canary} audit"))
        store.append_traces([TraceEvent(trace_id="trc_1", run_id="run_1", seq=0, at=NOW,
                                        actor="rules", step="note", args={"q": canary},
                                        result={"amount_pence": 216000})])
        store.update_draft_status("drf_1", DraftStatus.APPROVED)
        store.set_alert_state("alr_1", "resolved")
    con = sqlite3.connect(db)
    try:
        tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert tables == set(ALLOWED_CLEAR_COLUMNS)
        for table in sorted(tables):
            columns = [row[1] for row in con.execute(f"PRAGMA table_info({table})")]
            assert set(columns) <= ALLOWED_CLEAR_COLUMNS[table], table
            clear = [c for c in columns if c not in ENCRYPTED_COLUMNS]
            if not clear:
                continue
            for row in con.execute(f"SELECT {', '.join(clear)} FROM {table}"):
                for value in row:
                    assert value not in amounts, (table, value)
                    assert canary not in str(value), (table, value)
    finally:
        con.close()
    on_disk = b"".join(p.read_bytes() for p in db.parent.iterdir() if p.name.startswith(db.name))
    assert canary.encode() not in on_disk


def test_request_ids_are_scoped_to_the_bank(store):
    """Request ids are the bank's own, so (request_id, audience) is the key. Another bank's use
    of an id neither conflicts with, replaces nor reveals this bank's request."""
    from alibi.core.store import AmbiguousRequest

    period = {"from": "2026-10-01T00:00:00Z", "to": "2026-11-01T00:00:00Z"}
    squat = rfc(issuer="bank_b", audience="bank_b", txn_ids=None, period=period)
    assert store.save_request(squat) == "inserted"
    assert store.save_request(rfc()) == "inserted"            # bank_a's genuine request
    assert store.get_request("rfc_a_001", audience="bank_a") == rfc()
    assert store.get_request("rfc_a_001", audience="bank_b") == squat
    # A bare id resolves to the request received first (bank_b squatted first here), so a later
    # bank never captures the earlier one's drafts and packages; the holder API asks strictly.
    assert store.get_request("rfc_a_001") == squat
    with pytest.raises(AmbiguousRequest):
        store.get_request("rfc_a_001", strict=True)
    # A conflict is only ever with the same bank's own earlier request.
    with pytest.raises(IdempotencyConflict):
        store.save_request(rfc(issuer="bank_b", audience="bank_b", txn_ids=None, period=period,
                               expires_at="2026-11-12T09:00:00Z"))
    assert store.save_request(rfc()) == "duplicate"
    # A bank that never used the id gets the same NotFound as for an id nobody used.
    with pytest.raises(NotFound) as unused:
        store.get_request("rfc_a_001", audience="bank_c")
    with pytest.raises(NotFound) as nobody:
        store.get_request("rfc_zzz", audience="bank_c")
    assert (str(unused.value), str(nobody.value)) == ("no request 'rfc_a_001'",
                                                      "no request 'rfc_zzz'")
    # The two requests' claims stay apart.
    mine = claim("clm_a", rref("po_5001"))
    theirs = Claim.model_validate({**claim("clm_b", rref("po_5001")).to_json_dict(), "audience": "bank_b"})
    store.save_claim(mine)
    store.save_claim(theirs)
    assert store.list_claims(request_id="rfc_a_001", audience="bank_a") == [mine]
    assert store.list_claims(request_id="rfc_a_001", audience="bank_b") == [theirs]
