"""Reconciliation: arithmetic in code, currency and double-count guards, settlement timing."""

import pytest

from alibi.core.canon import digest
from alibi.core.reconcile import (
    BANK_CREDIT,
    BANK_DEBIT,
    FAILED_STATUSES,
    METHOD_NET_PAYOUT,
    PENDING_STATUSES,
    SETTLED_STATUSES,
    SIGN_BY_KIND,
    DoubleCountError,
    MissingAmountError,
    MixedCurrencyError,
    ReconcileError,
    check_settlement_timing,
    compare_to_target,
    credit_supported,
    reconcile_credit,
    settlement_effective_time,
    sum_terms,
    term_for,
)
from alibi.core.schemas import (
    BankTxn,
    Calculation,
    EvidenceRecord,
    MerchantEvent,
    RecordKind,
    SourceTrust,
    parse_utc,
)

BOOKED = "2026-11-03T10:00:00Z"
ORDER_IDS = ["ord_1001", "ord_1002", "ord_1003", "ord_1004"]


# --------------------------------------------------------------------------- fixture builders


def rec(record_id, kind, amount, *, revision=1, currency="GBP",
        occurred_at="2026-10-15T12:00:00Z", **fields) -> EvidenceRecord:
    """A stored merchant record. Reconciliation ignores source trust, so these records are
    self-supplied and need no issuer signature."""
    event = MerchantEvent.model_validate({
        "event_id": f"evt_{record_id}_r{revision}", "record_id": record_id,
        "revision": revision, "issuer_id": "threadline", "subject_ref": "subj_maya",
        "kind": kind, "occurred_at": occurred_at, "amount_pence": amount,
        "currency": None if amount is None else currency, **fields,
    })
    return EvidenceRecord.from_event(event, source_trust=SourceTrust.SELF_SUPPLIED,
                                     connector="self_supplied_import",
                                     received_at="2026-11-03T12:30:00Z",
                                     payload_sha256=digest(event))


def bank_txn(amount=216000, *, txn_id="txn_9001", revision=1, currency="GBP",
             booked_at=BOOKED, refs=("PO-5001", "STL-88001")) -> BankTxn:
    return BankTxn.model_validate({
        "txn_id": txn_id, "revision": revision, "subject_ref": "subj_maya",
        "account_ref": "acc_maya_bank_a", "amount_pence": amount, "currency": currency,
        "booked_at": booked_at, "description": "THREADLINE PAYOUT", "refs": list(refs),
        "provenance": {"connector": "bank_feed", "connection_id": "conn_bank_a_maya",
                       "retrieved_at": "2026-11-03T10:05:00Z", "source_version": "v1"},
    })


def components() -> list[EvidenceRecord]:
    """Canonical October sales, refund and fee: 240000 - 18000 - 6000 = 216000."""
    orders = [
        rec(record_id, "order", amount, occurred_at=at, status="completed")
        for record_id, amount, at in [
            ("ord_1001", 60000, "2026-10-04T10:00:00Z"),
            ("ord_1002", 72000, "2026-10-09T15:30:00Z"),
            ("ord_1003", 48000, "2026-10-14T09:15:00Z"),
            ("ord_1004", 60000, "2026-10-22T18:45:00Z"),
        ]
    ]
    refund = rec("rfd_2001", "refund", 18000, occurred_at="2026-10-25T11:00:00Z",
                 status="completed", related_record_ids=["ord_1003"])
    fee = rec("fee_3001", "fee", 6000, occurred_at="2026-10-31T23:00:00Z", status="completed")
    return [*orders, refund, fee]


def payout(amount=216000, currency="GBP", *, record_id="po_5001",
           payment_ref="PO-5001") -> EvidenceRecord:
    return rec(record_id, "payout", amount, currency=currency,
               occurred_at="2026-11-02T09:00:00Z", payment_ref=payment_ref, status="initiated",
               related_record_ids=[*ORDER_IDS, "rfd_2001", "fee_3001"])


