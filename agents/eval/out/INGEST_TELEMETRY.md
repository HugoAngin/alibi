# Optional aggregate ingest telemetry

Patch: `integration-audit-INGEST_TELEMETRY.patch`, SHA-256 `a73f50cd7775b4b41d916f277a4573e4b6b3bc210d4aa137b7b912ca4414b5f5`.
Base: immutable HEAD `91b5fbaf1382b4eac12369872fc6d1c9711a71db` (current final public tree when snapshot was copied). Native owner: lead. Staged only; shared checkout, Git, captures and processes unchanged.

The adapter adds `ingest_telemetry` to each returned prediction, including a request refused from the passport. `run_case(..., ingest_telemetry=False)` disables the metadata; default True makes the existing worker retain it without a worker invocation change. Existing predictions and captures are not modified.

Fields contain fixed labels and nonnegative counts only:

- `merchant.accepted/skipped/refused`: cumulative observed delivery outcomes. Reasons: inserted, corrected, duplicate, stale, not_delivered, mapping_error, untrusted_issuer, ingest_refused, other_error.
- `imports.accepted/skipped/refused`: cumulative bootstrap/advance import statuses. Refused imports count as skipped because the importer explicitly skips them; those two counts overlap and must not be summed. Reasons distinguish unsupported_kind from other import_refused.
- `visible_by_kind`: separate input inventory counts, normalised through the existing kind map, with every unknown label replaced by unsupported. These are input kind totals, not a joint outcome-by-kind matrix.
- `available`: counts of active stored source records by issuer_verified/self_supplied/bank_observed. These are source records, not bank transaction counts.
- `version: 1`; a diagnostic collection exception yields only version plus `collection_error: 1`, never an exception string.

Inventory is sampled immediately before each request's investigation, after seed or clock advance. It excludes future deliveries and lookup-only records not yet retrieved. This distinguishes empty starting merchant inventory from refused/skipped input. It does not capture a seed exception that aborts before any prediction, or a killed worker. It is not package content and is not passed to agents or scorers as input.

No identifiers, raw kinds, text, refs, signatures, key seeds or error messages are emitted by telemetry. Error reasons are mapped to a finite vocabulary; the raw reason is never retained in this metadata. No imports are remapped or retried. The collector reads the existing merchant delivery log once per request and queries the existing store inventory; it performs no evidence lookup. The original algorithm, model calls, retrieval budget and signing/approval paths are unchanged.

## Tests

**68 passed in 3.70s** (`integration-audit-telemetry-tests.log`). All three new tests fail on the unchanged adapter (`integration-audit-telemetry-before.log`).

```sh
UV_PROJECT_ENVIRONMENT=/Users/tamim/code/agentverse_wtt/.venv uv run --frozen --no-sync python -m pytest -q alibi/tests/test_ingest_telemetry.py alibi/tests/test_adapter.py alibi/tests/test_connectors_self_supplied.py alibi/tests/test_trusted_synthetic_issuers.py --basetemp=./telemetry-final2-state -o cache_dir=./telemetry-cache
```

Public-only controls: valid synthetic import and merchant records; an unsupported import containing sentinel kind/ID/text; untrusted issuer refusal; a future unsupported import visible only at the second RFC; identical claims with telemetry enabled/disabled. Sentinel values and sensitive field names do not appear in telemetry. No hidden inputs, gold, worker output or live model calls were used.

Root owns integration and capture execution. This is diagnosability work and makes no accuracy, latency or benchmark claim.
