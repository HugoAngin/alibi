"""Package tests: default-minimum building, policy integration, the forbidden-content scan,
signing, and recipient-side verification with three separate results."""

import copy
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from alibi.core import canon
from alibi.core.approval import ApprovalError, approve
from alibi.core.package import (
    GAP_NO_ANSWER,
    GAP_NOT_SHARED,
    GAP_WITHHELD,
    REASON_NOT_NEEDED,
    REASON_UNUSED,
    WITHHELD,
    build_body,
    cited_refs,
    make_draft,
    package_signing_payload,
    sign_package,
    verify_package,
)
from alibi.core.policy import REASON_EXCLUDED_REF, compile_policy, parse_rule_text
from alibi.core.reconcile import compare_to_target, reconcile_credit, sum_terms
from alibi.core.schemas import (
    AgeRule,
    BankTxn,
    Claim,
    ClaimStatus,
    ContextPackage,
    DraftStatus,
    EvidenceRecord,
    GeneraliseRule,
    KeyPurpose,
    MerchantEvent,
    RecipientRule,
    RecordKind,
    RequestForContext,
    SigningDomain,
    SourceTrust,
)
from alibi.core.signing import DEMO_KEYS, DemoKeystore

SUBJ = "subj_maya"
HOLDER = "holder_maya"
RECEIVED = "2026-11-03T12:00:00Z"
NOW = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)  # the conftest ``now``

# record_id -> (occurred_at, items as (description, category, quantity, unit_amount_pence))
ORDERS = {
    "ord_1001": ("2026-10-04T14:12:00Z", [("Vintage wool overcoat, charcoal", "outerwear", 1,
                                           60000)]),
    "ord_1002": ("2026-10-11T09:30:00Z", [("Denim jacket, washed indigo", "outerwear", 1, 42000),
                                          ("Leather ankle boots, tan", "footwear", 1, 30000)]),
    "ord_1003": ("2026-10-18T16:45:00Z", [("Silk midi dress, emerald", "dresses", 1, 30000),
                                          ("Cashmere scarf, oatmeal", "accessories", 1, 18000)]),
    "ord_1004": ("2026-10-25T11:05:00Z", [("Corduroy trousers, rust", "trousers", 2, 15000),
                                          ("Hand-knitted cardigan, cream", "knitwear", 1, 30000)]),
}
DESCRIPTIONS = sorted({row[0] for _, rows in ORDERS.values() for row in rows})
COMPONENTS = [*ORDERS, "rfd_2001", "fee_3001"]
MESSAGE = ("Your October earnings of 2160.00 GBP have been released to your seller balance. "
           "Withdrawals to your bank account are sent separately as payouts.")
RECEIPT_TEXT = "Northgate Retail receipt NGR-731942: 1 x Rechargeable desk lamp, matte black."


# --------------------------------------------------------------------------- builders


def event(record_id, kind, occurred_at, *, revision=1, issuer_id="threadline", **fields):
    return MerchantEvent.model_validate({
        "event_id": f"evt_{record_id}_r{revision}", "record_id": record_id, "revision": revision,
        "issuer_id": issuer_id, "subject_ref": SUBJ, "kind": kind, "occurred_at": occurred_at,
        **fields})


def signed(keystore, ev: MerchantEvent) -> EvidenceRecord:
    """An issuer-verified record with a real merchant_demo signature over its event body."""
    body = ev.to_json_dict()
    sig = keystore.sign(body, kid="merchant_demo", domain=SigningDomain.RECORD)
    return EvidenceRecord.from_event(
        ev, source_trust=SourceTrust.ISSUER_VERIFIED, connector="merchant_webhook",
        received_at=RECEIVED,
        payload_sha256=canon.digest({"event": body, "signature": sig.to_json_dict()}),
        issuer_signature=sig, signed_body=body)


def raw_payload(record: EvidenceRecord) -> dict:
    """The received payload whose digest is the record's payload_sha256."""
    return {"event": record.signed_body, "signature": record.issuer_signature.to_json_dict()}


def self_supplied(ev: MerchantEvent) -> EvidenceRecord:
    return EvidenceRecord.from_event(
        ev, source_trust=SourceTrust.SELF_SUPPLIED, connector="self_supplied_import",
        received_at=RECEIVED, payload_sha256=canon.digest({"event": ev.to_json_dict()}))


def bank_txn(txn_id="txn_9001", amount=216000, *, booked_at="2026-11-03T10:00:00Z",
             description="THREADLINE PAYOUT PO-5001 STL-88001", refs=("PO-5001", "STL-88001"),
             revision=1) -> BankTxn:
    return BankTxn.model_validate({
        "txn_id": txn_id, "revision": revision, "subject_ref": SUBJ,
        "account_ref": "acc_maya_bank_a", "amount_pence": amount, "currency": "GBP",
        "booked_at": booked_at, "description": description, "refs": list(refs),
        "provenance": {"connector": "bank_feed", "connection_id": "conn_bank_a_maya",
                       "retrieved_at": "2026-11-03T10:05:00Z", "source_version": "feed-v1"}})


def item_rows(rows):
    return [{"description": d, "category": c, "quantity": q, "unit_amount_pence": u}
            for d, c, q, u in rows]


def settlement(keystore, revision=1, **over) -> EvidenceRecord:
    fields = {"status": "settled", "effective_at": "2026-11-03T07:45:00Z",
              "payment_ref": "PO-5001", "settlement_ref": "STL-88001",
              "amount_pence": 216000, "currency": "GBP"}
    fields.update(over)
    return signed(keystore, event("stl_88001", "settlement", fields.pop("occurred_at",
                                                                        "2026-11-03T08:00:00Z"),
                                  revision=revision, **fields))


