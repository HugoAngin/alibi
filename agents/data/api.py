"""Data's public interface. Teammates import only this module.

    from agents.data.api import load_cases, load_case, canonical_case, FAMILIES

Cases are read from the committed JSON in agents/data/out/ (regenerate with
`uv run --frozen python -m agents.data.ledgers`). Every listing passes through
tools.lockbox.visible on `case_number`, so sealed cases (>= lockbox_from) never come back
from here. Data only ships development cases below 1000 anyway.

Case layout (all amounts integer pence GBP, all times UTC ISO 8601 with Z):
    case_number, family, variant, title, label, subject_ref
    merchant_deliveries: [{received_at, event}]   in arrival order, duplicates included
    bank_deliveries:     [{received_at, row}]     in arrival order, duplicates included
    rfcs:                [{request_id, parent_request_id, audience, subject_ref, txn_ids,
                           period, question_ids, issued_at, expires_at}]
    holder_claim:        {txn_id, explanation, stated_minor: {sales, refunds, fees, net}}
    gold:                {links, active_revisions, breakdown_minor, claim_status, ingest, notes}

`gold` is the development answer key. Strip it with `without_gold` before handing a case to
an agent under test. Field-level detail is in agents/data/README.md.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

from tools.lockbox import visible

OUT = Path(__file__).resolve().parent / "out"
FAMILIES = ("clean", "bundled_payment", "balance_vs_settlement", "pending_correction",
            "duplicate_record", "missing_proof")
CANONICAL_CASE = 1


def load_index() -> list[dict]:
    """One summary row per visible case: number, family, variant, title, file, gold status."""
    import pandas as pd

    rows = json.loads((OUT / "index.json").read_text())["cases"]
    frame = visible(pd.DataFrame(rows), "case_number")
    return frame.to_dict("records")


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
    """Maya: sales 240000 - refunds 18000 - fees 6000 = credit 216000 pence."""
    return load_case(CANONICAL_CASE)


def without_gold(case: dict) -> dict:
    """A copy of the case with the answer key removed, for feeding a system under test."""
    stripped = copy.deepcopy(case)
    stripped.pop("gold", None)
    return stripped


def known_at(case: dict, at: str) -> dict:
    """Point-in-time view: only deliveries received at or before `at` (UTC ISO with Z)."""
    view = without_gold(case)
    view["merchant_deliveries"] = [d for d in view["merchant_deliveries"] if d["received_at"] <= at]
    view["bank_deliveries"] = [d for d in view["bank_deliveries"] if d["received_at"] <= at]
    view["rfcs"] = [r for r in view["rfcs"] if r["issued_at"] <= at]
    return view