def settlement(status="settled", *, effective_at="2026-11-03T07:45:00Z",
               occurred_at="2026-11-03T08:00:00Z", revision=1, amount=216000,
               record_id="stl_88001", payment_ref="PO-5001", settlement_ref="STL-88001",
               **fields) -> EvidenceRecord:
    """stl_88001. Defaults to the `supported` variant: settled, effective before booking."""
    if effective_at is not None:
        fields["effective_at"] = effective_at
    return rec(record_id, "settlement", amount, revision=revision, occurred_at=occurred_at,
               payment_ref=payment_ref, settlement_ref=settlement_ref, status=status, **fields)


# --------------------------------------------------------------------------- arithmetic


def test_canonical_credit_reconciles_and_cites_every_source():
    comps, po, stl, credit = components(), payout(), settlement(), bank_txn()
    calc = reconcile_credit(credit, components=comps, payout=po, settlement=stl)

    assert sum(t.signed_pence for t in calc.terms if t.kind == "order") == 240000
    assert [t.signed_pence for t in calc.terms] == [60000, 72000, 48000, 60000, -18000, -6000]
    assert calc.total_pence == 240000 - 18000 - 6000 == 216000
    assert calc.method == METHOD_NET_PAYOUT and calc.currency == "GBP"
    assert calc.target_ref == credit.ref() and calc.target_pence == 216000
    assert calc.difference_pence == 0 and calc.balanced is True
    assert calc.cited_refs == [c.ref() for c in comps] + [po.ref(), stl.ref(), credit.ref()]
    assert calc.timing is not None and calc.timing.ok is True
    assert credit_supported(calc)
    assert "216000" not in calc.timing.reason  # reasons carry no amounts
    # The structured result survives a JSON round trip; the schema re-checks the sums.
    assert Calculation.model_validate(calc.to_json_dict()) == calc


@pytest.mark.parametrize("second_revision", [1, 2])
def test_same_record_cited_twice_is_double_counting(second_revision):
    first = rec("rfd_2001", "refund", 18000)
    again = rec("rfd_2001", "refund", 18000, revision=second_revision)
    with pytest.raises(DoubleCountError):
        sum_terms([first, again], method="refunds")
    with pytest.raises(DoubleCountError):
        reconcile_credit(bank_txn(), components=[*components(), again])


def test_same_txn_cited_twice_is_double_counting():
    with pytest.raises(DoubleCountError):
        sum_terms([bank_txn(), bank_txn(revision=2)], method="credits")


def test_mixed_currencies_are_rejected():
    gbp = rec("ord_1001", "order", 60000)
    eur = rec("ord_1002", "order", 72000, currency="EUR")
    with pytest.raises(MixedCurrencyError):
        sum_terms([gbp, eur], method="sales")
    with pytest.raises(MixedCurrencyError):
        sum_terms([gbp], method="sales", currency="EUR")
    with pytest.raises(MixedCurrencyError):  # components against a credit in another currency
        reconcile_credit(bank_txn(currency="EUR"), components=components())
    with pytest.raises(MixedCurrencyError):
        compare_to_target(sum_terms([gbp], method="sales"), bank_txn(60000, currency="EUR"))
    with pytest.raises(MixedCurrencyError):  # a payout in another currency
        reconcile_credit(bank_txn(), components=components(), payout=payout(currency="EUR"))


def test_missing_amount_is_rejected():
    message = rec("msg_7001", "message", None, text="Funds released to your seller balance.")
    with pytest.raises(MissingAmountError):
        term_for(message, sign=1)
    with pytest.raises(MissingAmountError):
        sum_terms([message], method="x")
    with pytest.raises(MissingAmountError):
        sum_terms([message], method="x", signs={RecordKind.MESSAGE: 1})
    with pytest.raises(MissingAmountError):
        compare_to_target(sum_terms(components(), method="x"), message)


