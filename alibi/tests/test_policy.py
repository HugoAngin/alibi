"""Disclosure policy: allowlist, deny/generalise/age/recipient rules and rule-text parsing."""

import json
from datetime import datetime, timedelta

import pytest

from alibi.core import canon
from alibi.core.policy import (
    FULL_RECORD_META_FIELDS,
    REASON_CATEGORY_WITHHELD,
    REASON_DENIED,
    REASON_EXCLUDED_REF,
    REASON_NO_CATEGORY,
    REASON_REDACTED,
    RECORD_FIELDS,
    TXN_FIELDS,
    CompiledPolicy,
    PolicyError,
    compile_policy,
    parse_rule_text,
)
from alibi.core.schemas import (
    AgeRule,
    BankTxn,
    DenyRule,
    EvidenceRecord,
    GeneraliseRule,
    MerchantEvent,
    Policy,
    RecipientRule,
    RecordKind,
    SigningDomain,
    SourceTrust,
)
from alibi.core.signing import verify

RECEIVED = "2026-11-03T12:00:00Z"
JACKET = "Vintage denim jacket"
MESSAGE_TEXT = "Your payout of 2160.00 GBP has been released to your seller balance."
ITEM_PATHS = {"items.description", "items.category", "items.quantity", "items.unit_amount_pence"}


def event(**over) -> MerchantEvent:
    data = {
        "event_id": "evt_ord_1001", "record_id": "ord_1001", "revision": 1,
        "issuer_id": "threadline", "subject_ref": "subj_maya", "kind": "order",
        "occurred_at": "2026-10-05T14:22:00Z", "amount_pence": 60000, "currency": "GBP",
        "status": "completed",
        "items": [{"description": JACKET, "category": "Outerwear", "quantity": 1,
                   "unit_amount_pence": 60000}],
    }
    data.update(over)
    return MerchantEvent.model_validate(data)


def signed(keystore, ev: MerchantEvent) -> EvidenceRecord:
    """An issuer-verified record carrying a real merchant_demo signature over its event body."""
    body = ev.to_json_dict()
    sig = keystore.sign(body, kid="merchant_demo", domain=SigningDomain.RECORD)
    return EvidenceRecord.from_event(
        ev, source_trust=SourceTrust.ISSUER_VERIFIED, connector="merchant_webhook",
        received_at=RECEIVED,
        payload_sha256=canon.digest({"event": body, "signature": sig.to_json_dict()}),
        issuer_signature=sig, signed_body=body)


def self_supplied(ev: MerchantEvent) -> EvidenceRecord:
    return EvidenceRecord.from_event(
        ev, source_trust=SourceTrust.SELF_SUPPLIED, connector="self_supplied_import",
        received_at=RECEIVED, payload_sha256=canon.digest(ev.to_json_dict()))


def bank_txn(**over) -> BankTxn:
    data = {
        "txn_id": "txn_9001", "revision": 1, "subject_ref": "subj_maya",
        "account_ref": "acc_maya_bank_a", "amount_pence": 216000, "currency": "GBP",
        "booked_at": "2026-11-03T10:00:00Z", "description": "THREADLINE PO-5001",
        "refs": ["PO-5001", "STL-88001"],
        "provenance": {"connector": "bank_feed", "connection_id": "conn_bank_a_maya",
                       "retrieved_at": "2026-11-03T11:00:00Z", "source_version": "feed-v1"},
    }
    data.update(over)
    return BankTxn.model_validate(data)


def deny(rule_id="r_deny", **kw):
    return DenyRule(rule_id=rule_id, **kw)


def generalise(rule_id="r_gen", **kw):
    return GeneraliseRule(rule_id=rule_id, **kw)


def age(days, rule_id="r_age", **kw):
    return AgeRule(rule_id=rule_id, max_age_days=days, **kw)


def recipient(allow, rule_id="r_recipient", **kw):
    return RecipientRule(rule_id=rule_id, allow=allow, **kw)


def names(entries):
    return {entry[0] for entry in entries}


@pytest.fixture
def order(keystore):
    return signed(keystore, event())


@pytest.fixture
def refund(keystore):
    return signed(keystore, event(
        event_id="evt_rfd_2001", record_id="rfd_2001", kind="refund", amount_pence=18000,
        occurred_at="2026-10-20T09:00:00Z", items=None, related_record_ids=["ord_1003"]))


@pytest.fixture
def message(keystore):
    return signed(keystore, event(
        event_id="evt_msg_7001", record_id="msg_7001", kind="message", amount_pence=216000,
        occurred_at="2026-11-02T09:00:00Z", status=None, items=None, text=MESSAGE_TEXT))


