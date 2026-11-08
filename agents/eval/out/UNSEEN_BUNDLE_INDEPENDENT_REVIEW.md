# Independent composed-tree review

No concrete regression found in the requested telemetry privacy/cutoff and no-progress boundaries. **30 selected tests passed in 3.11s**, `integration-audit-composed-review.log`. No full suite, hidden input, gold inspection or live model call was used.

Source: read-only copy of `/var/folders/h5/vyz923lx1t7_ll1y6mb_9xbm0000gn/T/alibi-unseen-generalisation-zau4pblt/tree`, with adapter/runner/actions/gateway/merchant SHA-256 values in `integration-audit-composed-review-hashes.json`. The source tree was being finalised by its owner; conclusions bind to those copied file bytes. Neither shared source nor that tree was changed.

## Concrete checks

- A public lookup-only payout's availability is delayed to the second RFC. The new covering query returns no result before then, and telemetry's issuer-verified inventory remains unchanged. After advancing to the availability cutoff the query returns it and current inventory grows. The earlier telemetry object retains its original count.
- A future unsupported import is absent from first-request refusal counts and appears only at the second request.
- Deliberately injected raw error text, IDs, payload text and an arbitrary outcome are converted to static classes. A thrown collection exception returns only `{version: 1, collection_error: 1}`.
- Fresh, correctly signed settlement revisions 1, 2 and 3 are all retrieved across three Challenger rounds; no no-progress stop occurs.
- Repeated `refresh_txn` calls with identical revision are excluded from the stop and retain all three rounds.
- Two transient lookup failures followed by a successful lookup retain all retries.
- Repeating an unchanged successful or empty lookup stops after its feedback has been reviewed, preserving the unresolved Challenger result.
- Existing query tests also pass scope/date checks, active-revision scope revalidation, atomic rollback on a later bad signature, and private clue/order dependency tracking.

Interpretation: telemetry is a snapshot before **each request's** investigation, not a single case-start inventory. Later requests can legitimately count evidence retrieved for an earlier request. Delivery/import totals are cumulative visible-input counts; inventory is active stored source counts. This distinction is documented, not a leak.

Optional test-only handoff: `integration-audit-REVIEW_CONTROLS.patch` adds four controls (fresh revisions, refresh, delayed payout cutoff, raw error/exception suppression). No production fix is proposed.

```sh
UV_PROJECT_ENVIRONMENT=/Users/tamim/code/agentverse_wtt/.venv uv run --frozen --no-sync python -m pytest -q alibi/tests/test_telemetry_review_controls.py alibi/tests/test_runtime_review_controls.py alibi/tests/test_ingest_telemetry.py alibi/tests/test_agents_no_progress.py alibi/tests/test_payout_query.py alibi/tests/test_query_safety.py --basetemp=./composed-review-state -o cache_dir=./review-cache
```

This review makes no claim about live timeout recovery or unseen accuracy.