def ledger(keystore) -> dict:
    """Maya's canonical October ledger: 240000 sales - 18000 refund - 6000 fee = 216000."""
    ev = {}
    for record_id, (when, rows) in ORDERS.items():
        items = item_rows(rows)
        total = sum(i["quantity"] * i["unit_amount_pence"] for i in items)
        ev[record_id] = signed(keystore, event(record_id, "order", when, status="completed",
                                               items=items, amount_pence=total, currency="GBP"))
    ev["rfd_2001"] = signed(keystore, event(
        "rfd_2001", "refund", "2026-10-22T10:00:00Z", status="completed",
        items=item_rows([("Cashmere scarf, oatmeal", "accessories", 1, 18000)]),
        related_record_ids=["ord_1003"], amount_pence=18000, currency="GBP"))
    ev["fee_3001"] = signed(keystore, event("fee_3001", "fee", "2026-10-31T18:00:00Z",
                                            status="completed", amount_pence=6000, currency="GBP"))
    ev["msg_7001"] = signed(keystore, event(
        "msg_7001", "message", "2026-11-01T08:00:00Z", status="released_to_balance",
        text=MESSAGE, amount_pence=216000, currency="GBP"))
    ev["po_5001"] = signed(keystore, event(
        "po_5001", "payout", "2026-11-02T08:30:00Z", status="initiated", payment_ref="PO-5001",
        related_record_ids=COMPONENTS, amount_pence=216000, currency="GBP"))
    ev["stl_88001"] = settlement(keystore)
    ev["txn_9001"] = bank_txn()
    return ev


def rfc_a(**over) -> RequestForContext:
    data = {
        "issuer": "bank_a", "audience": "bank_a", "request_id": "rfc_a_001", "subject_ref": SUBJ,
        "txn_ids": ["txn_9001"],
        "questions": [{"question_id": "q1",
                       "text": "Please explain the source of the 2160.00 GBP credit txn_9001."}],
        "issued_at": "2026-11-04T09:00:00Z", "expires_at": "2026-11-11T09:00:00Z"}
    data.update(over)
    return RequestForContext.model_validate(data)


def rfc_b(**over) -> RequestForContext:
    data = {
        "issuer": "bank_b", "audience": "bank_b", "request_id": "rfc_b_001", "subject_ref": SUBJ,
        "period": {"from": "2026-10-01T00:00:00Z", "to": "2026-11-01T00:00:00Z"},
        "questions": [
            {"question_id": "q1", "text": "What were your documented sales in October?"},
            {"question_id": "q2", "text": "What refunds did you issue over the same period?"}],
        "issued_at": "2026-11-05T09:00:00Z", "expires_at": "2026-11-12T09:00:00Z"}
    data.update(over)
    return RequestForContext.model_validate(data)


def claim_a(ev, **over) -> Claim:
    """Supported RFC A answer: the credit is payout PO-5001, reconciled and settled in time."""
    calc = reconcile_credit(ev["txn_9001"], components=[ev[c] for c in COMPONENTS],
                            payout=ev["po_5001"], settlement=ev["stl_88001"])
    data = {
        "claim_id": "clm_a_q1", "request_id": "rfc_a_001", "question_id": "q1",
        "statement": ("The 2160.00 GBP credit is the threadline payout PO-5001, settled as "
                      "STL-88001 before the credit was booked."),
        "status": ClaimStatus.SUPPORTED,
        "rationale": ("October sales less the refund rfd_2001 and the platform fee reconcile to "
                      "the payout and the credit; the settlement was effective before booking."),
        "evidence_refs": [ev["po_5001"].ref(), ev["stl_88001"].ref(), ev["txn_9001"].ref()],
        "calculation": calc, "created_at": NOW}
    data.update(over)
    return Claim(**data)


def claims_b(ev) -> list[Claim]:
    """Supported RFC B answers whose text names items (the scan must catch them)."""
    orders = [ev[record_id] for record_id in ORDERS]
    return [
        Claim(claim_id="clm_b_q1", request_id="rfc_b_001", question_id="q1",
              statement=("Documented October sales were 240000 pence, including a Vintage wool "
                         "overcoat, charcoal."),
              status=ClaimStatus.SUPPORTED,
              rationale=("Four completed orders, such as the Silk midi dress, emerald and the "
                         "Cashmere scarf, oatmeal."),
              evidence_refs=[o.ref() for o in orders],
              calculation=sum_terms(orders, method="documented_sales"), created_at=NOW),
        Claim(claim_id="clm_b_q2", request_id="rfc_b_001", question_id="q2",
              statement="One refund of 18000 pence was issued for the Cashmere scarf, oatmeal.",
              status=ClaimStatus.SUPPORTED,
              rationale="The refund record rfd_2001 refunds part of order ord_1003.",
              evidence_refs=[ev["rfd_2001"].ref()],
              calculation=sum_terms([ev["rfd_2001"]], method="documented_refunds"),
              created_at=NOW),
    ]


def retail(keystore):
    """A self-supplied receipt explaining a Bank A card debit."""
    receipt = self_supplied(event(
        "self_rcpt_4001", "receipt", "2026-11-05T13:20:00Z", issuer_id="northgate_retail",
        status="paid", payment_ref="NGR-731942", text=RECEIPT_TEXT,
        items=item_rows([("Rechargeable desk lamp, matte black", "homeware", 1, 4599)]),
        amount_pence=4599, currency="GBP"))
    debit = bank_txn("txn_9002", -4599, booked_at="2026-11-06T09:00:00Z",
                     description="NORTHGATE RETAIL CARD PAYMENT", refs=("NGR-731942",))
    request = rfc_a(request_id="rfc_a_002", txn_ids=["txn_9002"], questions=[
        {"question_id": "q1", "text": "What was the 45.99 GBP card payment on 6 November for?"}])
    claim = Claim(claim_id="clm_retail_q1", request_id="rfc_a_002", question_id="q1",
                  statement="The card payment is the purchase on receipt NGR-731942.",
                  status=ClaimStatus.SUPPORTED, rationale="The receipt amount matches the debit.",
                  evidence_refs=[receipt.ref(), debit.ref()],
                  calculation=compare_to_target(sum_terms([receipt], method="receipt_total"),
                                                debit),
                  created_at=NOW)
    return SimpleNamespace(receipt=receipt, debit=debit, request=request, claim=claim)


