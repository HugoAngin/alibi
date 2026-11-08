# Public accuracy integration — current native main; preserves SP-07

Apply this patch alone to exact base `1e871aa25a309df5707bfe6c31a1520f2f761d5d`. Do not also apply its constituent patches. This is public development work; the active candidate-5 capture is unchanged. No scorer, gold, trust registry, signature check or private input was changed. No models were called.

The bundle includes the completed text retrieval/client-normalisation patch, one-pass timing, pending gaps, batch allocation and private dependencies, holder-answer semantics, source-only/API claim parity, private text/clue dependencies, and GAP_DIAGNOSTICS. Integration required one mechanical move: API parity's new `source_only` method is inserted before the already-added `linked_batch` method, preserving both. All other hunks applied without rejects. Supported and unresolved text-dependent answers retain private clue revisions, unresolved calculations/citations stay stripped, and batch reuse checks include all rule-linked members.

Validation: **52 focused tests passed** (6.96 seconds); all combined new/regression suites plus unresolved-dependency checks. Actual public 80-case run with unchanged v2 scorer: rules **73/80** safe-complete, **0 runtime failures, 0 forbidden, 0 unnecessary**. Remaining rules cases: 39, 40, 42, 43, 45, 46, 47. Empty-proposal `FakeClient` one-pass: **50/80**, **0 runtime failures, 0 forbidden, 12 unnecessary**. Both retain all 94 RFCs; fake calls are not model results. Numbers are unregistered public diagnostics until root records the committed patch rehearsal; they are not held-out claims.

A clean archive of the stated base accepts this patch; every changed output file matches the tested combined tree byte for byte. The scorer, `alibi/app/actions.py` and `alibi/tests/test_app_integration.py` remain byte-identical to that base. In particular, the incremental patch preserves main's SP-07 refusal and tests.

Patch SHA256: `1d7f66b48ec07daa1092c5eec90c010810ad85f0056210054b81dafcbf0a4136`.
Combined tested tree: `/var/folders/h5/vyz923lx1t7_ll1y6mb_9xbm0000gn/T/alibi-public-bundle-brvcucdf`.
Verified clean-apply tree: `/var/folders/h5/vyz923lx1t7_ll1y6mb_9xbm0000gn/T/alibi-apply-public_accuracy_increment-jqmm5p58`.
Retained diagnostic rows and metrics: `/var/folders/h5/vyz923lx1t7_ll1y6mb_9xbm0000gn/T/alibi-public-bundle-brvcucdf/.baseline/public-results`.
Full native suite is being run separately after this artifact handoff; no full-suite result is claimed here.

## Exact constituent hashes

- `ONE_PASS_TIMING.patch`: `91c65d662aa01f6b5ee4d0e02f2b6e3c86ec85ad5ea68f06991ad1de848635b4`
- `PENDING_GAP.patch`: `87f33a66cecdbbdda3b155086b12e401c6330db19f47f8170f4b610e97cbad26`
- `RETRIEVAL_ACCURACY.patch`: `31fdfaa1cf673943ee89c3a75eeee0c977ebb67a53963b3562090328b9ab0516`
- `BATCH_DEPENDENCIES.patch`: `663d2c643c785a55c118a52256c07be138f71a74dc1be99abaf2b5460fc93270`
- `ANSWER_ACCURACY.patch`: `3517b18b47f8ff2b6f3395716d6d90a0fc80240188e9a9ba826f059eacc01b6d`
- `TEXT_LOOKUP_SAFETY.patch`: `5d7d6dd9d7382c8708e46fcf9ea60a7475f1ae585f349542dae02a83f4f4ae90`
- `GAP_DIAGNOSTICS.patch`: `9919ee4da21dc6d2e21ceb9b3986c42a12e1c116e4ee0d621ccb50eec5d093af`
- `API_CLAIM_PARITY.patch`: `cfb6cbb13d0c41e17021dcade7d69ed32cc0716a26667c165623ed757ceaf902`

## Focused reproduction

```sh
uv run --frozen python -m pytest -q alibi/tests/test_settlement_batch.py alibi/tests/test_batch_dependencies.py alibi/tests/test_answer_accuracy.py alibi/tests/test_text_lookup_safety.py alibi/tests/test_gap_diagnostics.py alibi/tests/test_pending_gap.py alibi/tests/test_one_pass_timing.py alibi/tests/test_unresolved_dependencies.py alibi/tests/test_api_claim_parity.py
```

After committing this evaluator artifact, `agents.eval.public_patch_rehearsal` can capture the public rules and optional FakeClient results from the exact base plus this single patch, with hash-bound replay. Neither staged future-scorer patch is part of this integration.
