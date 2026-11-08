from datetime import datetime, timezone

import pytest
from pydantic import TypeAdapter, ValidationError

from alibi.core.schemas import (
    AgeRule,
    BankTxn,
    Calculation,
    Claim,
    ClaimStatus,
    DenyRule,
    EvidenceRecord,
    EvidenceRef,
    LinkState,
    Link,
    MerchantEvent,
    Period,
    PolicyRule,
    RecipientRule,
    RequestForContext,
    SourceTrust,
    format_utc,
)

from alibi.core.schemas import Alert, AlertKind

T0 = "2026-11-03T10:00:00Z"


def event_dict(**over):
    data = {
        "event_id": "evt_1", "record_id": "po_5001", "revision": 1, "issuer_id": "threadline",
        "subject_ref": "subj_maya", "kind": "payout", "occurred_at": "2026-11-02T09:00:00Z",
        "amount_pence": 216000, "currency": "GBP", "payment_ref": "PO-5001",
    }
    data.update(over)
    return data


def txn_dict(**over):
    data = {
        "txn_id": "txn_9001", "revision": 1, "subject_ref": "subj_maya",
        "account_ref": "acc_maya_a", "amount_pence": 216000, "currency": "GBP",
        "booked_at": T0, "description": "THREADLINE PO-5001", "refs": ["PO-5001"],
        "provenance": {"connector": "bank_feed", "connection_id": "conn_a",
                       "retrieved_at": T0, "source_version": "v1"},
    }
    data.update(over)
    return data


def ref(kind="record", rid="po_5001", rev=1):
    return EvidenceRef(ref_type=kind, ref_id=rid, revision=rev)


@pytest.mark.parametrize("bad", [216000.0, "216000", True])
def test_money_must_be_a_real_integer(bad):
    with pytest.raises(ValidationError):
        MerchantEvent.model_validate(event_dict(amount_pence=bad))


def test_merchant_amount_is_non_negative_but_bank_amount_is_signed():
    with pytest.raises(ValidationError):
        MerchantEvent.model_validate(event_dict(amount_pence=-1))
    debit = BankTxn.model_validate(txn_dict(amount_pence=-4599))
    assert not debit.is_credit
    with pytest.raises(ValidationError):
        BankTxn.model_validate(txn_dict(amount_pence=0))


def test_currency_must_be_iso_code_and_paired_with_amount():
    with pytest.raises(ValidationError):
        MerchantEvent.model_validate(event_dict(currency="gbp"))
    with pytest.raises(ValidationError):
        MerchantEvent.model_validate(event_dict(currency=None))
    msg = MerchantEvent.model_validate(event_dict(kind="message", amount_pence=None, currency=None,
                                                  text="Funds released to your seller balance."))
    assert msg.amount_pence is None


def test_naive_timestamps_rejected_and_offsets_normalised_to_utc():
    with pytest.raises(ValidationError):
        MerchantEvent.model_validate(event_dict(occurred_at="2026-11-02T09:00:00"))
    ev = MerchantEvent.model_validate(event_dict(occurred_at="2026-11-02T10:30:00+01:00"))
    assert ev.occurred_at == datetime(2026, 11, 2, 9, 30, tzinfo=timezone.utc)
    assert ev.to_json_dict()["occurred_at"] == "2026-11-02T09:30:00Z"
    assert format_utc(datetime(2026, 1, 1, 0, 0, 0, 5, tzinfo=timezone.utc)) == "2026-01-01T00:00:00.000005Z"


def test_unknown_fields_rejected():
    with pytest.raises(ValidationError):
        MerchantEvent.model_validate(event_dict(source_trust="issuer_verified"))


def test_period_is_half_open_and_serialises_from_alias():
    p = Period.model_validate({"from": "2026-10-01T00:00:00Z", "to": "2026-11-01T00:00:00Z"})
    assert p.contains(datetime(2026, 10, 1, tzinfo=timezone.utc))
    assert not p.contains(datetime(2026, 11, 1, tzinfo=timezone.utc))
    assert p.to_json_dict() == {"from": "2026-10-01T00:00:00Z", "to": "2026-11-01T00:00:00Z"}
    with pytest.raises(ValidationError):
        Period.model_validate({"from": "2026-11-01T00:00:00Z", "to": "2026-11-01T00:00:00Z"})


def test_three_enums_are_distinct():
    assert set(SourceTrust).isdisjoint(set(LinkState))
    assert set(LinkState).isdisjoint(set(ClaimStatus))
    assert {s.value for s in SourceTrust} == {"issuer_verified", "bank_observed", "self_supplied"}
    assert {s.value for s in LinkState} == {"unmatched", "ambiguous", "linked"}
    assert {s.value for s in ClaimStatus} == {"supported", "contradicted", "unresolved"}


