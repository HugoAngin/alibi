"""Gold replayed as a prediction, for checking the scorer and showing the predictions shape.

    uv run --frozen python -m alibi.eval.oracle --out data/oracle.jsonl

This is not a system under test. For every request it writes the prediction gold describes:
the expected status and gap code per question, gold's facts, the first acceptable evidence
set as citations, the same refs attached as redacted extracts of non-sensitive fields, gold's
links, and a private exclusion list of everything else observable. The scorer must give it a
perfect score on every case; ``alibi/tests/test_eval_scorer.py`` asserts exactly that.
"""
from __future__ import annotations

import argparse
import copy
import sys
from pathlib import Path
from typing import Any

from alibi.eval.common import (SOURCE_TRUST, EvalError, active_version, load_dev_cases,
                               observable, parse_ref, requests_of, write_jsonl)

SAFE_FIELDS = {
    "bank": ("txn_id", "revision", "booked_at", "amount_minor", "currency", "payment_reference"),
    "merchant": ("record_id", "revision", "kind", "occurred_at", "amount_minor", "currency",
                 "status", "payout_reference", "settled_at", "settlement_batch_id",
                 "covers_record_ids", "related_record_id", "fee_type", "destination"),
}
SAFE_FIELDS["import"] = SAFE_FIELDS["merchant"]


def attachment_for(ref: str, versions: list[dict], trust: dict[str, str]) -> dict:
    """A redacted extract of the active version, disclosing only non-sensitive fields."""
    kind, _ident = parse_ref(ref)
    row = active_version(versions)
    fields = [f for f in SAFE_FIELDS[kind] if row.get(f) is not None]
    return {"ref": ref, "form": "redacted_extract", "fields": fields,
            "source_trust": trust.get(ref, SOURCE_TRUST[kind])}


def exclusion_for(ref: str, versions: list[dict]) -> dict:
    kind, _ident = parse_ref(ref)
    row = active_version(versions)
    if kind == "bank" and row.get("amount_minor", 0) < 0:
        reason = "unrelated_activity"
    elif row.get("kind") == "purchase":
        reason = "unrelated_activity"
    else:
        reason = "not_needed_for_question"
    return {"ref": ref, "reason": reason}


def predict(case: dict, request_id: str) -> dict:
    gold = case.get("gold")
    if not gold:
        raise EvalError(f"case {case.get('case_number')} has no gold to replay")
    rfc = next(r for r in requests_of(case) if r["request_id"] == request_id)
    answers = gold["answers"].get(request_id, {})
    seen = observable(case, rfc["issued_at"])
    trust = gold.get("source_trust") or {}
    claims: dict[str, Any] = {}
    disclosed: list[str] = []
    for qid, qgold in answers.items():
        sets = [s for s in qgold.get("evidence_sets") or [] if s]
        refs = list(sets[0]) if sets else []
        claims[qid] = {
            "status": qgold["status"],
            "gap": qgold.get("gap"),
            "facts": copy.deepcopy(qgold.get("facts") or {}),
            "evidence_refs": refs,
            "statement": f"Gold replay: {qid} is {qgold['status']}.",
            "rationale": "Derived from gold; cites one acceptable evidence set.",
        }
        for ref in refs:
            if ref not in disclosed:
                disclosed.append(ref)
    attachments = [attachment_for(ref, seen[ref], trust) for ref in disclosed if ref in seen]
    exclusions = [exclusion_for(ref, versions) for ref, versions in seen.items()
                  if ref not in disclosed]
    return {
        "case_number": case["case_number"],
        "request_id": request_id,
        "system": "oracle",
        "links": copy.deepcopy(gold.get("links") or []),
        "claims": claims,
        "package": {"request_id": request_id, "audience": rfc["audience"],
                    "empty": not attachments, "attachments": attachments},
        "exclusions": exclusions,
        "cost": {"model_calls": 0, "tool_calls": 0, "tokens": 0, "latency_ms": 0.0,
                 "cost_usd_estimate": 0.0},
        "retrieval": {"lookups": 0, "observed_inputs": 0, "strategy": "gold replay"},
        "failure": None,
    }


def predictions_for(cases: list[dict]) -> list[dict]:
    return [predict(case, rfc["request_id"]) for case in cases for rfc in requests_of(case)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, required=True, help="jsonl to write")
    parser.add_argument("--families", nargs="*", help="only these families")
    parser.add_argument("--exclude-demo", action="store_true")
    args = parser.parse_args(argv)
    try:
        cases = load_dev_cases(args.families, include_demo=not args.exclude_demo)
        rows = [{"system": {"name": "oracle", "note": "gold replay; not a system under test"}}]
        rows += predictions_for(cases)
        count = write_jsonl(args.out, rows) - 1
    except EvalError as exc:
        print(f"oracle: {exc}", file=sys.stderr)
        return 2
    print(f"oracle: wrote {count} predictions for {len(cases)} cases to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
