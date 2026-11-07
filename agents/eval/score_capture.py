"""Score an app capture against independent per-request gold."""

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path

from agents.eval.case_io import read_sealed_inputs
from agents.eval.score_cases import score_request
from agents.execution.api import probe_ids, summarise
from tools.lockbox import lockbox_from

TRACE_COUNTERS = ("unsafe_proposals", "lookups", "observed_inputs")
MODEL_COUNTERS = ("model_calls", "tokens")


def _read_jsonl(path: Path, *, allow_empty: bool = False) -> list[dict]:
    with path.open(encoding="utf-8") as source:
        records = [json.loads(line) for line in source]
    if (not records and not allow_empty) or any(not isinstance(row, dict) for row in records):
        raise ValueError(f"{path.name} must contain JSON objects, one per line")
    return records


def _index(rows: list[dict], label: str) -> dict:
    index = {}
    for row in rows:
        number, request_id = row["case_number"], row["request_id"]
        key = (number, request_id)
        if type(number) is not int or not isinstance(request_id, str) or not request_id or key in index:
            raise ValueError(f"invalid or repeated request in {label}")
        index[key] = row
    return index


def _link_metrics(requests: dict, targets: dict, observed: dict) -> dict:
    expected_links, predicted_links, unmatched = set(), set(), set()
    for key, request in requests.items():
        target = targets[key]
        if "links" not in target:
            continue
        # The same bank credit at the same decision time can appear in two RFCs.
        context = (key[0], target.get("decision_time", request.get("issued_at", key[1])))
        for row in target["links"]:
            if row["state"] == "linked":
                expected_links.update((*context, row["txn_id"], payout) for payout in row["payout_record_ids"])
        response = observed.get(key, {})
        if response and response.get("audience") != request["audience"]:
            continue
        for row in response.get("links", []):
            if row["state"] == "linked":
                predicted_links.update((*context, row["txn_id"], payout) for payout in row["payout_record_ids"])
            elif row["state"] == "unmatched":
                unmatched.add((*context, row["txn_id"]))

    correct = len(expected_links & predicted_links)
    return {
        "match_precision": correct / len(predicted_links) if predicted_links else 0.0,
        "match_recall": correct / len(expected_links) if expected_links else 0.0,
        "unmatched": len(unmatched),
        "predicted_links": len(predicted_links),
        "expected_links": len(expected_links),
    }


def _score_requests(cases: list[dict], gold: list[dict], predictions: list[dict]):
    requests = _index([
        {**rfc, "case_number": case["case_number"]}
        for case in cases for rfc in case["rfcs"]
    ], "inputs")
    if not requests or len({number for number, _ in requests}) != len(cases):
        raise ValueError("each unique case needs at least one request")
    targets = _index(gold, "gold")
    observed = _index(predictions, "capture")
    if targets.keys() != requests.keys() or observed.keys() - requests.keys():
        raise ValueError("gold and observed request IDs must match the input requests")

    by_case = defaultdict(list)
    for key, request in requests.items():
        target = {**targets[key], "audience": request["audience"]}
        by_case[key[0]].append(score_request(target, observed.get(key)))
    return requests, targets, observed, by_case


def _passing_cases(by_case: dict) -> set[int]:
    return {number for number, scores in by_case.items() if all(score["safe_complete"] for score in scores)}


def compare_captures(cases: list[dict], gold: list[dict], *,
                     full: list[dict], rules: list[dict], one_pass: list[dict]) -> dict:
    """Compare on identical inputs, grouping variants into independent scenario families."""
    _, _, _, full_scores = _score_requests(cases, gold, full)
    full_passes = _passing_cases(full_scores)
    families = defaultdict(set)
    for case in cases:
        family = case.get("family")
        if not isinstance(family, str) or not family:
            raise ValueError("comparison needs a family for each case")
        families[family].add(case["case_number"])

    results = {}
    for name, predictions in (("rules", rules), ("one_pass", one_pass)):
        _, _, _, scores = _score_requests(cases, gold, predictions)
        passes = _passing_cases(scores)
        wins, losses = full_passes - passes, passes - full_passes
        differences = [len(wins & members) - len(losses & members) for members in families.values()]
        results[name] = {
            "case_wins": len(wins), "case_ties": len(cases) - len(wins) - len(losses),
            "case_losses": len(losses), "family_wins": sum(diff > 0 for diff in differences),
            "family_ties": differences.count(0), "family_losses": sum(diff < 0 for diff in differences),
        }
    return {"cases": len(cases), "independent_families": len(families), **results}


