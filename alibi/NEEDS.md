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
`invalid_disposition`, `invalid_package`, `clock_backwards`, `request_expired`, `audience_mismatch`,
`package_id_reused`, `package_rejected`, `disposition_final`, `scope_wider_than_parent`,
`sink_unavailable`. `audience_mismatch`, `package_id_reused`, `request_expired`, `disposition_final`
and `scope_wider_than_parent` already appear in `agents/execution/out/consent_matrix.md`.
