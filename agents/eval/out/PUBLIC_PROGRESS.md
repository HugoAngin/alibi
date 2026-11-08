# Public development progress

| Build | Safe-complete cases | Worker failures | Forbidden items | Unnecessary items | Verified record |
| --- | ---: | ---: | ---: | ---: | --- |
| Original58bf0d4 public rules |35/80|0|0|92|ee8e4efb16 (before)|
| Pushed1b5c21a public rules |41/80|0|0|72|ee8e4efb16 (after), PASS|
| Committed development patch bundle, including private dependency preservation and precise pending gap |43/80|0|0|47|967e253df2, PASS|
| Candidate5 plus timing, pending gap and safe settlement-batch patches |47/80|0|0|47|cc6f30aff2, PASS|

The last two rows are patched development trees, not the immutable candidate5 result. Their receipts pin the base revision, patch hashes, public inputs, predictions, scorer and replay implementation. No model calls were used. No gold, scoring criteria or source-verification checks were altered.

The complete native integration bundle passed1426 tests with no failures or expected failures in a disposable tree. The additional pending-gap change passed104 rules/core regressions. These software checks do not imply80/80 evaluation accuracy.

Candidate5's exact public live gate completed Cases1 and2 in agents, one-pass and rules modes, with all six worker isolation and explicit trusted-setup fingerprint checks passing. The separately authorised candidate5 second look on the reused44-case pack started12:53 with a13:25 stop. It is post-hoc development evidence; the original frozen result remains unchanged. Further public fixes cannot alter that pinned capture.

The latest batch row also records an explicitly offline FakeClient one-pass control at45/80; that is not live Gemini performance. The batch correction/allocation regressions and adjacent rules suites passed120 tests.