def test_terms_take_direction_from_kind_and_bank_sign():
    credit_term = term_for(bank_txn())
    assert (credit_term.kind, credit_term.sign, credit_term.amount_pence) == (BANK_CREDIT, 1, 216000)
    assert credit_term.ref == bank_txn().ref()
    debit_term = term_for(bank_txn(-4599, txn_id="txn_9100"))
    assert (debit_term.kind, debit_term.sign, debit_term.amount_pence) == (BANK_DEBIT, -1, 4599)
    assert debit_term.signed_pence == -4599
    assert term_for(rec("ord_1001", "order", 60000)).signed_pence == 60000
    assert term_for(rec("rfd_2001", "refund", 18000)).signed_pence == -18000
    assert term_for(rec("fee_3001", "fee", 6000)).signed_pence == -6000
    assert term_for(payout(), sign=1).signed_pence == 216000
    with pytest.raises(ReconcileError):  # payouts have no default sign
        term_for(payout())
    with pytest.raises(ReconcileError):  # a bank amount keeps its own sign
        term_for(bank_txn(), sign=-1)
    with pytest.raises(ReconcileError):
        term_for(rec("ord_1001", "order", 60000), sign=2)
    with pytest.raises(ReconcileError):
        term_for({"amount_pence": 100})


def test_debit_target_compares_by_signed_amount():
    receipt = rec("rcp_4001", "receipt", 4599, status="paid")
    debit = bank_txn(-4599, txn_id="txn_9100", refs=())
    purchase = compare_to_target(sum_terms([receipt], method="purchase"), debit)
    assert purchase.total_pence == -4599 and purchase.balanced is True  # default: money out
    as_income = compare_to_target(
        sum_terms([receipt], method="receipt", signs={RecordKind.RECEIPT: 1}), debit)
    assert as_income.target_pence == -4599 and as_income.difference_pence == 9198
    assert as_income.balanced is False


def test_compare_to_target_guards():
    calc = sum_terms(components(), method="net")
    targeted = compare_to_target(calc, bank_txn())
    assert targeted.cited_refs[-1] == bank_txn().ref()
    assert calc.target_ref is None  # the input calculation is not modified
    with pytest.raises(ReconcileError):  # already has a target
        compare_to_target(targeted, payout())
    credits = sum_terms([bank_txn()], method="credits")
    with pytest.raises(DoubleCountError):  # the target is already a term
        compare_to_target(credits, bank_txn(revision=2))


def test_sum_terms_custom_signs_and_empty_sums():
    comps = components()
    sales = sum_terms([c for c in comps if c.kind == RecordKind.ORDER], method="sales")
    refunds = sum_terms([c for c in comps if c.kind == RecordKind.REFUND], method="refunds",
                        signs={RecordKind.REFUND: 1})
    assert (sales.total_pence, refunds.total_pence) == (240000, 18000)
    assert sales.cited_refs == [c.ref() for c in comps[:4]]
    empty = sum_terms([], method="sales", currency="GBP")
    assert empty.total_pence == 0 and empty.terms == []
    with pytest.raises(ReconcileError):  # no items and no currency
        sum_terms([], method="sales")
    with pytest.raises(ReconcileError):  # a kind with no sign cannot be summed
        sum_terms([payout()], method="x")


def test_default_signs_and_errors():
    assert SIGN_BY_KIND == {RecordKind.ORDER: 1, RecordKind.RECEIPT: -1,
                            RecordKind.REFUND: -1, RecordKind.FEE: -1}
    with pytest.raises(TypeError):  # read-only: it is a default argument
        SIGN_BY_KIND[RecordKind.PAYOUT] = 1
    for error in (MixedCurrencyError, DoubleCountError, MissingAmountError):
        assert issubclass(error, ReconcileError)
    assert issubclass(ReconcileError, ValueError)