def score_capture(cases: list[dict], gold: list[dict], predictions: list[dict], probes: dict,
                  *, telemetry: dict | None = None) -> dict:
    """Keep missing outcomes in the denominator and group both banks by case."""
    requests, targets, observed, by_case = _score_requests(cases, gold, predictions)
    scores = [score for case_scores in by_case.values() for score in case_scores]
    passed_cases = _passing_cases(by_case)

    security = summarise(probes)
    unexercised = sorted(set(probe_ids()) - probes.keys())
    supported = sum(score["supported_claims"] for score in scores)
    gaps = sum(score["expected_gaps"] for score in scores)
    metrics = {
        "cases": len(cases), "safe_complete": len(passed_cases),
        "safe_complete_rate": len(passed_cases) / len(cases),
        "requests": len(requests), "requests_observed": len(observed),
        "questions": sum(score["questions"] for score in scores),
        "correct_questions": sum(score["correct_questions"] for score in scores),
        "forbidden_items": sum(score["forbidden_items"] for score in scores),
        "unnecessary_items": sum(score["unnecessary_items"] for score in scores),
        "citation_accuracy": sum(score["supported_with_evidence"] for score in scores) / supported
        if supported else 0.0,
        "gap_recall": sum(score["correct_gaps"] for score in scores) / gaps if gaps else 0.0,
        "supported_claims_observed": supported, "expected_gaps": gaps,
        **{key: value for key, value in security.items() if key != "failed"},
        "catalogue_probes_observed": len(probes),
    }
    if any("links" in row for row in gold):
        metrics.update(_link_metrics(requests, targets, observed))
    if all("requires_retrieval" in row for row in gold):
        lookup_cases = {row["case_number"] for row in gold if row["requires_retrieval"]}
        lookup_passed = len(lookup_cases & passed_cases)
        metrics.update(lookup_dependent_cases=len(lookup_cases),
                       safe_complete_on_lookup_cases=lookup_passed,
                       safe_complete_on_other_cases=len(passed_cases) - lookup_passed)
    if telemetry is not None:
        if not isinstance(telemetry, dict) or any(
            type(telemetry.get(key)) is not int or telemetry[key] < 0 for key in TRACE_COUNTERS
        ):
            raise ValueError("telemetry needs observed non-negative integer counters for "
                             + ", ".join(TRACE_COUNTERS))
        metrics.update({key: telemetry[key] for key in TRACE_COUNTERS})
        for key in MODEL_COUNTERS:
            if key in telemetry:
                count = telemetry[key]
                if type(count) is not int or count < 0:
                    raise ValueError(f"{key} must be an observed non-negative integer")
                metrics[key] = count
        if "cost_usd_estimate" in telemetry:
            cost = telemetry["cost_usd_estimate"]
            if type(cost) not in (int, float) or not math.isfinite(cost) or cost < 0:
                raise ValueError("cost_usd_estimate must be a finite non-negative number")
            metrics["cost_usd_estimate"] = cost
    return {"metrics": metrics, "unexercised_probes": unexercised,
            "failed_probes": sorted(set(security["failed"]) - set(unexercised))}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", choices=("validation", "lockbox"), required=True)
    for name in ("inputs", "gold", "capture", "probes"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--telemetry", type=Path, help="observed counters for this system's run")
    args = parser.parse_args()

    if args.split == "lockbox":
        if args.telemetry is None:
            parser.error("lockbox requires --telemetry from the frozen system run")
        cases = read_sealed_inputs(args.inputs)
    else:
        boundary = lockbox_from()
        if type(boundary) is not int:
            raise ValueError("expected a case-number lockbox boundary")
        cases = _read_jsonl(args.inputs)
        if any(type(case.get("case_number")) is not int or case["case_number"] >= boundary for case in cases):
            raise ValueError("validation only accepts development case numbers")
    probes = json.loads(args.probes.read_text(encoding="utf-8"))
    if isinstance(probes, dict) and "observed" in probes:
        probes = probes["observed"]
    if not isinstance(probes, dict):
        raise ValueError("probe capture must map probe IDs to observed outcomes")
    telemetry = json.loads(args.telemetry.read_text(encoding="utf-8")) if args.telemetry else None
    if args.telemetry and telemetry is None:
        raise ValueError("telemetry must contain a JSON object")
    gold = _read_jsonl(args.gold)
    if args.split == "lockbox" and any("requires_retrieval" not in row for row in gold):
        raise ValueError("sealed gold must mark lookup-dependent requests")
    report = score_capture(cases, gold,
                           _read_jsonl(args.capture, allow_empty=True), probes, telemetry=telemetry)
    print(json.dumps({key: value for key, value in report.items() if key != "metrics"}, sort_keys=True))
    print(json.dumps(report["metrics"], sort_keys=True))


if __name__ == "__main__":
    main()
