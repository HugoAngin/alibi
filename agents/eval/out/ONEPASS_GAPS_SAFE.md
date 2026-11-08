# Safe one-pass balance-release diagnostics

Replacement for the un-applied `ONEPASS_GAPS.patch`; do not apply both. Apply after `QUERY_COMPLETE.patch` (SHA256 `e54593eec6730b8e941f59edf91b64a2590af2c367ee001d9271b65daf5eec22`) on the fixed public75 bundle + compatibility tree, not a moving checkout. Follow with `RULES_RELEASE_DIAGNOSTIC.patch` for the rules safety correction.

Patch SHA256: `b49abc2b7949cf2f62b22aea705ee18a0757a66f673e274838f79d633a515a91`.

The proposed same-amount-message heuristic could falsely call unrelated, unsigned or foreign-currency messages balance releases and lost correction dependencies. A shared conservative recognizer now requires the subject, issuer-verified trust, message kind, exact amount/currency, credit direction and pre-booking time window. It then requires native `released_to_balance` status or an affirmative explicit release-to-balance text phrase when native status is absent. Non-release statuses, negatives and conditional text fail closed to the generic unresolved diagnostic. Text is a diagnostic only; no source support or settlement proof is granted.

One-pass handles release-only messages without extra queries or model calls and applies the same check to the existing provisional-payout diagnostic. Examined message revisions remain private dependencies in positive and negative outcomes; public citations, calculations and attachments remain empty. Revising a message invalidates claims, drafts and approvals in either direction. A private dependency cannot bypass the model-reference gate: policy and message rejection now run before code-reference deduplication, preserving the existing unsafe proposal count regression.

The text recognizer deliberately accepts a narrow vocabulary. Some genuine release paraphrases will retain a generic gap; it does not claim general natural-language understanding. Native public balance-release mapping drops the original event kind/status when absent, so explicit issuer text is required. The existing fixed lookup strategy is unchanged.

Validation uses public synthetic inputs only, with the unchanged v2 scorer and FakeClient (live Gemini methods explicitly disabled). No private data, gold edits, scorer edits, live models, or active capture changes. No shared native files were edited.

Against QUERY_COMPLETE's fixed public75 tree, both patches apply cleanly in order and reproduce the tested source bytes. Clean-apply copy: `/var/folders/h5/vyz923lx1t7_ll1y6mb_9xbm0000gn/T/alibi-release-apply-hw9l1iyn`.

Focused verification: **64 passed in 5.67s**, covering the 20 new release safety tests, baseline one-pass behavior, timing, lookup, classification, unresolved dependency handling and existing gap diagnostics. Command (from the scratch root):

```sh
UV_PROJECT_ENVIRONMENT=/Users/tamim/code/agentverse_wtt/.venv uv run --frozen python -m pytest -q alibi/tests/test_release_diagnostic_safety.py alibi/tests/test_rules_release_diagnostic_safety.py alibi/tests/test_one_pass_timing.py alibi/tests/test_one_pass_lookup.py alibi/tests/test_one_pass_classification.py alibi/tests/test_baselines_one_pass.py alibi/tests/test_unresolved_dependencies.py alibi/tests/test_gap_diagnostics.py
```

Public80 offline verification with both patches: rules **75/80**, 0 runtime failures, 0 forbidden items, 0 unnecessary items (unchanged); FakeClient one-pass **52/80**, 0 runtime failures, 0 forbidden items, 12 unnecessary items (previously 50/80 and 12 unnecessary). Both modes answer all 94 RFCs. The one-pass gains are public17 and public18. FakeClient numbers are deterministic validation controls, not measured model performance.

Retained scratch: `/var/folders/h5/vyz923lx1t7_ll1y6mb_9xbm0000gn/T/alibi-release-safety-g0g0ighk`; predictions and metrics in `.baseline/public-results/`, offline runner `.baseline/score_public.py`. Reproduce with `PYTHONPATH=. UV_PROJECT_ENVIRONMENT=/Users/tamim/code/agentverse_wtt/.venv uv run --frozen python .baseline/score_public.py` in a fresh copy/output directory. Owner should register replayable metrics separately before submission claims.