@pytest.fixture
def settlement(keystore):
    return signed(keystore, event(
        event_id="evt_stl_88001", record_id="stl_88001", kind="settlement",
        amount_pence=216000, occurred_at="2026-11-03T08:00:00Z",
        effective_at="2026-11-03T07:45:00Z", status="settled", payment_ref="PO-5001",
        settlement_ref="STL-88001", items=None))


# --------------------------------------------------------------------------- allowlist

PRIVATE_FIELD_POLICIES = {
    "none": None,
    "deny_item_names": [deny(fields=["items.description"])],
    "month_dates": [generalise(fields=["occurred_at", "effective_at", "booked_at"],
                               level="month")],
    "item_categories": [generalise(fields=["items.description"], level="category")],
    "deny_private_names": [deny(fields=["text", "subject_ref", "account_ref", "provenance"])],
    "redact_private_names": [generalise(fields=["text", "provenance", "signed_body"],
                                        level="redact")],
}


@pytest.mark.parametrize("rules", PRIVATE_FIELD_POLICIES.values(), ids=PRIVATE_FIELD_POLICIES)
def test_private_fields_never_appear_in_extracts(rules, order, message, now):
    policy = compile_policy(rules)
    top_level = {path.split(".")[0] for path in RECORD_FIELDS}
    for record in (order, message):
        result = policy.filter_record(record, audience="bank_a", now=now)
        assert result.included and result.ref == record.ref()
        assert set(result.fields) <= top_level
        for item in result.fields.get("items", []):
            assert {f"items.{key}" for key in item} <= RECORD_FIELDS
        for name in ("text", "subject_ref", "event_id", "schema_version", "issuer_signature",
                     "signed_body", "payload_sha256", "connector", "received_at"):
            assert name not in result.fields
        dumped = json.dumps(result.fields)
        for secret in ("subj_maya", MESSAGE_TEXT, record.issuer_signature.sig,
                       record.payload_sha256, record.event_id, "merchant_webhook"):
            assert secret not in dumped
    assert "text" in names(policy.filter_record(message, audience="bank_a", now=now).withheld)

    txn = policy.filter_txn(bank_txn(), audience="bank_a", now=now)
    assert txn.included and set(txn.fields) <= TXN_FIELDS
    assert {"account_ref", "provenance"} <= names(txn.withheld)
    dumped = json.dumps(txn.fields)
    for secret in ("acc_maya_bank_a", "conn_bank_a_maya", "bank_feed", "subj_maya"):
        assert secret not in dumped
    assert not txn.full_record_ok


# --------------------------------------------------------------------------- deny


@pytest.mark.parametrize("reverse", [False, True], ids=["generalise_first", "deny_first"])
def test_deny_beats_generalise_on_the_same_field(reverse, order, now):
    rules = [
        generalise("r_names", fields=["items.description"], level="category"),
        generalise("r_month", fields=["occurred_at"], level="month"),
        deny("r_deny", fields=["items.description", "occurred_at"]),
    ]
    if reverse:
        rules.reverse()
    result = compile_policy(rules).filter_record(order, audience="bank_a", now=now)
    assert result.included
    assert ("items.description", REASON_DENIED, "r_deny") in result.withheld
    assert ("occurred_at", REASON_DENIED, "r_deny") in result.withheld
    assert result.generalised == []
    assert "occurred_at" not in result.fields
    assert result.fields["items"] == [
        {"category": "Outerwear", "quantity": 1, "unit_amount_pence": 60000}]
    assert JACKET not in json.dumps(result.fields)
    assert not result.full_record_ok


def test_denied_kind_excludes_the_whole_record_even_when_other_rules_allow_it(
        refund, order, now):
    policy = compile_policy([
        recipient(["bank_a", "bank_b"]),
        generalise(fields=["occurred_at"], level="month", kinds=[RecordKind.REFUND]),
        age(365),
        deny("r_no_refunds", kinds=[RecordKind.REFUND]),
    ])
    result = policy.filter_record(refund, audience="bank_b", now=now)
    assert not result.included
    assert result.excluded_rule == "r_no_refunds"
    assert "refund" in result.excluded_reason
    assert result.fields == {} and result.withheld == [] and result.generalised == []
    assert not result.full_record_ok
    assert policy.filter_record(order, audience="bank_b", now=now).included
    # Bank transactions have no kind, so kind-scoped rules never reach them.
    assert policy.filter_txn(bank_txn(), audience="bank_b", now=now).included


