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
