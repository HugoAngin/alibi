# Needs from S2 (connectors and bank side)

Changes S2 needs in files it does not own (`alibi/core`, `alibi/gateway.py`, `alibi/api.py`,
`alibi/fixtures`, `shared/CONTRACTS.md`). Each is worked around locally as described.

## 1. Pin `merchant_mock` in the core demo keys (`alibi/core/signing.py`)

`DEMO_KEYS` pins `merchant_demo` to the S1 fixture issuer `threadline`. The team's cases are issued
by `mock-marketplace`, and `ingest_merchant_event` refuses a key pinned to another issuer. S2 adds
`merchant_mock` (record signing, issuer `mock-marketplace`) in `alibi/connectors/keys.py` and
writes a five-key `trust_registry.json`. Because `alibi.api.default_keystore()` rewrites that file
with the core's four keys, every passport call in the integrated demo must pass the connector
keystore and its registry explicitly (`run_investigation(..., registry=...)`,
`sign_and_package(..., keystore=...)`, `verify_package(..., registry=...)`).

Ask: add `"merchant_mock": (KeyPurpose.RECORD_SIGNING, "mock-marketplace")` to `DEMO_KEYS` so the
default keystore and registry carry it too.

## 2. Accept the case payout as settlement evidence (`alibi/gateway.py`, `alibi/core/investigate_rules.py`, `alibi/core/reconcile.py`)

The team's data model folds settlement status into the payout record (`status` settled | pending |
failed, `settled_at`); there is no separate settlement record, and the store's record identity
includes `kind`, so one record cannot be served as both a `payout` and a `settlement`. S2 maps a
case payout to one core `payout` with `status` and `effective_at` (`MAPPING.md`), and the merchant's
`GET /settlements?payout_reference=` returns that record.

Today `Gateway.lookup_settlement` rejects anything but `kind: settlement`, `_Run.settles` requires
`kind == settlement`, and `reconcile.settlement_effective_time` returns `None` for a payout. So on
the team's cases the rules engine never retrieves the lookup-only payout and answers `unresolved`.

Ask: treat a `payout` record that carries `status` (and `effective_at` when settled) as settlement
evidence for its own `payment_ref`, in the gateway's result check, in `settles()` and in
`settlement_effective_time` / `check_settlement_timing`. The timing rules stay the same: settled
with `effective_at <= booked_at` supports; pending observed at or after booking, failed, or
effective after booking contradicts.

## 3. Let the gateway look up a reference carried in the subject's own evidence text

`Gateway.lookup_settlement` only allows a `payment_ref` carried in an issuer-verified record's
`payment_ref` field. In case 1 the withdrawal reference `PO-0001-01` appears only in the balance
release's message text (`text` after mapping) and on the bank credit's `refs`. S2 does not parse
text in the connector (that would hand every system the hidden-reference answers), so the agents
and the rules engine cannot ask for `PO-0001-01` through the gateway.

Ask: also allow a reference that appears on one of the subject's own bank transactions (`refs` or
description tokens) or in the `text` of the subject's issuer-verified records, still bound to the
subject's own issuers.

## 4. Core slots for `counterparty_name` and `withdrawal_id`

`BankTxn` has no counterparty field, so the bank row's `counterparty_name` ("Mock Marketplace
Payments", "Payflux Holdings") stays in the connector's `extra` and never reaches the passport; the
`merchant_unverified` alert rule (`agents/data/README.md`) needs it. A payout's `withdrawal_id`
(`W-5716`, cases 44 and 45, matched by the bank's `payment_reference`) has no core slot either.

Ask: add optional `counterparty: ShortText | None` to `BankTxn` and `withdrawal_ref: Ref | None`
to the merchant record, or confirm `description` / `settlement_ref` as the intended carriers.

## 5. Refusal codes to pin (`shared/CONTRACTS.md`, Refusals row)

S2's services use: `unauthenticated`, `not_found`, `unknown_case`, `unknown_event`, `unknown_record`,
`unknown_request`, `invalid_request`, `invalid_time`, `invalid_query`, `invalid_correction`,
`invalid_disposition`, `invalid_package`, `method_not_allowed`, `clock_backwards`, `request_expired`, `audience_mismatch`,
`package_id_reused`, `package_rejected`, `disposition_final`, `scope_wider_than_parent`,
`sink_unavailable`. `audience_mismatch`, `package_id_reused`, `request_expired`, `disposition_final`
and `scope_wider_than_parent` already appear in `agents/execution/out/consent_matrix.md`.

## 6. The pinned period question is classed as "several totals" (`alibi/core/investigate_rules.py`)

`q.sales_refunds_in_period` reads "Please provide documented marketplace sales and refunds between
01 October 2026 and 31 October 2026." `classify_question` sees two kinds (orders, refunds) and no
net cue, so the rules engine answers `unresolved` ("names several totals") and bank_b's package
carries no evidence. Data's gold expects `supported` with the sales and refund records attached
(two documented totals, not a net figure).

Ask: when a period question names several kinds without a net cue, answer with one documented
total per kind (two calculations, or one claim per kind), instead of `several_totals`.

## 7. A redelivered superseded revision is reported `duplicate`, not `stale` (`alibi/core/store.py`)

Case 23 redelivers revision 1 of `rec-0023-009` after revision 2 was accepted. `put_source` finds
the same `(record_id, revision)` row with identical content and returns `duplicate`, although that
row is no longer active; data's gold calls it `stale_revision_ignored`. The passport state is
right (revision 2 stays active, both revisions in history); only the outcome label differs. S2's
merchant log reports the sink's word, so eval's ingest comparison must accept `duplicate` for
that outcome (the merchant tests do).

Ask: return `stale` when the matched existing row is inactive (`PutResult.active` is already
`False` there).
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