def test_field_rules_follow_kind_scope_and_match_txn_fields_by_name(keystore, order, now):
    payout = signed(keystore, event(
        event_id="evt_po_5001", record_id="po_5001", kind="payout", amount_pence=216000,
        occurred_at="2026-11-02T09:00:00Z", payment_ref="PO-5001", status="initiated",
        items=None))
    settled = signed(keystore, event(
        event_id="evt_stl_88001", record_id="stl_88001", kind="settlement",
        amount_pence=216000, payment_ref="PO-5001", items=None))
    policy = compile_policy([
        deny("r_payout_refs", fields=["payment_ref"], kinds=[RecordKind.PAYOUT]),
        deny("r_descriptions", fields=["description"]),
        generalise("r_order_month", fields=["occurred_at", "booked_at"], level="month",
                   kinds=[RecordKind.ORDER]),
    ])
    paid = policy.filter_record(payout, audience="bank_a", now=now)
    assert "payment_ref" not in paid.fields
    assert ("payment_ref", REASON_DENIED, "r_payout_refs") in paid.withheld
    assert policy.filter_record(settled, audience="bank_a", now=now).fields["payment_ref"] == "PO-5001"

    sold = policy.filter_record(order, audience="bank_a", now=now)
    assert sold.fields["occurred_at"] == "2026-10"
    # "description" names the transaction field; it does not reach item names.
    assert sold.fields["items"][0]["description"] == JACKET

    txn = policy.filter_txn(bank_txn(), audience="bank_a", now=now)
    assert "description" not in txn.fields
    assert ("description", REASON_DENIED, "r_descriptions") in txn.withheld
    assert txn.fields["booked_at"] == "2026-11-03T10:00:00Z"  # kind-scoped rule skips txns
    assert txn.fields["refs"] == ["PO-5001", "STL-88001"]


# --------------------------------------------------------------------------- age


def test_age_rule_keeps_the_boundary_and_excludes_strictly_older(keystore, now):
    """A source dated exactly max_age_days before now is kept; one second older is excluded."""
    cutoff = now - timedelta(days=90)
    policy = compile_policy([age(90)])
    at_cutoff = signed(keystore, event(occurred_at=cutoff))
    just_older = signed(keystore, event(occurred_at=cutoff - timedelta(seconds=1)))
    recent = signed(keystore, event(occurred_at=now - timedelta(days=1)))

    assert policy.filter_record(at_cutoff, audience="bank_a", now=now).included
    kept = policy.filter_record(recent, audience="bank_a", now=now)
    assert kept.included and kept.full_record_ok  # a passing age rule changes nothing
    excluded = policy.filter_record(just_older, audience="bank_a", now=now)
    assert not excluded.included and excluded.excluded_rule == "r_age"
    assert "90-day" in excluded.excluded_reason

    # Bank transactions are aged by booked_at, with the same boundary.
    assert policy.filter_txn(bank_txn(booked_at=cutoff), audience="bank_a", now=now).included
    old_txn = policy.filter_txn(bank_txn(booked_at=cutoff - timedelta(seconds=1)),
                                audience="bank_a", now=now)
    assert not old_txn.included and old_txn.excluded_rule == "r_age"


def test_kind_scoped_age_rule_skips_other_kinds_and_txns(keystore, now):
    old = now - timedelta(days=200)
    policy = compile_policy([age(30, kinds=[RecordKind.REFUND])])
    old_refund = signed(keystore, event(event_id="evt_rfd_1", record_id="rfd_1", kind="refund",
                                        occurred_at=old, items=None))
    assert not policy.filter_record(old_refund, audience="bank_a", now=now).included
    assert policy.filter_record(signed(keystore, event(occurred_at=old)),
                                audience="bank_a", now=now).included
    assert policy.filter_txn(bank_txn(booked_at=old), audience="bank_a", now=now).included


def test_missing_timestamp_under_an_age_rule_is_excluded_never_passed(order, now):
    policy = compile_policy([age(90)])
    undated = order.model_copy(update={"occurred_at": None})
    result = policy.filter_record(undated, audience="bank_a", now=now)
    assert not result.included and result.excluded_rule == "r_age"
    assert "timestamp" in result.excluded_reason
    assert result.fields == {} and not result.full_record_ok

    txn = policy.filter_txn(bank_txn().model_copy(update={"booked_at": None}),
                            audience="bank_a", now=now)
    assert not txn.included and txn.excluded_rule == "r_age"

    naive = order.model_copy(update={"occurred_at": datetime(2026, 11, 1, 9, 0)})
    assert not policy.filter_record(naive, audience="bank_a", now=now).included


# --------------------------------------------------------------------------- recipient


