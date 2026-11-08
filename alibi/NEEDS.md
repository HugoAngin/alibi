# Needs from other sessions (S3, Gemini agents)

Things this session could not do inside `alibi/agents/` and `alibi/baselines/`. Each names the
file another session owns and the exact change.

1. **`alibi/api.py` dispatch on mode.** `run_investigation(..., mode="agents")` should call
   `alibi.agents.runner.run_investigation(store, request, client=..., gateway=..., now=...)` and
   `mode="single_call"` should call `alibi.baselines.one_pass.run_investigation(...)`. Both take
   the same `(store, request, *, gateway, settlement_lookup, txn_refresh, budget, registry, now)`
   arguments as the rules version plus an optional `client` (default `GeminiClient()`, or the
   replay client when `ALIBI_REPLAY=1`). Until then callers import the runner directly.
2. **Gateway lookup keys.** The team merchant lookup takes `covers_record_id` and `payout_date`
   (`shared/CONTRACTS.md`, Questions and matching). `alibi/gateway.py` only has
   `lookup_settlement(payment_ref)` and `refresh_txn(txn_id)`. `alibi/agents/actions.py` serves
   `lookup_payouts_covering` and `lookup_by_date` from the passport listing meanwhile; the
   integration session should add connector-backed `lookup_payouts(covers_record_id=...)` and
   `lookup_payouts(payout_date=...)` to the gateway and point `actions.py` at them.
3. **Replay recordings live under `alibi/fixtures/replays/`.** The S3 brief asks for
   `fixtures/replays/<case>_<variant>.json`; this session added only that new directory and did
   not touch the existing fixture files or `alibi/fixtures/maya.py`.
