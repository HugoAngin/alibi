"""The case-to-core bridge: every development case maps, field by field, and the clock hides
what has not arrived yet."""

from datetime import datetime, timezone

import pytest

from agents.data.api import load_case, load_cases
from alibi.connectors import keys
from alibi.connectors.case_source import (
    KIND_MAP,
    CaseClock,
    MappingError,
    default_as_of,
    load_case_without_gold,
    map_bank_row,
    map_import,
    map_merchant_event,
    map_rfc,
)
from alibi.core.ingest import import_self_supplied, ingest_merchant_event
from alibi.core.schemas import BankTxn, MerchantEvent, RequestForContext, format_utc
from alibi.core.store import Store

RFC_A_TIME = "2026-11-02T09:00:00Z"


@pytest.fixture(scope="module")
def case1():
    return load_case(1)


@pytest.fixture(scope="module")
def all_cases():
    return load_cases()


def _event(case, record_id):
    for d in [*case["merchant_deliveries"], *case["merchant_lookup"]]:
        if d["event"]["record_id"] == record_id:
            return d["event"]
    raise KeyError(record_id)


# --------------------------------------------------------------------------- whole-corpus checks


def test_every_case_object_maps_to_a_valid_core_object(all_cases):
    counts = {"events": 0, "imports": 0, "rows": 0, "rfcs": 0}
    for case in all_cases:
        for d in [*case["merchant_deliveries"], *case["merchant_lookup"]]:
            mapped = map_merchant_event(d["event"])
            MerchantEvent.model_validate(mapped.body)
            if d["event"]["kind"] == "receipt" and d["event"].get("amount_minor") is None:
                assert mapped.body["kind"] == "message"
            else:
                assert mapped.body["kind"] == KIND_MAP[d["event"]["kind"]]
            assert mapped.extra["case_kind"] == d["event"]["kind"]
            counts["events"] += 1
        for d in case["imports"]:
            mapped = map_import(d["record"])
            MerchantEvent.model_validate(mapped.body)
            if d["record"]["kind"] == "receipt":
                assert mapped.body["kind"] == "message" and mapped.body["text"] == d["record"]["text"]
            assert "claimed_trust" not in mapped.body and "import_source" not in mapped.body
            assert mapped.extra["tagged"] == "self_supplied"
            counts["imports"] += 1
        for d in case["bank_deliveries"]:
            mapped = map_bank_row(d["row"], subject_ref=case["subject_ref"],
                                  connection_id="conn-test", retrieved_at=d["received_at"])
            BankTxn.model_validate(mapped.body)
            counts["rows"] += 1
        for rfc in case["rfcs"]:
            mapped = map_rfc(rfc)
            RequestForContext.model_validate(mapped.body)
            counts["rfcs"] += 1
    assert counts["events"] > 1000 and counts["rows"] > 200 and counts["rfcs"] >= 70
    assert counts["imports"] >= 2


def test_no_free_text_reference_is_parsed_into_a_core_field(case1):
    release = map_merchant_event(_event(case1, "rec-0001-020"))
    assert release.body["kind"] == "message"
    assert "PO-0001-01" in release.body["text"]
    assert release.body["payment_ref"] == "REL-0001-01"      # the release's own reference only
    assert release.body["related_record_ids"] == _event(case1, "rec-0001-020")["covers_record_ids"]
    assert release.extra["destination"] == "platform_balance"


# --------------------------------------------------------------------------- field mapping


def test_case1_payout_maps_to_a_core_payout_with_settlement_fields(case1):
    mapped = map_merchant_event(_event(case1, "rec-0001-021"))
    body = mapped.body
    assert body["kind"] == "payout"
    assert body["payment_ref"] == "PO-0001-01"
    assert body["amount_pence"] == 216000 and body["currency"] == "GBP"
    assert body["status"] == "settled"
    assert body["effective_at"] == "2026-10-27T16:00:00Z"
    assert body["occurred_at"] == "2026-10-26T09:00:00Z"
    assert len(body["related_record_ids"]) == 18
    assert body["schema_version"] == "alibi.merchant_event.v1"
    for name in ("payout_reference", "status", "settled_at", "covers_record_ids", "destination"):
        assert name in mapped.extra
    assert "settlement_ref" not in body and "text" not in body