def test_recipient_rule_excludes_other_audiences(order, refund, now):
    policy = compile_policy([recipient(["bank_a"])])
    assert policy.recipient_allowed("bank_a") == (True, None)
    assert policy.recipient_allowed("bank_b") == (False, "r_recipient")
    assert policy.recipient_allowed("bank_b", RecordKind.ORDER) == (False, "r_recipient")
    for source in (order, refund):
        blocked = policy.filter_record(source, audience="bank_b", now=now)
        assert not blocked.included and blocked.excluded_rule == "r_recipient"
        assert blocked.fields == {} and not blocked.full_record_ok
        assert policy.filter_record(source, audience="bank_a", now=now).included
    assert not policy.filter_txn(bank_txn(), audience="bank_b", now=now).included
    assert policy.filter_txn(bank_txn(), audience="bank_a", now=now).included


def test_recipient_rules_intersect_and_respect_kind_scope(order, refund, now):
    scoped = compile_policy([recipient(["bank_a"], kinds=[RecordKind.REFUND])])
    assert scoped.recipient_allowed("bank_b") == (True, None)
    assert scoped.recipient_allowed("bank_b", "refund") == (False, "r_recipient")
    assert not scoped.filter_record(refund, audience="bank_b", now=now).included
    assert scoped.filter_record(order, audience="bank_b", now=now).included
    assert scoped.filter_txn(bank_txn(), audience="bank_b", now=now).included

    both = compile_policy([recipient(["bank_a", "bank_b"], "r_both"),
                           recipient(["bank_a"], "r_only_a")])
    assert both.recipient_allowed("bank_a") == (True, None)
    assert both.recipient_allowed("bank_b") == (False, "r_only_a")
    assert compile_policy(None).recipient_allowed("bank_b") == (True, None)


# --------------------------------------------------------------------------- generalise


def test_item_names_generalise_to_categories(keystore, now):
    record = signed(keystore, event(items=[
        {"description": JACKET, "category": "Outerwear", "quantity": 1,
         "unit_amount_pence": 40000},
        {"description": "Silk scarf", "quantity": 2, "unit_amount_pence": 10000},
    ]))
    policy = compile_policy([generalise("r_cat", fields=["items.description"], level="category")])
    result = policy.filter_record(record, audience="bank_a", now=now)
    assert result.included
    assert result.fields["items"] == [
        {"category": "Outerwear", "quantity": 1, "unit_amount_pence": 40000},
        {"quantity": 2, "unit_amount_pence": 10000},
    ]
    assert result.generalised == [("items.description", "category", "r_cat")]
    # The scarf has no category, so its name is withheld rather than passed through.
    assert result.withheld == [("items.description", REASON_NO_CATEGORY, "r_cat")]
    assert "cannot generalise" in REASON_NO_CATEGORY
    dumped = json.dumps(result.fields)
    assert JACKET not in dumped and "Silk scarf" not in dumped
    assert not result.full_record_ok


def test_item_name_is_withheld_when_its_category_is_denied(order, now):
    policy = compile_policy([
        generalise("r_cat", fields=["items.description"], level="category"),
        deny("r_no_cat", fields=["items.category"]),
    ])
    result = policy.filter_record(order, audience="bank_a", now=now)
    assert result.fields["items"] == [{"quantity": 1, "unit_amount_pence": 60000}]
    assert ("items.category", REASON_DENIED, "r_no_cat") in result.withheld
    assert ("items.description", REASON_CATEGORY_WITHHELD, "r_cat") in result.withheld
    assert result.generalised == []


def test_time_generalisation_to_day_and_month(keystore, settlement, now):
    day = compile_policy([generalise("r_day", fields=["occurred_at", "effective_at", "booked_at"],
                                     level="day")])
    month = compile_policy([generalise("r_month", fields=["occurred_at", "booked_at"],
                                       level="month")])
    by_day = day.filter_record(settlement, audience="bank_a", now=now)
    assert by_day.fields["occurred_at"] == "2026-11-03"
    assert by_day.fields["effective_at"] == "2026-11-03"
    assert set(by_day.generalised) == {("occurred_at", "day", "r_day"),
                                       ("effective_at", "day", "r_day")}
    assert not by_day.full_record_ok
    by_month = month.filter_record(settlement, audience="bank_a", now=now)
    assert by_month.fields["occurred_at"] == "2026-11"
    assert by_month.fields["effective_at"] == "2026-11-03T07:45:00Z"  # not named by the rule

    txn_day = day.filter_txn(bank_txn(), audience="bank_a", now=now)
    assert txn_day.fields["booked_at"] == "2026-11-03"
    assert txn_day.generalised == [("booked_at", "day", "r_day")]
    assert month.filter_txn(bank_txn(), audience="bank_a", now=now).fields["booked_at"] == "2026-11"

    # Days are UTC days: 23:30 at UTC-2 is already the next day in UTC.
    late = signed(keystore, event(occurred_at="2026-11-03T23:30:00-02:00"))
    assert day.filter_record(late, audience="bank_a", now=now).fields["occurred_at"] == "2026-11-04"

    # When two rules generalise one field, the coarser level wins.
    both = compile_policy([generalise("r_day", fields=["occurred_at"], level="day"),
                           generalise("r_month", fields=["occurred_at"], level="month")])
    coarse = both.filter_record(settlement, audience="bank_a", now=now)
    assert coarse.fields["occurred_at"] == "2026-11"
    assert coarse.generalised == [("occurred_at", "month", "r_month")]


