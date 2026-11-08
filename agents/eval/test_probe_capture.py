import pytest

from agents.eval.probe_capture import score_probe_steps
from agents.execution.api import summarise


def test_repeated_probes_require_all_steps_and_preserve_observed_failures():
    steps = [{"case_number": number, "request_id": "r1", "step_id": "s1", "probe_id": probe}
             for number, probe in enumerate(("SP-01", "SP-01", "SP-02", "SP-02", "SP-30"), 1)]
    observations = [{**step, "outcome": outcome} for step, outcome in zip(
        (steps[0], steps[2], steps[3], steps[4]), ("refused", "accepted", "error", "accepted"),
    )]

    report = score_probe_steps(steps, observations)

    assert report == {
        "observed": {"SP-02": "accepted", "SP-30": "accepted"},
        "missing_steps": 1,
        "error_steps": 1,
        "incorrect_steps": 1,
        "incomplete_probes": ["SP-01", "SP-02"],
    }
    assert summarise(report["observed"])["probes_blocked"] == 0
    assert summarise(report["observed"])["controls_allowed"] == 1
    observations.append({**steps[1], "outcome": "refused"})
    assert score_probe_steps(steps, observations)["observed"]["SP-01"] == "refused"


@pytest.mark.parametrize(
    ("retry_outcome", "aggregate", "incorrect_steps", "error_steps", "missing_steps"),
    [
        ("accepted", {"SP-31": "refused"}, 0, 0, 0),
        ("refused", {"SP-31": "accepted"}, 1, 0, 0),
        ("error", {}, 0, 1, 0),
        (None, {}, 0, 0, 1),
    ],
)
def test_receipt_idempotency_requires_accepting_identical_bytes_and_refusing_conflicting_bytes(
    retry_outcome, aggregate, incorrect_steps, error_steps, missing_steps,
):
    request = {"case_number": 1, "request_id": "r1"}
    plan = [
        {**request, "step_id": "identical-retry", "probe_id": "SP-31", "expected_outcome": "accepted"},
        {**request, "step_id": "conflicting-retry", "probe_id": "SP-31", "expected_outcome": "refused"},
    ]
    observations = [{**request, "step_id": "conflicting-retry", "outcome": "refused"}]
    if retry_outcome is not None:
        observations.append({**request, "step_id": "identical-retry", "outcome": retry_outcome})

    report = score_probe_steps(plan, observations)

    assert report == {
        "observed": aggregate,
        "missing_steps": missing_steps,
        "error_steps": error_steps,
        "incorrect_steps": incorrect_steps,
        "incomplete_probes": ["SP-31"] if missing_steps or error_steps else [],
    }
    assert summarise(report["observed"])["probes_blocked"] == int(retry_outcome == "accepted")


def test_expected_outcome_must_be_accepted_or_refused():
    step = {"case_number": 1, "request_id": "r1", "step_id": "s1", "probe_id": "SP-31",
            "expected_outcome": "error"}

    with pytest.raises(ValueError, match="invalid expected probe step outcome"):
        score_probe_steps([step], [])


def test_observation_cannot_be_credited_to_a_different_probe():
    step = {"case_number": 1, "request_id": "r1", "step_id": "s1", "probe_id": "SP-27"}
    observation = {**step, "probe_id": "SP-25", "outcome": "refused"}
    with pytest.raises(ValueError, match="observed probe ID differs"):
        score_probe_steps([step], [observation])