def pipeline(keystore, request, claims, evidence, policy=None, *, now=NOW):
    """build -> draft -> approve -> sign, the way the dispatch path runs it."""
    compiled = compile_policy(policy)
    body, review = build_body(request, claims, evidence, compiled, now=now)
    draft = make_draft(request, body, review, policy_version=compiled.policy_version,
                       source_revisions=cited_refs(claims), now=now)
    approval = approve(draft, holder_id=HOLDER, now=now)
    revisions = {ref.token(): ref.revision for ref in approval.source_revisions}
    package = sign_package(body, approval, keystore, policy_version=compiled.policy_version,
                           revisions=revisions, now=now)
    return SimpleNamespace(body=body, review=review, draft=draft, approval=approval,
                           revisions=revisions, package=package, compiled=compiled)


def attachment(body, ref_id):
    return next((a for a in body.attachments if a.ref.ref_id == ref_id), None)


def body_text(body) -> str:
    return json.dumps(body.to_json_dict(), ensure_ascii=False)


def resign(raw: dict, keystore) -> dict:
    """Recompute the body digest and the Alibi signature, as a dishonest signer could."""
    raw["body_sha256"] = canon.digest(raw["body"])
    raw["signature"] = keystore.sign(package_signing_payload(raw), kid="alibi_demo",
                                     domain=SigningDomain.PACKAGE).to_json_dict()
    return raw


def raw_attachment(raw: dict, ref_id: str) -> dict:
    return next(a for a in raw["body"]["attachments"] if a["ref"]["ref_id"] == ref_id)


@pytest.fixture
def ev(keystore):
    return ledger(keystore)


@pytest.fixture
def control(keystore, ev):
    """RFC A answered from the whole ledger with no policy: the valid control package."""
    return pipeline(keystore, rfc_a(), [claim_a(ev)], list(ev.values()))


# --------------------------------------------------------------------------- verification


def test_valid_control_package_is_accepted_with_three_separate_results(control, registry, now):
    result = verify_package(control.package, registry, expected_audience="bank_a", now=now)

    assert result.accepted is True
    assert result.errors == []
    assert result.package_sha256 == canon.digest(control.package.to_json_dict())
    auth = result.authentication
    assert auth.valid and auth.package_id == control.package.package_id
    assert auth.audience == "bank_a" and auth.kid == "alibi_demo" and auth.reasons == []

    by_id = {s.ref.ref_id: s for s in result.sources}
    assert set(by_id) == {*COMPONENTS, "po_5001", "stl_88001", "txn_9001"}
    for record_id in [*COMPONENTS, "po_5001", "stl_88001"]:
        source = by_id[record_id]
        assert source.form == "full_record" and source.attestation == "issuer_signature"
        assert source.signature_valid is True and source.lower_trust is False
    credit = by_id["txn_9001"]
    assert credit.form == "redacted_extract" and credit.claimed_trust == SourceTrust.BANK_OBSERVED
    assert credit.signature_valid is None and credit.lower_trust is True

    assert [(c.question_id, c.status, c.calculation_valid) for c in result.claims] == [
        ("q1", ClaimStatus.SUPPORTED, True)]
    assert result.claims[0].evidence_refs == control.body.claims[0].evidence_refs


def test_verify_accepts_json_text_and_bytes(control, registry, now):
    text = json.dumps(control.package.to_json_dict())
    for received in (text, text.encode("utf-8"), control.package.to_json_dict()):
        result = verify_package(received, registry, expected_audience="bank_a", now=now)
        assert result.accepted is True


def test_tampered_package_fails(control, registry, now):
    raw = control.package.to_json_dict()
    raw_attachment(raw, "txn_9001")["content"]["amount_pence"] = 2160000  # inflate the credit

    result = verify_package(raw, registry, expected_audience="bank_a", now=now)

    assert result.accepted is False
    assert result.authentication.valid is False
    reasons = " | ".join(result.authentication.reasons)
    assert "body_sha256 does not match" in reasons
    assert "signature does not verify" in reasons


def test_wrong_recipient_package_fails(control, registry, now):
    result = verify_package(control.package, registry, expected_audience="bank_b", now=now)

    assert result.accepted is False
    assert result.authentication.valid is False
    assert any("addressed to bank_a, not bank_b" in r for r in result.authentication.reasons)


def test_expired_package_fails(control, registry):
    expires_at = control.package.body.expires_at
    for moment in (expires_at, expires_at + timedelta(seconds=1)):
        result = verify_package(control.package, registry, expected_audience="bank_a", now=moment)
        assert result.accepted is False
        assert any("expired" in r for r in result.authentication.reasons)
    just_before = verify_package(control.package, registry, expected_audience="bank_a",
                                 now=expires_at - timedelta(seconds=1))
    assert just_before.accepted is True


def test_self_supplied_attachment_without_signature_is_accepted_at_lower_trust(keystore, registry,
                                                                                now):
    case = retail(keystore)
    run = pipeline(keystore, case.request, [case.claim], [case.receipt, case.debit])
    receipt = attachment(run.body, "self_rcpt_4001")
    assert receipt.form == "redacted_extract" and receipt.issuer_signature is None
    assert receipt.source_trust == SourceTrust.SELF_SUPPLIED
    assert "text" not in receipt.content  # free text never leaves in an extract

    result = verify_package(run.package, registry, expected_audience="bank_a", now=now)
    assert result.accepted is True
    source = next(s for s in result.sources if s.ref.ref_id == "self_rcpt_4001")
    assert source.claimed_trust == SourceTrust.SELF_SUPPLIED
    assert source.attestation == "package_signer"
    assert source.signature_valid is None and source.lower_trust is True
    assert result.claims[0].status == ClaimStatus.SUPPORTED
    assert result.claims[0].calculation_valid is True

    # The same unsigned self-supplied record sent in full-record form is also reviewable.
    raw = run.package.to_json_dict()
    raw_attachment(raw, "self_rcpt_4001")["form"] = "full_record"
    resign(raw, keystore)
    as_full = verify_package(raw, registry, expected_audience="bank_a", now=now)
    assert as_full.accepted is True
    source = next(s for s in as_full.sources if s.ref.ref_id == "self_rcpt_4001")
    assert source.form == "full_record" and source.signature_valid is None
    assert source.lower_trust is True


