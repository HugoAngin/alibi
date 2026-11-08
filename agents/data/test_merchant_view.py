"""The marketplace view of case 1 must trace every value to the case file and the receipts."""
from __future__ import annotations

import json

from agents.data import api, merchant_view
from agents.data.receipts import OUT as RECEIPTS

VIEW = json.loads((merchant_view.OUT / "case_0001.json").read_text())
CASE = api.canonical_case()
EVENTS = {d["event"]["record_id"]: d["event"] for d in CASE["merchant_deliveries"] + CASE["merchant_lookup"]}


def test_committed_view_matches_build():
    assert VIEW == merchant_view.build(), "rebuild: uv run --frozen python -m agents.data.merchant_view"


def test_values_trace_to_case_1():
    for o in VIEW["orders"]:
        e, fee = EVENTS[o["record_id"]], EVENTS[o["fee_record_id"]]
        assert (e["kind"], e["amount_minor"], e["occurred_at"]) == ("sale", o["price_minor"], o["sold_at"])
        assert (fee["kind"], fee["related_record_id"], fee["amount_minor"]) == ("fee", o["record_id"], o["fee_minor"])
    for r in VIEW["refunds"]:
        e = EVENTS[r["record_id"]]
        assert (e["kind"], e["amount_minor"], e["occurred_at"]) == ("refund", r["amount_minor"], r["refunded_at"])
    [p] = VIEW["payouts"]
    e = EVENTS[p["record_id"]]
    assert (e["payout_reference"], e["amount_minor"], e["settled_at"]) == (p["payout_ref"], p["amount_minor"], p["settled_at"])
    assert VIEW["totals"]["net_minor"] == p["amount_minor"] == 216000
    t = CASE["gold"]["breakdown_minor"]
    assert (VIEW["totals"]["sales_minor"], VIEW["totals"]["refunds_minor"], VIEW["totals"]["fees_minor"]) == \
        (t["sales"], t["refunds"], t["fees"])
    [n] = VIEW["notifications"]
    assert n["body"] == EVENTS[n["record_id"]]["message"]
    [buy] = VIEW["purchases"]
    assert buy["sensitive"] and f"merchant:{buy['record_id']}" in CASE["gold"]["forbidden_refs"]


def test_refs_match_receipts():
    receipts = {r["links"]["record_id"]: r for r in json.loads((RECEIPTS / "receipts.json").read_text())["receipts"]
                if r["links"] and r["links"]["case_number"] == 1}
    for o in VIEW["orders"]:
        if o["record_id"] in receipts:
            r = receipts[o["record_id"]]
            assert (r["reference"], r["items"][0]["description"]) == (o["order_ref"], o["title"])
    for x in VIEW["refunds"]:
        if x["record_id"] in receipts:
            assert receipts[x["record_id"]]["reference"] == x["refund_ref"]
    assert len({o["order_ref"] for o in VIEW["orders"]}) == len(VIEW["orders"]) == 8