def test_sale_fee_refund_purchase_mapping(case1):
    sale = map_merchant_event(_event(case1, "rec-0001-001")).body
    assert sale["kind"] == "order" and sale["items"] == [{"description": "second-hand clothing lot 1"}]
    assert sale["status"] == "completed" and sale["amount_pence"] == 30000
    fee = map_merchant_event(_event(case1, "rec-0001-002"))
    assert fee.body["kind"] == "fee" and fee.body["related_record_ids"] == ["rec-0001-001"]
    assert fee.extra["fee_type"] == "commission" and "status" not in fee.body
    refund = map_merchant_event(_event(case1, "rec-0001-005")).body
    assert refund["kind"] == "refund" and refund["related_record_ids"] == ["rec-0001-003"]
    assert refund["status"] == "completed"
    purchase = map_merchant_event(_event(case1, "rec-0001-019")).body
    assert purchase["kind"] == "receipt"
    assert purchase["items"] == [{"description": "prescription eyewear", "category": "health"}]


def test_pending_payout_keeps_status_as_of_in_extra_and_no_effective_time():
    case2 = load_case(2)
    mapped = map_merchant_event(_event(case2, "rec-0002-021"))
    assert mapped.body["status"] == "pending"
    assert "effective_at" not in mapped.body
    assert mapped.extra["status_as_of"] == "2026-10-29T06:30:00Z"
    assert mapped.extra["settled_at"] is None


def test_correction_sets_corrects_revision():
    case21 = load_case(21)
    correction = next(d["event"] for d in case21["merchant_deliveries"]
                      if d["event"]["revision"] == 2)
    body = map_merchant_event(correction).body
    assert body["corrects_revision"] == 1 and body["revision"] == 2
    assert body["correction_reason"] == correction["correction_reason"]


def test_batch_withdrawal_and_order_number_mapping():
    case9 = load_case(9)
    batched = next(d["event"] for d in [*case9["merchant_deliveries"], *case9["merchant_lookup"]]
                   if d["event"].get("settlement_batch_id"))
    mapped = map_merchant_event(batched)
    assert mapped.body["settlement_ref"] == batched["settlement_batch_id"]
    assert mapped.extra["settlement_batch_id"] == batched["settlement_batch_id"]
    case44 = load_case(44)
    payout = map_merchant_event(case44["merchant_lookup"][0]["event"])
    assert payout.extra["withdrawal_id"] == "W-5716"
    assert "W-5716" not in payout.body.values()
    sale = next(d["event"] for d in case44["merchant_deliveries"]
                if d["event"]["kind"] == "sale" and d["event"].get("order_number"))
    assert map_merchant_event(sale).body["payment_ref"] == str(sale["order_number"])


def test_documents_map_to_messages_with_text():
    case46 = load_case(46)
    notice = next(d["event"] for d in case46["merchant_deliveries"] if d["event"]["kind"] == "notice")
    mapped = map_merchant_event(notice)
    assert mapped.body["kind"] == "message" and mapped.body["text"] == notice["text"]
    assert "amount_pence" not in mapped.body and mapped.extra["notice_type"] == notice["notice_type"]


def test_lookup_note_is_carried_as_text():
    case59 = load_case(59)
    payout = next(d["event"] for d in case59["merchant_lookup"] if d["event"].get("note"))
    assert map_merchant_event(payout).body["text"] == payout["note"]