def test_bad_issuer_signature_rejects_even_with_a_valid_alibi_signature(control, keystore,
                                                                       registry, now):
    raw = control.package.to_json_dict()
    tampered = raw_attachment(raw, "ord_1001")
    assert tampered["form"] == "full_record"
    tampered["content"]["items"][0]["description"] = "Designer handbag"
    resign(raw, keystore)  # only the issuer signature can now catch the change

    result = verify_package(raw, registry, expected_audience="bank_a", now=now)

    assert result.authentication.valid is True
    assert result.accepted is False
    source = next(s for s in result.sources if s.ref.ref_id == "ord_1001")
    assert source.signature_valid is False and source.lower_trust is True
    assert any(source.attachment_id in e and "issuer signature" in e for e in result.errors)
    others = [s for s in result.sources if s.ref.ref_id != "ord_1001"]
    assert all(s.signature_valid in (True, None) for s in others)


def test_issuer_signature_must_come_from_the_named_issuers_key(tmp_path, now):
    specs = {**DEMO_KEYS, "merchant_other": (KeyPurpose.RECORD_SIGNING, "otherco")}
    keystore = DemoKeystore(tmp_path / "multi", specs=specs).ensure()
    ev = ledger(keystore)
    raw = pipeline(keystore, rfc_a(), [claim_a(ev)], list(ev.values())).package.to_json_dict()
    forged = raw_attachment(raw, "fee_3001")
    forged["issuer_signature"] = keystore.sign(forged["content"], kid="merchant_other",
                                               domain=SigningDomain.RECORD).to_json_dict()
    resign(raw, keystore)

    result = verify_package(raw, keystore.registry(), expected_audience="bank_a", now=now)

    assert result.authentication.valid is True
    assert result.accepted is False
    assert any("does not belong to issuer 'threadline'" in e for e in result.errors)


def test_full_record_content_must_match_its_reference(control, keystore, registry, now):
    raw = control.package.to_json_dict()
    raw_attachment(raw, "fee_3001")["ref"]["ref_id"] = "fee_3002"  # signed body says fee_3001
    resign(raw, keystore)

    result = verify_package(raw, registry, expected_audience="bank_a", now=now)

    assert result.authentication.valid is True
    assert result.accepted is False
    assert any("record_id does not match" in e for e in result.errors)


def test_calculation_is_rechecked_against_disclosed_amounts(control, keystore, registry, now):
    raw = control.package.to_json_dict()
    raw_attachment(raw, "txn_9001")["content"]["amount_pence"] = 216500
    resign(raw, keystore)

    result = verify_package(raw, registry, expected_audience="bank_a", now=now)

    # Authentication and source trust stand on their own; only the claim's arithmetic fails.
    assert result.accepted is True and result.authentication.valid is True
    assert result.claims[0].status == ClaimStatus.SUPPORTED
    assert result.claims[0].calculation_valid is False


def test_verify_never_raises_on_bad_input(control, registry, now):
    raw = control.package.to_json_dict()
    raw["unexpected"] = True
    for bad in (b"{not json", "[]", 12345, None, {"schema_version": "alibi.package.v1"}, raw,
                '{"a": 1, "a": 2}'):
        result = verify_package(bad, registry, expected_audience="bank_a", now=now)
        assert result.accepted is False
        assert result.errors and result.authentication.valid is False


@pytest.mark.parametrize("same_id", [True, False])
def test_a_second_copy_of_a_signed_source_rejects_the_package(control, keystore, registry, now,
                                                              same_id):
    raw = control.package.to_json_dict()
    terms = {t["ref"]["ref_id"]: t for t in raw["body"]["claims"][0]["calculation"]["terms"]}
    for record_id, delta in (("ord_1001", -5000), ("ord_1002", 5000)):  # still balances
        terms[record_id]["amount_pence"] += delta
        shadow = copy.deepcopy(raw_attachment(raw, record_id))
        del shadow["issuer_signature"]
        shadow.update(form="redacted_extract", content={
            "record_id": record_id, "revision": 1, "kind": "order",
            "amount_pence": terms[record_id]["amount_pence"], "currency": "GBP"})
        if not same_id:
            shadow["attachment_id"] = f"att_shadow_{record_id}"
        raw["body"]["attachments"].append(shadow)
    resign(raw, keystore)

    result = verify_package(raw, registry, expected_audience="bank_a", now=now)

    assert result.authentication.valid is True
    assert result.accepted is False
    assert "record:ord_1001@1 is attached more than once" in result.errors
    assert any(e.endswith("appears more than once") for e in result.errors) is same_id
    assert result.claims[0].calculation_valid is False  # never checked against the unsigned copy


def test_a_second_answer_to_one_question_rejects_the_package(control, keystore, registry, now):
    raw = control.package.to_json_dict()
    other = copy.deepcopy(raw["body"]["claims"][0])
    del other["calculation"]
    other.update(statement="The credit is not explained.", status="unresolved",
                 gap="No payout matches the credit.")
    raw["body"]["claims"].append(other)
    resign(raw, keystore)

    result = verify_package(raw, registry, expected_audience="bank_a", now=now)

    assert result.authentication.valid is True
    assert result.accepted is False
    assert "question q1 is answered more than once" in result.errors