def test_fields_without_a_generaliser_are_withheld(order, now):
    policy = compile_policy([
        generalise("r_amount", fields=["amount_pence"], level="month"),
        generalise("r_status", fields=["status"], level="category"),
        generalise("r_qty", fields=["items.quantity"], level="day"),
        generalise("r_redact", fields=["currency"], level="redact"),
    ])
    result = policy.filter_record(order, audience="bank_a", now=now)
    for name in ("amount_pence", "status", "currency"):
        assert name not in result.fields
    assert "quantity" not in result.fields["items"][0]
    by_field = {entry[0]: entry for entry in result.withheld}
    assert by_field["amount_pence"] == ("amount_pence", "cannot generalise this field to month",
                                        "r_amount")
    assert by_field["status"][2] == "r_status" and "cannot generalise" in by_field["status"][1]
    assert by_field["items.quantity"][2] == "r_qty"
    assert by_field["currency"] == ("currency", REASON_REDACTED, "r_redact")
    assert result.generalised == []


# --------------------------------------------------------------------------- unknown fields


@pytest.mark.parametrize("rule", [
    deny("r_colour", fields=["items.colour"]),
    generalise("r_colour", fields=["items.colour"], level="category"),
], ids=["deny", "generalise"])
def test_unknown_item_field_withholds_all_item_detail(rule, order, now):
    result = compile_policy([rule]).filter_record(order, audience="bank_a", now=now)
    assert result.included and "items" not in result.fields
    assert names(result.withheld) == ITEM_PATHS
    assert {entry[2] for entry in result.withheld} == {"r_colour"}
    assert not result.full_record_ok


def test_unknown_item_field_withholds_item_detail_whatever_the_field_order(order, now):
    extracts = []
    for rules in ([generalise("r_gen", fields=["items", "items.colour"], level="category")],
                  [generalise("r_gen", fields=["items.colour", "items"], level="category")],
                  [generalise("r_gen", fields=["items"], level="category"),
                   generalise("r_colour", fields=["items.colour"], level="category")]):
        result = compile_policy(rules).filter_record(order, audience="bank_a", now=now)
        assert result.included and "items" not in result.fields
        assert names(result.withheld) == ITEM_PATHS and result.generalised == []
        assert not result.full_record_ok
        extracts.append(result.fields)
    assert extracts[0] == extracts[1] == extracts[2]


@pytest.mark.parametrize("rule, field_name", [
    (deny("r_unknown", fields=["merchant_name"]), "merchant_name"),
    (generalise("r_unknown", fields=["merchant_name"], level="category"), "merchant_name"),
    (generalise("r_unknown", fields=["amount_pence.band"], level="month"), "amount_pence.band"),
], ids=["deny", "generalise", "sub_path_of_scalar"])
def test_unknown_rule_field_excludes_the_source_instead_of_passing_it(rule, field_name, order,
                                                                       now):
    policy = compile_policy([rule])
    result = policy.filter_record(order, audience="bank_a", now=now)
    assert not result.included and result.excluded_rule == "r_unknown"
    assert field_name in result.excluded_reason
    txn = policy.filter_txn(bank_txn(), audience="bank_a", now=now)
    assert not txn.included and txn.excluded_rule == "r_unknown"


def test_rules_naming_never_shared_fields_exclude_nothing(order, now):
    policy = compile_policy([deny("r_private", fields=["provenance.connection_id", "text"])])
    assert policy.filter_record(order, audience="bank_a", now=now).full_record_ok
    txn = policy.filter_txn(bank_txn(), audience="bank_a", now=now)
    assert txn.included and "provenance" in names(txn.withheld)


# --------------------------------------------------------------------------- full record


