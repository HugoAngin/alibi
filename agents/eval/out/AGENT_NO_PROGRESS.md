# Agent repeated-read runtime fix

Staged owner-applicable patch: `integration-audit-AGENT_NO_PROGRESS.patch` (SHA-256 `df940ac11a7f203c261f64b52a10f1809daa02136a6caf213032e24c7dabeb3f`). Native owner: lead. No shared source, Git state, live models, capture processes, private cases/gold or private worker output was touched.

Base candidate5: `3c5047639aade89f1916163ae2ad3a00050d1143`. Tested source archive HEAD: `af751cdb001be8a4debe76683736f33ee2bdd00d`. Runner is unchanged between candidate5 and observed current HEAD `5063d15ac8baa8b0eeb0009f68d7c59a8b837e3e` (native path diff empty).

## Reproduced bug

`_Run.investigate_credit` continues another expensive Challenger round when the model repeats an identical read, receives the same revisions or no record, and therefore adds no material. The existing action-budget test explicitly reproduces three identical settlement calls and three Challenger calls at default max_rounds=3. With max_rounds=8 and action_budget=3, two new regressions observed four Challenger rounds, including one whose action can only fail because its budget is exhausted.

Two new tests failed against the unchanged source, and the transient-error retry control passed: `integration-audit-runtime-before.log`.

## Minimal change

After executing a consecutive identical settlement/covering/date read, stop if its result is ok, not_found or refused and adds no new material. The Challenger has already reviewed the first result and issued its second proposal. Keep that latest proposal and the normal deterministic verdict, Challenger veto, Detective second-opinion check, Curator and downstream policy/approval processing.

- No stop on the first action, so fresh evidence gets its follow-up Challenger review.
- No stop when the repeat retrieves a new revision.
- Transient errors reset the comparison and retain retries.
- `refresh_txn` is excluded so subsequent corrections remain observable.
- No change to models, prompts, configured round/call budgets, timeouts, retrieval scope, trust, or the rules baseline.
- A trace note records `stop_reason=repeated_action_no_progress`.

In the reproduced repeated-read scenario, the default loop uses **two rather than three Challenger calls**, and two rather than three identical reads. With max_rounds=8 it uses two rather than four Challenger calls. The test asserts four total FakeClient model calls (Detective + two Challenger + Curator). These are call-count regressions, not live latency or benchmark measurements. The six reported capture timeouts have not been attributed to this loop; no claim of timeout recovery or unseen accuracy improvement is made.

## Validation

**61 passed in 4.76s**, `integration-audit-runtime-tests.log`:

```sh
UV_PROJECT_ENVIRONMENT=/Users/tamim/code/agentverse_wtt/.venv uv run --frozen --no-sync python -m pytest -q alibi/tests/test_agents_no_progress.py alibi/tests/test_agents_runner.py alibi/tests/test_agents_roles.py alibi/tests/test_agents_payout_settlement.py alibi/tests/test_unresolved_dependencies.py --basetemp=./runtime-final-state -o cache_dir=./runtime-cache
```

Tests include repeated successful/empty lookup, transient failure recovery, latest Challenger veto, stale proposal handling, code recomputed contradictions, role reference filtering, and unresolved dependency privacy. One existing test changes only its expected redundant call count and adds the stop-reason assertion; its unresolved verdict and exhausted-budget control are retained.

This fix changes when a no-progress agent loop stops and should be identified as a candidate revision in any reused-data execution; it is not an equivalent replay of candidate5. Root owns integration and any reused-data run. No public live trace was required or inspected; the concrete loop bug is independently reproduced with public fixtures.