def test_verifier_binds_the_subject_and_the_request(control, keystore, registry, now):
    # Another seller's genuine threadline payout, attached as a signed full record.
    foreign = signed(keystore, event("po_9999", "payout", "2026-11-02T08:30:00Z",
                                     subject_ref="subj_other", status="settled",
                                     payment_ref="PO-9999", amount_pence=500000, currency="GBP"))
    raw = control.package.to_json_dict()
    raw["body"]["attachments"].append({
        "attachment_id": canon.make_id("att", "rfc_a_001", foreign.ref().key()),
        "ref": foreign.ref().to_json_dict(), "form": "full_record",
        "source_trust": "issuer_verified", "issuer_id": "threadline",
        "content": foreign.signed_body,
        "issuer_signature": foreign.issuer_signature.to_json_dict()})
    resign(raw, keystore)

    mixed = verify_package(raw, registry, expected_audience="bank_a", now=now)
    assert mixed.authentication.valid is True and mixed.accepted is False
    assert "the attachments are about more than one subject" in mixed.errors

    bound = verify_package(raw, registry, expected_audience="bank_a", now=now,
                           expected_subject=SUBJ)
    assert bound.accepted is False
    source = next(s for s in bound.sources if s.ref.ref_id == "po_9999")
    assert source.signature_valid is True and source.lower_trust is True
    assert any(source.attachment_id in e and "subject_ref does not match" in e
               for e in bound.errors)

    honest = verify_package(control.package, registry, expected_audience="bank_a", now=now,
                            expected_subject=SUBJ, expected_request_id="rfc_a_001")
    assert honest.accepted is True and honest.errors == []
    replay = verify_package(control.package, registry, expected_audience="bank_a", now=now,
                            expected_request_id="rfc_a_002")
    assert replay.accepted is False and replay.authentication.valid is False
    assert any("answers request rfc_a_001, not rfc_a_002" in r
               for r in replay.authentication.reasons)


def test_sign_package_binds_package_id_and_signature(control, registry):
    package = control.package
    assert package.package_id == canon.make_id("pkg", control.approval.approval_id,
                                               control.approval.body_sha256)
    assert package.body_sha256 == canon.digest(control.body) == control.approval.body_sha256
    assert package.issued_at == NOW
    payload = package_signing_payload(package)
    assert set(payload) == {"schema_version", "package_id", "issued_at", "body", "body_sha256"}
    from alibi.core.signing import verify
    entry = verify(payload, package.signature, registry, domain=SigningDomain.PACKAGE)
    assert entry.kid == "alibi_demo"


def test_sign_package_refuses_a_stale_approval_before_signing(control, keystore):
    stale = {**control.revisions, "record:stl_88001": 2}
    with pytest.raises(ApprovalError) as info:
        sign_package(control.body, control.approval, keystore,
                     policy_version=control.compiled.policy_version, revisions=stale, now=NOW)
    assert any("record:stl_88001 changed" in p for p in info.value.problems)


# --------------------------------------------------------------------------- building


def test_default_minimum_attaches_only_cited_evidence(control):
    body, review = control.body, control.review
    attached = {a.ref.ref_id for a in body.attachments}
    assert attached == {*COMPONENTS, "po_5001", "stl_88001", "txn_9001"}
    assert "msg_7001" not in attached
    entry = next(e for e in review.excluded if e.ref.ref_id == "msg_7001")
    assert entry.reason == REASON_NOT_NEEDED
    assert MESSAGE not in body_text(body)
    # Every related record is in the package, so the payout goes out as its signed record.
    payout = attachment(body, "po_5001")
    assert payout.form == "full_record"
    assert payout.issuer_signature is not None and payout.issuer_id == "threadline"
    credit = attachment(body, "txn_9001")
    assert credit.form == "redacted_extract" and "account_ref" not in credit.content
    assert body.claims[0].status == ClaimStatus.SUPPORTED
    assert body.claims[0].calculation is not None


def test_full_record_content_is_the_exact_signed_body(control, ev):
    for record_id in ("po_5001", "stl_88001", "ord_1003"):
        attached = attachment(control.body, record_id)
        assert attached.content == ev[record_id].signed_body
        assert attached.issuer_signature == ev[record_id].issuer_signature


def test_dont_share_item_names_removes_them_from_attachments_and_claim_text(keystore, ev):
    rule = parse_rule_text("don't share item names").rule
    run = pipeline(keystore, rfc_b(), claims_b(ev), list(ev.values()), [rule])
    body, review = run.body, run.review
    text = body_text(body)

    for description in DESCRIPTIONS:
        assert description not in text
    for record_id in [*ORDERS, "rfd_2001"]:
        extract = attachment(body, record_id)
        assert extract.form == "redacted_extract"
        assert all("description" not in item and "category" in item
                   for item in extract.content["items"])
    q1, q2 = body.claims
    assert q1.status == q2.status == ClaimStatus.SUPPORTED  # the evidence itself is shared
    assert WITHHELD in q1.statement and WITHHELD in q1.rationale and WITHHELD in q2.statement
    assert "240000 pence" in q1.statement
    assert any(e.field == "items.description" and e.rule_id == rule.rule_id
               for e in review.withheld_fields)
    assert any("q1 statement" in e.reason for e in review.withheld_fields)


def test_item_names_rule_keeps_records_without_items_as_full_records(keystore, ev):
    rule = parse_rule_text("don't share item names").rule
    run = pipeline(keystore, rfc_a(), [claim_a(ev)], list(ev.values()), [rule])
    assert attachment(run.body, "po_5001").form == "full_record"
    assert attachment(run.body, "stl_88001").form == "full_record"
    assert attachment(run.body, "ord_1001").form == "redacted_extract"
    assert run.body.claims[0].status == ClaimStatus.SUPPORTED