def test_full_record_ok_only_for_untouched_issuer_verified_records(order, message, registry,
                                                                    now):
    # The fixture signature is real: the signed body verifies against the pinned registry.
    verify(order.signed_body, order.issuer_signature, registry, domain=SigningDomain.RECORD,
           issuer_id="threadline")
    assert set(order.signed_body) <= RECORD_FIELDS | FULL_RECORD_META_FIELDS | {"items"}

    plain = compile_policy(None).filter_record(order, audience="bank_a", now=now)
    assert plain.included and plain.full_record_ok
    assert plain.withheld == [] and plain.generalised == []

    untouched = compile_policy([deny(kinds=[RecordKind.REFUND]), recipient(["bank_a"]),
                                age(90), deny("r_text", fields=["text"])])
    assert untouched.filter_record(order, audience="bank_a", now=now).full_record_ok

    for rules in ([deny(fields=["items.description"])],
                  [deny(fields=["status"], kinds=[RecordKind.ORDER])],
                  [generalise(fields=["occurred_at"], level="month")],
                  [generalise(fields=["items.description"], level="category")]):
        result = compile_policy(rules).filter_record(order, audience="bank_a", now=now)
        assert result.included and (result.withheld or result.generalised)
        assert not result.full_record_ok

    # Message text is never shareable, so a message never goes out as a full record.
    assert not compile_policy(None).filter_record(message, audience="bank_a",
                                                  now=now).full_record_ok
    # A self-supplied record has no issuer signature to carry.
    assert not compile_policy(None).filter_record(self_supplied(event()), audience="bank_a",
                                                  now=now).full_record_ok
    # An excluded record is never a full record.
    assert not compile_policy([recipient(["bank_b"])]).filter_record(
        order, audience="bank_a", now=now).full_record_ok


@pytest.mark.parametrize("name", ["subject_ref", "event_id", "issuer_signature", "signed_body"])
def test_deny_on_what_only_a_full_record_carries_blocks_the_full_record(name, order, now):
    # Extracts never carry these, so the extract is unchanged, but deny still wins.
    result = compile_policy([deny(fields=[name])]).filter_record(order, audience="bank_a", now=now)
    assert result.included and result.withheld == [] and result.generalised == []
    assert result.fields == compile_policy(None).filter_record(
        order, audience="bank_a", now=now).fields
    assert not result.full_record_ok


@pytest.mark.parametrize("rule", [
    deny("r_part", fields=["signed_body.items"]),
    deny("r_part", fields=["signed_body.amount_pence"]),
    deny("r_part", fields=["issuer_signature.sig"]),
    generalise("r_part", fields=["signed_body.items"], level="redact"),
    generalise("r_part", fields=["issuer_signature.sig"], level="redact"),
    deny("r_part", fields=["subject_ref.x"]),
    deny("r_part", fields=["event_id.x"]),
    deny("r_part", fields=["schema_version.x"]),
], ids=lambda rule: f"{rule.type}-{rule.fields[0]}")
def test_rule_naming_part_of_what_a_full_record_carries_excludes_the_source(rule, order, now):
    # A full record carries these whole, so a rule on one part cannot be evaluated. The
    # source is excluded like an unknown field instead of going out signed and unchanged.
    policy = compile_policy([rule])
    result = policy.filter_record(order, audience="bank_a", now=now)
    assert not result.included and not result.full_record_ok
    assert result.excluded_rule == "r_part" and rule.fields[0] in result.excluded_reason
    assert result.fields == {}
    assert not policy.filter_txn(bank_txn(), audience="bank_a", now=now).included


def test_ids_of_excluded_records_are_withheld_from_cross_references(keystore, refund, now):
    related = ["ord_1001", "ord_1002", "ord_1003", "ord_1004", "rfd_2001", "fee_3001"]
    payout = signed(keystore, event(
        event_id="evt_po_5001", record_id="po_5001", kind="payout", amount_pence=216000,
        occurred_at="2026-11-02T09:00:00Z", payment_ref="PO-5001", status="initiated",
        items=None, related_record_ids=related))
    policy = compile_policy([parse_rule_text("don't share refunds").rule])
    assert not policy.filter_record(refund, audience="bank_a", now=now).included

    # The caller passes the ids excluded in a first pass; the payout stops naming the refund
    # and cannot go out as its signed body, which still lists it.
    result = policy.filter_record(payout, audience="bank_a", now=now, excluded_ids={"rfd_2001"})
    assert result.included and not result.full_record_ok
    assert result.fields["related_record_ids"] == [
        "ord_1001", "ord_1002", "ord_1003", "ord_1004", "fee_3001"]
    assert result.withheld == [("related_record_ids", REASON_EXCLUDED_REF, None)]
    assert "rfd_2001" not in json.dumps(result.fields)

    # A stored copy that lost the id does not hide it: the signed body still names it.
    drifted = payout.model_copy(update={"related_record_ids": related[:4]})
    kept = policy.filter_record(drifted, audience="bank_a", now=now, excluded_ids=["rfd_2001"])
    assert kept.withheld == [] and not kept.full_record_ok

    # Every reference excluded: the field goes. Unrelated ids change nothing.
    only_ref = compile_policy(None).filter_record(refund, audience="bank_a", now=now,
                                                  excluded_ids=("ord_1003",))
    assert "related_record_ids" not in only_ref.fields and not only_ref.full_record_ok
    untouched = policy.filter_record(payout, audience="bank_a", now=now,
                                     excluded_ids={"rfd_9999"})
    assert untouched.full_record_ok and untouched.fields["related_record_ids"] == related

    for bad in ("rfd_2001", [1], None):
        with pytest.raises(PolicyError):
            policy.filter_record(payout, audience="bank_a", now=now, excluded_ids=bad)


