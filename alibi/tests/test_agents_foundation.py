"""Foundation of the Gemini agents: material, typed outputs, the 5-day candidate rule, actions.

Everything runs on tmp_path stores loaded through ``alibi.api.load_fixture``; records that no
fixture variant carries are made with ``model_copy(update=...)`` from stored ones. No network,
no Gemini.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pydantic
import pytest

from alibi.agents.actions import ACTION_BUDGET, ActionBudget, ActionResult, execute_action
from alibi.agents.candidates import RULE_TEXT, WINDOW, CandidateResult, settlement_candidates
from alibi.agents.material import (
    Material,
    challenger_input,
    curator_input,
    detective_input,
    matcher_input,
    record_view,
    rfc_view,
    shareable_item,
    txn_view,
)
from alibi.agents.models import (
    ROLE_MODELS,
    ActionArgs,
    ChallengerOutput,
    CuratorOutput,
    DetectiveOutput,
    MatcherOutput,
    NextAction,
    NextCheck,
    OnePassOutput,
    ValidationFailure,
    parse_output,
    undeclared_fields,
    validate_refs,
)
from alibi.api import load_fixture, open_store
from alibi.core.ingest import ingest_merchant_event
from alibi.core.schemas import ItemDetail, LinkState, RecordKind, SourceTrust
from alibi.gateway import Gateway
from alibi.llm.prompts import ROLES, load_prompt
from alibi.tests.test_llm_prompts import expected_outputs

SUBJECT = "subj_maya"
CREDIT = "txn_9001"
PAYOUT = "po_5001"
SETTLEMENT = "stl_88001"
BOOKED = datetime(2026, 11, 3, 10, 0, tzinfo=timezone.utc)
# Payload content that must never reach an agent's feedback or the Curator's field list.
CONTENT = ("216000", "seller balance", "£2,160", "THREADLINE PAYOUT")


# --------------------------------------------------------------------------- helpers


def load(tmp_path, keystore, variant: str):
    store = open_store(tmp_path / f"{variant}.db")
    fx = load_fixture(store, variant, keystore=keystore)
    return store, fx


def gateway_for(store, fx, registry, *, run_id="run_foundation", **kw):
    kw.setdefault("settlement_lookup", fx["settlement_lookup"])
    kw.setdefault("txn_refresh", fx["txn_refresh"])
    return Gateway(store, subject_ref=fx["subject_ref"], run_id=run_id, registry=registry,
                   now=fx["now"], request_id="rfc_a_001", **kw)


def material_of(store, fx) -> Material:
    """Material holding every pushed record and bank txn of the fixture (nothing looked up)."""
    material = Material(fx["subject_ref"])
    for record in store.list_sources(fx["subject_ref"]):
        material.add_record(record)
    for txn in store.list_bank_txns(fx["subject_ref"]):
        material.add_txn(txn)
    return material


def fetch_settlement(store, fx, registry):
    """The lookup-only settlement record, retrieved through the gateway (ingests it too)."""
    return gateway_for(store, fx, registry, run_id="run_fetch").lookup_settlement("PO-5001")


def settlement_like(payout, **updates):
    """A settlement record derived from a stored payout, without revalidation."""
    base = {"record_id": SETTLEMENT, "kind": RecordKind.SETTLEMENT, "status": "settled",
            "settlement_ref": "STL-88001", "effective_at": BOOKED - timedelta(hours=2),
            "occurred_at": BOOKED - timedelta(hours=2), "related_record_ids": []}
    base.update(updates)
    return payout.model_copy(update=base)


def self_supplied(record, **updates):
    return record.model_copy(update={"source_trust": SourceTrust.SELF_SUPPLIED,
                                     "issuer_signature": None, "signed_body": None, **updates})


@pytest.fixture
def supported(tmp_path, keystore):
    return load(tmp_path, keystore, "supported")


@pytest.fixture
def records(supported):
    store, fx = supported
    return {r.record_id: r for r in store.list_sources(fx["subject_ref"])}


@pytest.fixture
def credit(supported):
    store, _ = supported
    return store.get_bank_txn(CREDIT)


def rejected_for(result: CandidateResult, record_id: str):
    matches = [r for r in result.rejected if record_id in [p.record_id for p in r.payouts]]
    assert len(matches) == 1, result.summary()
    return matches[0]


# =========================================================================== candidates


class TestCandidateRule:
    def test_provisional_before_lookup_and_settled_after(self, supported, registry):
        store, fx = supported
        txn = store.get_bank_txn(CREDIT)
        before = settlement_candidates(txn, store.list_sources(SUBJECT))
        assert before.state == LinkState.UNMATCHED and before.chosen is None
        assert [c.tier for c in before.provisional] == ["provisional"]
        assert before.provisional[0].record_ids == [PAYOUT]
        assert before.provisional[0].settlement is None and before.provisional[0].settled_at is None
        assert before.pool == before.provisional
        assert before.rejected == []

        fetch_settlement(store, fx, registry)
        after = settlement_candidates(txn, store.list_sources(SUBJECT))
        assert after.state == LinkState.LINKED
        chosen = after.chosen
        assert chosen is not None and chosen.tier == "settled"
        assert chosen.record_ids == [PAYOUT]
        assert chosen.settlement.record_id == SETTLEMENT
        assert chosen.settled_at == "2026-11-03T07:45:00Z"
        assert chosen.total_pence == 216000 and chosen.currency == "GBP"
        assert chosen.reference_matched is True
        assert after.provisional == [] and after.pool == after.settled
        assert "settled po_5001@1" in after.summary() and "link linked" in after.summary()

    @pytest.mark.parametrize("offset, expected_in", [
        (WINDOW, True),                         # exactly 5 days before booking is in
        (WINDOW + timedelta(seconds=1), False),  # 5 days and one second is out
    ])
    def test_window_is_inclusive_at_five_days(self, records, credit, offset, expected_in):
        effective = BOOKED - offset
        settlement = settlement_like(records[PAYOUT], effective_at=effective,
                                     occurred_at=effective + timedelta(minutes=5))
        result = settlement_candidates(credit, [records[PAYOUT], settlement])
        if expected_in:
            assert result.state == LinkState.LINKED
            assert result.chosen.settled_at == "2026-10-29T10:00:00Z"
        else:
            assert result.state == LinkState.UNMATCHED and result.settled == []
            rejection = rejected_for(result, PAYOUT)
            assert rejection.conflict is False
            assert "more than 5 days before the booking" in rejection.reason

    def test_settlement_after_booking_is_a_conflict(self, records, credit):
        late = BOOKED + timedelta(minutes=1)
        settlement = settlement_like(records[PAYOUT], effective_at=late, occurred_at=late)
        result = settlement_candidates(credit, [records[PAYOUT], settlement])
        assert result.state == LinkState.UNMATCHED and result.provisional == []
        rejection = rejected_for(result, PAYOUT)
        assert rejection.conflict is True and rejection.settlement is settlement
        assert "after the booking" in rejection.reason
        assert result.conflicts == [rejection]

    def test_pending_at_or_after_booking_is_a_conflict(self, tmp_path, keystore, registry):
        store, fx = load(tmp_path, keystore, "contradiction")
        fetch_settlement(store, fx, registry)   # pending, observed 2026-11-03T12:00Z
        result = settlement_candidates(store.get_bank_txn(CREDIT), store.list_sources(SUBJECT))
        assert result.state == LinkState.UNMATCHED
        assert result.settled == [] and result.provisional == []
        rejection = rejected_for(result, PAYOUT)
        assert rejection.conflict is True
        assert "still pending at 2026-11-03T12:00:00Z" in rejection.reason
        assert "at or after the booking at 2026-11-03T10:00:00Z" in rejection.reason

    def test_pending_exactly_at_booking_is_a_conflict(self, records, credit):
        settlement = settlement_like(records[PAYOUT], status="pending", effective_at=None,
                                     occurred_at=BOOKED)
        result = settlement_candidates(credit, [records[PAYOUT], settlement])
        assert rejected_for(result, PAYOUT).conflict is True

    def test_pending_before_booking_is_rejected_without_conflict(self, records, credit):
        settlement = settlement_like(records[PAYOUT], status="pending", effective_at=None,
                                     occurred_at=BOOKED - timedelta(hours=1))
        result = settlement_candidates(credit, [records[PAYOUT], settlement])
        assert result.state == LinkState.UNMATCHED and result.provisional == []
        rejection = rejected_for(result, PAYOUT)
        assert rejection.conflict is False
        assert "does not show the funds reached the bank" in rejection.reason

    def test_failed_payout_is_a_conflict(self, records, credit):
        payout = records[PAYOUT].model_copy(update={"status": "failed"})
        result = settlement_candidates(credit, [payout])
        rejection = rejected_for(result, PAYOUT)
        assert rejection.conflict is True and rejection.reason == "payout status is failed"

    def test_failed_settlement_is_a_conflict(self, records, credit):
        settlement = settlement_like(records[PAYOUT], status="failed", effective_at=None)
        result = settlement_candidates(credit, [records[PAYOUT], settlement])
        rejection = rejected_for(result, PAYOUT)
        assert rejection.conflict is True and "has status failed" in rejection.reason

    def test_failed_settlement_wins_over_a_later_settled_one(self, records, credit):
        failed = settlement_like(records[PAYOUT], status="failed", effective_at=None,
                                 occurred_at=BOOKED - timedelta(hours=5))
        settled = settlement_like(records[PAYOUT], revision=2)
        result = settlement_candidates(credit, [records[PAYOUT], failed, settled])
        assert result.settled == [] and rejected_for(result, PAYOUT).conflict is True

    def test_settlement_amount_mismatch_is_a_conflict(self, records, credit):
        settlement = settlement_like(records[PAYOUT], amount_pence=200000)
        result = settlement_candidates(credit, [records[PAYOUT], settlement])
        rejection = rejected_for(result, PAYOUT)
        assert rejection.conflict is True and "200000 GBP" in rejection.reason

    def test_payout_total_mismatch_is_rejected(self, records, credit):
        payout = records[PAYOUT].model_copy(update={"amount_pence": 200000})
        result = settlement_candidates(credit, [payout])
        rejection = rejected_for(result, PAYOUT)
        assert rejection.conflict is False
        assert rejection.reason == "total 200000 differs from the credit's 216000"

    def test_currency_mismatch_is_rejected(self, records, credit):
        payout = records[PAYOUT].model_copy(update={"currency": "EUR"})
        result = settlement_candidates(credit, [payout])
        rejection = rejected_for(result, PAYOUT)
        assert rejection.conflict is False and "EUR differs from the credit's GBP" in rejection.reason

    def test_credit_reference_naming_none_of_the_payouts_refs_is_rejected(self, records, credit):
        other = credit.model_copy(update={"refs": ["PO-9999"], "description": "PAYOUT PO-9999"})
        result = settlement_candidates(other, [records[PAYOUT]])
        rejection = rejected_for(result, PAYOUT)
        assert rejection.conflict is False and "names none of this payout's references" in rejection.reason

    def test_credit_carrying_the_payout_reference_matches(self, records, credit):
        assert credit.refs == ["PO-5001", "STL-88001"]
        result = settlement_candidates(credit, [records[PAYOUT]])
        assert result.provisional[0].reference_matched is True
        assert "reference matches the credit" in result.provisional[0].summary()

    def test_reference_in_the_description_only_also_matches(self, records, credit):
        described = credit.model_copy(update={"refs": [], "description": "payout po-5001"})
        result = settlement_candidates(described, [records[PAYOUT]])
        assert result.provisional[0].reference_matched is True

    def test_credit_without_reference_matches_none(self, tmp_path, keystore):
        store, _ = load(tmp_path, keystore, "contradiction")   # 'INCOMING TRANSFER', no refs
        result = settlement_candidates(store.get_bank_txn(CREDIT), store.list_sources(SUBJECT))
        assert result.provisional[0].reference_matched is None
        assert "credit carries no reference" in result.provisional[0].summary()

    def test_message_records_are_never_candidates(self, records, credit):
        message = records["msg_7001"]
        assert message.kind == RecordKind.MESSAGE and message.amount_pence == credit.amount_pence
        result = settlement_candidates(credit, [message])
        assert result.settled == result.provisional == result.rejected == []
        assert result.state == LinkState.UNMATCHED

    def test_two_equal_payouts_are_ambiguous_in_both_tiers(self, tmp_path, keystore, registry):
        store, fx = load(tmp_path, keystore, "ambiguous")
        txn = store.get_bank_txn(CREDIT)
        provisional = settlement_candidates(txn, store.list_sources(SUBJECT))
        assert provisional.state == LinkState.AMBIGUOUS and provisional.settled == []
        assert [c.record_ids for c in provisional.provisional] == [["po_5001"], ["po_5002"]]
        assert provisional.chosen is None and provisional.pool == provisional.provisional

        gw = gateway_for(store, fx, registry)
        gw.lookup_settlement("PO-5001")
        gw.lookup_settlement("PO-5002")
        settled = settlement_candidates(txn, store.list_sources(SUBJECT))
        assert settled.state == LinkState.AMBIGUOUS and len(settled.settled) == 2
        assert settled.chosen is None and settled.pool == settled.settled
        assert "2 settled candidate(s)" in settled.summary()

    def test_one_settled_among_two_provisional_links(self, tmp_path, keystore, registry):
        store, fx = load(tmp_path, keystore, "ambiguous")
        gateway_for(store, fx, registry).lookup_settlement("PO-5001")
        result = settlement_candidates(store.get_bank_txn(CREDIT), store.list_sources(SUBJECT))
        assert result.state == LinkState.LINKED and result.chosen.record_ids == ["po_5001"]
        assert [c.record_ids for c in result.provisional] == [["po_5002"]]

    def test_payouts_sharing_a_settlement_ref_form_one_candidate(self, records, credit):
        first = records[PAYOUT].model_copy(update={"amount_pence": 100000, "settlement_ref": "STL-88001"})
        second = records[PAYOUT].model_copy(update={"record_id": "po_5002", "payment_ref": "PO-5002",
                                                    "amount_pence": 116000, "settlement_ref": "STL-88001",
                                                    "occurred_at": BOOKED - timedelta(hours=20)})
        provisional = settlement_candidates(credit, [first, second])
        assert provisional.state == LinkState.UNMATCHED and len(provisional.provisional) == 1
        group = provisional.provisional[0]
        assert group.record_ids == [PAYOUT, "po_5002"] and group.group_ref == "STL-88001"
        assert group.total_pence == 216000 and group.reference_matched is True

        settlement = settlement_like(records[PAYOUT])   # PO-5001, 216000, settled before booking
        settled = settlement_candidates(credit, [first, second, settlement])
        assert settled.state == LinkState.LINKED
        assert settled.chosen.record_ids == [PAYOUT, "po_5002"]
        assert settled.chosen.settlement is settlement
        assert "po_5001@1, po_5002@1" in settled.chosen.summary()

    def test_self_supplied_payout_is_rejected(self, records, credit):
        own = self_supplied(records[PAYOUT])
        result = settlement_candidates(credit, [own])
        rejection = rejected_for(result, PAYOUT)
        assert rejection.conflict is False and "self-supplied payout" in rejection.reason
        assert result.provisional == []

    def test_self_supplied_settlement_is_rejected(self, records, credit):
        settlement = self_supplied(settlement_like(records[PAYOUT]))
        result = settlement_candidates(credit, [records[PAYOUT], settlement])
        rejection = rejected_for(result, PAYOUT)
        assert rejection.conflict is False and "self-supplied settlement" in rejection.reason
        assert rejection.settlement is settlement

    def test_debit_has_no_candidates(self, records, credit):
        debit = credit.model_copy(update={"amount_pence": -216000})
        result = settlement_candidates(debit, list(records.values()))
        assert result == CandidateResult()
        assert result.state == LinkState.UNMATCHED and result.summary().startswith("link unmatched")

    def test_other_subjects_records_are_ignored(self, records, credit):
        foreign = records[PAYOUT].model_copy(update={"subject_ref": "subj_other"})
        assert settlement_candidates(credit, [foreign]) == CandidateResult()

    def test_rule_text_states_the_pinned_rule(self):
        assert "5 days" in RULE_TEXT and "never after" in RULE_TEXT
        assert "platform balance is never a candidate" in RULE_TEXT
        assert WINDOW == timedelta(days=5)


# =========================================================================== material


class TestMaterial:
    def test_add_has_get_and_latest_revision(self, supported, registry, records):
        store, fx = supported
        material = Material(SUBJECT)
        first = fetch_settlement(store, fx, registry)
        second = first.model_copy(update={"revision": 2, "status": "pending"})
        assert material.add_record(first) is True
        assert material.add_record(first) is False          # same revision again
        assert material.add_record(second) is True
        assert material.has(SETTLEMENT) and material.has(SETTLEMENT, 1) and material.has(SETTLEMENT, 2)
        assert not material.has(SETTLEMENT, 3) and not material.has("po_9999") and not material.has("")
        assert material.get(SETTLEMENT).revision == 2       # latest
        assert material.get(SETTLEMENT, 1) is first
        assert material.get("po_9999") is None and material.ref_for("po_9999") is None
        assert material.ref_for(SETTLEMENT).key() == "record:stl_88001@2"
        assert material.ref_for(SETTLEMENT, 1).revision == 1
        assert material.observed_inputs == 2                # distinct revisions
        assert [r.revision for r in material.records()] == [2]
        assert len(material.records(all_revisions=True)) == 2
        assert material.keys() == ["stl_88001@1", "stl_88001@2"]

    def test_records_by_kind_and_txns(self, supported, records, credit):
        store, fx = supported
        material = material_of(store, fx)
        assert [r.record_id for r in material.records(kinds=["payout"])] == [PAYOUT]
        assert {r.kind for r in material.records(kinds=[RecordKind.ORDER, RecordKind.FEE])} == {
            RecordKind.ORDER, RecordKind.FEE}
        assert len(material.records()) == len(records)
        assert [t.txn_id for t in material.txns()] == [CREDIT]
        assert material.get(CREDIT) == credit and material.ref_for(CREDIT).ref_type == "txn"
        newer = credit.model_copy(update={"revision": 2})
        material.add_txn(newer)
        assert material.get(CREDIT).revision == 2 and len(material.txns()) == 1
        assert material.observed_inputs == len(records) + 2

    def test_lookup_count_counts_only_connector_reaching_lookups(self):
        material = Material(SUBJECT)
        assert material.add_lookup("lookup_settlement", {"payout_reference": "PO-5001"}, "ok") == 0
        assert material.lookup_count == 1
        material.add_lookup("lookup_by_date", {"payout_date": "2026-11-02"}, "ok")   # passport-side
        material.add_lookup("lookup_payouts_covering", {"record_id": "ord_1001"}, "ok")
        assert material.lookup_count == 1
        material.add_lookup("refresh_txn", {"txn_id": CREDIT}, "refused")
        material.add_lookup("lookup_settlement", {"payout_reference": "PO-9"}, "budget_exceeded")
        material.add_lookup("lookup_settlement", {}, "skipped")
        assert material.lookup_count == 1
        material.add_lookup("refresh_txn", {"txn_id": CREDIT}, "unavailable")
        material.add_lookup("lookup_settlement", {"payout_reference": "PO-5001"}, "not_found")
        assert material.lookup_count == 3 and len(material.lookups) == 8
        assert material.lookups[0].as_dict() == {
            "index": 0, "tool": "lookup_settlement", "args": {"payout_reference": "PO-5001"},
            "outcome": "ok", "refs": [], "reason": None, "actor": "gateway"}

    def test_via_lookup_records_the_fetching_lookup(self, supported, registry):
        store, fx = supported
        material = Material(SUBJECT)
        settlement = fetch_settlement(store, fx, registry)
        payout = store.get_source(PAYOUT)
        material.add_record(payout)
        index = material.add_lookup("lookup_settlement", {"payout_reference": "PO-5001"}, "ok",
                                    refs=["stl_88001@1"], actor="challenger")
        material.add(settlement, via_lookup=index)
        assert material.via_lookup(SETTLEMENT, 1) == index
        assert material.via_lookup(PAYOUT, 1) is None
        assert material.via_lookup("po_9999", 1) is None
        # Re-adding a pushed record through a lookup fills in the provenance; a later lookup
        # never overwrites the first.
        material.add(payout, via_lookup=index)
        assert material.via_lookup(PAYOUT, 1) == index
        later = material.add_lookup("lookup_by_date", {"payout_date": "2026-11-02"}, "ok")
        material.add(payout, via_lookup=later)
        assert material.via_lookup(PAYOUT, 1) == index
        assert record_view(settlement, material)["via_lookup"] == index

    def test_references_and_knows_reference(self, supported):
        store, fx = supported
        material = material_of(store, fx)
        assert material.references() == {"PO-5001", "STL-88001"}   # 'THREADLINE' has no digit
        assert material.knows_reference("PO-5001") and material.knows_reference("po-5001")
        assert material.knows_reference("  stl-88001 ")
        assert not material.knows_reference("PO-9999")
        assert not material.knows_reference("") and not material.knows_reference(None)
        assert Material(SUBJECT).references() == set()

    def test_subject_mismatch_raises(self, records, credit):
        material = Material("subj_other")
        with pytest.raises(ValueError, match="subject"):
            material.add_record(records[PAYOUT])
        with pytest.raises(ValueError, match="subject"):
            material.add(credit)
        assert material.observed_inputs == 0


class TestViews:
    def test_record_view_hides_items_subject_and_signature(self, supported, records):
        store, fx = supported
        material = material_of(store, fx)
        order = records["ord_1001"].model_copy(update={"items": [
            ItemDetail(description="Vintage jacket", category="clothing", quantity=1,
                       unit_amount_pence=60000),
            ItemDetail(description="Silk scarf", category="accessories")]})
        view = record_view(order, material)
        text = json.dumps(view)
        assert view["item_count"] == 2 and view["item_categories"] == ["accessories", "clothing"]
        assert "Vintage" not in text and "scarf" not in text and "items" not in view
        for hidden in ("subject_ref", "account_ref", "issuer_signature", "signed_body",
                       "payload_sha256", "event_id", "connector", "received_at"):
            assert hidden not in view
        assert view["trust"] == "issuer_verified" and view["attestation"] == "issuer_signature"
        assert view["redacted"] is False and view["via_lookup"] is None
        assert view["amount"] == 60000 and view["currency"] == "GBP"
        assert view["occurred_at"] == "2026-10-04T14:12:00Z"

    def test_record_view_of_payout_message_and_settlement(self, supported, records, registry):
        store, fx = supported
        payout = record_view(records[PAYOUT])
        assert payout["payout_reference"] == "PO-5001" and payout["status"] == "initiated"
        assert payout["covers_record_ids"][0] == "ord_1001" and "via_lookup" not in payout
        message = record_view(records["msg_7001"])
        assert message["kind"] == "message" and "seller balance" in message["message"]
        assert "message" not in record_view(records["msg_7001"], include_text=False)
        settlement = record_view(fetch_settlement(store, fx, registry))
        assert settlement["settled_at"] == "2026-11-03T07:45:00Z"
        assert settlement["status_observed_at"] == "2026-11-03T08:00:00Z"
        assert settlement["destination"] == "bank" and settlement["settlement_reference"] == "STL-88001"
        own = record_view(self_supplied(records[PAYOUT]))
        assert own["trust"] == "self_supplied" and own["attestation"] == "none"

    def test_txn_view_hides_account_and_provenance(self, supported, credit):
        store, fx = supported
        view = txn_view(credit, material_of(store, fx), link_state="linked", linked_to=PAYOUT)
        assert "account_ref" not in view and "provenance" not in view and "subject_ref" not in view
        assert view["kind"] == "bank_credit" and view["amount"] == 216000
        assert view["booked_at"] == "2026-11-03T10:00:00Z"
        assert view["payment_references"] == ["PO-5001", "STL-88001"]
        assert view["link_state"] == "linked" and view["linked_to"] == PAYOUT
        assert view["trust"] == "bank_observed" and view["attestation"] == "none"
        plain = txn_view(credit.model_copy(update={"amount_pence": -500}))
        assert plain["kind"] == "bank_debit" and "link_state" not in plain and "via_lookup" not in plain

    def test_shareable_item_lists_field_names_only(self, supported, records, credit):
        store, fx = supported
        material = material_of(store, fx)
        payout = shareable_item(records[PAYOUT], material)
        text = json.dumps(payout)
        assert payout["fields"] == ["amount_pence", "currency", "issuer_id", "kind", "occurred_at",
                                    "payment_ref", "record_id", "related_record_ids", "revision",
                                    "source_trust", "status"]
        assert "216000" not in text and "PO-5001" not in text and "message" not in payout["fields"]
        message = shareable_item(records["msg_7001"], material)
        assert message["fields"][-1] == "message" and "seller balance" not in json.dumps(message)
        with_items = shareable_item(records["ord_1001"].model_copy(update={"items": [
            ItemDetail(description="Vintage jacket", category="clothing")]}))
        assert "items.description" in with_items["fields"] and "items.category" in with_items["fields"]
        assert "items.quantity" not in with_items["fields"]
        assert with_items["item_categories"] == ["clothing"] and "Vintage" not in json.dumps(with_items)
        txn = shareable_item(credit, material)
        assert txn["kind"] == "bank_credit" and txn["booked_at"] == "2026-11-03T10:00:00Z"
        assert txn["fields"] == ["amount_pence", "booked_at", "currency", "description", "refs",
                                 "revision", "source_trust", "txn_id"]
        assert "THREADLINE" not in json.dumps(txn) and "account_ref" not in txn["fields"]

    def test_rfc_view_handles_both_scope_kinds(self, supported):
        store, _ = supported
        txn_scoped = rfc_view(store.get_request("rfc_a_001"))
        assert txn_scoped["scope"] == {"period": None, "txn_ids": [CREDIT]}
        assert txn_scoped["audience"] == "bank_a" and txn_scoped["issued_at"] == "2026-11-04T09:00:00Z"
        assert [q["question_id"] for q in txn_scoped["questions"]] == ["q1"]
        period_scoped = rfc_view(store.get_request("rfc_b_001"))
        assert period_scoped["scope"] == {
            "period": {"from": "2026-10-01T00:00:00Z", "to": "2026-11-01T00:00:00Z"}, "txn_ids": []}
        assert [q["question_id"] for q in period_scoped["questions"]] == ["q1", "q2"]
        for view in (txn_scoped, period_scoped):
            assert set(view) == {"request_id", "audience", "scope", "questions", "issued_at"}
            assert "subject_ref" not in view and "expires_at" not in view

    def test_role_inputs_are_json_serialisable(self, supported, records, credit, registry):
        store, fx = supported
        material = material_of(store, fx)
        settlement = fetch_settlement(store, fx, registry)
        index = material.add_lookup("lookup_settlement", {"payout_reference": "PO-5001"}, "ok",
                                    refs=["stl_88001@1"])
        material.add(settlement, via_lookup=index)
        rfc = store.get_request("rfc_a_001")
        merchant = material.records(kinds=["order", "refund", "fee", "payout", "message"])
        claim = {"claim_id": "c1", "question_id": "q1", "status": "unresolved", "refs": []}
        inputs = {
            "matcher": matcher_input(material, credit, [records[PAYOUT]], [records["msg_7001"]],
                                     rule=RULE_TEXT),
            "detective": detective_input(material, rfc, [(credit, "linked", PAYOUT)], merchant,
                                         [settlement], pattern_checks={"unusual_inflow": False},
                                         merchant_lookup={"merchant_id": "threadline"}),
            "challenger": challenger_input(material, claim, [credit], merchant, [settlement],
                                           round_index=1, actions_left=2),
            "curator": curator_input(material, rfc, [claim], [credit, *merchant, settlement],
                                     {"deny": ["items.description"]}),
        }
        for role, payload in inputs.items():
            round_trip = json.loads(json.dumps(payload))
            assert round_trip == payload, role
        assert inputs["matcher"]["candidate_rule"] == RULE_TEXT
        assert inputs["matcher"]["entry"]["record_id"] == CREDIT
        assert inputs["detective"]["bank"][0]["link_state"] == "linked"
        assert inputs["detective"]["lookups_so_far"][0]["refs"] == ["stl_88001@1"]
        assert inputs["detective"]["settlements"][0]["via_lookup"] == index
        gw = inputs["challenger"]["gateway"]
        assert gw == {"available_actions": ["lookup_settlement", "lookup_payouts_covering",
                                            "lookup_by_date", "refresh_txn", "none"],
                      "actions_left": 2, "round": 1}
        assert all("fields" in item for item in inputs["curator"]["shareable"])
        assert "216000" not in json.dumps(inputs["curator"]["shareable"])


# =========================================================================== models


class TestOutputs:
    @pytest.mark.parametrize("role", ROLES)
    def test_worked_examples_parse(self, role):
        prompt = load_prompt(role)
        for example in expected_outputs(prompt):
            parsed = parse_output(role, example)
            assert isinstance(parsed, ROLE_MODELS[role])
            assert undeclared_fields(role, example) == []

    def test_worked_example_details_survive_parsing(self):
        matcher = parse_output("matcher", expected_outputs(load_prompt("matcher"))[0])
        assert matcher.decision == "linked" and matcher.chosen_record_id == "po_7781"
        assert [c.relationship for c in matcher.candidates][:2] == ["exact", "part_of_bundle"]
        challenger = parse_output("challenger", expected_outputs(load_prompt("challenger"))[0])
        assert challenger.next_action.type == "lookup_settlement"
        assert challenger.next_action.args.payout_reference == "RH-PO-7781"
        assert challenger.bank_settlement_established is False
        detective = parse_output("detective", expected_outputs(load_prompt("detective"))[0])
        assert detective.claims[0].next_checks[0].type == "lookup_settlement"
        assert detective.alerts[0].kind == "unusual_inflow"
        curator = parse_output("curator", expected_outputs(load_prompt("curator"))[0])
        assert curator.exclusions[0].reason == "policy_denied"
        policy = parse_output("policy_parse", expected_outputs(load_prompt("policy_parse"))[0])
        assert [r.kind for r in policy.rules][:2] == ["deny", "generalise"]
        assert policy.rules[2].params.max_age_days == 365

    def test_undeclared_fields_are_reported_and_dropped(self):
        data = {"claim_id": "c1", "proposed_status": "supported", "bank_settlement_established": True,
                "secret_note": "do not share", "share_everything": True}
        assert undeclared_fields("challenger", data) == ["secret_note", "share_everything"]
        parsed = parse_output("challenger", data)
        assert not hasattr(parsed, "secret_note") and "secret_note" not in parsed.model_dump()
        assert parsed.next_action.type == "none" and parsed.payout_reference == ""
        nested = {"disclosures": [], "exclusions": [], "gaps": [{"question_id": "q1", "why": "x"}]}
        assert undeclared_fields("curator", nested) == []   # only top-level keys are reported
        assert "why" not in parse_output("curator", nested).gaps[0].model_dump()

    def test_one_pass_output_parses(self):
        parsed = parse_output("one_pass", {
            "claims": [{"question_id": "q1", "status": "unresolved",
                        "refs": [{"record_id": PAYOUT, "revision": 1, "role": "confirms"}]}],
            "disclosures": [{"question_id": "q1", "record_id": PAYOUT, "revision": 1, "form": "full"}]})
        assert isinstance(parsed, OnePassOutput) and parsed.disclosures[0].fields == []

    @pytest.mark.parametrize("role, data", [
        ("matcher", {"entry": {"record_id": CREDIT, "revision": 1}, "decision": "maybe"}),
        ("detective", {"claims": [{"claim_id": "c1", "question_id": "q1", "status": "likely"}]}),
        ("challenger", {"claim_id": "c1", "proposed_status": "supported",
                        "bank_settlement_established": True,
                        "next_action": {"type": "merchant_lookup", "args": {}}}),
        ("curator", {"exclusions": [{"record_id": PAYOUT, "revision": 1, "reason": "whatever"}]}),
        ("policy_parse", {"rules": [{"rule_id": "r1", "kind": "block", "target": "all",
                                     "confidence": 0.5}]}),
        ("matcher", {"entry": {"record_id": CREDIT, "revision": 0}, "decision": "unmatched"}),
        ("challenger", {"claim_id": "c1", "proposed_status": "supported"}),   # required missing
    ])
    def test_invalid_enum_or_shape_raises(self, role, data):
        with pytest.raises(pydantic.ValidationError):
            parse_output(role, data)

    def test_validation_failure_as_dict(self):
        failure = ValidationFailure("challenger", "confirming_refs[0]", "po_9999", 1,
                                    "not in the retrieved material")
        assert failure.as_dict() == {"role": "challenger", "path": "confirming_refs[0]",
                                     "record_id": "po_9999", "revision": 1,
                                     "reason": "not in the retrieved material"}
        assert json.loads(json.dumps(failure.as_dict())) == failure.as_dict()


class TestValidateRefs:
    @pytest.fixture
    def material(self, supported):
        store, fx = supported
        return material_of(store, fx)

    def test_matcher_unknown_chosen_record_is_downgraded(self, material):
        out = MatcherOutput(entry={"record_id": CREDIT, "revision": 1}, decision="linked",
                            chosen_record_id="po_9999", chosen_revision=1,
                            candidates=[{"record_id": PAYOUT, "revision": 1, "relationship": "exact",
                                         "confidence": 0.9},
                                        {"record_id": "po_9999", "revision": 1, "relationship": "exact",
                                         "confidence": 0.9},
                                        {"record_id": PAYOUT, "revision": 7, "relationship": "exact",
                                         "confidence": 0.9}])
        cleaned, failures = validate_refs(out, material, role="matcher")
        assert cleaned.decision == "unmatched" and cleaned.chosen_record_id == ""
        assert cleaned.chosen_revision == 0
        assert [c.record_id for c in cleaned.candidates] == [PAYOUT]
        assert [f.path for f in failures] == ["candidates[1]", "candidates[2]", "chosen_record_id"]
        assert failures[1].revision == 7 and failures[2].record_id == "po_9999"
        assert out.decision == "linked"   # the input is not mutated

    def test_matcher_known_chosen_record_is_kept_and_entry_checked(self, material):
        out = MatcherOutput(entry={"record_id": "txn_9999", "revision": 1}, decision="linked",
                            chosen_record_id=PAYOUT, chosen_revision=0)
        cleaned, failures = validate_refs(out, material, role="matcher")
        assert cleaned.decision == "linked" and cleaned.chosen_record_id == PAYOUT
        assert [f.path for f in failures] == ["entry"]
        ambiguous = MatcherOutput(entry={"record_id": CREDIT, "revision": 1}, decision="ambiguous",
                                  chosen_record_id=PAYOUT, chosen_revision=1)
        cleaned, failures = validate_refs(ambiguous, material, role="matcher")
        assert cleaned.chosen_record_id == "" and failures == []

    def test_detective_refs_and_next_checks(self, material):
        out = DetectiveOutput(
            alerts=[{"kind": "unexplained_credit",
                     "refs": [{"record_id": CREDIT, "revision": 1}, {"record_id": "txn_9", "revision": 1}]}],
            claims=[{"claim_id": "c1", "question_id": "q1", "status": "unresolved",
                     "refs": [{"record_id": PAYOUT, "revision": 1, "role": "confirms"},
                              {"record_id": SETTLEMENT, "revision": 1, "role": "confirms"}],
                     "next_checks": [
                         {"type": "lookup_settlement", "args": {"payout_reference": "PO-5001"}},
                         {"type": "lookup_settlement", "args": {"payout_reference": "PO-9999"}},
                         {"type": "lookup_payouts_covering", "args": {"record_id": "ord_9999"}},
                         {"type": "lookup_payouts_covering", "args": {"record_id": "ord_1001"}},
                         {"type": "lookup_by_date", "args": {"payout_date": "2 Nov 2026"}},
                         {"type": "refresh_txn", "args": {"txn_id": "txn_9999"}},
                         {"type": "merchant_lookup", "args": {"merchant_id": "threadline"}},
                         {"type": "lookup_settlement", "args": {"payout_reference": "PO-5001",
                                                                "record_id": "po_9999"}},
                     ]}])
        cleaned, failures = validate_refs(out, material, role="detective")
        assert [r.record_id for r in cleaned.alerts[0].refs] == [CREDIT]
        assert [r.record_id for r in cleaned.claims[0].refs] == [PAYOUT]
        checks = cleaned.claims[0].next_checks
        assert [c.type for c in checks] == ["lookup_settlement", "none", "none", "lookup_payouts_covering",
                                            "none", "none", "none", "none"]
        assert checks[1].args == ActionArgs() and checks[1].reason.startswith("dropped: payout_reference")
        assert checks[4].reason == "dropped: payout_date is not YYYY-MM-DD"
        assert checks[6].reason == "dropped: merchant_lookup is not available through the gateway"
        paths = [f.path for f in failures]
        assert paths == ["alerts[0].refs[1]", "claims[0].refs[1]", "claims[0].next_checks[1]",
                         "claims[0].next_checks[2]", "claims[0].next_checks[4]",
                         "claims[0].next_checks[5]", "claims[0].next_checks[6]",
                         "claims[0].next_checks[7]"]
        assert failures[-1].record_id == "po_9999"

    def test_challenger_reference_and_action(self, material):
        out = ChallengerOutput(
            claim_id="c1", proposed_status="supported", bank_settlement_established=True,
            payout_reference="PO-9999",
            confirming_refs=[{"record_id": PAYOUT, "revision": 1}, {"record_id": "po_9999", "revision": 1}],
            conflicting_refs=[{"record_id": SETTLEMENT, "revision": 1}],
            next_action={"type": "lookup_settlement", "args": {"payout_reference": "PO-9999"}})
        cleaned, failures = validate_refs(out, material, role="challenger")
        assert cleaned.payout_reference == ""
        assert [r.record_id for r in cleaned.confirming_refs] == [PAYOUT]
        assert cleaned.conflicting_refs == []
        assert cleaned.next_action.type == "none" and cleaned.next_action.args == ActionArgs()
        assert [f.path for f in failures] == ["confirming_refs[1]", "conflicting_refs[0]",
                                              "payout_reference", "next_action"]
        assert failures[2].revision is None and failures[2].record_id == "PO-9999"
        # A known reference and a known txn pass untouched, case-insensitively.
        fine = ChallengerOutput(claim_id="c1", proposed_status="unresolved",
                                bank_settlement_established=False, payout_reference="po-5001",
                                next_action={"type": "refresh_txn", "args": {"txn_id": CREDIT}})
        cleaned, failures = validate_refs(fine, material, role="challenger")
        assert failures == [] and cleaned.payout_reference == "po-5001"
        assert cleaned.next_action.type == "refresh_txn"

    def test_curator_disclosures_outside_shareable_are_dropped(self, material):
        shareable = {(CREDIT, 1), (PAYOUT, 1)}
        out = CuratorOutput(
            disclosures=[{"question_id": "q1", "record_id": CREDIT, "revision": 1, "form": "full"},
                         {"question_id": "q1", "record_id": "msg_7001", "revision": 1, "form": "extract"},
                         {"question_id": "q1", "record_id": "po_9999", "revision": 1, "form": "full"},
                         {"question_id": "q1", "record_id": PAYOUT, "revision": 2, "form": "full"}],
            exclusions=[{"record_id": "msg_7001", "revision": 1, "reason": "not_needed_for_any_question"},
                        {"record_id": "rcpt_9", "revision": 1, "reason": "policy_denied"}])
        cleaned, failures = validate_refs(out, material, role="curator", shareable=shareable)
        assert [d.record_id for d in cleaned.disclosures] == [CREDIT]
        assert [e.record_id for e in cleaned.exclusions] == ["msg_7001"]   # excluding needs no shareability
        assert [(f.path, f.reason) for f in failures] == [
            ("disclosures[1]", "not in the shareable evidence"),
            ("disclosures[2]", "not in the retrieved material"),
            ("disclosures[3]", "not in the retrieved material"),
            ("exclusions[1]", "not in the retrieved material")]
        # Without a shareable set, any retrieved record may be disclosed.
        cleaned, failures = validate_refs(out, material, role="curator")
        assert [d.record_id for d in cleaned.disclosures] == [CREDIT, "msg_7001"]

    def test_one_pass_refs_and_disclosures(self, material):
        out = OnePassOutput(
            claims=[{"question_id": "q1", "status": "supported",
                     "refs": [{"record_id": PAYOUT, "revision": 1}, {"record_id": "po_9999", "revision": 1}]}],
            disclosures=[{"question_id": "q1", "record_id": PAYOUT, "revision": 1, "form": "full"},
                         {"question_id": "q1", "record_id": "msg_7001", "revision": 1, "form": "full"},
                         {"question_id": "q1", "record_id": "po_9999", "revision": 1, "form": "full"}])
        cleaned, failures = validate_refs(out, material, role="one_pass", shareable={(PAYOUT, 1)})
        assert [r.record_id for r in cleaned.claims[0].refs] == [PAYOUT]
        assert [d.record_id for d in cleaned.disclosures] == [PAYOUT]
        assert [f.path for f in failures] == ["claims[0].refs[1]", "disclosures[1]", "disclosures[2]"]
        assert all(f.role == "one_pass" for f in failures)
        assert all(isinstance(f.as_dict()["revision"], int) for f in failures)


# =========================================================================== actions


class TestActions:
    @pytest.fixture
    def run(self, supported, registry):
        store, fx = supported
        return store, fx, gateway_for(store, fx, registry), material_of(store, fx)

    @staticmethod
    def act(type_: str, **args) -> NextAction:
        return NextAction(type=type_, args=ActionArgs(**args), reason="test")

    def test_lookup_settlement_found_and_added_with_via_lookup(self, run):
        store, fx, gw, material = run
        before = material.observed_inputs
        budget = ActionBudget()
        result = execute_action(gw, material, self.act("lookup_settlement", payout_reference="PO-5001"),
                                budget)
        assert isinstance(result, ActionResult)
        assert result.outcome == "ok" and result.reason is None and result.found
        assert [r.record_id for r in result.records] == [SETTLEMENT] and result.txns == []
        assert result.new_keys == ["stl_88001@1"] and result.lookup_index == 0
        assert material.has(SETTLEMENT, 1) and material.via_lookup(SETTLEMENT, 1) == 0
        assert material.observed_inputs == before + 1
        assert material.lookups[0].tool == "lookup_settlement" and material.lookups[0].actor == "challenger"
        assert material.lookups[0].refs == ["stl_88001@1"] and material.lookup_count == 1
        assert budget.used == 1 and budget.remaining == ACTION_BUDGET - 1
        assert gw.calls_used == 1 and gw.trace[-1].outcome == "ok"
        assert store.get_source(SETTLEMENT).record_id == SETTLEMENT   # ingested into the passport
        # Looking it up again returns the same record and adds nothing new.
        again = execute_action(gw, material, self.act("lookup_settlement", payout_reference="PO-5001"),
                               budget)
        assert again.outcome == "ok" and again.new_keys == [] and again.lookup_index == 1

    def test_unknown_reference_is_refused_before_the_gateway(self, run):
        store, fx, gw, material = run
        budget = ActionBudget()
        result = execute_action(gw, material, self.act("lookup_settlement", payout_reference="PO-9999"),
                                budget)
        assert result.outcome == "refused" and not result.found
        assert result.reason == "payout_reference is not carried by any retrieved record"
        assert gw.calls_used == 0 and gw.trace == []          # the connector was never asked
        assert material.lookups[result.lookup_index].outcome == "refused"
        assert material.lookup_count == 0
        # Documented behaviour: the action budget is spent before the material check, so a
        # refused action costs a unit (flagged in the S3 notes as a candidate for change).
        assert budget.used == 1

    def test_lookup_returning_none_is_not_found(self, supported, registry):
        store, fx = supported
        gw = gateway_for(store, fx, registry, settlement_lookup=lambda ref: None)
        material = material_of(store, fx)
        result = execute_action(gw, material, self.act("lookup_settlement", payout_reference="PO-5001"),
                                ActionBudget())
        assert result.outcome == "not_found" and result.records == []
        assert result.reason == "the merchant knows no settlement for PO-5001"
        assert not material.has(SETTLEMENT) and result.new_keys == []
        assert material.lookup_count == 1                       # the connector was reached

    def test_budget_exhaustion_adds_nothing(self, run):
        store, fx, gw, material = run
        before = material.observed_inputs
        budget = ActionBudget(cap=0)
        result = execute_action(gw, material, self.act("lookup_settlement", payout_reference="PO-5001"),
                                budget)
        assert result.outcome == "budget_exceeded" and result.records == [] and result.new_keys == []
        assert result.reason == "the action budget of 0 is spent"
        assert material.observed_inputs == before and not material.has(SETTLEMENT)
        assert material.lookups[result.lookup_index].outcome == "budget_exceeded"
        assert material.lookup_count == 0 and gw.calls_used == 0
        assert budget.used == 0 and budget.remaining == 0

    def test_budget_counts_each_executed_action(self, run):
        store, fx, gw, material = run
        budget = ActionBudget(cap=2)
        assert execute_action(gw, material, self.act("lookup_by_date", payout_date="2026-11-02"),
                              budget).outcome == "ok"
        assert execute_action(gw, material, self.act("refresh_txn", txn_id=CREDIT), budget).outcome == "ok"
        third = execute_action(gw, material, self.act("lookup_settlement", payout_reference="PO-5001"),
                               budget)
        assert third.outcome == "budget_exceeded" and third.reason == "the action budget of 2 is spent"
        assert budget.used == 2 and budget.remaining == 0
        for bad in (-1, True, 2.0):
            with pytest.raises(ValueError):
                ActionBudget(cap=bad)

    def test_lookup_payouts_covering_finds_the_payout(self, run):
        store, fx, gw, material = run
        result = execute_action(gw, material, self.act("lookup_payouts_covering", record_id="ord_1001"),
                                ActionBudget())
        assert result.outcome == "ok" and [r.record_id for r in result.records] == [PAYOUT]
        assert result.new_keys == []            # the payout was already pushed into the material
        # A pushed record that a lookup returns again gains that lookup as its provenance.
        assert material.via_lookup(PAYOUT, 1) == result.lookup_index
        assert material.lookups[result.lookup_index].refs == ["po_5001@1"]
        unknown = execute_action(gw, material, self.act("lookup_payouts_covering", record_id="ord_9999"),
                                 ActionBudget())
        assert unknown.outcome == "refused" and unknown.reason == "record_id is not in the retrieved material"
        nothing = execute_action(gw, material, self.act("lookup_payouts_covering", record_id="msg_7001"),
                                 ActionBudget())
        assert nothing.outcome == "not_found" and nothing.reason == "no payout or settlement covers msg_7001"

    def test_lookup_payouts_covering_adds_a_payout_the_material_lacks(self, supported, registry):
        store, fx = supported
        gw = gateway_for(store, fx, registry)
        material = Material(SUBJECT)
        material.add_record(store.get_source("ord_1001"))
        result = execute_action(gw, material, self.act("lookup_payouts_covering", record_id="ord_1001"),
                                ActionBudget())
        assert result.outcome == "ok" and result.new_keys == ["po_5001@1"]
        assert material.via_lookup(PAYOUT, 1) == result.lookup_index
        assert material.lookup_count == 0       # a passport listing is not a connector call

    def test_lookup_by_date(self, run):
        store, fx, gw, material = run
        result = execute_action(gw, material, self.act("lookup_by_date", payout_date="2026-11-02"),
                                ActionBudget())
        assert result.outcome == "ok" and [r.record_id for r in result.records] == [PAYOUT]
        empty = execute_action(gw, material, self.act("lookup_by_date", payout_date="2026-11-09"),
                               ActionBudget())
        assert empty.outcome == "not_found" and empty.reason == "no payout or settlement on 2026-11-09"
        for bad in ("2026-13-02", "2 Nov 2026", "2026-11-2", "20261102"):
            result = execute_action(gw, material, self.act("lookup_by_date", payout_date=bad),
                                    ActionBudget())
            assert result.outcome == "refused" and result.reason == "payout_date is not YYYY-MM-DD"
        no_date = execute_action(gw, material, self.act("lookup_by_date"), ActionBudget())
        assert no_date.outcome == "refused"

    def test_lookup_by_date_matches_a_settlements_effective_day(self, run):
        store, fx, gw, material = run
        execute_action(gw, material, self.act("lookup_settlement", payout_reference="PO-5001"),
                       ActionBudget())
        result = execute_action(gw, material, self.act("lookup_by_date", payout_date="2026-11-03"),
                                ActionBudget())
        assert result.outcome == "ok" and [r.record_id for r in result.records] == [SETTLEMENT]

    def test_refresh_txn(self, run):
        store, fx, gw, material = run
        result = execute_action(gw, material, self.act("refresh_txn", txn_id=CREDIT), ActionBudget())
        assert result.outcome == "ok" and [t.txn_id for t in result.txns] == [CREDIT]
        assert result.records == [] and material.lookup_count == 1
        assert result.feedback()["returned"] == [{"record_id": CREDIT, "revision": 1, "kind": "bank_txn"}]
        unknown = execute_action(gw, material, self.act("refresh_txn", txn_id="txn_9999"), ActionBudget())
        assert unknown.outcome == "refused" and unknown.reason == "txn_id is not in the retrieved material"

    def test_refresh_without_connector_is_unavailable(self, supported, registry):
        store, fx = supported
        gw = gateway_for(store, fx, registry, txn_refresh=None)
        material = material_of(store, fx)
        result = execute_action(gw, material, self.act("refresh_txn", txn_id=CREDIT), ActionBudget())
        assert result.outcome == "unavailable" and "no bank refresh connector" in result.reason

    def test_merchant_lookup_and_none_do_not_reach_the_gateway(self, run):
        store, fx, gw, material = run
        budget = ActionBudget()
        check = NextCheck(type="merchant_lookup", args=ActionArgs(merchant_id="threadline"))
        result = execute_action(gw, material, check, budget)
        assert result.outcome == "refused"
        assert result.reason == "merchant_lookup is not available through the gateway"
        assert result.lookup_index is None and result.args == {"merchant_id": "threadline"}
        skipped = execute_action(gw, material, self.act("none"), budget)
        assert skipped.outcome == "skipped" and skipped.reason == "no retrieval requested"
        assert skipped.tool == "none" and skipped.lookup_index is None
        assert budget.used == 0 and gw.calls_used == 0 and material.lookups == []

    def test_feedback_carries_ids_kinds_and_statuses_only(self, run):
        store, fx, gw, material = run
        result = execute_action(gw, material, self.act("lookup_settlement", payout_reference="PO-5001"),
                                ActionBudget())
        feedback = result.feedback()
        assert feedback == {"tool": "lookup_settlement", "args": {"payout_reference": "PO-5001"},
                            "outcome": "ok", "reason": None,
                            "returned": [{"record_id": SETTLEMENT, "revision": 1, "kind": "settlement",
                                          "status": "settled"}],
                            "lookup_index": 0}
        text = json.dumps(feedback)
        for secret in CONTENT:
            assert secret not in text
        assert "STL-88001" not in text and "effective_at" not in text
        covering = execute_action(gw, material, self.act("lookup_payouts_covering", record_id="ord_1001"),
                                  ActionBudget()).feedback()
        assert covering["returned"] == [{"record_id": PAYOUT, "revision": 1, "kind": "payout",
                                        "status": "initiated"}]
        assert "216000" not in json.dumps(covering)

    def test_gateway_scope_error_becomes_a_refused_outcome(self, run):
        store, fx, gw, material = run
        # The material knows PO-7777 through a self-supplied payout, but no issuer-verified
        # record of the subject carries it, so the gateway refuses the connector call.
        own = self_supplied(store.get_source(PAYOUT), record_id="po_self", payment_ref="PO-7777")
        material.add_record(own)
        assert material.knows_reference("PO-7777")
        result = execute_action(gw, material, self.act("lookup_settlement", payout_reference="PO-7777"),
                                ActionBudget())
        assert result.outcome == "refused" and result.records == []
        assert result.reason == "payment_ref is not carried by this subject's issuer evidence"
        assert gw.trace[-1].step == "refusal" and gw.trace[-1].outcome == "refused"
        assert material.lookups[result.lookup_index].outcome == "refused"
        assert material.lookup_count == 0 and not material.has(SETTLEMENT)

    def test_gateway_budget_error_becomes_a_typed_outcome(self, supported, registry):
        store, fx = supported
        gw = gateway_for(store, fx, registry, budget=0)
        material = material_of(store, fx)
        result = execute_action(gw, material, self.act("refresh_txn", txn_id=CREDIT), ActionBudget())
        assert result.outcome == "budget_exceeded" and "call budget of 0" in result.reason
        assert material.lookup_count == 0

    def test_connector_failure_is_unavailable_without_the_payload(self, supported, registry):
        def broken(ref):
            raise RuntimeError("secret connector detail 216000")
        store, fx = supported
        gw = gateway_for(store, fx, registry, settlement_lookup=broken)
        material = material_of(store, fx)
        result = execute_action(gw, material, self.act("lookup_settlement", payout_reference="PO-5001"),
                                ActionBudget())
        assert result.outcome == "unavailable"
        assert "RuntimeError" in result.reason and "216000" not in result.reason

    def test_pending_correction_refresh_of_a_settlement_changes_the_candidate_rule(
            self, tmp_path, keystore, registry):
        """End to end: a lookup links the credit, the pending correction then contradicts it."""
        store, fx = load(tmp_path, keystore, "pending_correction")
        gw = gateway_for(store, fx, registry)
        material = material_of(store, fx)
        txn = store.get_bank_txn(CREDIT)
        first = execute_action(gw, material, self.act("lookup_settlement", payout_reference="PO-5001"),
                               ActionBudget())
        assert first.outcome == "ok"
        assert settlement_candidates(txn, material.records()).state == LinkState.LINKED
        for correction in fx["corrections"]:
            ingest_merchant_event(store, registry, correction, subject_ref=SUBJECT, now=fx["now"])
        second = execute_action(gw, material, self.act("lookup_settlement", payout_reference="PO-5001"),
                                ActionBudget())
        assert second.outcome == "ok" and second.new_keys == ["stl_88001@2"]
        assert material.get(SETTLEMENT).revision == 2 and material.observed_inputs == first.lookup_index + 11
        after = settlement_candidates(txn, material.records())
        assert after.state == LinkState.UNMATCHED and after.conflicts[0].conflict is True
        assert "stl_88001@2 was still pending" in after.conflicts[0].reason
