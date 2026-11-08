"""Keyless replay benchmark on public cases 1-80: fixed baseline retrieval vs the evidence engine.

Both systems get the same request view from ``request_context`` and the same scoped lookup
callback, and both drafts come from the same rules (``baselines._rules``). Changed cases are
attributed to holds when any engine answer in the case was held, to the stricter index when the
engine excluded a record for a reason the baseline does not apply, and otherwise to retrieval.
Public development gold is read here for scoring and never passed to either
system. Scores are agreement of the rules draft with gold on status, gap, facts and evidence
set per question; they are not the official scorer, policy gate or package pipeline, so they
are not a safe-complete rate.

    uv run --frozen python -m agents.eval.benchmark_evidence_engine            # stable metrics
    uv run --frozen python -m agents.eval.benchmark_evidence_engine --timed    # adds CPU replay timing on stderr

The last stdout line is one flat JSON object of numeric metrics, identical on every run.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import json
import sys
from time import perf_counter

from agents.data.api import load_cases, without_gold
from agents.eval.baselines import _prepare, _rules
from agents.eval.evidence_engine import DEFAULT_BUDGET, evidence_report

# Exclusion reasons the fixed baseline does not apply (it also drops duplicates, conflicting
# redeliveries, stale revisions and foreign subjects).
STRICT = ("after_decision_time", "malformed", "identity_collision", "conflicting_identity")


def _ids(refs) -> list[str]:
    return [ref["source_id"] if isinstance(ref, dict) else ref.source_id for ref in refs]


def _exact(status: str, gap, facts: dict, refs: list[str], expected: dict) -> bool:
    return (status == expected["status"] and gap == expected["gap"] and facts == expected["facts"]
            and any(set(refs) == set(group) for group in expected["evidence_sets"]))


def _baseline(case: dict, request_id: str) -> tuple[dict, dict, int]:
    evidence, counters = _prepare(case, request_id)
    draft = _rules(evidence)
    answers = {question: (row["status"], row["gap"], row["facts"], _ids(row["evidence_refs"]))
               for question, row in draft["answers"].items()}
    links = {link["txn_id"]: (link["state"], list(link["payout_record_ids"])) for link in draft["links"]}
    return answers, links, counters["lookups"], counters["observed_inputs"]


def _engine(case: dict, request_id: str, budget: int):
    report = evidence_report(case, request_id, budget=budget)
    answers = {answer.question_id: (answer.status, answer.gap, answer.facts, _ids(answer.evidence_refs))
               for answer in report.answers}
    links = {credit.txn_id: (credit.link_state, list(credit.payout_record_ids)) for credit in report.credits}
    return answers, links, report.calls, report.observed_inputs, report


def run(numbers: list[int] | None = None, budget: int = DEFAULT_BUDGET, timed: bool = False) -> dict:
    cases = [case for case in load_cases() if not numbers or case["case_number"] in numbers]
    totals = defaultdict(int)
    families = defaultdict(lambda: defaultdict(int))
    changed = []
    seconds = {"baseline": 0.0, "engine": 0.0}
    for case in cases:
        gold = case["gold"]
        stripped = without_gold(case)
        gold_links = {link["txn_id"]: (link["state"], list(link["payout_record_ids"])) for link in gold["links"]}
        exact = {"baseline": True, "engine": True}
        held = strict = False
        family = families[case["family"]]
        family["cases"] += 1
        for rfc in stripped["rfcs"]:
            request_id = rfc["request_id"]
            started = perf_counter()
            base = _baseline(stripped, request_id)
            seconds["baseline"] += perf_counter() - started
            started = perf_counter()
            *eng, report = _engine(stripped, request_id, budget)
            seconds["engine"] += perf_counter() - started
            totals["requests"] += 1
            totals["engine_cache_hits"] += report.cache_hits
            totals["engine_budget_skips"] += sum(action.state == "skipped_budget" for action in report.actions)
            totals["engine_lookup_failures"] += sum(action.outcome in ("error", "refused") for action in report.actions)
            totals["engine_not_found"] += sum(action.outcome == "not_found" for action in report.actions)
            totals["engine_held_answers"] += sum(bool(answer.hold_reasons) for answer in report.answers)
            strict_here = sum(reason in STRICT for _, reason in report.exclusions)
            totals["engine_strict_exclusions"] += strict_here
            held |= any(answer.hold_reasons for answer in report.answers)
            strict |= bool(strict_here)
            if len(report.credits) == 1 and "q.source_of_funds" in rfc["question_ids"]:
                # Secondary diagnostic only: the draft answers above keep the shared rules' gap.
                expected = gold["answers"][request_id]["q.source_of_funds"]["gap"]
                totals["gap_diagnoses"] += 1
                totals["engine_gap_diagnosis_agree"] += report.credits[0].diagnosed_gap == expected
            for name, (answers, links, lookups, observed) in (("baseline", base), ("engine", eng)):
                totals[f"{name}_lookups"] += lookups
                totals[f"{name}_observed_inputs"] += observed
                family[f"{name}_lookups"] += lookups
                for txn_id, expected in gold_links.items():
                    if txn_id in (rfc.get("txn_ids") or ()):
                        totals["links"] += name == "baseline"
                        totals[f"{name}_links_exact"] += links.get(txn_id) == expected
                for question in rfc["question_ids"]:
                    expected = gold["answers"][request_id][question]
                    status, gap, facts, refs = answers[question]
                    ok = _exact(status, gap, facts, refs, expected)
                    totals["answers"] += name == "baseline"
                    totals[f"{name}_answers_exact"] += ok
                    totals[f"{name}_status_correct"] += status == expected["status"]
                    totals[f"{name}_overclaims"] += status == "supported" and expected["status"] != "supported"
                    totals[f"{name}_missed_support"] += expected["status"] == "supported" and status != "supported"
                    exact[name] &= ok
        totals["cases"] += 1
        for name in exact:
            totals[f"{name}_cases_exact"] += exact[name]
            family[f"{name}_cases_exact"] += exact[name]
        if exact["baseline"] != exact["engine"]:
            causes = tuple(name for name, on in (("strict_index", strict), ("hold", held)) if on) or ("retrieval",)
            changed.append((case["case_number"], case["family"], "improved" if exact["engine"] else "regressed",
                            "+".join(causes)))

    metrics = dict(totals)
    for kind in ("improved", "regressed"):
        label = "improvements" if kind == "improved" else "regressions"
        metrics[f"engine_case_{label}"] = sum(row[2] == kind for row in changed)
        for cause in ("retrieval", "strict_index", "hold"):   # a case can involve both strict_index and hold
            metrics[f"engine_case_{label}_{cause}"] = sum(row[2] == kind and cause in row[3].split("+")
                                                          for row in changed)
    metrics.setdefault("engine_strict_exclusions", 0)
    metrics["families"] = len(families)
    metrics["families_improved"] = sum(f["engine_cases_exact"] > f["baseline_cases_exact"] for f in families.values())
    metrics["families_regressed"] = sum(f["engine_cases_exact"] < f["baseline_cases_exact"] for f in families.values())
    metrics["families_fewer_lookups"] = sum(f["engine_lookups"] < f["baseline_lookups"] for f in families.values())
    metrics["families_more_lookups"] = sum(f["engine_lookups"] > f["baseline_lookups"] for f in families.values())
    metrics["budget"] = budget
    return {"metrics": metrics, "families": {name: dict(row) for name, row in sorted(families.items())},
            "changed": changed, "seconds": seconds if timed else None}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--case", type=int, action="append", help="public case number; repeat for several")
    parser.add_argument("--budget", type=int, default=DEFAULT_BUDGET, help="engine lookup calls per request")
    parser.add_argument("--timed", action="store_true", help="print CPU replay seconds on stderr only")
    args = parser.parse_args()
    result = run(args.case, args.budget, args.timed)
    print(f"{'family':24} cases  exact(base→engine)  lookups(base→engine)")
    for name, row in result["families"].items():
        print(f"{name:24} {row['cases']:5}  {row['baseline_cases_exact']:5} → {row['engine_cases_exact']:<5}"
              f"        {row['baseline_lookups']:4} → {row['engine_lookups']}")
    for number, family, kind, cause in result["changed"]:
        print(f"case {number} ({family}): {kind} ({cause})")
    if result["seconds"] is not None:
        print("CPU replay seconds (local, no model or network): "
              + json.dumps({key: round(value, 3) for key, value in result["seconds"].items()}), file=sys.stderr)
    print(json.dumps(result["metrics"], sort_keys=True))


if __name__ == "__main__":
    main()