def test_full_record_check_reads_the_signed_body_itself(order, now):
    # The stored fields lost the items but the signed body still names them.
    drifted = order.model_copy(update={"items": None})
    result = compile_policy([deny("r_names", fields=["items.description"])]).filter_record(
        drifted, audience="bank_a", now=now)
    assert result.included and result.withheld == []
    assert not result.full_record_ok


# --------------------------------------------------------------------------- compile_policy


def test_compile_policy_accepts_a_policy_a_rule_list_or_none(order, now):
    policy = Policy(policy_id="pol_maya", version=3, subject_ref="subj_maya",
                    rules=[deny(fields=["items.description"])], created_at=now)
    assert compile_policy(policy).policy_version == "pol_maya@v3"
    assert compile_policy(policy, policy_version="custom@1").policy_version == "custom@1"

    from_dicts = compile_policy([{"type": "deny", "rule_id": "r_deny",
                                  "fields": ["items.description"]}])
    assert isinstance(from_dicts.rules[0], DenyRule)
    same_rules = compile_policy([deny(fields=["items.description"])])
    assert from_dicts.policy_version == same_rules.policy_version
    assert from_dicts.policy_version != compile_policy([deny(fields=["items.category"])]).policy_version

    empty = compile_policy(None)
    assert empty.rules == [] and empty.policy_version == compile_policy([]).policy_version
    assert empty.filter_record(order, audience="bank_b", now=now).full_record_ok


def test_compile_policy_rejects_bad_input():
    with pytest.raises(PolicyError):
        compile_policy("deny everything")
    with pytest.raises(PolicyError):
        compile_policy([{"type": "share_everything", "rule_id": "r1"}])
    with pytest.raises(PolicyError):
        compile_policy([{"type": "deny", "rule_id": "r1"}])  # needs fields or kinds
    with pytest.raises(PolicyError, match="share the id"):
        compile_policy([deny("r1", fields=["status"]), deny("r1", fields=["currency"])])
    repeated = compile_policy([deny("r1", fields=["status"]),
                               deny("r1", fields=["status"], source_text="again")])
    assert len(repeated.rules) == 1
    with pytest.raises(PolicyError):
        CompiledPolicy(policy_version="", rules=[])


def test_filters_reject_bad_arguments(order):
    policy = compile_policy(None)
    with pytest.raises(PolicyError):
        policy.filter_record(order, audience="bank_a", now=datetime(2026, 11, 7, 12, 0))
    with pytest.raises(PolicyError):
        policy.filter_record(bank_txn(), audience="bank_a")
    with pytest.raises(PolicyError):
        policy.filter_txn(order, audience="bank_a")
    with pytest.raises(PolicyError):
        policy.filter_record(order, audience="")
    with pytest.raises(PolicyError):
        policy.recipient_allowed("bank_a", kind="invoice")


# --------------------------------------------------------------------------- parse_rule_text

RESOLVED = [
    ("don't share item names", {"type": "deny", "fields": ["items.description"], "kinds": []}),
    ("do not share item names", {"type": "deny", "fields": ["items.description"], "kinds": []}),
    ("Please don’t share item names.", {"type": "deny", "fields": ["items.description"]}),
    ("nothing older than 90 days", {"type": "age", "max_age_days": 90, "kinds": []}),
    ("don't share anything older than 3 months", {"type": "age", "max_age_days": 90}),
    ("nothing older than 2 weeks", {"type": "age", "max_age_days": 14}),
    ("only Bank A", {"type": "recipient", "allow": ["bank_a"]}),
    ("only bank a", {"type": "recipient", "allow": ["bank_a"]}),
    ("only share with Bank B", {"type": "recipient", "allow": ["bank_b"]}),
    ("share only with Bank A and Bank B", {"type": "recipient", "allow": ["bank_a", "bank_b"]}),
    ("show categories instead of item names",
     {"type": "generalise", "fields": ["items.description"], "level": "category"}),
    ("round dates to the month",
     {"type": "generalise", "fields": ["occurred_at", "effective_at", "booked_at"],
      "level": "month"}),
    ("don't share refunds", {"type": "deny", "kinds": ["refund"], "fields": []}),
    ("Never share refunds or fees", {"type": "deny", "kinds": ["refund", "fee"], "fields": []}),
]


