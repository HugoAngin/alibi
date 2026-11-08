# Case schema to core types: the mapping

`alibi/connectors/case_source.py` turns the team's case objects (`agents/data/README.md`, "Case
layout") into the core wire types (`alibi/core/schemas.py`). It interprets nothing: no reference
is parsed out of free text, no status is guessed, no field is invented. What the core type cannot
hold under its own name travels beside the signed body in an `extra` dict (never inside it, since
the core models reject unknown fields), so nothing from the case is lost on the merchant or bank
side, and the passport stores exactly what the core schema allows.

Every `map_*` function validates its result with the core model and raises `MappingError` (a
`ValueError` naming fields only) when a case object does not fit.

## Merchant events (`map_merchant_event`, `map_import`)

Case kind to core `kind`:

| Case kind | Core kind | Why |
| --- | --- | --- |
| `sale` | `order` | Money in to the seller's balance. |
| `fee` | `fee` | Subtracts. |
| `refund` | `refund` | Subtracts. |
| `purchase` | `receipt` | The holder buying something: an outflow, like S1's retail receipt. A sensitive distractor the policy can deny. |
| `receipt` (imports) | `receipt`, or `message` when it carries no amount | A photographed receipt with text only is a document, not money. |
| `balance_release` | `message` | A release to the platform balance is not a bank settlement and never a settlement candidate. The message text travels verbatim in `text`. |
| `payout` | `payout` | The case payout carries its own settlement status and time, so it is one core payout with `status` and `effective_at`. There is no separate core `settlement` record: the store's record identity includes `kind`, so one record cannot be both. |
| `notice`, `statement` | `message` | Free text, no amount. |
| `order_completed`, `order_returned`, `service_charge`, `weekly_statement`, `earnings_update` (second platform, schema `sp-2.1`) | `order`, `refund`, `fee`, `payout`, `message` | `agents.data.api.to_contract` first maps the raw event onto the contract field names (`value_pence` → `amount_minor`, `transfer_ref` → `payout_reference`, `transfer_status` → `status`, `paid_at` → `settled_at`, `line_record_ids` → `covers_record_ids`); the rows above then apply. `extra.case_kind` keeps the raw kind. A reference such as `SPLAT W37 4588` keeps its spaces: the core `Ref` pattern allows internal spaces. |

Field by field (case field, core field):

| Case field | Core field | Notes |
| --- | --- | --- |
| `schema_version` ("1") | `schema_version` = `alibi.merchant_event.v1` | |
| `event_id`, `record_id`, `revision`, `issuer_id`, `subject_ref`, `occurred_at` | same names | Identity passes through unchanged. IDs such as `rec-0001-021` and `maya-001` fit the core `Ident` pattern. |
| `amount_minor`, `currency` | `amount_pence`, `currency` | Integer pence, positive; the direction comes from the kind. |
| `status` | `status` | Unchanged (`completed`, `pending`, `settled`, `failed`). Also kept in `extra`. |
| `payout_reference` (payout) | `payment_ref` | The exact reference. |
| `settlement_batch_id` (payout) | `settlement_ref` | A grouping reference for bundled payouts. Also in `extra`. |
| `settled_at` (payout) | `effective_at` | Null when not settled, so pending and failed payouts have no effective time. Also in `extra`. |
| `covers_record_ids` (payout, balance_release) | `related_record_ids` | Also in `extra`. |
| `related_record_id` (fee, refund) | `related_record_ids` (one entry) | Also in `extra`. |
| `release_reference` (balance_release) | `payment_ref` | The release's own reference (`REL-0001-01`), never a reference quoted in the message. Also in `extra`. |
| `message` (balance_release) | `text` | Verbatim. Always data, never instructions. |
| `note` (payout, lookup results) | `text` | Verbatim: the injection carrier reaches the system under test exactly as the merchant serves it. |
| `text` (notice, statement, receipt) | `text` | Verbatim. |
| `item` | `items[0].description` | |
| `category` | `items[0].category` | |
| `order_number` (sale) | `payment_ref` (as a string) | A buyer payment reference. Also in `extra`. |
| `correction_reason` | `correction_reason` + `corrects_revision` = `revision` - 1 | The core needs both. |
| `destination`, `withdrawal_id`, `status_as_of`, `fee_type`, `notice_type`, `statement_type` | none | `extra` only. `withdrawal_id` has no core slot, so a bank reference that names a withdrawal ID (cases 44, 45) cannot be matched by the core rules engine; see `alibi/NEEDS.md`. |
| `import_source`, `claimed_trust` (imports) | none | `extra` only. `claimed_trust` is a trust claim; the import path tags the stored record `self_supplied` and moves its id into the `self_` namespace whatever the record says. |

`extra` always carries `case_kind` (the original kind) and, for imports, `tagged: "self_supplied"`.

## Bank rows (`map_bank_row`)

| Case field | Core field | Notes |
| --- | --- | --- |
| `txn_id`, `revision`, `account_ref`, `currency`, `booked_at`, `description` | same names | |
| `amount_minor` | `amount_pence` | Signed from the holder's view (a credit is positive). |
| `payment_reference` | `refs` (one entry, or `[]` when null) | Also in `extra`. |
| `counterparty_name` | none | `extra` only. The core `BankTxn` has no counterparty field; see `alibi/NEEDS.md`. |
| (connector) | `subject_ref` | The connection's subject, set by the connector. |
| (connector) | `provenance` | `connector: bank_feed`, `connection_id`, `retrieved_at` (the feed's clock when read), `source_version` (`mock_bank_feed.v1`), `cursor` (the page the row came from). |

## Requests for Context (`map_rfc`)

| Case field | Core field | Notes |
| --- | --- | --- |
| `request_id`, `parent_request_id`, `subject_ref`, `audience`, `questions`, `issued_at`, `expires_at` | same names | `parent_request_id` is omitted when null. |
| `audience` | `issuer` | The bank that asks is the bank that receives. The bank service signs the request with that bank's key. |
| `txn_ids` (non-empty) or `period` (object) | exactly one of `txn_ids` / `period` | The other is omitted. Both or neither is a `MappingError`, matching the pinned RFC scope rule. |
| `question_ids` | none | Derivable from `questions`; kept in `extra`. |

## The simulated clock (`CaseClock`)

`as_of` defaults to the case's first RFC `issued_at` (`2026-11-02T09:00:00Z` for case 1). A
pushed delivery is visible once `received_at <= as_of`, a lookup-only record once
`available_from <= as_of`, an RFC once `issued_at <= as_of`. The clock only moves forward.
`CaseClock.lookup(**one_key)` is `agents.data.api.lookup` at the clock's moment, so the merchant
service serves exactly what the data layer defines, including the case 2 payout that appears only
from `2026-10-29T06:35:00Z` (five minutes after its `status_as_of`).

## Keys

The core pins `merchant_demo` to the S1 issuer `threadline`; the cases' issuer is `mock-marketplace`.
`alibi/connectors/keys.py` adds `merchant_mock` (record signing, issuer `mock-marketplace`) to the
demo keystore. `connector_keystore()` writes a five-key `trust_registry.json`, a superset of the
core's. `alibi.api.default_keystore()` would rewrite it with four keys, so pass the connector
keystore and its registry explicitly (`alibi/NEEDS.md` asks the core to adopt the fifth key).
