# One-pass gap-label increment (public only, staged, not applied)

Base: copied native tree `alibi-public-bundle-brvcucdf` (rules 73/80, FakeClient one-pass 50/80).
Patch: `ONEPASS_GAPS.patch` (native `alibi/` only). Apply with `patch -p1` from the bundle root.

## Change
`alibi/baselines/one_pass.py` `txn_outcome`: when no payout record exists and an already-retrieved
`message` record has the credit's exact amount, the gap is `balance_release_not_settlement` rather
than `no_merchant_records`. The fixed retrieval, model calls, lookups, trust/consent/allocation and
disclosure paths are unchanged. No case-ID conditionals. No gold, scorer or fixture edits.

## Evidence (offline FakeClient only; this does NOT measure real Gemini performance)
- New `alibi/tests/test_one_pass_release_gap.py` (public cases 17, 18, 32): fails before on 17 and 18
  (32 passes as a control), and all 3 pass after.
- Existing one-pass tests (baselines, classification, lookup) plus the new test: 33 passed.
- Public 80 one-pass with FakeClient: 50/80 before, 52/80 after. 0 failures, 0 forbidden items,
  and 12 unnecessary items (cases 17, 18 and 32 contribute 0 after the fix; I did not attribute the 12 per case).
  Remaining failures: 9-12, 25, 33-50, 66, 68, 73-75.
- Full suite not run (per instruction). The rules mode was not rerun, since the patch touches only one_pass.

## Not done (time limit)
- 19 and 32 already pass. 25 (`contradicted` where `settlement_failed` is expected on source_of_funds)
  and 35 (source-only support via the rules `source_only` helper) are not yet addressed.
Measured outputs: `/tmp/onepass-gaps-out/` (external, disposable).