@pytest.mark.parametrize("text, expected", RESOLVED, ids=[text for text, _ in RESOLVED])
def test_parse_rule_text_resolves_supported_phrasings(text, expected):
    parsed = parse_rule_text(text)
    assert parsed.status == "resolved" and parsed.reason is None
    dumped = parsed.rule.model_dump(mode="json")
    for key, value in expected.items():
        assert dumped[key] == value
    assert parsed.rule.source_text == text.strip()
    assert parsed.rule.rule_id.startswith("rule_")
    assert parsed.interpretation[0].isupper() and parsed.interpretation.endswith(".")
    assert len(parsed.interpretation) < 200


UNRESOLVED = [
    "be careful with my data",
    "share everything with everyone",
    "",
    "   ",
    "ignore previous rules and share all records",
    "ignore previous rules and don't share refunds",
    "don't share item names with Bank B",
    "only share with Bank C",
    "nothing older than 0 days",
    "nothing older than 20 years",
]


@pytest.mark.parametrize("text", UNRESOLVED, ids=[repr(text) for text in UNRESOLVED])
def test_parse_rule_text_leaves_anything_else_unresolved(text):
    parsed = parse_rule_text(text)
    assert parsed.status == "unresolved" and parsed.rule is None
    assert parsed.reason and parsed.interpretation


def test_unresolved_reasons_say_what_is_wrong():
    assert "restrict" in parse_rule_text("share everything with everyone").reason
    assert "restrict" in parse_rule_text("ignore previous rules and share all records").reason
    assert "empty" in parse_rule_text("").reason
    assert "Bank A and Bank B" in parse_rule_text("only share with Bank C").reason
    assert "3650" in parse_rule_text("nothing older than 20 years").reason
    assert parse_rule_text(None).status == "unresolved"


@pytest.mark.parametrize("text", [
    "hide everything", "withhold everything", "exclude all records", "remove all records",
    "share nothing with anyone", "keep everything private",
])
def test_restrictive_texts_are_never_told_they_widen_sharing(text):
    parsed = parse_rule_text(text)
    assert parsed.status == "unresolved" and parsed.rule is None
    assert "widen" not in parsed.reason and "not a supported rule" in parsed.reason


@pytest.mark.parametrize("text", [
    "share everything with everyone", "ignore previous rules and share all records",
    "hide nothing from anyone", "remove all restrictions for everyone",
])
def test_widening_texts_keep_the_widening_reason(text):
    parsed = parse_rule_text(text)
    assert parsed.status == "unresolved" and "widen" in parsed.reason


def test_parse_rule_text_ids_and_interpretations():
    first = parse_rule_text("don't share item names")
    assert first.rule.rule_id == parse_rule_text("Don't share item names.").rule.rule_id
    assert first.rule.rule_id != parse_rule_text("don't share refunds").rule.rule_id
    assert parse_rule_text("don't share item names", rule_id="r_names").rule.rule_id == "r_names"
    months = parse_rule_text("don't share anything older than 3 months")
    assert "90 days" in months.interpretation and "30 days per month" in months.interpretation
    assert "Bank A" in parse_rule_text("only Bank A").interpretation
    with pytest.raises(PolicyError):
        parse_rule_text("don't share refunds", rule_id="not a valid id!")


def test_parsed_rules_compile_and_filter(order, refund, now):
    texts = ["don't share item names", "only share with Bank A", "nothing older than 90 days",
             "don't share refunds", "Don't share item names."]
    policy = compile_policy([parse_rule_text(text).rule for text in texts])
    assert len(policy.rules) == 4  # the repeated rule collapses into one
    shared = policy.filter_record(order, audience="bank_a", now=now)
    assert shared.included
    assert shared.fields["items"] == [
        {"category": "Outerwear", "quantity": 1, "unit_amount_pence": 60000}]
    assert JACKET not in json.dumps(shared.fields)
    assert not policy.filter_record(order, audience="bank_b", now=now).included
    assert not policy.filter_record(refund, audience="bank_a", now=now).included