def test_sums_beyond_the_canonical_json_range_are_rejected():
    # Each amount canonicalises on its own, but canon cannot hash a result past 2**53 - 1,
    # so the error comes here, typed, instead of later from the store or the signer.
    top = 2**53 - 1
    orders = [rec("ord_a", "order", top), rec("ord_b", "order", top)]
    assert digest(sum_terms(orders[:1], method="sales"))  # the boundary itself is fine
    with pytest.raises(ReconcileError, match="out of range"):
        sum_terms(orders, method="sales")
    with pytest.raises(ReconcileError, match="out of range"):  # negative totals too
        sum_terms([rec("rfd_a", "refund", top), rec("rfd_b", "refund", top)], method="refunds")
    with pytest.raises(ReconcileError, match="out of range"):  # a safe total and target
        compare_to_target(sum_terms(orders[:1], method="x"), bank_txn(-top, txn_id="txn_big"))
    with pytest.raises(ReconcileError, match="out of range"):
        reconcile_credit(bank_txn(), components=orders)


# --------------------------------------------------------------------------- settlement timing


def test_status_vocabulary_is_exact():
    assert SETTLED_STATUSES == {"settled", "paid", "completed", "succeeded"}
    assert PENDING_STATUSES == {"pending", "processing", "in_transit", "initiated", "scheduled"}
    assert FAILED_STATUSES == {"failed", "reversed", "returned", "cancelled"}
    assert SETTLED_STATUSES.isdisjoint(PENDING_STATUSES | FAILED_STATUSES)
    assert PENDING_STATUSES.isdisjoint(FAILED_STATUSES)


def test_settled_before_booking_passes():
    stl, credit = settlement(), bank_txn()
    timing = check_settlement_timing(stl, credit)
    assert timing.ok is True
    assert timing.settlement_effective_at == parse_utc("2026-11-03T07:45:00Z")
    assert timing.settlement_ref == stl.ref() and timing.credit_ref == credit.ref()
    assert timing.booked_at == parse_utc(BOOKED)


@pytest.mark.parametrize("status", sorted(SETTLED_STATUSES))
def test_every_settled_status_counts(status):
    assert check_settlement_timing(settlement(status), bank_txn()).ok


def test_effective_at_wins_over_a_later_observation():
    # Retrieved by follow-up lookup after the booking, but effective before it.
    looked_up = settlement(occurred_at="2026-11-04T09:00:00Z")
    assert check_settlement_timing(looked_up, bank_txn()).ok


def test_equal_times_count_as_on_time():
    assert check_settlement_timing(settlement(effective_at=BOOKED), bank_txn()).ok
    assert check_settlement_timing(settlement(effective_at=None, occurred_at=BOOKED),
                                   bank_txn()).ok


def test_settlement_effective_after_booking_fails_timing():
    late = settlement(effective_at="2026-11-03T10:00:01Z")
    timing = check_settlement_timing(late, bank_txn())
    assert timing.ok is False
    assert timing.settlement_effective_at == parse_utc("2026-11-03T10:00:01Z")
    assert "after the credit was booked" in timing.reason
    calc = reconcile_credit(bank_txn(), components=components(), payout=payout(), settlement=late)
    assert calc.balanced is True  # the amounts still reconcile...
    assert not credit_supported(calc)  # ...but a later settlement cannot explain the credit


def test_settled_without_effective_at_uses_occurred_at():
    early = settlement(effective_at=None, occurred_at="2026-11-03T09:30:00Z")
    assert settlement_effective_time(early) == early.occurred_at
    timing = check_settlement_timing(early, bank_txn())
    assert timing.ok is True and timing.settlement_effective_at == early.occurred_at
    late = settlement(effective_at=None, occurred_at="2026-11-03T11:00:00Z")
    assert settlement_effective_time(late) == late.occurred_at
    assert check_settlement_timing(late, bank_txn()).ok is False


