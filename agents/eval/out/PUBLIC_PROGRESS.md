# Public development progress

| Build | Safe-complete cases | Worker failures | Forbidden items | Unnecessary items | Verified record |
| --- | ---: | ---: | ---: | ---: | --- |
| Original58bf0d4 public rules |35/80|0|0|92|ee8e4efb16 (before)|
| Pushed1b5c21a public rules |41/80|0|0|72|ee8e4efb16 (after), PASS|
| Committed development patch bundle, including private dependency preservation and precise pending gap |43/80|0|0|47|967e253df2, PASS|
| Candidate5 plus timing, pending gap and safe settlement-batch patches |47/80|0|0|47|cc6f30aff2, PASS|
| Combined retrieval, answer facts, precise gaps and source-only proof, based on1e871aa |73/80|0|0|0|469663fef7, PASS|

The last two rows are patched development trees, not the immutable candidate5 result. Their receipts pin the base revision, patch hashes, public inputs, predictions, scorer and replay implementation. No model calls were used. No gold, scoring criteria or source-verification checks were altered.

The complete native integration bundle passed1426 tests with no failures or expected failures in a disposable tree. The additional pending-gap change passed104 rules/core regressions. These software checks do not imply80/80 evaluation accuracy.

Candidate5's exact public live gate completed Cases1 and2 in agents, one-pass and rules modes, with all six worker isolation and explicit trusted-setup fingerprint checks passing. The separately authorised candidate5 second look on the reused44-case pack started12:53 with a13:25 stop. It is post-hoc development evidence; the original frozen result remains unchanged. Further public fixes cannot alter that pinned capture.

The latest batch row also records an explicitly offline FakeClient one-pass control at45/80; that is not live Gemini performance. The batch correction/allocation regressions and adjacent rules suites passed120 tests.

The73/80 rules result is a public development capture with unchanged gold and scorer. Its exact incremental patch is `PUBLIC_ACCURACY_INCREMENT.patch`, against1e871aa; the equivalent full patch againstcandidate5 is `PUBLIC_ACCURACY_BUNDLE.patch`. The hash-bound formal replay passed as469663fef7. FakeClient one-pass is50/80, not live Gemini performance. The52 focused regression tests passed. All1471 unique native tests subsequently passed across the full run and focused reruns:1462 initial passes, eight missing-git archive-environment failures resolved by nine seal-test passes, and one outdated gap assertion resolved by21 adapter-test passes with GAP_DIAGNOSTICS_COMPAT.patch. This is not described as a single clean full-suite run. Seven public cases remain:39,40,42,43,45,46,47. Trust-policy and exact reference-normalization questions were raised to lead; no gold or trust rules were weakened.
