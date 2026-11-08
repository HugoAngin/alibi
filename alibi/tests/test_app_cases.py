"""The case adapter loads data's cases through the real ingest paths and the rules engine
reaches the expected verdicts on the two demo cases. No network, no Gemini, temp dirs only."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agents.data.api import load_case
from alibi import api
from alibi.app import cases
from alibi.app.config import APP_KEYS
from alibi.core.schemas import MerchantEvent, RequestForContext
from alibi.core.signing import DemoKeystore

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def app_keystore(tmp_path):
    return DemoKeystore(tmp_path / "keys", specs=APP_KEYS).ensure()


def _event(case, record_id):
    pool = [d["event"] for d in case["merchant_deliveries"]] + [d["event"] for d in case["merchant_lookup"]]
    return next(e for e in pool if e["record_id"] == record_id)


def test_kind_mapping_keeps_release_and_payout_as_two_records():
    case = load_case(1)
    release = cases.to_merchant_event(_event(case, "rec-0001-020"))
    payout = cases.to_merchant_event(_event(case, "rec-0001-021"))
    assert release["record_id"] != payout["record_id"]
    assert release["kind"] == "payout" and release["status"] == "released_to_balance"
    assert release["payment_ref"] == "PO-0001-01"          # the withdrawal the message names
    assert release["text"].startswith("Good news!")
    assert payout["kind"] == "settlement" and payout["status"] == "settled"
    assert payout["payment_ref"] == "PO-0001-01"
    assert payout["effective_at"] == "2026-10-27T16:00:00Z"
    assert payout["amount_pence"] == 216000 and payout["currency"] == "GBP"
    assert set(payout["related_record_ids"]) == set(_event(case, "rec-0001-021")["covers_record_ids"])
    for mapped in (release, payout):
        MerchantEvent.model_validate(mapped)   # strict schema, extra fields rejected


def test_sale_fee_refund_and_purchase_map_to_core_kinds():
    case = load_case(1)
    sale = cases.to_merchant_event(_event(case, "rec-0001-001"))
    fee = cases.to_merchant_event(_event(case, "rec-0001-002"))
    refund = cases.to_merchant_event(_event(case, "rec-0001-005"))
    purchase = cases.to_merchant_event(_event(case, "rec-0001-019"))
    assert sale["kind"] == "order" and sale["items"] == [{"description": "second-hand clothing lot 1"}]
    assert fee["kind"] == "fee" and fee["related_record_ids"] == ["rec-0001-001"]
    assert refund["kind"] == "refund" and refund["status"] == "completed"
    assert purchase["kind"] == "receipt" and purchase["items"][0]["category"] == "health"
    for mapped in (sale, fee, refund, purchase):
        MerchantEvent.model_validate(mapped)


def test_pending_payout_observation_time_is_status_as_of():
    case = load_case(2)
    pending = cases.to_merchant_event(_event(case, "rec-0002-021"))
    assert pending["kind"] == "settlement" and pending["status"] == "pending"
    assert pending["occurred_at"] == "2026-10-29T06:30:00Z"
    assert "effective_at" not in pending


def test_bank_row_and_rfc_mapping():
    case = load_case(1)
    row = next(d for d in case["bank_deliveries"] if d["row"]["txn_id"] == "txn-0001-004")
    txn = cases.to_bank_txn(row["row"], subject_ref="maya-001", connection_id="conn-bank-a-maya-001",
                            received_at=row["received_at"])
    assert txn["amount_pence"] == 216000 and txn["refs"] == ["PO-0001-01"]
    assert "Mock Marketplace Payments" in txn["description"]
    rfc_a = cases.to_rfc(case["rfcs"][0])
    rfc_b = cases.to_rfc(case["rfcs"][1])
    assert rfc_a["issuer"] == "bank_a" and rfc_a["txn_ids"] == ["txn-0001-004"] and "period" not in rfc_a
    assert rfc_b["issuer"] == "bank_b" and "txn_ids" not in rfc_b and rfc_b["period"]["from"] == "2026-10-01T00:00:00Z"
    RequestForContext.model_validate(rfc_a)
    RequestForContext.model_validate(rfc_b)


def test_load_is_idempotent_and_keeps_lookup_records_out(tmp_path, app_keystore):
    case = load_case(1)
    store = api.open_store(tmp_path / "c1.db")
    first = cases.load_case(store, case, app_keystore)
    assert first["skipped"] == []
    assert len(first["records"]) == 20 and len(first["txns"]) == 4
    assert first["requests"] == ["rfc-0001-01", "rfc-0001-02"]
    assert "rec-0001-021" not in first["records"]      # lookup-only, not pushed
    second = cases.load_case(store, case, app_keystore)
    assert set(second["statuses"].values()) == {"duplicate"}
    record = store.get_source("rec-0001-020")
    assert record.received_at.isoformat().startswith("2026-10-26T08:05")   # each at its own received_at
    store.close()


@pytest.mark.parametrize("case_number,expected", [(1, "supported"), (2, "contradicted")])
def test_rules_verdict_on_demo_cases(tmp_path, app_keystore, case_number, expected):
    case = load_case(case_number)
    store = api.open_store(tmp_path / f"c{case_number}.db")
    loaded = cases.load_case(store, case, app_keystore)
    lookup = cases.settlement_lookup_for(case, app_keystore, lambda: NOW)
    result = api.run_investigation(store, loaded["requests"][0], mode="rules", settlement_lookup=lookup,
                                   registry=app_keystore.registry(), now=NOW)
    by_q = {c.question_id: c for c in result.claims}
    assert by_q["q.source_of_funds"].status.value == expected
    lookups = [t for t in result.trace if t.tool == "lookup_settlement" and t.step == "tool_call"]
    assert lookups and lookups[0].result["found"] is True
    settlement = store.get_source(f"rec-000{case_number}-021")
    assert settlement.connector == "merchant_lookup"
    if expected == "supported":
        calc = by_q["q.source_of_funds"].calculation
        assert calc.total_pence == 216000 and calc.balanced is True
    store.close()


def test_settlement_lookup_respects_merchant_availability(tmp_path, app_keystore):
    case = load_case(1)
    early = datetime(2026, 10, 27, 0, 0, tzinfo=timezone.utc)   # before the payout exists
    assert cases.settlement_lookup_for(case, app_keystore, lambda: early)("PO-0001-01") is None
    found = cases.settlement_lookup_for(case, app_keystore, lambda: NOW)("PO-0001-01")
    assert found["event"]["kind"] == "settlement" and found["signature"]["kid"] == "mock_marketplace"
    assert cases.settlement_lookup_for(case, app_keystore, lambda: NOW)("PO-9999-99") is None
