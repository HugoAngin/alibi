"""The pinned gold rules as a system under test, with one fixed retrieval strategy.

    uv run --frozen python -m alibi.eval.rules_reference                      # write and score
    uv run --frozen python -m alibi.eval.rules_reference --out data/rules_reference.jsonl --no-score
    uv run --frozen python -m alibi.eval.rules_reference --families clean

For every request it takes ``agents.data.api.known_at(case, issued_at)`` (pushed records only),
replays the deliveries with idempotency by connector and event ID and the revision rule, then
applies the fixed retrieval strategy "look up any payout reference named in a balance_release
message" through ``agents.data.api.lookup``. It links the credit with the pinned candidate rule
(one settled payout or one whole settlement batch whose total equals the credit, settled at
most 5 days before booking and never after, reference equal to the bank payment reference when
present), computes the breakdown from the active revisions the payouts cover, derives the claim
status and the per-question answers with gap codes, and emits a prediction in the shape of
``alibi/eval/predictions.md``: claims, package (redacted extracts of the cited records), a
private exclusion list and cost and retrieval counts.

This is the rules baseline's expected behaviour and a scorer sanity check: it must be
safe-complete on every clean case. It is not Alibi: it reads no free text beyond the reference
tokens in release messages and makes no model calls.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

from agents.data import api as data_api
from alibi.eval import scorer
from alibi.eval.common import (Q_BREAKDOWN, Q_PERIOD, Q_SOURCE, SOURCE_TRUST, EvalError,
                               load_dev_cases, parse_time, write_jsonl)
from alibi.eval.oracle import SAFE_FIELDS

SYSTEM = "rules_reference"
STRATEGY = "look up any payout reference named in a balance_release message"
SETTLEMENT_WINDOW = timedelta(days=5)
REFERENCE = re.compile(r"\b([A-Z]{2,8}-[0-9A-Z]{2,}(?:-[0-9A-Z]{2,})*)\b")
KEY = {"sale": "sales", "refund": "refunds", "fee": "fees"}
DEFAULT_OUT = Path("data") / "rules_reference.jsonl"


def ts(text: str):
    return parse_time(text)


# --------------------------------------------------------------------------- ingest and retrieval


def ingest(entries: list[dict], key: str, ident: str, connector: str, active: dict,
           seen: dict) -> int:
    """Replay deliveries in arrival order. Idempotency is by connector and ID: identical bytes
    are a duplicate and different bytes a rejected conflict, so the original stands either way.
    A revision at or below the active one is stale. Returns the number of records read."""
    for delivery in entries:
        obj = delivery[key]
        mark = (connector, obj[ident])
        if mark in seen:
            continue
        seen[mark] = json.dumps(obj, sort_keys=True)
        current = active.get(obj["record_id"])
        if current is None or obj["revision"] > current[0]["revision"]:
            active[obj["record_id"]] = (obj, connector)
    return len(entries)


def replay(view: dict) -> tuple[dict[str, tuple[dict, str]], dict[str, dict], int]:
    """Active merchant and import records by record_id, bank rows by txn_id, records read."""
    active: dict[str, tuple[dict, str]] = {}
    seen: dict = {}
    read = ingest(view["merchant_deliveries"], "event", "event_id", "merchant", active, seen)
    read += ingest(view["imports"], "record", "event_id", "import", active, seen)
    bank: dict[str, dict] = {}
    for delivery in view["bank_deliveries"]:
        bank.setdefault(delivery["row"]["txn_id"], delivery["row"])
    read += len(view["bank_deliveries"])
    return active, bank, read


def references_in(message: str, exclude: set[str | None]) -> list[str]:
    """Reference-like tokens (letters, hyphen, digits) in a free-text message."""
    out: list[str] = []
    for token in REFERENCE.findall(message or ""):
        if token not in exclude and token not in out:
            out.append(token)
    return out


def retrieve(case: dict, at: str, active: dict) -> tuple[int, int, list[str]]:
    """The fixed strategy: one lookup per payout reference named in a balance_release message.
    Fetched records join the active set under the revision rule. Returns lookups, records
    read and the references tried."""
    lookups = hits = 0
    tried: list[str] = []
    releases = [event for event, _ in active.values() if event["kind"] == "balance_release"]
    for release in releases:
        for ref in references_in(release.get("message", ""), {release.get("release_reference")}):
            if ref in tried:
                continue
            tried.append(ref)
            lookups += 1
            for event in data_api.lookup(case, at, payout_reference=ref):
                hits += 1
                current = active.get(event["record_id"])
                if current is None or event["revision"] > current[0]["revision"]:
                    active[event["record_id"]] = (event, "merchant")
    return lookups, hits, tried


# --------------------------------------------------------------------------- gold rules


def candidates(credit: dict, active: dict) -> list[list[dict]]:
    """One settled payout, or one whole settlement batch, whose total equals the credit, settled
    at most 5 days before booking and never after, with the bank reference when present."""
    booked = ts(credit["booked_at"])
    payouts = [e for e, _ in active.values() if e["kind"] == "payout"
               and e.get("destination") == "bank" and e.get("status") == "settled"]
    batches: dict[str, list[dict]] = {}
    groups: list[tuple[str | None, list[dict]]] = []
    for payout in payouts:
        if payout.get("settlement_batch_id"):
            batches.setdefault(payout["settlement_batch_id"], []).append(payout)
        else:
            groups.append((payout.get("payout_reference"), [payout]))
    groups += list(batches.items())
    out = []
    for reference, group in groups:
        last = max(ts(p["settled_at"]) for p in group)
        if (sum(p["amount_minor"] for p in group) == credit["amount_minor"]
                and last <= booked <= last + SETTLEMENT_WINDOW
                and credit.get("payment_reference") in (None, reference)):
            out.append(group)
    return out


def breakdown_for(payouts: list[dict], active: dict) -> tuple[dict | None, list[str], list[str]]:
    """Sales, refunds and fees from the active revisions the payouts cover; None if any is missing."""
    covered = [r for p in payouts for r in p.get("covers_record_ids", [])]
    missing = [r for r in covered if r not in active]
    if missing:
        return None, covered, missing
    total = {"sales": 0, "refunds": 0, "fees": 0}
    for record_id in covered:
        event = active[record_id][0]
        bucket = KEY.get(event.get("kind"))
        if bucket is None:
            return None, covered, [record_id]
        total[bucket] += event["amount_minor"]
    total["net"] = total["sales"] - total["refunds"] - total["fees"]
    return total, covered, []


def claim_status(stated: dict, breakdown: dict | None, cited_reference: str | None, credit: dict,
                 active: dict) -> str:
    if breakdown is not None:
        return "supported" if stated == breakdown else "contradicted"
    cited = [e for e, _ in active.values() if e["kind"] == "payout" and cited_reference
             and e.get("payout_reference") == cited_reference]
    conflicts = any(e.get("status") != "settled" or ts(e["settled_at"]) > ts(credit["booked_at"])
                    for e in cited)
    return "contradicted" if conflicts else "unresolved"


def gap_for(state: str, credit: dict, cited_reference: str | None, active: dict) -> str:
    """Why the credit is not explained, as one of the nine pinned gap codes."""
    if state == "ambiguous":
        return "ambiguous_candidates"
    if not active:
        return "no_merchant_records"
    payouts = [e for e, _ in active.values() if e["kind"] == "payout"]
    names = {r for r in (credit.get("payment_reference"), cited_reference) if r}
    referenced = [p for p in payouts
                  if p.get("payout_reference") in names or p.get("settlement_batch_id") in names]
    related = referenced + [p for p in payouts
                            if p["amount_minor"] == credit["amount_minor"] and p not in referenced]
    booked = ts(credit["booked_at"])
    if any(p.get("status") == "failed" for p in related):
        return "settlement_failed"
    if any(p.get("status") == "pending" for p in related):
        return "settlement_pending_at_booking"
    if any(p.get("status") == "settled" and ts(p["settled_at"]) > booked for p in related):
        return "settlement_after_credit"
    if any(p["amount_minor"] != credit["amount_minor"] for p in referenced):
        return "amount_mismatch"
    if any(e["kind"] == "balance_release" and e["amount_minor"] == credit["amount_minor"]
           for e, _ in active.values()):
        return "balance_release_not_settlement"
    return "settlement_not_found"


def ref_of(record_id: str, active: dict) -> str:
    connector = active[record_id][1]
    return f"{'import' if connector == 'import' else 'merchant'}:{record_id}"


# --------------------------------------------------------------------------- answers


def claim(status: str, gap: str | None, facts: dict, refs: list[str], statement: str) -> dict:
    return {"status": status, "gap": gap, "facts": facts, "evidence_refs": refs,
            "statement": statement, "rationale": f"Rules: {statement}"}


def period_claim(rfc: dict, active: dict) -> dict:
    if not active:
        return claim("unresolved", "no_merchant_records", {}, [], "no merchant records on file")
    lo, hi = ts(rfc["period"]["from"]), ts(rfc["period"]["to"])
    inside = [e for e, _ in active.values() if e.get("kind") in ("sale", "refund")
              and lo <= ts(e["occurred_at"]) < hi]
    sales = [e for e in inside if e["kind"] == "sale"]
    refunds = [e for e in inside if e["kind"] == "refund"]
    facts = {"sales_minor": sum(e["amount_minor"] for e in sales),
             "refunds_minor": sum(e["amount_minor"] for e in refunds),
             "sale_count": len(sales), "refund_count": len(refunds)}
    return claim("supported", None, facts, [ref_of(e["record_id"], active) for e in inside],
                 f"{len(sales)} sales and {len(refunds)} refunds in the period, from active revisions")


def extract(ref: str, active: dict, bank: dict) -> dict:
    kind, ident = ref.split(":", 1)
    row = bank[ident] if kind == "bank" else active[ident][0]
    fields = [f for f in SAFE_FIELDS[kind] if row.get(f) is not None]
    return {"ref": ref, "form": "redacted_extract", "fields": fields, "source_trust": SOURCE_TRUST[kind]}


def exclusions_for(disclosed: set[str], active: dict, bank: dict) -> list[dict]:
    out = []
    for txn_id, row in bank.items():
        ref = f"bank:{txn_id}"
        if ref not in disclosed:
            out.append({"ref": ref, "reason": "unrelated_activity" if row["amount_minor"] < 0
                        else "not_needed_for_question"})
    for record_id, (event, _connector) in active.items():
        ref = ref_of(record_id, active)
        if ref not in disclosed:
            out.append({"ref": ref, "reason": "unrelated_activity" if event.get("kind") == "purchase"
                        else "not_needed_for_question"})
    return out


def answer_request(case: dict, rfc: dict) -> dict:
    started = time.perf_counter()
    at = rfc["issued_at"]
    view = data_api.known_at(case, at)
    active, bank, observed = replay(view)
    lookups, hits, _tried = retrieve(case, at, active)
    observed += hits
    holder = case["holder_claim"]
    credit = bank.get(holder["txn_id"])
    prediction: dict[str, Any] = {
        "case_number": case["case_number"], "request_id": rfc["request_id"], "system": SYSTEM,
        "links": [], "claims": {}, "package": None, "exclusions": [],
        "cost": {"model_calls": 0, "tool_calls": lookups, "tokens": 0, "latency_ms": 0.0,
                 "cost_usd_estimate": 0.0},
        "retrieval": {"lookups": lookups, "observed_inputs": observed, "strategy": STRATEGY},
        "failure": None,
    }
    if credit is None:
        prediction["failure"] = {"type": "error",
                                 "detail": f"{holder['txn_id']} is not in the bank feed at {at}"}
        return prediction
    cands = candidates(credit, active)
    state = {0: "unmatched", 1: "linked"}.get(len(cands), "ambiguous")
    payouts = cands[0] if state == "linked" else []
    breakdown, covered, missing = breakdown_for(payouts, active) if payouts else (None, [], [])
    status = claim_status(holder["stated_minor"], breakdown, holder.get("payout_reference"), credit, active)
    prediction["links"] = [{"txn_id": credit["txn_id"], "state": state,
                            "payout_record_ids": [p["record_id"] for p in payouts],
                            "candidate_record_ids": sorted(p["record_id"] for g in cands for p in g),
                            "method": "reference_amount_date"}]
    txn = f"bank:{credit['txn_id']}"
    po_refs = [ref_of(p["record_id"], active) for p in payouts]
    gap = None if state == "linked" else gap_for(state, credit, holder.get("payout_reference"), active)
    claims: dict[str, dict] = {}
    for qid in rfc["question_ids"]:
        if qid == Q_SOURCE:
            if state == "linked":
                facts = {"payout_references": [p.get("payout_reference") for p in payouts],
                         "settled_at": [p.get("settled_at") for p in payouts]}
                claims[qid] = claim("supported", None, facts, [txn] + po_refs,
                                    f"credit settled by {', '.join(facts['payout_references'])}")
            else:
                claims[qid] = claim("unresolved", gap, {}, [], f"no settlement explains the credit ({gap})")
        elif qid == Q_BREAKDOWN:
            if breakdown is not None:
                claims[qid] = claim(status, None, {"breakdown_minor": breakdown},
                                    [txn] + po_refs + [ref_of(r, active) for r in covered],
                                    f"sales {breakdown['sales']} - refunds {breakdown['refunds']} - fees "
                                    f"{breakdown['fees']} = {breakdown['net']}; holder's statement {status}")
            else:
                code = "missing_cited_record" if state == "linked" and missing else gap
                claims[qid] = claim("unresolved", code, {}, [], f"breakdown not verifiable ({code})")
        elif qid == Q_PERIOD:
            claims[qid] = period_claim(rfc, active)
        else:
            claims[qid] = claim("unresolved", "no_merchant_records", {}, [], f"unknown question {qid}")
    disclosed: list[str] = []
    for one in claims.values():
        for ref in one["evidence_refs"]:
            if ref not in disclosed:
                disclosed.append(ref)
    prediction["claims"] = claims
    prediction["package"] = {"request_id": rfc["request_id"], "audience": rfc["audience"],
                             "empty": not disclosed,
                             "attachments": [extract(ref, active, bank) for ref in disclosed]}
    prediction["exclusions"] = exclusions_for(set(disclosed), active, bank)
    prediction["cost"]["latency_ms"] = round((time.perf_counter() - started) * 1000, 3)
    return prediction


def predictions_for(cases: list[dict]) -> list[dict]:
    return [answer_request(case, rfc) for case in cases for rfc in case["rfcs"]]


# --------------------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="predictions jsonl to write")
    parser.add_argument("--families", nargs="*", help="only these families")
    parser.add_argument("--exclude-demo", action="store_true")
    parser.add_argument("--no-score", action="store_true", help="write the predictions only")
    args = parser.parse_args(argv)
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass
    try:
        cases = load_dev_cases(args.families, include_demo=not args.exclude_demo)
        rows = predictions_for(cases)
        write_jsonl(args.out, [{"system": {"name": SYSTEM, "strategy": STRATEGY,
                                           "model_calls": 0}}] + rows)
        print(f"{SYSTEM}: wrote {len(rows)} predictions for {len(cases)} cases to {args.out}",
              file=sys.stderr)
        if args.no_score:
            return 0
        system = scorer.build_system(SYSTEM, rows, args.out)
        scores = scorer.run(cases, [system])
    except EvalError as exc:
        print(f"{SYSTEM}: {exc}", file=sys.stderr)
        return 2
    print(scorer.family_table(scores))
    print()
    print(scorer.comparison_table(scores))
    for note in scorer.limitations(scores):
        print(f"note: {note}")
    failure = scorer.representative_failure(scores[0])
    print(f"representative failure: {failure}" if failure else "no failures")
    print(json.dumps(scorer.results_line(scores[0])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
