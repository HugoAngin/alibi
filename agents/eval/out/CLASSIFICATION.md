Integration update: rebased onto the current answered-helper imports. Apply together with UNRESOLVED_EVIDENCE.patch: the unresolved breakdown keeps its source outcome as private dependencies; the paired claim builder removes public citations/calculation while retaining examined_refs. These two fields are dependency preservation, not disclosed evidence.

# One-pass question classification: proposed fix (public cases only)

The native change is staged for the application owner. The original public checks used `f77b141`. The patch now applies to the current answered-helper imports and must be paired with `UNRESOLVED_EVIDENCE.patch`. The earlier issuer, classification, bank, draft-order and port-probe combination passed 167 targeted tests in a disposable copy; the newer dependency, classification and lookup combination passed 357 targeted tests.

## Finding

`classify_question` (`alibi/core/investigate_rules.py:235`) is the keyword-only view, and it is not what misclassifies public Case 1.

- **Source:** all 80 public `q.source_of_funds` texts already classify as `source_of_funds`. One-pass Case 1 source is unresolved with `no_merchant_records` because of retrieval: `one_pass.fixed_lookups` never looks up `PO-0001-01`. That is out of scope here and unchanged.
- **Breakdown and period:** one-pass calls `classify_question` directly (`alibi/baselines/one_pass.py:459`) instead of `classify()`, which applies the pinned ids and the breakdown fallback.
  - So all 80 `q.breakdown` questions come out `unmapped`.
  - All 14 `q.sales_refunds_in_period` questions come out `several_totals`.
  - `one_pass.answer` (line 464) turns both into unresolved.
  - An existing test pins `classify_question` returning `unmapped` for breakdown text (`test_core_investigation_fixes.py:368`).
- **Why the fix can't live in `classify_question` alone:** returning `breakdown` there would crash one-pass, because `period_total` has `assert period is not None`. Returning `documented_totals` would mis-sum, because `{ORDER: 1}` signs raise on the refunds. So the fix has to be at the call site.

## Patches (apply in order, `patch -p1` from the repo root; `classify-combined.patch` = 01+02+03)

| File | Change |
| --- | --- |
| `classify-01-investigate_rules.patch` | Real semantic fix in `classify_question`. A question with a breakdown word (calculated, reconcile, breakdown, made up, ...) and no source cue besides "explain" or "why" is no longer `source_of_funds`. It returns `unmapped` (keyword view), and `classify()` makes it `breakdown`. "arrive(d) at" is excluded, so "Why did £2,160 arrive at your account?" stays source. Mixed questions with origin, where or come from stay source. Before the patch, "Please explain how this credit was calculated." was `source_of_funds`. |
| `classify-02-one_pass.patch` | Needed for the one-pass goal. `answer` uses `core_rules.classify`. The new `breakdown()` follows the source verdict: for one supported credit it returns the source's own reconciliation; otherwise it is **unresolved with the source gap and cites nothing** (no refs, no calculation). `source_once()` caches the source verdict, so links and notes are written once. `period_total` sums `documented_totals` with per-kind signs and states each kind's total. |
| `classify-03-test_one_pass_classification.patch` | New `alibi/tests/test_one_pass_classification.py` (6 tests; a copy is alongside). |

Apply from the repo root with `git apply agents/eval/out/CLASSIFICATION.patch`.

## Tests (all `uv run --frozen` with the repo's locked env, `ALIBI_DATA_DIR` set to a temp dir)

```
uv run --frozen python -m pytest -q alibi/tests/test_one_pass_classification.py
```

- **New file, unpatched code:** 5 failed, 1 passed. The test that passes is the public no-regression guard.
- **New file, patched:** 6 passed.
- **Focused suites, patched** (new file + `test_investigate_rules`, `test_core_investigation_fixes`, `test_baselines_one_pass`, `test_facts`, `test_adapter`, `test_agents_runner`): 181 passed.
- **Full `alibi/tests` + `agents/eval`:**
  - Before: 15 failed / 2195 passed. 5 of those failures are the new tests.
  - After: 10 failed / 2200 passed.
  - The 10 are the same set before and after: `test_eval_seal` ×8 and `test_freeze_pack` fail because the scratch copy has no `.git` (`git rev-parse HEAD failed`), plus `test_demo_services` ctrl-c timing.
- **Combined with the sibling `unified.patch`** (unresolved evidence): both apply cleanly in either order; the focused suites plus `test_bank_service` give 215 passed.

## Public 80-case impact (offline `FakeClient`, no model; `classify-repro/dump80.py`, dumps alongside)

- **Rules:** 0 of 174 claims changed.
- **One-pass:** 94 of 174 changed: every breakdown and period claim. No source claim changed.

| Claim type | Status matches public gold, before → after |
| --- | --- |
| breakdown | 23 → 49 /80 |
| period | 0 → 14 /14 |
| source | 50 → 50 /80 |

- **The 43 newly supported claims** have the same status and facts as rules.
  - 38 match public gold status and facts exactly.
  - Cases 79 and 80 (period): status and totals match gold, but the `by_issuer` split is missing, the same as rules.
  - Cases 8, 24 and 76 (breakdown): `breakdown_minor` equals gold, but gold says `contradicted` because the holder's `stated_minor` contradicts the records. Neither engine reads the holder statement, so this is an existing rules-level limitation.
- **Every supported one-pass breakdown** has a supported source in the same request (29/29).

## Limitations

- One-pass Case 1 source and breakdown stay unresolved until the separate retrieval fix (`fixed_lookups` reference lookup) lands. The breakdown cannot be supported without it, by design.
- One-pass outputs change after the frozen run. Label any rerun as public-only and post-hoc; the original unseen result is untouched.
- The `classify_question` change affects only unpinned question ids. Zero public questions are affected. The agents runner maps `q.breakdown` to source by id and is unaffected for pinned ids; for unpinned "explain how ... calculated" wording it now abstains instead of answering a source claim.
- No ruff or pyflakes in the env; files were checked with `py_compile` and the test suite.
