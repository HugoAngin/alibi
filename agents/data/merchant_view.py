"""The marketplace's own view of Maya's seller account, built from case 1.

    uv run --frozen python -m agents.data.merchant_view

Writes agents/data/out/merchant_view/case_0001.json: what Mock Marketplace itself would show Maya on
its seller pages (orders, refunds, fees, payouts, notifications, her own purchases), with every
amount, time and ID taken from the case file, and display references and item titles taken from the
order-confirmation receipts where one exists. web/apps/merchant copies these values and tests
against this file. SYNTHETIC.
"""
from __future__ import annotations

import json
from pathlib import Path

from agents.data import api
from agents.data.receipts import CASE1_SALES

OUT = Path(__file__).resolve().parent / "out" / "merchant_view"

# Display refs and titles for the four sales that have no receipt image (rcpt-01..04 cover the rest).
EXTRA_SALES = {
    "rec-0001-008": ("MM-48066", "Cashmere jumper and silk scarf"),
    "rec-0001-010": ("MM-48081", "Pleated midi skirt"),
    "rec-0001-015": ("MM-48122", "Two cotton shirts"),
    "rec-0001-017": ("MM-48139", "Bundle: 3 winter knits"),
}
REFUND_REASONS = {"rec-0001-005": "Button missing on arrival (partial refund)",
                  "rec-0001-012": "Hem slightly damaged (partial refund)"}


def build() -> dict:
    case = api.canonical_case()
    events = {d["event"]["record_id"]: d["event"] for d in case["merchant_deliveries"]}
    lookups = {d["event"]["record_id"]: d["event"] for d in case["merchant_lookup"]}
    shown = {rec: (ref, title) for rec, ref, _, _, title in CASE1_SALES} | EXTRA_SALES
    fee_of = {e["related_record_id"]: e for e in events.values() if e["kind"] == "fee"}
    orders = []
    for e in sorted((e for e in events.values() if e["kind"] == "sale"), key=lambda e: e["occurred_at"]):
        ref, title = shown[e["record_id"]]
        fee = fee_of[e["record_id"]]
        orders.append({"order_ref": ref, "title": title, "sold_at": e["occurred_at"],
                       "price_minor": e["amount_minor"], "fee_minor": fee["amount_minor"],
                       "status": "Completed", "record_id": e["record_id"], "fee_record_id": fee["record_id"]})
    by_rec = {o["record_id"]: o for o in orders}
    refunds = [{"refund_ref": f"{by_rec[e['related_record_id']]['order_ref']}-R1",
                "order_ref": by_rec[e["related_record_id"]]["order_ref"], "refunded_at": e["occurred_at"],
                "amount_minor": e["amount_minor"], "reason": REFUND_REASONS[e["record_id"]],
                "status": "Refunded", "record_id": e["record_id"]}
               for e in sorted((e for e in events.values() if e["kind"] == "refund"), key=lambda e: e["occurred_at"])]
    release = next(e for e in events.values() if e["kind"] == "balance_release")
    payout = next(e for e in lookups.values() if e["kind"] == "payout")
    purchase = next(e for e in events.values() if e["kind"] == "purchase")
    totals = {"sales_minor": sum(o["price_minor"] for o in orders),
              "refunds_minor": sum(r["amount_minor"] for r in refunds),
              "fees_minor": sum(o["fee_minor"] for o in orders)}
    totals["net_minor"] = totals["sales_minor"] - totals["refunds_minor"] - totals["fees_minor"]
    return {
        "label": "SYNTHETIC - the marketplace's own view of case 1, for web/apps/merchant",
        "source": "agents/data/out/cases/case_0001.json, agents/data/out/receipts/receipts.json",
        "merchant": {"id": "mock-marketplace", "name": "Mock Marketplace"},
        "seller": {"name": "Maya", "subject_ref": case["subject_ref"], "bank_account": "ending 00"},
        "orders": orders,
        "refunds": refunds,
        "payouts": [{"payout_ref": payout["payout_reference"], "requested_at": payout["occurred_at"],
                     "amount_minor": payout["amount_minor"], "status": "Paid to bank",
                     "settled_at": payout["settled_at"], "destination": "Bank account ending 00",
                     "covers_order_refs": [o["order_ref"] for o in orders],
                     "record_id": payout["record_id"]}],
        "notifications": [{"sent_at": release["occurred_at"], "title": "Funds available",
                           "body": release["message"], "record_id": release["record_id"]}],
        "purchases": [{"bought_at": purchase["occurred_at"], "title": purchase["item"].capitalize(),
                       "amount_minor": purchase["amount_minor"], "category": purchase["category"],
                       "record_id": purchase["record_id"], "sensitive": True}],
        "totals": totals,
    }


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    view = build()
    (OUT / "case_0001.json").write_text(json.dumps(view, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(view["totals"]))


if __name__ == "__main__":
    main()
