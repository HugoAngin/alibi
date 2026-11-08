"""Public FakeClient regressions for redundant Challenger rounds."""
import pytest
from alibi.tests.test_agents_runner import make, client_for, challenger, events
from alibi.agents.runner import run_stats
from alibi.core.schemas import ClaimStatus

@pytest.mark.parametrize("missing", [False, True])
def test_repeated_identical_lookup_without_new_material_stops_after_feedback(make, missing):
    ctx = make("supported")
    kw = {"settlement_lookup": lambda ref: None} if missing else {}
    result = ctx.run(client_for(challenger_script=challenger()), max_rounds=8, **kw)
    stats = run_stats(result)
    assert stats["rounds"] == 2
    assert stats["action_budget_used"] == 2
    assert len(events(result, actor="gateway", tool="lookup_settlement")) == 2
    assert stats["model_calls"] == 4
    assert result.claims[0].status == ClaimStatus.UNRESOLVED
    assert any(e.result.get("stop_reason") == "repeated_action_no_progress" for e in result.trace)


def test_transient_lookup_failure_still_allows_retry(make):
    ctx = make("supported")
    calls = []
    def lookup(ref):
        calls.append(ref)
        if len(calls) <= 2:
            raise RuntimeError("temporary public test outage")
        return ctx.fx["settlement_lookup"](ref)
    result = ctx.run(client_for(challenger_script=challenger()), settlement_lookup=lookup)
    assert len(calls) == 3
    assert run_stats(result)["rounds"] == 3
    assert not any(e.result.get("stop_reason") == "repeated_action_no_progress" for e in result.trace)
