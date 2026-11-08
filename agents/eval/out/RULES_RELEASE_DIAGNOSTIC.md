# Rules balance-release diagnostic safety

Apply after `ONEPASS_GAPS_SAFE.patch` on the QUERY_COMPLETE public75 tree. It uses that patch's shared `alibi/core/release.py` recognizer and test helpers. Do not apply standalone unless those files are present.

Patch SHA256: `b0e1137a1497818a416129e71584dda19050a6b0395b086b63c961736c52b622`.

The existing rules fallback treated any same-amount issuer message mentioning seller/platform balance as release proof. A negative notice such as “No funds are available in your seller balance” received the wrong diagnostic. Require the conservative affirmative release recognizer; preserve every examined issuer-message revision privately even when the diagnostic is rejected so a correction in either direction invalidates the unresolved claim and prior approval. Public citations and package attachments remain minimal. No retrieval or trust scope changes.

Controls cover genuine release, unrelated balance support text, negative and conditional text, wrong currency/status, holder imports, and correction invalidation with empty packages. This changes diagnostic specificity only; unresolved remains unresolved and no message becomes settlement evidence.

Validation uses public synthetic inputs only, with the unchanged v2 scorer and FakeClient (live Gemini methods explicitly disabled). No private data, gold edits, scorer edits, live models, or active capture changes. No shared native files were edited.

Against QUERY_COMPLETE's fixed public75 tree, both patches apply cleanly in order and reproduce the tested source bytes. Clean-apply copy: `/var/folders/h5/vyz923lx1t7_ll1y6mb_9xbm0000gn/T/alibi-release-apply-hw9l1iyn`.

Focused verification: **64 passed in 5.67s**, covering the 20 new release safety tests, baseline one-pass behavior, timing, lookup, classification, unresolved dependency handling and existing gap diagnostics. Command (from the scratch root):

```sh
UV_PROJECT_ENVIRONMENT=/Users/tamim/code/agentverse_wtt/.venv uv run --frozen python -m pytest -q alibi/tests/test_release_diagnostic_safety.py alibi/tests/test_rules_release_diagnostic_safety.py alibi/tests/test_one_pass_timing.py alibi/tests/test_one_pass_lookup.py alibi/tests/test_one_pass_classification.py alibi/tests/test_baselines_one_pass.py alibi/tests/test_unresolved_dependencies.py alibi/tests/test_gap_diagnostics.py
```

Public80 offline verification with both patches: rules **75/80**, 0 runtime failures, 0 forbidden items, 0 unnecessary items (unchanged); FakeClient one-pass **52/80**, 0 runtime failures, 0 forbidden items, 12 unnecessary items (previously 50/80 and 12 unnecessary). Both modes answer all 94 RFCs. The one-pass gains are public17 and public18. FakeClient numbers are deterministic validation controls, not measured model performance.

Retained scratch: `/var/folders/h5/vyz923lx1t7_ll1y6mb_9xbm0000gn/T/alibi-release-safety-g0g0ighk`; predictions and metrics in `.baseline/public-results/`, offline runner `.baseline/score_public.py`. Reproduce with `PYTHONPATH=. UV_PROJECT_ENVIRONMENT=/Users/tamim/code/agentverse_wtt/.venv uv run --frozen python .baseline/score_public.py` in a fresh copy/output directory. Owner should register replayable metrics separately before submission claims.
