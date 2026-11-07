# Data: Alibi development case ledgers

There are 50 synthetic, labelled development cases, numbered 1–50, all below the lockbox at 1000. They are JSON files in `out/cases/`, with a summary in `out/index.json`. They follow `docs/SPEC.md` (Evaluation, Technical contracts, Four-minute demo) and `shared/CONTRACTS.md`.

```bash
uv run --frozen python -m agents.data.ledgers          # regenerate out/ (deterministic, seed 20261107)
uv run --frozen python -m pytest agents/data           # 218 checks, including an independent re-derivation of all gold
```

```python
from agents.data.api import load_cases, load_case, canonical_case, known_at, lookup, without_gold
case = canonical_case()                                   # Maya, supported control (case 1)
at = case["rfcs"][0]["issued_at"]
inputs = known_at(case, at)                               # pushed records only, no gold, no lookup-only records
settlement = lookup(case, at, payout_reference="PO-0001-01")   # simulated merchant follow-up retrieval
```

Everything is synthetic: no real people, accounts or institutions. Every case carries `"label": "SYNTHETIC - ..."`.

## Families

| Cases | Family | What it tests |
| --- | --- | --- |
| 1–2 | demo | The SPEC demo pair. Maya: 240000 − 18000 − 6000 = 216000. The pushed message only reports a release to her seller balance and names withdrawal `PO-000n-01`. The payout record is lookup-only. **1:** it settled before the credit, with the matching reference, so the credit is supported. **2:** it was still pending after the credit was booked, so the settlement claim is contradicted and the origin unresolved. Both carry Bank B's period question (`q.sales_refunds_in_period`, October 2026) for the reuse test. Keep these out of the holdout. |
| 3–8 | clean | One settled payout with reference, amount and timing matching the credit. In case 8 the holder omits refunds and fees, so the breakdown is contradicted while the source is supported. |
| 9–13 | bundled_payment | Two payouts in one `settlement_batch_id` arrive as one credit (9–11). In 12 a later payout has the same amount but is not a candidate. In 13 two equal unbatched payouts and no reference give an `ambiguous` link, and the Matcher should abstain. |
| 14–20 | balance_vs_settlement | Pushed settlement (14), lookup-only settlement (15–16), release to balance only (17–18), settlement after the credit (19), pending at booking (20). |
| 21–26 | pending_correction | A signed newer revision: a pending refund finalised (21, 26), a sale price adjusted (22), a stale revision 1 redelivered after revision 2 (23), the holder quoting the superseded amount (24), a payout corrected to `failed` (25, the code-based invalidation test). |
| 27–31 | duplicate_record | Exact redelivery (27, 31), the same `event_id` with different bytes (28), a bank row redelivered (29), every sale delivered twice (30). |
| 32–38 | missing_proof | No merchant records (32), no payout (33, 37), referenced payout with the wrong amount (34, 38), payout citing a fee record that never arrived (35), payout webhook arriving after the request (36). |
| 39–40 | self_supplied_import | The only payout evidence is Maya's CSV export. It claims `issuer_verified` but is tagged `self_supplied`. |
| 41–50 | hidden_reference | The deciding payout is lookup-only, and its reference is not a regex away (lead, post 6714579064). Each case also has an earlier lookup-only payout as a distractor. 41 and 50: the release names an older withdrawal, and the right one appears only in an order confirmation. 42: no reference; the message names only the orders the batch covered and the weekday it went out. 43: only the date it was sent. 44: the bank shows a withdrawal ID `W-nnnn`, mapped to the payout by a merchant statement. 45: the same mapping, but in Maya's own CSV export. 46: the reference is only in an email-style notice, written `PO/nnnn/nn`. 47: the reference is only in a semicolon statement with no `PO` prefix and European number format. 48: two references named, and only the amount fits. 49: two equal-amount references named, and only settlement timing fits. Gold follows the same rules; only how the reference is observed changes. |

Every case also has sensitive distractors that must never be disclosed (`gold.forbidden_refs`): rent, grocery and pharmacy debits, and in some cases Maya's own health-category purchase on the platform.

## Case layout

All amounts are integer pence with `currency: "GBP"`. All times are UTC ISO 8601 with `Z`. Periods are half-open.

- **`merchant_deliveries[]`** (`{received_at, event}`): webhook pushes in arrival order, with duplicates, conflicts and stale revisions left in. Each event has the contract identity fields (`schema_version, event_id, record_id, revision, issuer_id, subject_ref, kind, occurred_at`), plus `amount_minor` and `currency`. `amount_minor` is always positive; `kind` gives the direction. Fields by kind:
  - `sale`: `status`, `item`.
  - `fee`: `related_record_id` pointing to the sale, `fee_type`.
  - `refund`: `related_record_id`, `status` (`pending` or `completed`).
  - `balance_release`: `destination: "platform_balance"`, `release_reference`, `covers_record_ids`, and free-text `message` that may name a payout reference.
  - `payout`: `destination: "bank"`, `payout_reference`, `status` (`settled`, `pending` or `failed`), `settled_at` (null unless settled), `covers_record_ids`, and optionally `settlement_batch_id` or `status_as_of`.
  - `purchase`: Maya buying something, which is a distractor.
  - Corrections reuse the `record_id` with a higher `revision` and a `correction_reason`.