def test_dont_share_refunds_withholds_the_cross_reference_in_two_passes(keystore, ev, registry,
                                                                        now):
    rule = parse_rule_text("don't share refunds").rule
    cites = ["po_5001", "stl_88001", "txn_9001", *COMPONENTS]
    claim = claim_a(ev, calculation=None, evidence_refs=[ev[key].ref() for key in cites])
    run = pipeline(keystore, rfc_a(), [claim], list(ev.values()), [rule])
    body, review = run.body, run.review

    # The payout's signed body names the refund, so it can only go out as an extract, and the
    # second filtering pass drops just the refund from its cross-references.
    payout = attachment(body, "po_5001")
    assert payout.form == "redacted_extract" and payout.issuer_signature is None
    assert payout.content["related_record_ids"] == [*ORDERS, "fee_3001"]
    assert attachment(body, "rfd_2001") is None
    assert "rfd_2001" not in body_text(body)
    excluded = next(e for e in review.excluded if e.ref.ref_id == "rfd_2001")
    assert excluded.rule_id == rule.rule_id
    assert any(e.ref.ref_id == "po_5001" and e.field == "related_record_ids"
               and e.reason == REASON_EXCLUDED_REF for e in review.withheld_fields)
    # The answer cited the refund, so it is downgraded and keeps only what is disclosed.
    q1 = body.claims[0]
    assert q1.status == ClaimStatus.UNRESOLVED and q1.gap == GAP_WITHHELD
    assert {r.ref_id for r in q1.evidence_refs} == set(cites) - {"rfd_2001"}
    change = review.claim_changes[0]
    assert "record:rfd_2001@1" in change.reason and "refund records are denied" in change.reason

    result = verify_package(run.package, registry, expected_audience="bank_a", now=now)
    assert result.accepted is True
    assert next(s for s in result.sources if s.ref.ref_id == "po_5001").lower_trust is True


def test_dropped_calculation_no_longer_discloses_its_terms(keystore, ev):
    rule = parse_rule_text("don't share refunds").rule
    body, review = build_body(rfc_a(), [claim_a(ev)], list(ev.values()), [rule], now=NOW)

    # The reconciliation counts the refund, so it cannot be shown; the orders and the fee were
    # cited only by it and stay out (default-minimum), and so do references to them.
    assert body.claims[0].status == ClaimStatus.UNRESOLVED and body.claims[0].calculation is None
    assert {a.ref.ref_id for a in body.attachments} == {"po_5001", "stl_88001", "txn_9001"}
    assert "related_record_ids" not in attachment(body, "po_5001").content
    unused = {e.ref.ref_id for e in review.excluded if e.reason == REASON_UNUSED}
    assert unused == set(ORDERS) | {"fee_3001"}
    text = body_text(body)
    assert not any(record_id in text for record_id in COMPONENTS)


def test_withheld_kind_is_not_named_in_claim_text(ev):
    rule = parse_rule_text("don't share refunds").rule
    q1, q2 = claims_b(ev)
    q1 = q1.model_copy(update={
        "statement": "Documented October sales were 240000 pence before Refunds and fees."})
    body, review = build_body(rfc_b(), [q1, q2], list(ev.values()), [rule], now=NOW)

    sales, refunds = body.claims
    assert sales.status == ClaimStatus.SUPPORTED
    assert sales.statement == ("Documented October sales were 240000 pence before "
                               f"{WITHHELD} and fees.")
    assert refunds.status == ClaimStatus.UNRESOLVED and refunds.gap == GAP_WITHHELD
    for claim in body.claims:
        assert "refund" not in f"{claim.statement} {claim.rationale} {claim.gap}".lower()
    assert any(e.field == "kind" and e.reason == "withheld text removed from q1 statement"
               for e in review.withheld_fields)


SALES_ONLY = [{"question_id": "q1", "text": "What were your documented sales in October?"}]


def test_denied_kind_is_never_named_even_when_no_claim_cites_it(ev):
    rule = parse_rule_text("don't share refunds").rule
    q1 = claims_b(ev)[0].model_copy(update={
        "statement": "Documented October sales were 240000 pence before refunds.",
        "rationale": "Four completed orders; one was later partly refunded."})
    body, review = build_body(rfc_b(questions=SALES_ONLY), [q1], list(ev.values()), [rule],
                              now=NOW)

    sales = body.claims[0]
    assert sales.status == ClaimStatus.SUPPORTED
    assert sales.statement == f"Documented October sales were 240000 pence before {WITHHELD}."
    assert sales.rationale == f"Four completed orders; one was later partly {WITHHELD}."
    assert any(e.field == "kind" and e.ref.ref_id == "rfd_2001" for e in review.withheld_fields)

    # An unresolved answer's own gap is scrubbed too, even when the passport holds no refund.
    gap_claim = Claim(claim_id="clm_gap", request_id="rfc_b_001", question_id="q1",
                      statement="Documented October sales are not established.",
                      status=ClaimStatus.UNRESOLVED, rationale="Sales could not be totalled.",
                      gap="The refund records for October could not be matched to the orders.",
                      created_at=NOW)
    no_refund = [item for key, item in ev.items() if key != "rfd_2001"]
    body, _ = build_body(rfc_b(questions=SALES_ONLY), [gap_claim], no_refund, [rule], now=NOW)
    assert body.claims[0].gap == (f"The {WITHHELD} records for October could not be matched to "
                                  "the orders.")


def test_denied_orders_are_not_named_as_sales(ev):
    rule = parse_rule_text("don't share sales").rule
    assert rule.kinds == [RecordKind.ORDER]
    q2 = claims_b(ev)[1].model_copy(update={
        "rationale": "The refund reverses part of one October sale."})
    refunds_only = rfc_b(questions=[{"question_id": "q2",
                                     "text": "What refunds did you issue over the same period?"}])
    body, _ = build_body(refunds_only, [q2], list(ev.values()), [rule], now=NOW)

    assert body.claims[0].status == ClaimStatus.SUPPORTED
    assert body.claims[0].rationale == f"The refund reverses part of one October {WITHHELD}."


