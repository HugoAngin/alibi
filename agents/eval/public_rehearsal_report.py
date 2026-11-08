"""Replay the committed public rehearsal; never launch a candidate or load sealed cases.

Run with ``uv run --frozen python -m agents.eval.public_rehearsal_report``.
The last line is a numeric JSON record suitable for tools/results.py.
"""

import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory

from agents.data.api import load_case
from agents.eval.run_sealed import PREDICTION_FILES, PROBE_FILE, score_saved
from alibi.eval import common, jsonschema_lite


CAPTURE = Path(__file__).resolve().parent / "out" / "public_rehearsal_c6ea270"
PARALLEL_CAPTURE = CAPTURE.parent / "public_parallel_c6ea270"
CANDIDATE3_CAPTURE = CAPTURE.parent / "public_candidate3_58bf0d4"
CANDIDATE3 = "58bf0d4a199f5ad3833fa9bde751d31140219975"
CANDIDATE = "c6ea270164ac992e7b5f3c0869a79d3d56bf74c8"
PUBLIC_CASES = (1, 2)


def _field_count(value, name: str) -> int:
    if isinstance(value, dict):
        return int(name in value) + sum(_field_count(item, name) for item in value.values())
    if isinstance(value, list):
        return sum(_field_count(item, name) for item in value)
    return 0


def _parallel_metrics(cases: list[dict], capture=PARALLEL_CAPTURE, candidate=CANDIDATE, prefix="parallel") -> dict:
    run = common.load_json(capture / "run.json")
    if run["input"]["candidate"] != candidate:
        raise ValueError("parallel capture candidate differs from the documented candidate")
    rows = [row for name in PREDICTION_FILES.values()
            for row in common.read_jsonl(capture / name)]
    if any(row["case_number"] not in PUBLIC_CASES for row in rows):
        raise ValueError("parallel capture contains a non-rehearsal case")
    with TemporaryDirectory(prefix="alibi-public-parallel-score-") as directory:
        output = Path(directory)
        for filename in (*PREDICTION_FILES.values(), PROBE_FILE):
            shutil.copyfile(capture / filename, output / filename)
        metrics = score_saved(output, cases, [], kind="public_live")
        scores = common.load_json(output / "scores.json")
        for mode, score in scores["independent"].items():
            metrics[f"second_scorer_{mode}_safe_complete"] = score["metrics"]["safe_complete"]
    schema = common.load_schema("prediction")
    metrics["prediction_schema_rows"] = len(rows)
    metrics["prediction_schema_invalid_rows"] = sum(bool(jsonschema_lite.errors(schema, row)) for row in rows)
    for failure in ("invalid_output", "timeout"):
        metrics[f"rfc_{failure}_count"] = sum((row.get("failure") or {}).get("type") == failure for row in rows)
    metrics["rfc_failures"] = sum(bool(row.get("failure")) for row in rows)
    scheduling = common.load_json(capture / "scheduling.json")
    metrics.update({f"scheduler_{key}": int(value) for key, value in scheduling["aggregate"].items()
                    if type(value) in (int, bool)})
    for mode, counts in scheduling["systems"].items():
        metrics.update({f"{mode}_workers_{key}": value for key, value in counts.items()})
    if prefix == "candidate3":
        # This committed aggregate retains the original worker measurements;
        # replay's temporary directory intentionally contains no worker logs.
        retained = common.load_json(capture / "scores.json")["retained_capture"]
        for mode, usage in retained["usage_by_system"].items():
            for key in ("model_calls", "tokens", "cost_usd_estimate"):
                metrics[f"retained_usage_{mode}_{key}"] = usage["totals"][key]
            metrics[f"retained_usage_{mode}_unknown_workers"] = usage["unknown_workers"]
        for mode, difference in retained["claim_divergence_by_system"].items():
            for key in ("differing_rfcs", "unknown_comparison_rfcs", "policy_filtering_rfcs"):
                metrics[f"claim_divergence_{mode}_{key}"] = difference[key]
    return {f"{prefix}_{key}": value for key, value in metrics.items()}


def replay() -> dict:
    """Score retained outputs using both existing scorers, writing only to a tempdir."""
    run = common.load_json(CAPTURE / "run.json")
    if run["candidate"] != CANDIDATE:
        raise ValueError("public capture candidate differs from the documented candidate")
    rows = {mode: common.read_jsonl(CAPTURE / filename)
            for mode, filename in PREDICTION_FILES.items()}
    if any(row["case_number"] not in PUBLIC_CASES for group in rows.values() for row in group):
        raise ValueError("capture contains a case outside the two public rehearsal cases")
    cases = [load_case(number) for number in PUBLIC_CASES]
    with TemporaryDirectory(prefix="alibi-public-score-") as directory:
        output = Path(directory)
        for filename in (*PREDICTION_FILES.values(), PROBE_FILE):
            shutil.copyfile(CAPTURE / filename, output / filename)
        metrics = score_saved(output, cases, [], kind="public_live")
        scores = common.load_json(output / "scores.json")
        # These are the existing second scorer's results, not a claim that its
        # input interpretation is independent of the adapter or official scorer.
        for mode, score in scores["independent"].items():
            metrics[f"second_scorer_{mode}_safe_complete"] = score["metrics"]["safe_complete"]

    schema = common.load_schema("prediction")
    predictions = [row for group in rows.values() for row in group]
    metrics["prediction_schema_rows"] = len(predictions)
    metrics["prediction_schema_invalid_rows"] = sum(
        bool(jsonschema_lite.errors(schema, row)) for row in predictions)
    for label in ("control", "restriction"):
        policy = common.load_json(CAPTURE / f"policy-{label}.json")
        metrics[f"policy_{label}_schema_rows"] = len(policy)
        metrics[f"policy_{label}_schema_invalid_rows"] = sum(
            bool(jsonschema_lite.errors(schema, row)) for row in policy)
        metrics[f"policy_{label}_package_amount_pence_fields"] = sum(
            _field_count(row["package"], "amount_pence") for row in policy)
        metrics[f"policy_{label}_claim_status_divergences"] = sum(
            claim["status"] != row["claims"][claim["question_id"]]["status"]
            for row in policy for claim in row.get("package_claims", []))
        metrics[f"policy_{label}_rfcs_with_claim_status_divergences"] = sum(
            any(claim["status"] != row["claims"][claim["question_id"]]["status"]
                for claim in row.get("package_claims", [])) for row in policy)

    usage = common.load_json(CAPTURE / "usage_summary.json")
    for mode, measured in usage.items():
        for key in ("model_calls", "tokens", "cost_usd_estimate"):
            metrics[f"retained_usage_{mode}_{key}"] = measured[key]
    metrics["retained_usage_cost_usd_estimate_total"] = round(sum(
        measured["cost_usd_estimate"] for measured in usage.values()), 10)
    metrics.update(_parallel_metrics(cases))
    metrics.update(_parallel_metrics(cases, CANDIDATE3_CAPTURE, CANDIDATE3, "candidate3"))
    return metrics


def main() -> None:
    print(json.dumps(replay(), sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