def test_import_strips_trust_claims_and_the_backend_tags_self_supplied(tmp_path):
    case39 = load_case(39)
    record = case39["imports"][0]["record"]
    mapped = map_import(record)
    assert mapped.extra["claimed_trust"] == "issuer_verified"
    assert mapped.body["issuer_id"] == "mock-marketplace"   # stays as data
    with Store(tmp_path / "p.db") as store:
        put = import_self_supplied(store, mapped.body, subject_ref=case39["subject_ref"],
                                   now=datetime(2026, 7, 13, 15, tzinfo=timezone.utc))
    assert put.record.source_trust == "self_supplied"
    assert put.record.record_id == "self_" + record["record_id"]


def test_bank_row_mapping_and_provenance(case1):
    row = case1["bank_deliveries"][-1]["row"]
    mapped = map_bank_row(row, subject_ref="maya-001", connection_id="conn-maya-bank",
                          retrieved_at="2026-10-28T07:30:00Z", cursor="c:4")
    body = mapped.body
    assert body["txn_id"] == "txn-0001-004" and body["amount_pence"] == 216000
    assert body["refs"] == ["PO-0001-01"] and body["account_ref"] == "acct-maya-001"
    assert body["provenance"] == {"connector": "bank_feed", "connection_id": "conn-maya-bank",
                                  "retrieved_at": "2026-10-28T07:30:00Z",
                                  "source_version": "mock_bank_feed.v1", "cursor": "c:4"}
    assert body["source_trust"] == "bank_observed"
    assert mapped.extra["counterparty_name"] == "Mock Marketplace Payments"
    debit = map_bank_row(case1["bank_deliveries"][0]["row"], subject_ref="maya-001",
                         connection_id="c", retrieved_at="2026-10-06T20:00:00Z").body
    assert debit["amount_pence"] == -65000 and debit["refs"] == []


def test_rfc_mapping_keeps_exactly_one_scope(case1):
    a = map_rfc(case1["rfcs"][0])
    assert a.body["issuer"] == "bank_a" and a.body["audience"] == "bank_a"
    assert a.body["txn_ids"] == ["txn-0001-004"] and "period" not in a.body
    assert "parent_request_id" not in a.body
    assert a.extra["question_ids"] == ["q.source_of_funds", "q.breakdown"]
    b = map_rfc(case1["rfcs"][1])
    assert b.body["period"] == {"from": "2026-10-01T00:00:00Z", "to": "2026-11-01T00:00:00Z"}
    assert "txn_ids" not in b.body and b.body["audience"] == "bank_b"
    both = dict(case1["rfcs"][0], period={"from": "2026-10-01T00:00:00Z", "to": "2026-11-01T00:00:00Z"})
    with pytest.raises(MappingError):
        map_rfc(both)
    neither = dict(case1["rfcs"][0], txn_ids=[])
    with pytest.raises(MappingError):
        map_rfc(neither)


def test_mapping_errors_are_typed(case1):
    with pytest.raises(MappingError):
        map_merchant_event({**_event(case1, "rec-0001-001"), "kind": "wire"})
    with pytest.raises(MappingError):
        map_merchant_event({**_event(case1, "rec-0001-001"), "amount_minor": 12.5})
    with pytest.raises(MappingError):
        map_merchant_event("not an object")
    with pytest.raises(MappingError):
        map_bank_row({}, subject_ref="s", connection_id="c", retrieved_at=RFC_A_TIME)


# --------------------------------------------------------------------------- signed and ingested


def test_signed_case_events_ingest_as_issuer_verified(tmp_path, case1):
    keystore = keys.connector_keystore(tmp_path / "keys", seed="s2")
    registry = keystore.registry()
    assert keys.MERCHANT_KID in registry.kids() and "merchant_demo" in registry.kids()
    with Store(tmp_path / "p.db") as store:
        for d in case1["merchant_deliveries"]:
            payload = keys.sign_merchant_event(map_merchant_event(d["event"]).body, keystore)
            put = ingest_merchant_event(store, registry, payload, subject_ref="maya-001",
                                        now=datetime.fromisoformat(d["received_at"].replace("Z", "+00:00")))
            assert put.status == "inserted"
        assert len(store.list_sources("maya-001")) == 20
        assert all(r.source_trust == "issuer_verified" for r in store.list_sources("maya-001"))