@pytest.mark.parametrize("observed", ["2026-11-03T12:00:00Z", BOOKED])
def test_pending_observed_at_or_after_booking_was_not_settled_at_booking(observed):
    # The `contradiction` variant: no platform refs on the credit, pending seen after booking.
    pending = settlement("pending", effective_at=None, occurred_at=observed)
    timing = check_settlement_timing(pending, bank_txn(refs=()))
    assert timing.ok is False
    assert "not settled at booking" in timing.reason
    assert timing.settlement_status == "pending"
    assert timing.settlement_effective_at is None
    assert timing.status_observed_at == parse_utc(observed)


@pytest.mark.parametrize("status", sorted(PENDING_STATUSES))
def test_pending_observed_before_booking_also_fails(status):
    pending = settlement(status, effective_at=None, occurred_at="2026-11-03T09:00:00Z")
    timing = check_settlement_timing(pending, bank_txn())
    assert timing.ok is False
    assert "no evidence it settled in time" in timing.reason
    assert "not settled at booking" not in timing.reason


def test_pending_settlement_effective_at_is_ignored():
    pending = settlement("pending", effective_at="2026-11-03T07:45:00Z")
    assert settlement_effective_time(pending) is None
    timing = check_settlement_timing(pending, bank_txn())
    assert timing.ok is False and timing.settlement_effective_at is None


def test_released_to_balance_is_not_settled():
    assert "released_to_balance" not in SETTLED_STATUSES | PENDING_STATUSES | FAILED_STATUSES
    released = settlement("released_to_balance")  # effective_at is before booking
    assert settlement_effective_time(released) is None
    timing = check_settlement_timing(released, bank_txn())
    assert timing.ok is False and timing.settlement_effective_at is None
    assert "not a settlement status" in timing.reason
    calc = reconcile_credit(bank_txn(), components=components(), payout=payout(),
                            settlement=released)
    assert calc.balanced is True and not credit_supported(calc)


@pytest.mark.parametrize("status", [*sorted(FAILED_STATUSES), None])
def test_failed_or_missing_status_fails_timing(status):
    timing = check_settlement_timing(settlement(status), bank_txn())
    assert timing.ok is False and timing.settlement_effective_at is None


def test_only_settlement_records_can_show_settlement():
    order = components()[0]
    assert order.status in SETTLED_STATUSES  # "completed", but an order is not a settlement
    assert settlement_effective_time(order) is None
    timing = check_settlement_timing(order, bank_txn())
    assert timing.ok is False and "not a settlement record" in timing.reason


def test_timing_needs_a_bank_credit():
    with pytest.raises(ReconcileError):
        check_settlement_timing(settlement(), bank_txn(-216000))


# --------------------------------------------------------------------------- credit reconciliation


def test_payout_amount_that_disagrees_with_components_is_not_balanced():
    po, credit = payout(amount=200000), bank_txn()
    calc = reconcile_credit(credit, components=components(), payout=po, settlement=settlement())
    assert calc.total_pence == 216000
    assert calc.balanced is False
    assert calc.target_ref == po.ref() and calc.target_pence == 200000
    assert calc.difference_pence == 16000
    assert credit.ref() in calc.cited_refs and po.ref() in calc.cited_refs
    assert not credit_supported(calc)


def test_components_that_do_not_add_up_are_not_balanced():
    credit = bank_txn()
    calc = reconcile_credit(credit, components=components()[:-1], payout=payout(),
                            settlement=settlement())
    assert calc.total_pence == 222000
    assert calc.target_ref == credit.ref() and calc.difference_pence == 6000
    assert calc.balanced is False
    assert calc.timing.ok is True and not credit_supported(calc)


