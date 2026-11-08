"""Data's public interface. Teammates import only this module.

    from agents.data.api import load_cases, load_case, canonical_case, known_at, without_gold

Cases are read from the committed JSON in agents/data/out/ (regenerate with
`uv run --frozen python -m agents.data.ledgers`). Every listing passes through
tools.lockbox.visible on `case_number`, so sealed cases (>= lockbox_from) never come back from
here. Data only ships development and demo cases below 1000.

Case layout (integer pence GBP, UTC ISO 8601 with Z; full detail in agents/data/README.md):
    case_number, family, variant, title, label, subject_ref
    merchant_deliveries: [{received_at, event}]          webhook pushes in arrival order
    merchant_lookup:     [{available_from, event}]       only via follow-up retrieval
    imports:             [{received_at, record}]         holder imports, tagged self_supplied
    bank_deliveries:     [{received_at, row}]            feed rows in arrival order
    rfcs:                [{request_id, audience, txn_ids | period, question_ids, questions, ...}]
    holder_claim:        {txn_id, explanation, payout_reference, stated_minor}
    gold:                development answer key; strip it with without_gold()
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

from tools.lockbox import visible

OUT = Path(__file__).resolve().parent / "out"
FAMILIES = ("demo", "clean", "bundled_payment", "balance_vs_settlement", "pending_correction",
            "duplicate_record", "missing_proof", "self_supplied_import", "hidden_reference",
            "injection", "alert",
            "second_platform")
CANONICAL_CASE = 1            # Maya, supported control
CONTRADICTION_CASE = 2        # Maya, payout still pending at booking


def load_index() -> list[dict]:
    """One summary row per visible case: number, family, variant, title, file, gold status."""
    import pandas as pd

    rows = json.loads((OUT / "index.json").read_text())["cases"]
    return visible(pd.DataFrame(rows), "case_number").to_dict("records")


def load_case(case_number: int) -> dict:
    """One case by number. Refuses numbers the lockbox hides."""
    match = [r for r in load_index() if r["case_number"] == case_number]
    if not match:
        raise KeyError(f"case {case_number} is not a visible development case")
    return json.loads((OUT / match[0]["file"]).read_text())


def load_cases(family: str | None = None) -> list[dict]:
    """All visible cases, optionally one family, in case-number order."""
    if family is not None and family not in FAMILIES:
        raise ValueError(f"unknown family {family!r}; expected one of {FAMILIES}")
    rows = [r for r in load_index() if family is None or r["family"] == family]
    return [json.loads((OUT / r["file"]).read_text()) for r in rows]


def canonical_case() -> dict:
    """Maya: sales 240000 - refunds 18000 - fees 6000 = credit 216000 pence (supported control)."""
    return load_case(CANONICAL_CASE)


def without_gold(case: dict) -> dict:
    """A copy of the case with the answer key removed, for feeding a system under test."""
    stripped = copy.deepcopy(case)
    stripped.pop("gold", None)
    return stripped


def known_at(case: dict, at: str, include_lookup: bool = False) -> dict:
    """Point-in-time view without gold: what was received or issued by `at`.

    Lookup-only records are left out unless include_lookup is True, because a system under
    test must earn them through retrieval (use lookup() as the simulated merchant endpoint).
    """
    view = without_gold(case)
    view["merchant_deliveries"] = [d for d in view["merchant_deliveries"] if d["received_at"] <= at]
    view["merchant_lookup"] = ([d for d in view["merchant_lookup"] if d["available_from"] <= at]
                               if include_lookup else [])
    view["imports"] = [d for d in view["imports"] if d["received_at"] <= at]
    view["bank_deliveries"] = [d for d in view["bank_deliveries"] if d["received_at"] <= at]
    view["rfcs"] = [r for r in view["rfcs"] if r["issued_at"] <= at]
    return view


SECOND_PLATFORM = "second-platform"
_SP_KIND = {"order_completed": "sale", "order_returned": "refund", "service_charge": "fee",
            "weekly_statement": "payout", "earnings_update": "notice"}
_SP_STATUS = {"paid": "settled", "scheduled": "pending", "failed": "failed"}


def to_contract(event: dict) -> dict:
    """Reference mapping of a merchant event onto the contract fields the pinned rules use.

    Mock-marketplace events already use them and come back unchanged (as a copy). Second-platform
    events map: order_completed/order_returned/service_charge -> sale/refund/fee with
    value_pence -> amount_minor; weekly_statement -> payout with net_payable_pence -> amount_minor,
    transfer_ref -> payout_reference, transfer_status paid|scheduled|failed -> settled|pending|failed,
    paid_at -> settled_at, line_record_ids -> covers_record_ids, destination "bank";
    earnings_update -> notice. The original fields are kept alongside."""
    ev = copy.deepcopy(event)
    if ev.get("issuer_id") != SECOND_PLATFORM:
        return ev
    kind = _SP_KIND.get(ev["kind"], ev["kind"])
    ev["source_kind"] = ev["kind"]
    ev["kind"] = kind
    if "currency_code" in ev:
        ev["currency"] = ev["currency_code"]
    if kind in ("sale", "refund", "fee"):
        ev["amount_minor"] = ev["value_pence"]
    if kind == "payout":
        ev.update(amount_minor=ev["net_payable_pence"], destination="bank",
                  payout_reference=ev["transfer_ref"], status=_SP_STATUS[ev["transfer_status"]],
                  settled_at=ev["paid_at"], covers_record_ids=ev["line_record_ids"])
    return ev


def lookup(case: dict, at: str, *, payout_reference: str | None = None,
           record_id: str | None = None, covers_record_id: str | None = None,
           payout_date: str | None = None) -> list[dict]:
    """Simulated merchant follow-up retrieval over pushed deliveries and lookup-only records
    available by `at`. Exactly one key per call; empty list if nothing matches.

    payout_reference  exact canonical reference, e.g. "PO-0041-02" (no fuzzy matching)
    record_id         one record by ID
    covers_record_id  payouts whose covers_record_ids include this sale, refund or fee record
    payout_date       payouts initiated or settled on this UTC date, "YYYY-MM-DD"
    """
    keys = [k for k in (payout_reference, record_id, covers_record_id, payout_date) if k]
    if len(keys) != 1:
        raise ValueError("give exactly one of payout_reference, record_id, covers_record_id, payout_date")
    pool = [d["event"] for d in case["merchant_deliveries"] if d["received_at"] <= at]
    pool += [d["event"] for d in case["merchant_lookup"] if d["available_from"] <= at]

    def match(raw: dict) -> bool:
        ev = to_contract(raw)
        if payout_reference:
            return payout_reference in (ev.get("payout_reference"), ev.get("statement_no"))
        if record_id:
            return ev["record_id"] == record_id
        if ev["kind"] != "payout":
            return False
        if covers_record_id:
            return covers_record_id in ev.get("covers_record_ids", [])
        return payout_date in (ev["occurred_at"][:10], (ev.get("settled_at") or "")[:10])

    hits, seen = [], set()
    for ev in pool:
        if ev["event_id"] in seen:
            continue
        if match(ev):
            hits.append(copy.deepcopy(ev))
            seen.add(ev["event_id"])
    return hits


def merchant_registry() -> dict:
    """The mock merchant registry (out/merchant_registry.json) that the lookup form reads."""
    return json.loads((OUT / "merchant_registry.json").read_text())


def merchant_lookup(query: str, checked_at: str) -> dict:
    """Mock registry check, in the shape alibi/app/UI.md gives `merchant_lookup.result`.
    Unknown names return status "unverified" with name None: unknown, not fraudulent."""
    reg = merchant_registry()

    def norm(s: str) -> str:
        return " ".join("".join(ch.lower() if ch.isalnum() else " " for ch in s).split())

    q = norm(query)
    hit = next((m for m in reg["merchants"]
                if q and q in [norm(m["name"])] + [norm(a) for a in m["aliases"]]), None)
    return {"query": query, "status": "verified" if hit else "unverified",
            "name": hit["name"] if hit else None, "registry": reg["registry"],
            "checked_at": checked_at,
            "notes": (f"Registered merchant, category: {hit['category']}." if hit else
                      "Not in the mock registry. Unverified means unknown to the registry, "
                      "not fraudulent.")}