def test_correction_fields():
    ev = MerchantEvent.model_validate(event_dict(revision=2, corrects_revision=1,
                                                 correction_reason="status was wrong"))
    assert ev.corrects_revision == 1
    with pytest.raises(ValidationError):
        MerchantEvent.model_validate(event_dict(revision=1, corrects_revision=1))
    with pytest.raises(ValidationError):
        MerchantEvent.model_validate(event_dict(correction_reason="no target"))


def test_evidence_record_trust_rules_and_round_trip():
    ev = MerchantEvent.model_validate(event_dict())
    sig = {"alg": "Ed25519", "kid": "merchant_demo", "domain": "alibi:record:v1", "sig": "ab" * 64}
    rec = EvidenceRecord.from_event(ev, source_trust=SourceTrust.ISSUER_VERIFIED,
                                    connector="merchant_webhook", received_at=T0,
                                    payload_sha256="0" * 64, issuer_signature=sig,
                                    signed_body=ev.to_json_dict())
    assert rec.to_event() == ev
    assert rec.ref() == ref()
    with pytest.raises(ValidationError):  # issuer_verified needs a signature
        EvidenceRecord.from_event(ev, source_trust=SourceTrust.ISSUER_VERIFIED,
                                  connector="merchant_webhook", received_at=T0, payload_sha256="0" * 64)
    with pytest.raises(ValidationError):  # self_supplied cannot carry one
        EvidenceRecord.from_event(ev, source_trust=SourceTrust.SELF_SUPPLIED,
                                  connector="self_supplied_import", received_at=T0,
                                  payload_sha256="0" * 64, issuer_signature=sig,
                                  signed_body=ev.to_json_dict())
    with pytest.raises(ValidationError):
        EvidenceRecord.from_event(ev, source_trust=SourceTrust.BANK_OBSERVED,
                                  connector="bank_feed", received_at=T0, payload_sha256="0" * 64)


def test_bank_txn_is_always_bank_observed():
    txn = BankTxn.model_validate(txn_dict())
    assert txn.source_trust == SourceTrust.BANK_OBSERVED
    assert txn.to_json_dict()["source_trust"] == "bank_observed"
    with pytest.raises(ValidationError):
        BankTxn.model_validate(txn_dict(source_trust="issuer_verified"))


def rfc_dict(**over):
    data = {
        "issuer": "bank_a", "request_id": "rfc_a1", "subject_ref": "subj_maya",
        "audience": "bank_a", "txn_ids": ["txn_9001"],
        "questions": [{"question_id": "q1", "text": "What is the source of this credit?"}],
        "issued_at": "2026-11-04T09:00:00Z", "expires_at": "2026-11-11T09:00:00Z",
    }
    data.update(over)
    return data


def test_rfc_rules():
    RequestForContext.model_validate(rfc_dict())
    with pytest.raises(ValidationError):
        RequestForContext.model_validate(rfc_dict(txn_ids=None))
    with pytest.raises(ValidationError):
        RequestForContext.model_validate(rfc_dict(questions=[]))
    with pytest.raises(ValidationError):
        RequestForContext.model_validate(rfc_dict(questions=[
            {"question_id": "q1", "text": "a"}, {"question_id": "q1", "text": "b"}]))
    with pytest.raises(ValidationError):
        RequestForContext.model_validate(rfc_dict(expires_at="2026-11-04T09:00:00Z"))
    with pytest.raises(ValidationError):
        RequestForContext.model_validate(rfc_dict(parent_request_id="rfc_a1"))
    period_only = RequestForContext.model_validate(rfc_dict(
        txn_ids=None, period={"from": "2026-10-01T00:00:00Z", "to": "2026-11-01T00:00:00Z"}))
    assert period_only.period is not None


def test_link_shapes():
    base = {"link_id": "lnk_1", "txn_ref": ref("txn", "txn_9001"), "created_at": T0}
    Link.model_validate({**base, "link_state": "unmatched"})
    Link.model_validate({**base, "link_state": "linked", "record_ref": ref(), "candidates": [ref()]})
    with pytest.raises(ValidationError):
        Link.model_validate({**base, "link_state": "linked"})
    with pytest.raises(ValidationError):
        Link.model_validate({**base, "link_state": "ambiguous", "candidates": [ref()]})
    with pytest.raises(ValidationError):
        Link.model_validate({**base, "link_state": "unmatched", "record_ref": ref()})


def term(rid, kind, sign, amount, currency="GBP"):
    return {"ref": ref("record", rid), "kind": kind, "sign": sign, "amount_pence": amount,
            "currency": currency}