def test_credit_supported_needs_balance_and_ok_timing():
    comps, po, credit = components(), payout(), bank_txn()
    assert credit_supported(reconcile_credit(credit, components=comps, payout=po,
                                             settlement=settlement()))

    no_settlement = reconcile_credit(credit, components=comps, payout=po)
    assert no_settlement.balanced is True and no_settlement.timing is None
    assert not credit_supported(no_settlement)

    late = reconcile_credit(credit, components=comps, payout=po,
                            settlement=settlement(effective_at="2026-11-03T11:00:00Z"))
    assert late.balanced is True and late.timing.ok is False
    assert not credit_supported(late)

    unbalanced = reconcile_credit(credit, components=comps[:-1], settlement=settlement())
    assert unbalanced.timing.ok is True and unbalanced.balanced is False
    assert not credit_supported(unbalanced)

    # Timing checked against a different credit does not support this one.
    other = check_settlement_timing(settlement(), bank_txn(txn_id="txn_9002"))
    mismatched = Calculation.model_validate({**no_settlement.to_json_dict(),
                                             "timing": other.to_json_dict()})
    assert not credit_supported(mismatched)


def test_signed_pending_correction_withdraws_support():
    # The `pending_correction` variant: revision 2 of stl_88001 says it was still pending.
    comps, po, credit = components(), payout(), bank_txn()
    first = settlement()
    correction = settlement("pending", revision=2, effective_at=None,
                            occurred_at="2026-11-04T09:00:00Z", corrects_revision=1,
                            correction_reason="Transfer had not settled.")
    assert credit_supported(reconcile_credit(credit, components=comps, payout=po,
                                             settlement=first))
    calc = reconcile_credit(credit, components=comps, payout=po, settlement=correction)
    assert calc.balanced is True and not credit_supported(calc)
    assert calc.timing.settlement_ref.revision == 2
    assert "not settled at booking" in calc.timing.reason
    assert correction.ref() in calc.cited_refs and first.ref() not in calc.cited_refs


def test_reconcile_credit_input_guards():
    comps = components()
    with pytest.raises(ReconcileError):  # a debit is not a credit
        reconcile_credit(bank_txn(-216000), components=comps)
    with pytest.raises(ReconcileError):  # the payout slot needs a payout record
        reconcile_credit(bank_txn(), components=comps, payout=settlement())
    with pytest.raises(ReconcileError):  # bank transactions are not components
        reconcile_credit(bank_txn(), components=[*comps, bank_txn(txn_id="txn_9002")])
    with pytest.raises(ReconcileError):  # a balance-release message is not a component
        message = rec("msg_7001", "message", 216000, text="Released to your seller balance.")
        reconcile_credit(bank_txn(), components=[*comps, message])


def test_receipts_are_not_payout_components():
    # A receipt records money spent (the retail purchase model), so it cannot stand in for
    # a sale in a payout breakdown.
    receipt = rec("rcp_4001", "receipt", 60000, status="paid")
    comps = [c for c in components() if c.record_id != "ord_1004"] + [receipt]
    with pytest.raises(ReconcileError, match="orders, refunds, fees"):
        reconcile_credit(bank_txn(), components=comps, payout=payout(), settlement=settlement())


# --------------------------------------------------------------------------- settlement evidence


@pytest.mark.parametrize("with_payout", [True, False])
def test_settlement_in_another_currency_is_rejected(with_payout):
    # Alibi never converts, so a EUR settlement cannot be cited in support of a GBP credit.
    with pytest.raises(MixedCurrencyError):
        reconcile_credit(bank_txn(), components=components(),
                         payout=payout() if with_payout else None,
                         settlement=settlement(currency="EUR"))


@pytest.mark.parametrize("with_payout", [True, False])
def test_settlement_amount_that_disagrees_is_not_supported(with_payout):
    credit, stl = bank_txn(), settlement(amount=1)
    calc = reconcile_credit(credit, components=components(),
                            payout=payout() if with_payout else None, settlement=stl)
    assert calc.timing.ok is True  # settled in time, but not for this credit's amount
    assert calc.total_pence == 216000 and calc.balanced is False
    assert calc.target_ref == stl.ref() and calc.target_pence == 1
    assert calc.difference_pence == 215999
    assert stl.ref() in calc.cited_refs and credit.ref() in calc.cited_refs
    assert not credit_supported(calc)


