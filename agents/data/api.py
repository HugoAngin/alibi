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
            "duplicate_record", "missing_proof", "self_supplied_import")
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


def lookup(case: dict, at: str, *, payout_reference: str | None = None,
           record_id: str | None = None) -> list[dict]:
    """Simulated merchant follow-up retrieval: events matching a payout reference or record ID,
    from pushed deliveries and lookup-only records, available by `at`. Empty list if none."""
    if not (payout_reference or record_id):
        raise ValueError("give a payout_reference or a record_id")
    pool = [d["event"] for d in case["merchant_deliveries"] if d["received_at"] <= at]
    pool += [d["event"] for d in case["merchant_lookup"] if d["available_from"] <= at]
    hits, seen = [], set()
    for ev in pool:
        if ev["event_id"] in seen:
            continue
        if (payout_reference and ev.get("payout_reference") == payout_reference) or \
                (record_id and ev["record_id"] == record_id):
            hits.append(copy.deepcopy(ev))
            seen.add(ev["event_id"])
    return hits
