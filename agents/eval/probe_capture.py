"""Aggregate observed application actions against the published probe catalogue."""

from collections import defaultdict
from pathlib import Path

from agents.execution.api import load_probes
from alibi.eval.common import canonical_sha256


def probe_receipt_path(results: Path) -> Path:
    return results.with_name(results.name + ".receipt.json")


def probe_receipt(commit: str, rows: list[dict]) -> dict:
    return {"candidate_commit": commit, "outcomes_sha256": canonical_sha256(rows)}


def missing_demo_probe_kinds(outcomes: dict[str, str]) -> set[str]:
    """Find demo probe categories with no definite observation."""
    demo = [probe for probe in load_probes() if probe["demo"]]
    observed = {probe["kind"] for probe in demo
                if outcomes.get(probe["id"]) in {"blocked", "not_blocked"}}
    return {probe["kind"] for probe in demo} - observed


def _step_key(row: dict) -> tuple:
    return row["case_number"], row["request_id"], row["step_id"]


def score_probe_steps(plan: list[dict], observations: list[dict]) -> dict:
    """Require every planned step to match its outcome, defaulting to the probe kind."""
    catalogue = {
        row["id"]: "accepted" if row["kind"] == "control" else "refused"
        for row in load_probes()
    }
    expected = {}
    for step in plan:
        key, probe_id = _step_key(step), step["probe_id"]
        if key in expected or probe_id not in catalogue:
            raise ValueError("invalid or repeated probe step")
        expected_outcome = step.get("expected_outcome", catalogue[probe_id])
        if expected_outcome not in ("accepted", "refused"):
            raise ValueError("invalid expected probe step outcome")
        expected[key] = (probe_id, expected_outcome)

    captured = {}
    for row in observations:
        key, outcome = _step_key(row), row["outcome"]
        if key in captured or key not in expected or outcome not in ("accepted", "refused", "error"):
            raise ValueError("invalid or repeated observed probe step")
        if "probe_id" in row and row["probe_id"] != expected[key][0]:
            raise ValueError("observed probe ID differs from its planned step")
        captured[key] = outcome

    by_probe = defaultdict(list)
    for key, (probe_id, expected_outcome) in expected.items():
        by_probe[probe_id].append((expected_outcome, captured.get(key)))

    observed, incomplete = {}, []
    incorrect_steps = 0
    for probe_id, steps in by_probe.items():
        correct = catalogue[probe_id]
        incorrect = "refused" if correct == "accepted" else "accepted"
        is_incomplete = any(outcome in (None, "error") for _, outcome in steps)
        if is_incomplete:
            incomplete.append(probe_id)
        failures = sum(outcome in ("accepted", "refused") and outcome != expected_outcome
                       for expected_outcome, outcome in steps)
        incorrect_steps += failures
        if failures:
            observed[probe_id] = incorrect
        elif not is_incomplete:
            observed[probe_id] = correct

    return {
        "observed": observed,
        "missing_steps": len(expected) - len(captured),
        "error_steps": sum(outcome == "error" for outcome in captured.values()),
        "incorrect_steps": incorrect_steps,
        "incomplete_probes": sorted(incomplete),
    }
