# API holder-claim parity and source-only projection (follow-on to ANSWER_ACCURACY)

This is a staged native patch that has not been committed. It is `API_CLAIM_PARITY.patch` (sha256 `cfb6cbb13d0c…ceaf902`). Apply it with `git apply` **after** `ANSWER_ACCURACY.patch`, which it does not rewrite. It applies cleanly to a fresh copy of the combined tree with ANSWER applied, and the result matches the tested files byte for byte.

It is public development evidence only. No private inputs and no model calls were used. Gold and the scorer are unchanged, and nothing checks a case ID. The active candidate-5 capture is unaffected.

## Changes

1. **API parity.** `api.run_investigation(mode="one_pass" | "single_call", holder_claim=...)` now forwards the claim through `one_pass.run_investigation` to `run_one_pass`. The claim is the same untrusted input as before: it is checked against the records and never used as evidence.
2. **Source-only projection, rules mode** (`_Run.source_only`). It runs only when the payout's component records are missing (`missing_cited_record`), a settlement record exists, there is no explicit conflict, and nothing is self-supplied. The source of funds is then **supported** only if all of these hold:
   - the payout and settlement are both `issuer_verified`;
   - both are for exactly the credit's amount and currency;
   - `reconcile.check_settlement_timing` passes;
   - the credit carries their reference, and no competing reference.

   When it fires:
   - Source of funds cites only the credit, payout and settlement, with no calculation and no component arithmetic. Its facts are `payout_references` and `settled_at`.
   - The breakdown stays **unresolved** with `missing_cited_record` and the missing record's sentence. It cites nothing, and its dependencies stay in `examined_refs`.
   - The one-payout-one-credit rule still applies afterwards, because the projection runs inside `linked()` before `credit_outcome`.
   - If any check fails, the earlier unresolved answer stands.

   Two existing tests now assert the new behaviour. The settled missing-component test expects supported, with no component cited and no calculation. The reversed-settlement case still expects contradicted. The pinned-gap `missing` row now uses an `on_hold` settlement, so it still checks that the source stays unresolved with `missing_cited_record`.

## Measurements (public 80, offline)

| System | ANSWER tree | + this patch | Gained | Lost | Forbidden | Unnecessary |
| --- | --- | --- | --- | --- | --- | --- |
| rules | 50/80 | **51/80** | 35 | none | 0 | 0 |
| one_pass, `FakeClient` (empty proposal) | 50/80 | 50/80 | none | none | 0 | 12 |

One-pass case 35 is unchanged because the projection is rules-only. One-pass would need its own equivalent, which is not done here. The public-80 runs don't cover the parity change, since the adapter already called `run_one_pass` with the claim. The direct API regression below covers it.

## Tests

- New file `alibi/tests/test_api_claim_parity.py` (5 tests):
  - One test calls `api.run_investigation` directly in `one_pass`, `single_call` and `rules` modes on the seeded public store, with an offline `FakeClient` pinned inside the test. A holder-stated sales figure that's too high gives contradicted, with the records' `breakdown_minor` and the same evidence. A matching claim stays supported.
  - Two tests use the public control case with one covered fee removed. The source is supported citing only bank and payout, with no calculation. The breakdown is unresolved with `missing_cited_record` and no evidence. If the payout amount also differs, the source stays unresolved.
- Focused suites: `test_api_claim_parity`, `test_answer_accuracy`, `test_investigate_rules`, `test_core_investigation_fixes`, `test_facts`, `test_api`, `test_baselines_one_pass`, `test_adapter`, `test_agents_payout_settlement`, `test_unresolved_dependencies`, `test_package`, `test_approval`, `test_one_pass_lookup` and `test_agents_runner` give **303 passed**. The full suite was not rerun, as instructed.

## Note for root

Checked: offline one-pass returns `supported` for case 1's breakdown, so the ANSWER patch's one-pass test does reach its contradiction assertions. The new direct API test's first draft failed because the adapter's rules mode passes no client, so the API built a Gemini client. That client reported Unavailable and made no model call. The test now pins its own `FakeClient`.
