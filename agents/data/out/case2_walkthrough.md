# Case 2 walkthrough: the contradiction variant

Case 2 (`settlement_pending`) tells the same story as case 1, with the same amounts (240000 − 18000 − 6000 = 216000 pence) and the same history, except for the settlement. The payout that the merchant message names was **still pending after the credit was booked**. Source: `agents.data.api.load_case(2)`. The gold is re-derived independently in `agents/data/test_ledgers.py`.

Use this page to tell the two runs apart on screen. The steps follow `demo_script.md`.

## Step-by-step: what must differ from case 1

| Step | Case 1 (supported control) | Case 2 (contradiction) | Look for |
| --- | --- | --- | --- |
| Start: history | Sales, refunds and fees as in case 1, with record IDs `rec-0001-*`. | Identical sales, refunds, fees and purchase, with IDs `rec-0002-*`. | Same totals. |
| Start: the credit | `txn-0001-004`, +£2,160.00, booked 2026-10-28 06:30Z, **"MOCK MARKETPLACE PAYOUT PO-0001-01"** from Mock Marketplace Payments, `payment_reference: PO-0001-01`. | `txn-0002-004`, +£2,160.00, booked 2026-10-28 06:30Z, **"FASTER PAYMENT"** from **Synthetic Sender**, `payment_reference: null`. | Case 2's credit carries **no marketplace reference** and has a different sender. |
| Start: alert | Unexplained credit (no settlement linked). | The same alert. | No difference yet. |
| 0:25 Release arrives | `rec-0001-020` balance_release, "… Withdrawal **PO-0001-01** … has been requested." | `rec-0002-020`, the same text but naming **PO-0002-01**. | The withdrawal reference. |
| 0:25 Reconciliation | `240000 − 18000 − 6000 = 216000`, which equals the credit. | The same arithmetic, which also equals the credit. | **The amounts reconcile in both.** This is the point: arithmetic alone cannot decide. |
| 0:25 Link state | Unmatched (the release is not a settlement). | Unmatched. | No difference yet. |
| 0:45 Bank A RFC | `rfc-0001-01`, audience `bank_a`, `txn_ids [txn-0001-004]`. | `rfc-0002-01`, audience `bank_a`, `txn_ids [txn-0002-004]`. The question wording is paraphrased: "Provide evidence of where the £2,160.00 credited to your account on 2026-10-28 came from." and "How does the credit reconcile to your underlying sales, refunds and fees?" | The RFC ID. |
| 0:45 Holder's claim | The credit is payout PO-0001-01 (sales 240000, refunds 18000, fees 6000). | The credit is payout **PO-0002-01** (same stated amounts). | |
| 1:25 Lookup result | `rec-0001-021`, `PO-0001-01`, status **settled**, `settled_at` **2026-10-27 16:00Z**, before booking. | `rec-0002-021`, `PO-0002-01`, status **pending**, `settled_at` **null**, `status_as_of` **2026-10-29 06:30Z**, a day after booking. Retrievable from 2026-10-29 06:35Z, before the RFC. | **settled vs pending**, and **before vs after** the booking time. |
| 1:25 Link state | **Linked** to `rec-0001-021`. | **Unmatched**. No settled payout fits the amount, timing and reference rule. | Link badge. |
| 1:25 Holder's settlement claim | **Supported**. | **Contradicted**: the cited payout was not settled when the credit was booked. | Claim badge, double border. |
| 1:25 `q.source_of_funds` | **Supported**. Evidence: `bank:txn-0001-004`, `merchant:rec-0001-021`. | **Unresolved**, gap `settlement_pending_at_booking`. No evidence set. | Unresolved badge, dashed. |
| 1:25 `q.breakdown` | **Supported**. Evidence: the credit, the payout and the 18 covered records. Facts: sales 240000, refunds 18000, fees 6000, net 216000. | **Unresolved**, same gap. The arithmetic is shown but establishes nothing about origin. | |
| 1:25 Trace status line | "Settlement precedes booking and the cited amounts reconcile." | "PO-0002-01 was still pending at 2026-10-29 06:30Z, after the credit was booked at 2026-10-28 06:30Z. Origin unresolved." (wording is the build's) | |
| 1:50 Review: included | Credit and payout, plus breakdown items. | **Nothing is needed for Bank A's questions** (gold evidence set is empty). The package states the gap. Any item included counts as unnecessary in scoring. | A minimal package. |
| 1:50 Review: excluded | Rent, grocery, pharmacy, eyewear purchase. | Rent `txn-0002-001`, grocery `-002`, pharmacy `-003`, eyewear `rec-0002-019`. These are forbidden in both cases. | |
| 1:50 Refused edit | Same mechanics. | Same mechanics, if there is anything to edit. With an empty package, demonstrate the refusal on case 1 instead. | |
| 2:25 Bank A package | Authentication valid; trust: issuer verified and bank observed; claim status **Supported** for both. | Authentication valid; little or no source trust to show; claim status **Unresolved** for both, with the gap shown. | "Valid package" sitting next to "unresolved claims": authentication ≠ truth. |
| 2:25 Disposition | **clear** (with a note). | **request_more**: ask Maya for settlement proof of the £2,160 credit. A follow-up has a new `request_id` with `parent_request_id: rfc-0002-01`, and any new disclosure needs fresh consent. | |
| 2:45 Bank B reuse | `rfc-0001-02`, October period, **Supported**: sales 240000 (8), refunds 18000 (2), 10 sale and refund records. | `rfc-0002-02`, the same question and the same answer from `rec-0002-*` records. **Unaffected** by the settlement problem. | Reuse works whether or not Bank A's question resolved. |

## What gold says (case 2)

```json
{"links": [{"txn_id": "txn-0002-004", "state": "unmatched", "payout_record_ids": []}],
 "claim_status": "contradicted",
 "answers": {
   "rfc-0002-01": {"q.source_of_funds": {"status": "unresolved", "gap": "settlement_pending_at_booking", "evidence_sets": [[]]},
                   "q.breakdown":       {"status": "unresolved", "gap": "settlement_pending_at_booking", "evidence_sets": [[]]}},
   "rfc-0002-02": {"q.sales_refunds_in_period": {"status": "supported",
                   "facts": {"sales_minor": 240000, "refunds_minor": 18000, "sale_count": 8, "refund_count": 2}}}},
 "requires_retrieval": [],
 "forbidden_refs": ["bank:txn-0002-001", "bank:txn-0002-002", "bank:txn-0002-003", "merchant:rec-0002-019"]}
```

`requires_retrieval` is empty because no answer cites the pending payout. A good system still has to look it up to *know* that the claim is contradicted, rather than unresolved for lack of trying. The scorer counts the claim status separately from the evidence sets.

## If the screen shows something else

- **Case 2 shows Supported:** the matcher accepted the amount without checking settlement status and timing. This is the failure the demo exists to prevent. Stop and switch to case 1.
- **Case 2 shows Contradicted for `q.source_of_funds`:** that is too strong. The evidence contradicts the *settlement* claim, but the money's origin is simply unknown, so it should be Unresolved. Tell lead or research; do not present it.
- **Case 1 shows Unresolved after the lookup:** the lookup did not happen or was not used. Open the trace's Lookups step. If it is empty, the Challenger did not call the merchant. Use the replay.