def test_a_withheld_id_never_matches_inside_a_shared_one(keystore, ev, registry, now):
    # Default-minimum leaves out an older ord_100; its id must not mangle ord_1001..ord_1004.
    ev["ord_100"] = signed(keystore, event(
        "ord_100", "order", "2026-09-12T10:00:00Z", status="completed", amount_pence=1500,
        currency="GBP", items=item_rows([("Wool socks", "accessories", 1, 1500)])))
    q1 = claims_b(ev)[0].model_copy(update={
        "rationale": "Orders ord_1001 to ord_1004 were completed; ord_100 is from September."})
    run = pipeline(keystore, rfc_b(questions=SALES_ONLY), [q1], list(ev.values()))

    for record_id in ORDERS:
        attached = attachment(run.body, record_id)
        assert attached.form == "full_record"
        assert attached.content == ev[record_id].signed_body
    assert attachment(run.body, "ord_100") is None
    assert run.body.claims[0].rationale == ("Orders ord_1001 to ord_1004 were completed; "
                                            f"{WITHHELD} is from September.")
    result = verify_package(run.package, registry, expected_audience="bank_b", now=now)
    assert result.accepted is True and result.errors == []


def test_scan_never_rewrites_an_extracts_own_identity(keystore, registry, now):
    # The withheld id ord_100 is a whole token of the shared id ord_100-b.
    old = signed(keystore, event("ord_100", "order", "2026-09-12T10:00:00Z", status="completed",
                                 amount_pence=1500, currency="GBP"))
    split = signed(keystore, event(
        "ord_100-b", "order", "2026-10-20T10:00:00Z", status="completed", amount_pence=2500,
        currency="GBP", items=item_rows([("Linen shirt, white", "shirts", 1, 2500)])))
    claim = Claim(claim_id="clm_q1", request_id="rfc_b_001", question_id="q1",
                  statement="Documented October sales were 2500 pence.",
                  status=ClaimStatus.SUPPORTED, rationale="One completed order.",
                  evidence_refs=[split.ref()],
                  calculation=sum_terms([split], method="documented_sales"), created_at=NOW)
    run = pipeline(keystore, rfc_b(questions=SALES_ONLY), [claim], [old, split],
                   [parse_rule_text("don't share item names").rule])

    extract = attachment(run.body, "ord_100-b")
    assert extract.form == "redacted_extract"
    assert extract.content["record_id"] == "ord_100-b"
    assert extract.content["issuer_id"] == "threadline"
    result = verify_package(run.package, registry, expected_audience="bank_b", now=now)
    assert result.accepted is True
    assert result.claims[0].calculation_valid is True


def test_supported_claim_is_downgraded_when_its_evidence_is_excluded_by_age(keystore, ev):
    age = AgeRule(rule_id="r_age_30", max_age_days=30)
    run = pipeline(keystore, rfc_b(), claims_b(ev), list(ev.values()), [age])
    body, review = run.body, run.review

    assert attachment(body, "ord_1001") is None  # 2026-10-04 is older than 30 days
    q1, q2 = body.claims
    assert q1.status == ClaimStatus.UNRESOLVED and q1.gap == GAP_WITHHELD
    assert q1.calculation is None
    assert {r.ref_id for r in q1.evidence_refs} == {"ord_1002", "ord_1003", "ord_1004"}
    assert q2.status == ClaimStatus.SUPPORTED
    change = review.claim_changes[0]
    assert (change.question_id, change.from_status, change.to_status) == (
        "q1", ClaimStatus.SUPPORTED, ClaimStatus.UNRESOLVED)
    assert "record:ord_1001@1" in change.reason and "older than the 30-day limit" in change.reason
    excluded = next(e for e in review.excluded if e.ref.ref_id == "ord_1001")
    assert excluded.rule_id == "r_age_30"
    text = body_text(body)
    assert "ord_1001" not in text and "Vintage wool overcoat" not in text
    assert any(g.question_id == "q1" and g.text == GAP_WITHHELD for g in review.gaps)


def test_review_lists_included_excluded_generalised_and_gaps(keystore, ev):
    rule = GeneraliseRule(rule_id="r_categories", fields=["items.description"], level="category")
    request = rfc_b(questions=[
        {"question_id": "q1", "text": "What were your documented marketplace sales in October?"},
        {"question_id": "q2", "text": "What refunds did you issue over the same period?"},
        {"question_id": "q3", "text": "Do you sell on any other platform?"}])
    body, review = build_body(request, claims_b(ev), list(ev.values()), [rule], now=NOW)

    assert {e.ref.ref_id for e in review.included} == {*ORDERS, "rfd_2001"}
    assert all("redacted extract" in e.reason for e in review.included)
    not_needed = {e.ref.ref_id for e in review.excluded if e.reason == REASON_NOT_NEEDED}
    assert not_needed == {"fee_3001", "msg_7001", "po_5001", "stl_88001", "txn_9001"}
    assert {(e.ref.ref_id, e.field, e.rule_id) for e in review.generalised} == {
        (record_id, "items.description", "r_categories") for record_id in [*ORDERS, "rfd_2001"]}
    assert [(g.question_id, g.text) for g in review.gaps] == [("q3", GAP_NO_ANSWER)]
    assert review.claim_changes == []

    order = attachment(body, "ord_1002")
    assert order.generalised_fields == ["items.description"]
    assert [item["category"] for item in order.content["items"]] == ["outerwear", "footwear"]
    assert [c.question_id for c in body.claims] == ["q1", "q2", "q3"]
    assert body.claims[2].status == ClaimStatus.UNRESOLVED
    for description in DESCRIPTIONS:
        assert description not in body_text(body)


