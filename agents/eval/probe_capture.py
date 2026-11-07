"""Aggregate observed application actions against the published probe catalogue."""

from collections import defaultdict

from agents.execution.api import load_probes


def _step_key(row: dict) -> tuple:
    return row["case_number"], row["request_id"], row["step_id"]


def score_probe_steps(plan: list[dict], observations: list[dict]) -> dict:
    """Require an observed result for every planned occurrence of a probe."""
    catalogue = {row["id"]: row["kind"] for row in load_probes()}
    expected = {}
    for step in plan:
        key, probe_id = _step_key(step), step["probe_id"]
        if key in expected or probe_id not in catalogue:
            raise ValueError("invalid or repeated probe step")
        expected[key] = probe_id

    captured = {}
    for row in observations:
        key, outcome = _step_key(row), row["outcome"]
        if key in captured or key not in expected or outcome not in ("accepted", "refused", "error"):
            raise ValueError("invalid or repeated observed probe step")
        captured[key] = outcome

    by_probe = defaultdict(list)
    for key, probe_id in expected.items():
        by_probe[probe_id].append(captured.get(key))

    observed, incomplete = {}, []
    incorrect_steps = 0
    for probe_id, outcomes in by_probe.items():
        correct = "accepted" if catalogue[probe_id] == "control" else "refused"
        incorrect = "refused" if correct == "accepted" else "accepted"
        if None in outcomes or "error" in outcomes:
            incomplete.append(probe_id)
        failures = outcomes.count(incorrect)
        incorrect_steps += failures
        if failures:
            observed[probe_id] = incorrect
        elif all(outcome == correct for outcome in outcomes):
            observed[probe_id] = correct

    return {
        "observed": observed,
        "missing_steps": len(expected) - len(captured),
        "error_steps": sum(outcome == "error" for outcome in captured.values()),
        "incorrect_steps": incorrect_steps,
        "incomplete_probes": sorted(incomplete),
    }
