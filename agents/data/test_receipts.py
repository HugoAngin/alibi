"""Checks the synthetic receipt images against their true fields and the cases they cite.

    uv run --frozen python -m pytest agents/data/test_receipts.py
"""
from __future__ import annotations

import json

import pytest
from PIL import Image

from agents.data import api
from agents.data.receipts import OUT

MANIFEST = json.loads((OUT / "receipts.json").read_text())
RECEIPTS = MANIFEST["receipts"]


def test_manifest():
    assert MANIFEST["label"].startswith("SYNTHETIC") and MANIFEST["count"] == len(RECEIPTS)
    assert len({r["id"] for r in RECEIPTS}) == len(RECEIPTS)
    layouts = {r["layout"] for r in RECEIPTS}
    assert {"handwritten", "printed_letter", "thermal_print", "web_screenshot"} <= layouts
    assert any(r["distortion"] == "photo_at_angle" for r in RECEIPTS)


@pytest.mark.parametrize("r", RECEIPTS, ids=lambda r: r["id"])
def test_image_and_fields(r):
    path = OUT / r["file"]
    assert path.exists() and path.stat().st_size < 1_000_000
    with Image.open(path) as img:
        assert img.format == "PNG" and img.width >= 400 and img.height >= 300
    assert isinstance(r["total_minor"], int) and r["currency"] == "GBP"
    if r["kind"] in ("payout_confirmation", "weekly_statement") and len(r["items"]) > 1:
        assert sum(i["amount_minor"] for i in r["items"]) == r["total_minor"]
    if not r["links"]:
        return
    case = api.load_case(r["links"]["case_number"])
    events = [d["event"] for d in case["merchant_deliveries"]] + [d["event"] for d in case["merchant_lookup"]]
    ev = next(e for e in events if e["record_id"] == r["links"]["record_id"])
    amount = ev.get("amount_minor", ev.get("value_pence", ev.get("net_payable_pence")))
    assert amount == r["total_minor"]
    if r["document_date"]:
        dates = {ev["occurred_at"][:10], (ev.get("settled_at") or "")[:10], (ev.get("paid_at") or "")[:10]}
        assert r["document_date"] in dates
    if r["reference"] and ev.get("payout_reference"):
        assert r["reference"] == ev["payout_reference"]


def test_sensitive_receipt_is_forbidden_in_its_case():
    r = next(r for r in RECEIPTS if r["id"] == "rcpt-08")
    case = api.load_case(r["links"]["case_number"])
    assert f"merchant:{r['links']['record_id']}" in case["gold"]["forbidden_refs"]
