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
4. **Core policy targets for the Gemini parser (`alibi/core/policy.py`, `alibi/core/schemas.py`).**
   `alibi.agents.policy_parse` maps the team's typed proposals (`agents.execution.api.compile_policy`
   shape) to `DenyRule` / `GeneraliseRule` / `AgeRule` / `RecipientRule`. Core cannot express, and
   the parser lists under `PolicyParse.core_reasons`: (a) `deny` or `recipient_restrict` with
   target `category` or `merchant` (core rules scope by record kind only; add `categories: list[str]`
   and `issuers: list[Ident]` to `_RuleBase`, matched against `items.category` and `issuer_id`);
   (b) `recipient_restrict` narrower than the whole package (`RecipientRule` has no target; add the
   same scoping fields plus `fields`); (c) record kinds `bank_credit` / `bank_debit` (bank rows have
   no `RecordKind`; a sign-based scope on `BankTxn.amount_pence` would do); (d) the team fields
   `counterparty_name`, `destination`, `release_reference`, which have no core field path; (e)
   `alert_preference`, which is not a disclosure rule and needs a home outside `Policy.rules`.
   Until then the integration session should enforce those proposals with
   `agents.execution.api.apply_policy`, or hold the package when any such rule is present.
5. **Usage on a schema failure (`alibi/llm/client.py`).** `SchemaValidationError` carries only `problems`,
   so a billed call whose answer failed the schema is counted as one `model_calls` by both the agents
   runner (`alibi.agents.roles.call_cost`) and the one-pass baseline, but its tokens are reported as 0.
   Add `usage: Usage` to `SchemaValidationError` (the client already has it in `_parse_and_validate`)
   and `roles.run_role` / `one_pass.propose` will pick it up through `getattr(exc, "usage", None)`.