def test_the_core_demo_key_cannot_sign_for_the_case_issuer(tmp_path, case1):
    keystore = keys.connector_keystore(tmp_path / "keys", seed="s2")
    payload = keys.sign_merchant_event(map_merchant_event(_event(case1, "rec-0001-001")).body,
                                       keystore, kid="merchant_demo")
    from alibi.core.ingest import IngestError
    with Store(tmp_path / "p.db") as store, pytest.raises(IngestError) as info:
        ingest_merchant_event(store, keystore.registry(), payload, subject_ref="maya-001",
                              now=datetime(2026, 10, 5, 10, 5, tzinfo=timezone.utc))
    assert "issuer" in info.value.reason


# --------------------------------------------------------------------------- the clock


def test_default_as_of_is_the_first_rfc_time(case1):
    assert format_utc(default_as_of(case1)) == RFC_A_TIME
    clock = CaseClock(case1)
    assert clock.as_of_text == RFC_A_TIME and clock.subject_ref == "maya-001"
    assert "gold" not in clock.case


def test_lookup_of_po_0001_01_is_visible_at_rfc_time_and_not_before(case1):
    clock = CaseClock(case1)
    [found] = clock.lookup(payout_reference="PO-0001-01")
    assert found["record_id"] == "rec-0001-021"
    assert clock.visible_lookup_records()[0]["event"]["record_id"] == "rec-0001-021"
    early = CaseClock(case1, as_of="2026-10-27T16:04:59Z")
    assert early.lookup(payout_reference="PO-0001-01") == []
    assert early.visible_lookup_records() == []
    exact = CaseClock(case1, as_of="2026-10-27T16:05:00Z")
    assert len(exact.lookup(payout_reference="PO-0001-01")) == 1


def test_case2_pending_payout_is_visible_only_from_its_availability():
    case2 = load_case(2)
    before = CaseClock(case2, as_of="2026-10-29T06:34:59Z")
    assert before.lookup(payout_reference="PO-0002-01") == []
    at = CaseClock(case2, as_of="2026-10-29T06:35:00Z")
    [payout] = at.lookup(payout_reference="PO-0002-01")
    assert payout["status"] == "pending" and payout["status_as_of"] == "2026-10-29T06:30:00Z"


def test_deliveries_after_as_of_are_hidden_and_advance_reveals_them(case1):
    clock = CaseClock(case1, as_of="2026-10-26T08:04:59Z")
    assert len(clock.visible_merchant_deliveries()) == 19
    assert len(clock.visible_bank_rows()) == 3
    clock.advance("2026-10-28T07:30:00Z")
    assert len(clock.visible_merchant_deliveries()) == 20
    assert len(clock.visible_bank_rows()) == 4
    newly = clock.visible_merchant_deliveries(since="2026-10-26T08:04:59Z")
    assert [d["event"]["record_id"] for d in newly] == ["rec-0001-020"]
    assert clock.visible_rfcs() == []
    clock.advance(RFC_A_TIME)
    assert [r["request_id"] for r in clock.visible_rfcs()] == ["rfc-0001-01"]


def test_clock_never_moves_backwards(case1):
    clock = CaseClock(case1)
    clock.advance(RFC_A_TIME)       # same moment is fine
    with pytest.raises(MappingError):
        clock.advance("2026-10-01T00:00:00Z")
    with pytest.raises(MappingError):
        CaseClock(case1, as_of="2026-11-02T09:00:00")   # naive time


def test_load_case_without_gold_hides_the_answer_key():
    case = load_case_without_gold(1)
    assert "gold" not in case and case["case_number"] == 1
    with pytest.raises(KeyError):
        load_case_without_gold(1000)
