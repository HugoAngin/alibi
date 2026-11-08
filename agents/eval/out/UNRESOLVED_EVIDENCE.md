# Unresolved disclosure with retained dependencies

`UNRESOLVED_EVIDENCE.patch` is staged for the native owner. Shared native files were not edited. The patch applies cleanly to current main and has been tested after `API_DRAFT_ORDER.patch`. Apply `CLASSIFICATION.patch` and `ONE_PASS_LOOKUP.patch` afterwards, followed by `ONE_PASS_LOOKUP_COMPAT.patch` for its changed retrieval expectation.

The earlier rules-only proposal dropped the revision references needed to invalidate unresolved claims, drafts and approvals after a correction. Current main's shared `answered_refs()` also drops those refs in all three modes, while its calculation helper still discloses unresolved calculations and their references. Keeping the references only in trace does not repair the dependency index.

The patch adds persisted, passport-only `Claim.examined_refs`. Every claim builder captures unresolved outcome evidence and all calculation dependencies before removing public citations and calculations. The shared store dependency helper includes those private refs, so source corrections and late saves keep the existing invalidation behaviour. Supported and contradicted answers keep their existing citations and calculations.

The API uses the complete dependency set for private package-building context and `Draft.source_revisions`; approvals inherit the same pins. Package selection still uses only disclosure citations, so unresolved-only packages have no attachments. Supplying the examined records as private context also lets the existing package scrubber remove their identifiers from recipient-facing text. `PackageClaim` and adapter citation output never receive `examined_refs`.

The fix also covers a rerun whose examined sources change while recipient body bytes stay identical: the API must not reuse the old draft or approval merely because the body hash matches. It compares complete dependency sets when rebuilding a review and when regenerating the approved body, and invalidates the old pending approval on changed dependencies.

Rules breakdown outcomes now retain their source outcome's internal evidence and calculation until the claim builder performs disclosure filtering. The paired classification patch does the same for the new one-pass breakdown path. This is required even when the final unresolved calculation is absent.

## Verification

- Seven new security regressions pass: empty signed/verifiable package with private pins; identifier scrubbing; persisted correction invalidation after database reopen; refusal of stale dispatch and stale review; late-save invalidation; changed-dependency consent refusal before and after rebuilding an identical body; unchanged supported/contradicted helper behaviour; and retention of breakdown source dependencies.
- Dependency fix plus API draft order and classification patches: **353 tests passed**, no skips or expected failures in those selected tests.
- The three non-strict bank-service expected-failure decorators are removed. Empty unresolved packages are tested explicitly, tampering changes signed claim text, and attachment viewing uses the supported Bank B package.
- Four tests formerly inspected unresolved calculations on disclosed claims. They now assert no disclosed calculation, retained private dependencies, and the same arithmetic results in the calculation trace.
- Adding the lookup patch initially produced **356 passes and one obsolete expectation**: the existing Case 2 test still expected zero lookups. The separate compatibility patch changes that test to require one lookup, an unresolved answer, no disclosed refs/calculation, and the pending payout retained among private examined refs.
- Final combined verification including that compatibility patch: **357 passed in 12.93 seconds**, with no expected failures.

Selected suites cover the API, draft ordering, store, approval, package, rules engine, core fixes, adapter, one-pass baseline, agent runner, payout/settlement flows, bank service and the new classification/lookup tests. Only public fixtures and fake clients were used; no live models, private cases, private gold or private captures were accessed. These are engineering checks, not a new accuracy score.

## Reproduce after applying the patches

```sh
uv run --frozen python -m pytest -q alibi/tests/test_unresolved_dependencies.py alibi/tests/test_api.py alibi/tests/test_api_draft_order.py alibi/tests/test_bank_service.py alibi/tests/test_investigate_rules.py alibi/tests/test_core_investigation_fixes.py alibi/tests/test_adapter.py alibi/tests/test_store.py alibi/tests/test_approval.py alibi/tests/test_package.py alibi/tests/test_baselines_one_pass.py alibi/tests/test_agents_runner.py alibi/tests/test_agents_payout_settlement.py alibi/tests/test_one_pass_classification.py alibi/tests/test_one_pass_lookup.py
```

## Limits

The private claim and trace may still contain source identifiers and detailed rationale; only the recipient package is scrubbed using the known private evidence context. Existing scrubber limits remain, including short values and values the recipient already knows. This does not claim complete semantic de-identification.

The retained dependencies are the revision-pinned records and calculations represented by the outcome. Arrival of a previously missing record with no existing dependency is a separate reinvestigation trigger, not a source-revision correction. Existing incomplete outcomes that never record an examined ref cannot be retroactively reconstructed from this patch. Low-level callers of `make_draft()` must provide complete claim dependencies as its updated documentation specifies; the public API does so.

No scorer, gold, consent policy or source signature checks are weakened. No prior frozen or reused-input result is replaced by this patch.