def test_settlement_for_another_payout_is_rejected():
    # The `ambiguous` variant: two payouts, each settled before booking by its own settlement.
    credit = bank_txn(refs=())
    po_1, po_2 = payout(), payout(record_id="po_5002", payment_ref="PO-5002")
    stl_1 = settlement()
    stl_2 = settlement(record_id="stl_88002", payment_ref="PO-5002", settlement_ref="STL-88002",
                       effective_at="2026-11-03T08:30:00Z", occurred_at="2026-11-03T08:45:00Z")
    for po, stl in [(po_1, stl_1), (po_2, stl_2)]:
        assert credit_supported(reconcile_credit(credit, components=components(), payout=po,
                                                 settlement=stl))
    for po, stl in [(po_1, stl_2), (po_2, stl_1)]:  # cross-paired
        with pytest.raises(ReconcileError, match="payment reference"):
            reconcile_credit(credit, components=components(), payout=po, settlement=stl)
    with pytest.raises(ReconcileError, match="payment reference"):  # nothing ties it
        reconcile_credit(credit, components=components(), payout=po_1,
                         settlement=settlement(payment_ref=None))


def test_other_records_in_the_settlement_slot_only_fail_timing():
    # The money and reference checks apply to settlement records; anything else (here the
    # balance-release message) cannot show settlement, so it fails timing with its reason.
    message = rec("msg_7001", "message", 216000, status="released_to_balance",
                  text="Funds released to your seller balance.")
    calc = reconcile_credit(bank_txn(), components=components(), payout=payout(),
                            settlement=message)
    assert calc.balanced is True and calc.timing.ok is False
    assert "not a settlement record" in calc.timing.reason
    assert not credit_supported(calc)


def test_credit_supported_rederives_the_verdict_from_explicit_fields():
    comps, po, credit = components(), payout(), bank_txn()
    honest = reconcile_credit(credit, components=comps, payout=po, settlement=settlement())
    assert credit_supported(Calculation.model_validate(honest.to_json_dict()))

    def flipped(calc: Calculation) -> Calculation:
        data = calc.to_json_dict()
        data["timing"]["ok"] = True
        return Calculation.model_validate(data)  # the schema accepts the flag as given

    pending = reconcile_credit(credit, components=comps, payout=po,
                               settlement=settlement("pending", effective_at=None,
                                                     occurred_at="2026-11-03T12:00:00Z"))
    late = reconcile_credit(credit, components=comps, payout=po,
                            settlement=settlement(effective_at="2026-11-03T11:00:00Z"))
    for calc in (pending, late):
        forged = flipped(calc)
        assert calc.timing.ok is False and forged.timing.ok is True
        assert not credit_supported(forged)

    unbalanced = reconcile_credit(credit, components=comps[:-1], payout=po,
                                  settlement=settlement())
    forced = unbalanced.model_copy(update={"balanced": True})  # model_copy skips validation
    assert forced.difference_pence == 6000 and not credit_supported(forced)


def test_allowed_signs_net_only_under_net_methods():
    from alibi.core.reconcile import METHOD_NET_PAYOUT, METHOD_NET_TOTAL, allowed_signs
    assert allowed_signs(METHOD_NET_PAYOUT, "refund") == {-1}
    assert allowed_signs(METHOD_NET_TOTAL, "fee") == {-1}
    assert allowed_signs(METHOD_NET_TOTAL, "order") == {1}
    # A one-kind total may show refunds of 18000 as +18000 or in the refund direction.
    assert allowed_signs("refunds_total", "refund") == {1, -1}
    assert allowed_signs("sales_total", "order") == {1}