def test_build_body_is_deterministic(ev):
    request, claims, evidence = rfc_b(), claims_b(ev), list(ev.values())
    policy = [parse_rule_text("don't share item names").rule]
    first, review = build_body(request, claims, evidence, policy, now=NOW)
    again, _ = build_body(request, claims, evidence, policy, now=NOW)
    shuffled, _ = build_body(request, list(reversed(claims)), list(reversed(evidence)), policy,
                             now=NOW)
    assert canon.canonical_bytes(first) == canon.canonical_bytes(again)
    assert canon.canonical_bytes(first) == canon.canonical_bytes(shuffled)
    keys = [a.ref.key() for a in first.attachments]
    assert keys == sorted(keys)
    assert all(a.attachment_id == canon.make_id("att", "rfc_b_001", a.ref.key())
               for a in first.attachments)
    assert first.expires_at == NOW + timedelta(days=7)


def test_forbidden_recipient_gets_no_attachments_and_unresolved_claims(keystore, ev):
    rule = RecipientRule(rule_id="r_only_a", allow=["bank_a"])
    body, review = build_body(rfc_b(), claims_b(ev), list(ev.values()), [rule], now=NOW)

    assert body.attachments == []
    assert [(c.question_id, c.status, c.gap) for c in body.claims] == [
        ("q1", ClaimStatus.UNRESOLVED, GAP_NOT_SHARED), ("q2", ClaimStatus.UNRESOLVED,
                                                         GAP_NOT_SHARED)]
    assert all(c.evidence_refs == [] and c.calculation is None for c in body.claims)
    assert len(review.excluded) == len(ev)
    assert all(e.rule_id == "r_only_a" and "bank_b is not allowed" in e.reason
               for e in review.excluded)
    assert [c.question_id for c in review.claim_changes] == ["q1", "q2"]
    for description in DESCRIPTIONS:
        assert description not in body_text(body)


def test_request_txn_stays_cited_when_not_attached(keystore, ev, registry, now):
    evidence = [item for key, item in ev.items() if key != "txn_9001"]
    run = pipeline(keystore, rfc_a(), [claim_a(ev)], evidence)

    claim = run.body.claims[0]
    assert attachment(run.body, "txn_9001") is None
    assert claim.status == ClaimStatus.SUPPORTED and claim.calculation is not None
    assert ev["txn_9001"].ref() in claim.evidence_refs  # the bank already holds its own credit
    result = verify_package(run.package, registry, expected_audience="bank_a", now=now)
    assert result.accepted is True
    assert result.claims[0].calculation_valid is None  # the credit amount is not in the package


def test_removed_source_is_reported_and_downgrades_the_claim(ev):
    body, review = build_body(rfc_a(), [claim_a(ev)], list(ev.values()), None, now=NOW,
                              removed=["stl_88001"])
    assert attachment(body, "stl_88001") is None
    assert body.claims[0].status == ClaimStatus.UNRESOLVED
    entry = next(e for e in review.excluded if e.ref.ref_id == "stl_88001")
    assert "removed by the account holder" in entry.reason
    assert "stl_88001" not in body_text(body)
    assert {r.ref_id for r in body.claims[0].evidence_refs} == {"po_5001", "txn_9001"}


def test_make_draft_merges_body_refs_into_source_revisions(control, ev):
    draft = control.draft
    assert draft.status == DraftStatus.PENDING
    assert draft.body_sha256 == canon.digest(control.body)
    keys = {ref.key() for ref in draft.source_revisions}
    assert {a.ref.key() for a in control.body.attachments} <= keys
    assert ev["msg_7001"].ref().key() not in keys


def test_store_roundtrip_keeps_the_package_verifiable(control, tmp_path, registry, now):
    from alibi.core.store import Store

    with Store(tmp_path / "db" / "passport.db") as store:
        assert store.save_package(control.package, approval_id=control.approval.approval_id) \
            == "inserted"
        loaded = store.get_package(control.package.package_id, audience="bank_a")
    assert isinstance(loaded, ContextPackage)
    assert verify_package(loaded, registry, expected_audience="bank_a", now=now).accepted


def test_one_kind_totals_keep_one_sign_and_net_totals_subtract():
    """A refunds total checks as +18000 or in the refund direction, never mixing signs or
    kinds. Only net methods subtract refunds and fees."""
    from alibi.core.package import _calc_check
    from alibi.core.schemas import CalcTerm, Calculation, EvidenceRef

    amounts = {"ord_1001": ("order", 60000), "rfd_2001": ("refund", 18000),
               "rfd_2002": ("refund", 2000), "fee_3001": ("fee", 6000)}
    refs = {rid: EvidenceRef(ref_type="record", ref_id=rid, revision=1) for rid in amounts}
    contents = {refs[rid].key(): {"kind": kind, "amount_pence": pence, "currency": "GBP"}
                for rid, (kind, pence) in amounts.items()}

    def calc(method, *signed_ids):
        terms = [CalcTerm(ref=refs[rid], kind=amounts[rid][0], sign=sign,
                          amount_pence=amounts[rid][1], currency="GBP")
                 for rid, sign in signed_ids]
        return Calculation(method=method, currency="GBP", terms=terms,
                           total_pence=sum(t.signed_pence for t in terms))

    assert _calc_check(calc("refunds_total", ("rfd_2001", 1), ("rfd_2002", 1)), contents) is True
    assert _calc_check(calc("refunds_total", ("rfd_2001", -1), ("rfd_2002", -1)),
                       contents) is True
    # A one-kind total cannot net its records against each other or add another kind.
    assert _calc_check(calc("refunds_total", ("rfd_2001", 1), ("rfd_2002", -1)),
                       contents) is False
    assert _calc_check(calc("refunds_total", ("rfd_2001", 1), ("ord_1001", 1)), contents) is False
    assert _calc_check(calc("net_total", ("ord_1001", 1), ("rfd_2001", -1), ("fee_3001", -1)),
                       contents) is True
    assert _calc_check(calc("net_total", ("ord_1001", 1), ("rfd_2001", 1)), contents) is False
