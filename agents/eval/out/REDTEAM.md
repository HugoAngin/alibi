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
