# Generalisation bundle: reused-input rules development result

The complete44-case rules run finished before13:36 with zero worker failures, zero scheduler errors and no model calls. The exact composite passed public Cases1and2 (both safe-complete), isolation and declared trusted-key checks before this run. This is another development look at already evaluated inputs, not a fresh unseen benchmark.

| Rules candidate | Main safe-complete | Adversarial safe-complete | Runtime complete | Main unnecessary items |
| --- | ---: | ---: | ---: | ---: |
| Candidate5,3c50476 |9/32|2/12|44/44|18|
|552b025 + generalisation bundle + question guard |10/32|0/12|44/44|18|

The new bundle is **not an overall evaluation improvement**: it gains one main case and loses two adversarial cases. Do not promote it as an accuracy win or replace either earlier capture. Forbidden-item count is zero in both new slices; this does not establish every security property. The original scorer and gold remain unchanged. No new agents or live one-pass held-out scores were measured within this cutoff.

Source identity: base `552b025add4b99ad3b41ae827fcbcb3badefd899`, committed `UNSEEN_GENERALISATION_BUNDLE.patch` SHA256 `4af740eef125993da8476d617526850b0f1eb3d9a03790594c623d6e80364ff6`, then `NAMED_TRANSACTION_QUESTION_GUARD.patch` SHA256 `e2bc4fed9969dc9ef3a599a89220bb6b249780f067847ce544adf4ec2dea7953`. The evaluator applied these exact committed native-only bytes inside isolated archives; it did not edit shared native source or pretend the base commit already contained the fixes. Each worker records both patches. Original frozen58bf0d4 and candidate5 captures stay unchanged.

Replayable runtime record `7c6dc607e1` PASS; score `33e46f1720` PASS; original integrity `752f72f28a` PASS. Full native bundle1544tests passed in one run; final question-guard correction passed92focused tests. Public regression75/80rules and52/80FakeClient one-pass is separately replay-verified as `d2cea158fa`; FakeClient is not Gemini performance.

## Newly observable input-path limits

The new aggregate-only ingest telemetry is present and valid in all92 retained request snapshots.32 snapshots have no issuer-verified records, and32 have merchant refusals; these counts were not joined and are not a proven overlap. Cumulative request-snapshot totals show664 accepted merchant deliveries and157 refused with the static reason `untrusted_issuer`; there are81 refused/skipped imports with `unsupported_kind` and zero accepted self-supplied imports. These are cumulative snapshots, not unique record or case counts; refused imports also count as skipped.

`untrusted_issuer` localises failure before the ingest sink: the simulated merchant could not select a configured signing key for an exact issuer/purpose pair. It is not the downstream bad-signature verification category. This strongly suggests a setup/issuer declaration gap for at least some rows, but counts alone do not establish which missing declarations are legitimate or whether a refusal is intentionally adversarial. The originally recovered static issuer constant matches the existing trusted setup. No new issuer was trusted from case contents, and no unsupported import kind was silently remapped.

Next required investigation is an independently supplied complete authoring issuer/key declaration and public reproductions for any additional import formats. Trust promotion or changing gold merely to raise the score would be invalid. Preserve this result while addressing those contracts. Raw inputs, predictions, identifiers and issuer values remain private.