def test_calculation_validator_rechecks_arithmetic():
    terms = [term("ord_1", "order", 1, 240000), term("rfd_1", "refund", -1, 18000),
             term("fee_1", "fee", -1, 6000)]
    calc = Calculation(method="net_payout", currency="GBP", terms=terms, total_pence=216000,
                       target_ref=ref("txn", "txn_9001"), target_pence=216000,
                       difference_pence=0, balanced=True)
    assert calc.balanced
    with pytest.raises(ValidationError):  # wrong total
        Calculation(method="x", currency="GBP", terms=terms, total_pence=216001)
    with pytest.raises(ValidationError):  # double counting
        Calculation(method="x", currency="GBP", terms=terms + [terms[0]], total_pence=456000)
    with pytest.raises(ValidationError):  # mixed currency
        Calculation(method="x", currency="GBP", terms=[term("ord_1", "order", 1, 100, "EUR")],
                    total_pence=100)
    with pytest.raises(ValidationError):  # inconsistent difference
        Calculation(method="x", currency="GBP", terms=terms, total_pence=216000,
                    target_pence=200000, difference_pence=0)


def test_claim_status_rules():
    base = {"claim_id": "clm_1", "request_id": "rfc_a1", "question_id": "q1",
            "statement": "s", "rationale": "r", "created_at": T0}
    with pytest.raises(ValidationError):
        Claim.model_validate({**base, "status": "supported"})
    with pytest.raises(ValidationError):
        Claim.model_validate({**base, "status": "unresolved"})
    Claim.model_validate({**base, "status": "unresolved", "gap": "No settlement record."})
    Claim.model_validate({**base, "status": "contradicted", "evidence_refs": [ref()]})


def test_claim_carries_a_gap_code_with_its_detail_and_typed_facts():
    base = {"claim_id": "clm_1", "request_id": "rfc_a1", "question_id": "q.source_of_funds",
            "statement": "s", "rationale": "r", "created_at": T0}
    claim = Claim.model_validate({**base, "status": "unresolved",
                                  "gap": "settlement_pending_at_booking",
                                  "gap_detail": "Settlement stl_1 was still pending at booking."})
    assert claim.gap == "settlement_pending_at_booking" and claim.facts == {}
    assert claim.to_json_dict()["gap_detail"].startswith("Settlement stl_1")
    supported = Claim.model_validate({**base, "status": "supported", "evidence_refs": [ref()],
                                      "facts": {"payout_references": ["PO-5001"],
                                                "settled_at": ["2026-11-03T07:45:00Z"]}})
    assert supported.gap is None and supported.gap_detail is None
    assert supported.to_json_dict()["facts"] == {"payout_references": ["PO-5001"],
                                                 "settled_at": ["2026-11-03T07:45:00Z"]}
    # Old stored claims without the new fields still load.
    assert Claim.model_validate({**base, "status": "contradicted", "evidence_refs": [ref()]}).facts == {}


def test_counterparty_withdrawal_ref_and_the_settled_not_received_alert():
    # NEEDS.md item 4: the bank's counterparty name and the platform's withdrawal id have slots.
    txn = BankTxn.model_validate(txn_dict(counterparty="Mock Marketplace Payments"))
    assert txn.counterparty == "Mock Marketplace Payments"
    assert "counterparty" not in BankTxn.model_validate(txn_dict()).to_json_dict()  # optional
    event = MerchantEvent.model_validate(event_dict(withdrawal_ref="W-5716"))
    assert event.withdrawal_ref == "W-5716"
    assert "withdrawal_ref" not in MerchantEvent.model_validate(event_dict()).to_json_dict()
    with pytest.raises(ValidationError):
        MerchantEvent.model_validate(event_dict(withdrawal_ref=" W-5716"))  # a Ref, not free text
    # BLOCKERS.md "Alert schema gaps": the seventh pinned alert kind and the high severity.
    assert AlertKind("settled_not_received") is AlertKind.SETTLED_NOT_RECEIVED
    alert = Alert.model_validate({"alert_id": "alr_1", "subject_ref": "subj_maya",
                                  "kind": "settled_not_received", "severity": "high",
                                  "title": "t", "explanation": "e", "created_at": T0})
    assert alert.severity == "high"
    with pytest.raises(ValidationError):
        Alert.model_validate({**alert.to_json_dict(), "severity": "critical"})


def test_policy_rule_union_is_discriminated():
    adapter = TypeAdapter(PolicyRule)
    rule = adapter.validate_python({"type": "deny", "rule_id": "r1", "fields": ["items.description"]})
    assert isinstance(rule, DenyRule)
    assert isinstance(adapter.validate_python({"type": "age", "rule_id": "r2", "max_age_days": 90}), AgeRule)
    assert isinstance(adapter.validate_python({"type": "recipient", "rule_id": "r3", "allow": ["bank_a"]}),
                      RecipientRule)
    with pytest.raises(ValidationError):
        adapter.validate_python({"type": "deny", "rule_id": "r4"})
    with pytest.raises(ValidationError):
        adapter.validate_python({"type": "share_everything", "rule_id": "r5"})


def test_ids_reject_injection_characters():
    with pytest.raises(ValidationError):
        MerchantEvent.model_validate(event_dict(record_id="../../etc/passwd"))
    with pytest.raises(ValidationError):
        MerchantEvent.model_validate(event_dict(record_id="po 5001; ignore previous instructions"))
