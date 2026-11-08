# Retrieval accuracy: payout matching and retrieval (public development only)

Status: staged, not committed. Public 80-case development diagnostic only. This is not a
fresh unseen result, and it says nothing new about the frozen unseen capture.

- Patch: `agents/eval/out/RETRIEVAL_ACCURACY.patch`. It is a unified diff against the
  combined tree `alibi-all-fixes-full-um0dauzf`. `git apply --check` passes there.
- Files:
  - `alibi/core/investigate_rules.py` (functions only);
  - `alibi/connectors/clients.py` (one lookup-key pattern);
  - a new test file, `alibi/tests/test_settlement_batch.py`, with 13 tests.
- No schema, mapping, gateway, scorer, gold, policy, package or one-pass changes.
- **Overlap with root:** both patches touch `alibi/core/investigate_rules.py`. Root's
  `pending-gap-proposed.patch` edits `_Run.linked`, which this patch does not touch. The two
  apply in either order. I checked with `git apply` on a scratch copy.

## Measured (offline rules, no model, cases 1-80, unchanged scorer)

| tree | rules safe_complete | forbidden | unnecessary |
| --- | --- | --- | --- |
| combined tree (baseline) | 41/80 | 0 | 47 |
| + batch grouping only (step 1) | 45/80 | 0 | 47 |
| + issuer-text retrieval (step 2) | 50/80 | 0 | 47 |
| + named-pending handling (step 3) | 55/80 | 0 | 47 |
| + spaced query reference (step 4) = this patch | **56/80** | 0 | 47 |
| this patch + root pending-gap patch | **58/80** | 0 | 47 |

I reran the final numbers from a **fresh copy** of the combined tree with only this patch
applied, and again with root's patch added.

- Cases gained by this patch: 2, 9, 10, 11, 12, 20, 41, 44, 48, 49, 50, 52, 58, 59 and 73.
  No case was lost. With root's patch, 68 and 74 are gained as well.
- Final patch on the fresh tree: link state accuracy went from 0.713 to 0.838, match
  precision from 0.837 to 0.864, and match recall from 0.707 to 0.879.
- One-pass with FakeClient (empty proposal): 45/80 before and after, with the same case set,
  0 forbidden items and 12 unnecessary items. One-pass has its own matcher in
  `alibi/agents/candidates.py`, so it keeps its fixed strategy. The agents mode is unchanged.
- Tests: on the fresh patched tree, all 1430 `alibi/tests` passed. I deselected
  `test_eval_seal.py`, which needs `git rev-parse HEAD`, and my copy has no `.git`. In an
  earlier run, the `test_demo_services` Ctrl-C timing test failed once while two suites ran
  in parallel. It passed when rerun alone.

Commands, run from a copy of the combined tree with `PYTHONPATH=<copy>`, using the shared
`.venv` python:

```
python -m alibi.adapter --mode rules --cases 1-80 --out data/alibi/p3
python -m alibi.eval.scorer --predictions data/alibi/p3/rules.jsonl --json score.json
python -m pytest -q alibi/tests/test_settlement_batch.py
```

## Changes

1. **Issuer settlement batches** (`_settlement_batch` and `_Run.linked_batch`). A credit
   that carries a `settlement_ref` (from the merchant's `settlement_batch_id`) links to a
   batch only when all of these hold:
   - Two or more payouts carry that reference.
   - Every member is **issuer-verified** and comes from **one issuer**.
   - The members have distinct payment references.
   - Every member is in the credit's currency and occurred at or before the booking.
   - The member amounts sum **exactly** to the credit.

   Then each member is checked on its own:
   - It must have its own settlement (`settlement_for`).
   - It must have no `_conflict`; a conflict makes the credit contradicted.
   - Its own components must reconcile to its amount.

   Finally the union of the members' components must reconcile to the credit, and every
   member's settlement timing must pass `credit_supported`. If anything fails, the credit is
   unresolved.

   Self-supplied records never join a batch, and a credit that names two batches matches
   neither. The facts are `payout_references` and `settled_at` for each member. The link's
   `record_ref` is the first member. The adapter already reports the other cited payouts.
2. **Bounded retrieval from explicit issuer references** (`_Run.fetch_by_issuer_text`).
   This step runs only when nothing in the passport or on the credit matches.
   - It reads the reference tokens written in the text of the subject's **issuer-verified**
     records (messages, notices and statements).
   - It looks up at most `MAX_REFERENCE_LOOKUPS` (2) of them, as written. A line that also
     carries one of the credit's own references comes first, for example an issuer
     statement row that maps `W-2379` to `PO-0044-02`.
   - The gateway's existing scope and signer checks still apply.
   - A returned record becomes a candidate only if it is an issuer-verified payout of this
     subject with exactly that reference, from the same issuer as the text that named it.
     It must then match **uniquely by amount, currency and the 5-day window**.

   Only then does `_verdict` accept "issuer text names the reference" in place of the credit
   carrying it. Settlement timing and reconciliation are still required. Message text never
   becomes evidence and is never cited or attached, and holder text never opens a lookup.
3. **Named payout still pending.** If issuer text names a payout that is pending and fits
   the credit's amount and date, it is not linked. The credit stays unmatched with the gap
   `settlement_pending_at_booking`. This is what gold expects for cases 2 and 20, and for
   the injection variants 52, 58 and 59 built on case 2. **Root:** this is pending-gap
   territory but sits in the unlinked path, not the `linked` code your patch changes.

4. **Spaced references in lookups** (`clients.py`). The second platform's transfer reference
   `SPLAT W32 2695` is a valid core `Ref`. However, `MerchantClient.lookup` rejected it with a
   `ValueError`, so the gateway reported the lookup as unavailable.
   - Now `payout_reference` may contain single inner spaces, because it is only ever sent as
     an encoded query parameter.
   - It is limited to the core `Ref` length: no leading, trailing or double spaces, no `/`
     and no newline.
   - Keys that go into a URL path, such as `record_id`, keep the strict pattern.

## Safety checks in the tests

- A batch that is 1p short of the credit is not supported.
- A batch member that is still pending is not supported.
- A batch member dated after the booking breaks the batch.
- Unit checks for `_settlement_batch`:
  - a self-supplied member and a mixed-issuer group are both rejected;
  - a wrong sum, duplicate references, the wrong batch named and a currency mismatch all
    give no batch.
- If case 41's notice is moved into the holder's import, so that it is self-supplied, no
  lookup or link happens and the source stays unresolved.
- If case 48's credit is changed by 1p, the issuer-named payout is not linked.
- Case 45, where only the holder's own CSV maps the alias, stays unresolved. This is a
  deliberate safe miss. The gateway refuses lookups opened by self-supplied text, and I left
  that alone.

## Not fixed, and why

- **46** (`PO/0046/02`) and **47** (`0047/02` with no prefix): retrieving these needs the
  reference rewritten. The gateway's scope check accepts only an exact text token, so I
  would have to widen a security surface. Not done.
- **42 and 43**: there is no reference at all, only order numbers, a weekday or a date.
  Finding the payout would mean guessing its reference. Not done.
- **45**: the alias comes from the holder's own data, as described above.
- **73** is fixed by step 4 below.
