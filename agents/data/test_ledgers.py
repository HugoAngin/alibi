"""Checks the development ledgers: determinism, contracts, and gold re-derived independently.

    uv run --frozen python -m pytest agents/data

The reference replay below re-derives each case's gold from its deliveries alone, using the
rules documented in agents/data/README.md, so a slip in the generator shows up here.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from agents.data import api, ledgers

CASES = api.load_cases()


def ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def replay(case: dict):
    """Ingest deliveries by the contract rules. Returns (outcomes, active events by record)."""
    seen: dict[str, str] = {}            # event_id -> canonical bytes
    active: dict[str, dict] = {}         # record_id -> active event
    outcomes = []
    for i, d in enumerate(case["merchant_deliveries"]):
        ev = d["event"]
        raw = json.dumps(ev, sort_keys=True)
        if ev["event_id"] in seen:
            outcome = "duplicate_ignored" if seen[ev["event_id"]] == raw else "rejected_conflict"
        else:
            cur = active.get(ev["record_id"])
            if cur is not None and ev["revision"] <= cur["revision"]:
                outcome = "stale_revision_ignored"
            else:
                outcome = "accepted"
                active[ev["record_id"]] = ev
            seen[ev["event_id"]] = raw
        outcomes.append(("merchant", i, outcome))
    rows: dict[str, str] = {}
    bank = []
    for i, d in enumerate(case["bank_deliveries"]):
        row = d["row"]
        raw = json.dumps(row, sort_keys=True)
        if row["txn_id"] in rows:
            outcome = "duplicate_ignored" if rows[row["txn_id"]] == raw else "rejected_conflict"
        else:
            outcome = "accepted"
            rows[row["txn_id"]] = raw
            bank.append(row)
        outcomes.append(("bank", i, outcome))
    return outcomes, active, bank


def candidates(credit: dict, active: dict) -> list[list[dict]]:
    """Single payouts or whole settlement batches matching the credit by the gold rule."""
    booked = ts(credit["booked_at"])
    payouts = [e for e in active.values() if e["kind"] == "payout" and e["destination"] == "bank"]
    groups: dict[str, list[dict]] = {}
    singles = []
    for p in payouts:
        (groups.setdefault(p["settlement_batch_id"], []) if p.get("settlement_batch_id")
         else singles).append(p)
    out = []
    for group in [[p] for p in singles] + list(groups.values()):
        last = max(ts(p["occurred_at"]) for p in group)
        if (sum(p["amount_minor"] for p in group) == credit["amount_minor"]
                and last <= booked and booked - last <= timedelta(days=ledgers.SETTLEMENT_WINDOW_DAYS)):
            out.append(group)
    return out


@pytest.mark.parametrize("case", CASES, ids=lambda c: f"case{c['case_number']}")
def test_gold_rederived(case):
    outcomes, active, bank = replay(case)
    gold = case["gold"]
    assert [(g["connector"], g["index"], g["outcome"]) for g in gold["ingest"]] == outcomes
    assert {r: e["revision"] for r, e in active.items()} == gold["active_revisions"]
    by_txn = {r["txn_id"]: r for r in bank}
    for link in gold["links"]:
        credit = by_txn[link["txn_id"]]
        cands = candidates(credit, active)
        state = {0: "unmatched", 1: "linked"}.get(len(cands), "ambiguous")
        assert state == link["state"]
        if state == "linked":
            assert sorted(p["record_id"] for p in cands[0]) == sorted(link["payout_record_ids"])
    stated = case["holder_claim"]["stated_minor"]
    if gold["breakdown_minor"] is None:
        assert gold["claim_status"] == "unresolved"
        return
    # Arithmetic over the active revisions the linked payouts cover; every cited record present.
    link = gold["links"][0]
    covered = [rid for pid in link["payout_record_ids"] for rid in active[pid]["covers_record_ids"]]
    assert all(rid in active for rid in covered)
    totals = {"sales": 0, "refunds": 0, "fees": 0}
    key = {"sale": "sales", "refund": "refunds", "fee": "fees"}
    for rid in covered:
        totals[key[active[rid]["kind"]]] += active[rid]["amount_minor"]
    totals["net"] = totals["sales"] - totals["refunds"] - totals["fees"]
    assert totals == gold["breakdown_minor"]
    assert totals["net"] == by_txn[link["txn_id"]]["amount_minor"]
    expect = "supported" if stated == totals else "contradicted"
    assert gold["claim_status"] == expect


@pytest.mark.parametrize("case", CASES, ids=lambda c: f"case{c['case_number']}")
def test_contract_shapes(case):
    assert 1 <= case["case_number"] < 1000
    for d in case["merchant_deliveries"]:
        ev = d["event"]
        assert isinstance(ev["amount_minor"], int) and ev["amount_minor"] > 0
        assert ev["currency"] == "GBP" and ev["occurred_at"].endswith("Z")
        assert ts(d["received_at"]) >= ts(ev["occurred_at"])
        for k in ("schema_version", "event_id", "record_id", "revision", "issuer_id",
                  "subject_ref", "kind", "occurred_at"):
            assert k in ev
    for d in case["bank_deliveries"]:
        row = d["row"]
        assert isinstance(row["amount_minor"], int) and row["currency"] == "GBP"
        assert ts(d["received_at"]) >= ts(row["booked_at"])
    for r in case["rfcs"]:
        assert r["audience"] in ("bank_a", "bank_b") and ts(r["expires_at"]) > ts(r["issued_at"])
    assert case["gold"]["claim_status"] in ("supported", "contradicted", "unresolved")
    for link in case["gold"]["links"]:
        assert link["state"] in ("unmatched", "ambiguous", "linked")


@pytest.mark.parametrize("case", CASES, ids=lambda c: f"case{c['case_number']}")
def test_evidence_known_before_rfc(case):
    """Everything the gold relies on was received before the first RFC was issued."""
    issued = case["rfcs"][0]["issued_at"]
    view = api.known_at(case, issued)
    full_outcomes, full_active, _ = replay(case)
    _, early_active, early_bank = replay(view)
    for link in case["gold"]["links"]:
        assert link["txn_id"] in {r["txn_id"] for r in early_bank}
        for pid in link["payout_record_ids"]:
            assert early_active[pid] == full_active[pid]
            for rid in full_active[pid]["covers_record_ids"]:
                if rid in full_active:
                    assert early_active[rid] == full_active[rid]


def test_committed_json_matches_generator():
    built = ledgers.build_cases()
    assert [c["case_number"] for c in built] == list(range(1, len(built) + 1))
    assert built == CASES, "out/ is stale: run uv run --frozen python -m agents.data.ledgers"


def test_canonical_fixture():
    case = api.canonical_case()
    assert case["gold"]["breakdown_minor"] == {"sales": 240000, "refunds": 18000,
                                               "fees": 6000, "net": 216000}
    assert [r["audience"] for r in case["rfcs"]] == ["bank_a", "bank_b"]


def test_every_family_and_state_present():
    assert {c["family"] for c in CASES} == set(api.FAMILIES)
    assert {c["gold"]["claim_status"] for c in CASES} == {"supported", "contradicted", "unresolved"}
    assert {l["state"] for c in CASES for l in c["gold"]["links"]} == {"linked", "unmatched", "ambiguous"}


def test_without_gold_and_unknown_case():
    assert "gold" not in api.without_gold(CASES[0]) and "gold" in CASES[0]
    with pytest.raises(KeyError):
        api.load_case(1000)
