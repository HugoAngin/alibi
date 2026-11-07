from agents.data.api import canonical_case, without_gold
from agents.eval.api import build_analyst_preview, build_mock_bank, build_mock_merchant


def test_canonical_connectors_keep_lookup_only_payout_out_of_pushes():
    case = canonical_case()
    merchant = build_mock_merchant(case)
    bank = build_mock_bank(case)
    payout, = [entry["event"] for entry in merchant["merchant_lookup"]]
    assert payout["record_id"] == "rec-0001-021"
    assert payout["kind"] == "payout" and payout["status"] == "settled" and payout["settled_at"]
    assert payout["record_id"] not in {entry["event"]["record_id"] for entry in merchant["merchant_deliveries"]}
    assert any(entry["event"]["kind"] == "balance_release" for entry in merchant["merchant_deliveries"])
    assert [(rfc["request_id"], rfc["audience"]) for rfc in bank["rfcs"]] == [
        ("rfc-0001-01", "bank_a"), ("rfc-0001-02", "bank_b"),
    ]
    merchant["merchant_lookup"].clear()
    bank["rfcs"].clear()
    assert case["merchant_lookup"] and case["rfcs"]


def test_analyst_preview_is_rederived_from_canonical_evidence_without_gold():
    preview = build_analyst_preview(without_gold(canonical_case()))
    assert preview["package"]["request_id"] == "rfc-0001-01"
    assert preview["package"]["txn_ids"] == ["txn-0001-004"]
    assert preview["reconciliation"]["expression"] == "240000 - 18000 - 6000 = 216000"
    assert preview["authentication"]["status"] == "preview"
    assert not preview["authentication"]["checks"]
    assert {item["form"] for item in preview["source_trust"]} == {"redacted_extract"}
    assert {item["signature_check"] for item in preview["source_trust"]} == {"not_checked"}
    source, breakdown = preview["evidence_by_question"]
    assert {item["record_id"] for item in source["evidence"]} == {"txn-0001-004", "rec-0001-021"}
    assert source["timing"]["payout_ref"] == "PO-0001-01"
    assert source["timing"]["released_to_balance_at"] < source["timing"]["bank_settled_at"]
    assert source["timing"]["settled_before_booking"]
    assert breakdown["calculation"]["matches"]