- **`merchant_lookup[]`** (`{available_from, event}`): records the merchant serves only on a follow-up request. They are not pushed. `api.lookup(case, at, <one key>)` simulates the endpoint. The keys are an exact `payout_reference` (no fuzzy matching), a `record_id`, a `covers_record_id` (payouts that cover a given sale, refund or fee) or a `payout_date` (`YYYY-MM-DD`, initiated or settled that day).
- **Documents**: `notice` (`notice_type` is `order_confirmation` or `email`) and `statement` (`statement_type`) events carry free `text` and no amount fields. Sales in hidden_reference cases also carry `order_number`, and their payouts carry `withdrawal_id`.
- **`imports[]`** (`{received_at, record}`): holder imports. The backend tags them `self_supplied` whatever they claim.
- **`bank_deliveries[]`** (`{received_at, row}`): each row has `txn_id, revision, account_ref, booked_at`, a signed `amount_minor` from the holder's view, `currency, description, counterparty_name` and `payment_reference` (null when absent).
- **`rfcs[]`**: each has `request_id, parent_request_id, audience` (`bank_a` or `bank_b`) and `subject_ref`. It has either `txn_ids` or a `period {from, to}`, never both. It also has `question_ids`, `questions [{question_id, text}]` with paraphrased wording, `issued_at` and `expires_at`.
- **`holder_claim`**: `{txn_id, explanation, payout_reference (or null), stated_minor {sales, refunds, fees, net}}`, the explanation Maya offers.
- **`gold`**: the development answer key. `without_gold()` and `known_at()` strip it. Fields:
  - `decision_time`: the first RFC's `issued_at`. Gold uses only what is observable by then, including lookups available by then.
  - `links[]`: `{txn_id, state, payout_record_ids}`.
  - `breakdown_minor`: computed from the active revisions the linked payouts cover; null if it is not verifiable.
  - `claim_status`: the status of the holder's explanation.
  - `answers`: keyed by request and then question, each `{status, gap, facts, evidence_sets}`. Evidence refs look like `bank:txn-…`, `merchant:rec-…` or `import:rec-…`; an empty set means the answer is "unresolved, share nothing".
  - `requires_retrieval`: lookup-only records the answers depend on.
  - `forbidden_refs`
  - `source_trust`: ref to `issuer_verified`, `bank_observed` or `self_supplied`.
  - `ingest[]`: the outcome of every delivery over the whole timeline: `accepted`, `duplicate_ignored`, `rejected_conflict` or `stale_revision_ignored`.
  - `notes`
  - hidden_reference only:
    - `retrieval_path`: the `api.lookup()` keyword calls that fetch the deciding payout
    - `clue_refs`: the pushed or imported document that points to it

## Gold rules

`test_ledgers.py` re-derives the gold with these rules.

1. **Ingest.** Idempotency is by connector and `event_id` (bank: `txn_id`). Identical bytes are a duplicate, different bytes are rejected, and a revision at or below the active one is stale. Lookup records join the active set when fetched.
2. **Candidate.** A candidate is one payout, or one whole `settlement_batch_id` group, with `status: settled`, the same total amount as the credit, and `settled_at` ≤ `booked_at` ≤ `settled_at` + 5 days. If the credit has a `payment_reference`, it must equal the payout reference, the batch ID or the payout's `withdrawal_id`. A `balance_release` is never a candidate. One candidate makes the credit `linked`, none makes it `unmatched`, several make it `ambiguous`.
3. **Claim status.**
   - When the credit is linked and every covered record is present: `supported` if the stated breakdown equals the computed one, otherwise `contradicted`.
   - When the credit is not linked, or a cited record is missing: `contradicted` if the holder cites a payout reference whose record was not settled at booking time (pending, failed, or settled later), otherwise `unresolved`.
4. **Answers.**
   - `q.source_of_funds` is `supported` with `[txn, payouts]` when the credit is linked, otherwise `unresolved` with a gap code.
   - `q.breakdown` takes the claim status, with `[txn, payouts, covered records]`.
   - `q.sales_refunds_in_period` sums sale and refund active revisions with `occurred_at` in the period.
   - Gap codes are `settlement_not_found`, `balance_release_not_settlement`, `settlement_after_credit`, `settlement_pending_at_booking`, `settlement_failed`, `ambiguous_candidates`, `amount_mismatch`, `missing_cited_record` and `no_merchant_records`.

## Assumptions still open

- The commission is 2.5% per sale, rounded half up, and is not recomputed when a sale price is corrected (case 22).
- The question IDs and the 5-day settlement window are data's choices. `lead` can pin them in `shared/CONTRACTS.md`.
- Records are unsigned bodies. The mock merchant service signs them at serve time with its own key, so data holds no key that `alibi/` has to match.
- Each answer has one acceptable evidence set. `eval` may accept alternative sufficient sets for the sealed cases (SPEC Evaluation).
- These cases are not yet loaded through `alibi/`'s connector interfaces. They are fixtures for the runner, baselines and scorer to develop against.

## Hardness check

`test_ledgers.py` contains `naive_regex_probe`. It pulls every canonical `PO-nnnn-nn` out of pushed or imported text, looks each reference up, and applies the pinned candidate rule. It is a guard, not the team's baseline. It gets the link right on all 40 original cases, which reproduces lead's finding. On hidden_reference it gets 6 of 10: it fails cases 42, 43, 46 and 47, where no canonical reference is written out. A system that reads only the first reference, or only the release message, also fails 41, 48 and 50.
