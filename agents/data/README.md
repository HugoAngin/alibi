# Data: Alibi development case ledgers

25 synthetic, labelled development cases (numbers 1–25, all below the lockbox at 1000), as JSON in `out/cases/` with a summary in `out/index.json`.

```bash
uv run --frozen python -m agents.data.ledgers          # regenerate out/ (deterministic, seed 20261107)
uv run --frozen python -m pytest agents/data           # checks, including an independent replay of every gold label
```

```python
from agents.data.api import load_cases, load_case, canonical_case, without_gold, known_at
case = canonical_case()                 # Maya: 240000 - 18000 - 6000 = 216000 pence
inputs = without_gold(case)             # what a system under test may see
as_of_rfc = known_at(case, case["rfcs"][0]["issued_at"])   # point-in-time view
```

All records are synthetic. No real people, accounts or institutions are involved. Every case carries `"label": "SYNTHETIC - ..."`.

## Families

| # | Family | Variants | What it tests |
| --- | --- | --- | --- |
| 1 | clean | `canonical_maya` | The demo fixture. Two RFCs: `bank_a` asks about the credit, then `bank_b` asks a different question (income in `[2026-10-01, 2026-11-01)`), answered from the same passport. |
| 2–5 | clean | a, b, c, `omits_refunds_fees` | One payout settles one credit. In the last variant the holder states sales equal to the net, so the claim is `contradicted`. |
| 6–9 | bundled_payment | a, b, `later_same_amount`, `two_candidates` | Two payouts sharing `settlement_batch_id` arrive as one credit. A later payout with the same amount is not a candidate. Two unbatched equal payouts make the link `ambiguous`. |
| 10–13 | balance_vs_settlement | settled, `release_only`, `settles_after_credit`, settled | A `balance_release` to the platform balance is never a bank settlement. A payout after the credit never explains it. |
| 14–17 | pending_correction | `refund_completed`, `sale_adjusted`, `stale_redelivery`, `holder_uses_old` | Revision 2 supersedes revision 1. A stale revision redelivered late is ignored. A holder quoting the old amount is `contradicted`. |
| 18–21 | duplicate_record | `exact_redelivery`, `conflicting_bytes`, `bank_row_redelivery`, `double_count_trap` | Idempotency by connector and `event_id`/`txn_id`. Identical bytes are ignored and different bytes are rejected. Counting duplicates would overstate sales. |
| 22–25 | missing_proof | `no_merchant_data`, `no_payout`, `amount_mismatch`, `missing_covered_record` | No support exists, so the claim is `unresolved`. In case 25 the payout links to the credit but cites a fee record that never arrived. |

Gold counts: 16 supported, 2 contradicted, 7 unresolved; link states are 19 linked, 5 unmatched and 1 ambiguous.

## Case layout

All amounts are integer pence with `currency: "GBP"`. All times are UTC ISO 8601 with `Z`.

- `merchant_deliveries[]`: `{received_at, event}` in arrival order, duplicates and stale redeliveries included. The event fields follow `shared/CONTRACTS.md` (`schema_version, event_id, record_id, revision, issuer_id, subject_ref, kind, occurred_at`) plus `amount_minor` (always positive, so the sign comes from `kind`) and `currency`.
  - `kind` is one of `sale`, `refund`, `fee`, `balance_release` or `payout`.
  - Refunds and fees carry `related_record_id`, which points to the sale.
  - Refunds also have `status`, either `pending` or `completed`.
  - Payouts carry `destination: "bank"` and `covers_record_ids`, and bundled payouts add `settlement_batch_id`.
  - A `balance_release` carries `destination: "platform_balance"`.
- `bank_deliveries[]`: `{received_at, row}`, where the row is `txn_id, revision, account_ref, booked_at, amount_minor` (signed, from the holder's view), plus `currency, description, counterparty_name`.
- `rfcs[]`: `request_id, parent_request_id, audience` (`bank_a` or `bank_b`), `subject_ref`, `txn_ids` or `period {from, to}` (half-open), `question_ids, issued_at, expires_at`.
- `holder_claim`: `{txn_id, explanation, stated_minor: {sales, refunds, fees, net}}`, the explanation Maya offers.
- `gold` (the development answer key):
  - `links[]`: `{txn_id, state, payout_record_ids}`
  - `active_revisions`: record to revision
  - `breakdown_minor`: computed from active covered records, or `null` when it cannot be verified
  - `claim_status`
  - `ingest[]`: the outcome for each delivery, one of `accepted`, `duplicate_ignored`, `rejected_conflict` or `stale_revision_ignored`
  - `notes`

## Assumptions to confirm against `alibi/SPEC.md`

I wrote these without the spec, because it is not on `main`. Post a `need` to `data` if any of them differ.

1. **Matching rule for gold.** A credit has a candidate when one bank payout, or one whole settlement batch, has the same amount, `occurred_at` ≤ `booked_at`, and is at most 5 days before it. One candidate makes it `linked`, several make it `ambiguous`, and none makes it `unmatched`.
2. **Claim status.** `supported` means the link exists and the stated breakdown equals the one computed from records. `contradicted` means the link exists but the breakdown differs. `unresolved` means there is no exact link, or a cited record is missing.
3. **Fees.** The fee is 2.5% of each sale, rounded half up, and is not recomputed when a sale price is later corrected (case 15).
4. **Question IDs** (`q.source_of_funds`, `q.breakdown`, `q.income_in_period`) are placeholders.
5. **No signatures.** Records are unsigned bodies. The mock merchant service signs them at serve time with its own key, so there is no key to keep in sync with `alibi/`.
6. **Trust.** Merchant deliveries come from the merchant connector (`issuer_verified` once signed). No case uses a holder import (`self_supplied`) yet.
