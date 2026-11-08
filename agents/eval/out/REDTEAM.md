# Offline red team of the published S3a client

Tested `alibi/llm` and the role schemas on public commit `d04ec78`. Each probe calls the published `GeminiClient.generate` with a synthetic transport response. The tests use temporary cache and audit files and make no Gemini or gateway calls.

| Confirmed finding | What the probe observes | Code and test |
| --- | --- | --- |
| Undeclared role output survives validation | Challenger returns `next_action.args.subject_ref` for another synthetic holder. Policy parse returns `params.allow_auto_share: true` on a deny rule. Both fields survive in `Result.data`. | [`challenger.json:44`](../../../alibi/agents/prompts/challenger.json#L44), [`policy_parse.json:24`](../../../alibi/agents/prompts/policy_parse.json#L24), [`schema.py:45`](../../../alibi/llm/schema.py#L45). `test_s3a_rejects_untyped_role_output[challenger-foreign-selector]` and `[policy-undeclared-parameter]`. |
| Nonfinite policy confidence bypasses JSON and range checks | The literal `NaN` passes the Python decoder and the policy's `[0, 1]` bounds. It reaches `Result.data` as a float, even though a confidence threshold cannot order it. | [`client.py:310`](../../../alibi/llm/client.py#L310), [`schema.py:19`](../../../alibi/llm/schema.py#L19), [`schema.py:71`](../../../alibi/llm/schema.py#L71). `test_s3a_rejects_untyped_role_output[policy-nonfinite-confidence]`. |
| Retries bypass both documented call caps | A transport that always returns a 503 is invoked three times with a process cap of one, and again three times with a task cap of one. | [`client.py:263`](../../../alibi/llm/client.py#L263), [`client.py:287`](../../../alibi/llm/client.py#L287). `test_s3a_retries_respect_each_transport_attempt_budget[process]` and `[task]`. |
| A modified cache entry bypasses the active role schema | Change the stored Challenger action from `lookup_settlement` to `dump_passport`. The next identical call returns the invalid action from disk. The fresh path refuses the same kind of enum violation. This requires a modified or corrupted local cache. | [`client.py:257`](../../../alibi/llm/client.py#L257), [`client.py:323`](../../../alibi/llm/client.py#L323). `test_s3a_does_not_replay_schema_invalid_cached_challenger_output`. |

The passing control verifies that a fresh response with an unknown action raises `SchemaValidationError` and that the LLM audit JSONL omits both the rejected action text and the input subject marker. See `test_s3a_live_parse_rejects_unknown_action_without_echoing_it_in_audit`.

Scoped retrieval and citation checks need the published gateway and role runner. Those checks include foreign or stale `(record_id, revision)` pairs, permitted lookup arguments, and settlement evidence at booking time. Policy application, downstream audit rendering, and bank-facing disclosures also need the integrated app. These probes show which data reaches the client boundary. They do not establish that a tool ran, a rule took effect, or a bank received private data.

Run the published unit suite and the red-team probes with expected failures enabled.

```sh
uv run --frozen --offline python -m pytest -q -rx alibi/tests agents/eval/test_redteam_llm.py
```

To reproduce the failing assertions directly, run the red-team file with `--runxfail`.

```sh
uv run --frozen --offline python -m pytest -q --runxfail --tb=short agents/eval/test_redteam_llm.py
```

## S1 core and the sharing boundary

Reproduced against the S1 merge on `main` at `42aebb3`, using signed synthetic inputs, a temporary encrypted passport and disposable keys. The reproduction file is `agents/eval/test_redteam_core.py`. No Gemini or HTTP calls are needed.

| Finding | Reproduced behaviour | Owner boundary |
| --- | --- | --- |
| Follow-up widens its parent’s scope | Bank B’s October request accepts a signed follow-up that starts in September. Parent issuer and subject are checked, but the requested interval is accepted unchanged. | [RFC intake](../../../alibi/core/ingest.py#L510). Enforce structural containment before saving the follow-up. |
| Settlement exceeds the five-day limit | A signed matching settlement dated 14 days before the credit leaves the source claim `supported` in both the investigation and the signed package. The exactly-five-day control still passes. | [Timing verdict](../../../alibi/core/reconcile.py#L254). Apply the published window to the effective settlement time as well as the payout candidate search. |
| Redacted package does not bind the subject | The holder’s policy withholds `subject_ref` and every attachment is an extract. The same signed package verifies for the correct and a different expected subject with the same request and recipient. | [Source consistency](../../../alibi/core/package.py#L962). Bind a recipient-scoped subject reference in signed scope or require trusted request-to-subject binding at verification. |
| Bank RFC identifiers share a global namespace | Bank B cannot submit an otherwise valid request when Bank A used the same external ID. The refusal reveals that the ID already exists. | [Request save](../../../alibi/core/ingest.py#L588). Scope externally supplied IDs by issuer. |
| Stored evidence can cross issuer boundaries | A correctly signed settlement from a different pinned merchant supports the original merchant’s payout when the lookup returns nothing and the record is already in the passport. | [Passport settlement fallback](../../../alibi/core/investigate_rules.py#L553). Require the matching payout’s issuer on both retrieval paths. |
| One payout supports two credits across RFCs | Two distinct credits with separate RFCs receive `supported` from the same payout. The existing duplicate check works inside a single investigation. | [Payout reuse](../../../alibi/core/investigate_rules.py#L412). Check competing links across the scoped credit history. |
| Pending settlement gap differs from the scorer contract | A request using `q.source_of_funds` keeps the pinned question ID and does not support the credit. It emits a sentence in `gap` where the evaluator expects `settlement_pending_at_booking`. | [Claim construction](../../../alibi/core/investigate_rules.py#L372). Preserve the sentence separately and emit the pinned code. |

The native core verifier accepted a package produced by `load_fixture` → `run_investigation` → `build_review` → `approve` → `sign_and_package`, while the earlier independent CLI rejected its native registry. The CLI now verifies both native and reference exports. For a native export, `--subject` requires the signed evidence to carry the expected subject and fails when a redaction makes that check unavailable. This changes the independent check only. The S1 app verifier issue above still needs its own fix.

```sh
uv run --frozen python -m pytest -q -rx agents/eval/test_redteam_core.py
uv run --frozen python -m pytest -q --runxfail --tb=short agents/eval/test_redteam_core.py
```

The first command records seven strict expected failures and a passing boundary control. The second reproduces the seven failing assertions directly. The published S1 interface is in-process, so these results establish core behaviour before the HTTP and S5 adapters apply their own checks.

## S5b scorer and retrieval accounting

Both probes use the first request of public Case 1 and a synthetic prediction derived from its public oracle. The same assertions run without an adapter, network calls or sealed data.

| Finding | Reproduced behaviour | Change needed |
| --- | --- | --- |
| Abstention hides an unsafe proposal | An `abstained` request carries a `proposed_package` containing a forbidden reference. It has no final package and correctly fails safe-complete, but the score drops the proposal and reports `unsafe_proposal=False`. | [Request scoring](../../../alibi/eval/scorer.py#L413). Score a supplied pre-policy proposal before handling the terminal outcome. Keep final disclosure and completion tied to the final package and outcome. |
| Potential visibility is treated as an actual lookup | The prediction cites a lookup-only merchant record with `retrieval.lookups=0`. The official scorer reports `safe_complete=True`. The same attachment is refused when its source is moved after the request time. | [Evidence visibility](../../../alibi/eval/common.py). Carry observed record references from the adapter into the score. Keep the time boundary and account for records persisted by an earlier authorised retrieval. |

```sh
uv run --frozen --offline python -m pytest -q -rx agents/eval/test_redteam_scorer.py
uv run --frozen --offline python -m pytest -q --runxfail --tb=short agents/eval/test_redteam_scorer.py
```

The first command records two strict expected failures and one passing time-boundary control. The second reproduces both failures. Compare pre-policy counts and lookup-backed completions after fixing the scorer and wiring the adapter's observed evidence.
