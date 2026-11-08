# Public-derived generalisation integration

Base native commit: `552b025add4b99ad3b41ae827fcbcb3badefd899` (public73 plus owner compatibility changes). Apply this single bundle to that exact base; do not also apply its constituent patches. Native changes already present at the base, including SP07 policy refusal, are preserved.

Bundle SHA256: `4af740eef125993da8476d617526850b0f1eb3d9a03790594c623d6e80364ff6` (102031 bytes, 21 native files).

This integrates bounded signed payout queries with atomic ingest and active-revision scope checks; private query/release correction dependencies; conservative release diagnostics; real query telemetry; repeated-read agent stopping; literal reference casing and explicit spaced references; semantic question classification across modes; and aggregate ingest diagnostics. Signatures, issuer trust, subject scope, consent, stale-source checks and package minimisation remain enforced. Reference values come from observed evidence and retain their literal spelling. No additional lookup/model budget, provider-key inference or source-trust promotion is introduced.

All patches applied cleanly except the optional REFERENCE_FORMATS regex insertion: its old context preceded `_ORIGIN`, while query support inserted additional clue regexes there. The same field regex was placed immediately after `_TEXT_REF`, retaining both query support and unchanged field-extraction behavior. No original patch artifact was rewritten.

Validation:

**1544 passed, 5 warnings in 82.21s** in one complete native-suite run on the final assembled tree, including all new query, release, no-progress, literal-reference, classification, telemetry, SP07, consent and seal tests. The warnings are existing Starlette cookie deprecations. Focused checks also passed before the full run; they are not added to this test count.

The final public80 regression uses the unchanged v2 scorer: rules75/80, 0 runtime failures, 0 forbidden items, 0 unnecessary items; FakeClient one-pass52/80, 0 runtime failures, 0 forbidden items, 12 unnecessary items. Both modes cover all94 RFCs. The rules failures remain39,40,45,46,47. These are public/offline development checks, not evidence of unseen accuracy or live-model performance. No private cases/gold, model calls, active captures or existing scored records were accessed or changed.

The bundle clean-applies to a fresh archive of552 and reproduces every changed source/test byte. All `alibi/eval/` files remain byte-identical to552. An isolated `.git` directory in the test archive points read-only to the base object's store for repository/seal checks; no shared Git metadata, commits or source files were changed.

Test tree: `/var/folders/h5/vyz923lx1t7_ll1y6mb_9xbm0000gn/T/alibi-unseen-generalisation-zau4pblt/tree`.
Full-suite log: its parent directory's `full-native.log`; exact constituent hashes: `patches.json`; changed-file hashes: `native_hashes.json`; clean application: sibling `clean-apply/`.
Public predictions/metrics: `tree/.baseline/public-results/`; offline runner: `tree/.baseline/score_public.py` (live Gemini methods disabled).

Commands from the test tree:

```sh
UV_PROJECT_ENVIRONMENT=/Users/tamim/code/agentverse_wtt/.venv uv run --frozen --no-sync python -m pytest -q alibi
PYTHONPATH=. UV_PROJECT_ENVIRONMENT=/Users/tamim/code/agentverse_wtt/.venv uv run --frozen --no-sync python .baseline/score_public.py
```

Owner should record the final candidate SHA and register replayable metrics before submission claims. This is a changed candidate for any reused development run; original candidate5 capture/scoring records must remain intact.

Constituent patch SHA256s:

- `QUERY_COMPLETE`: `e54593eec6730b8e941f59edf91b64a2590af2c367ee001d9271b65daf5eec22`
- `ONEPASS_GAPS_SAFE`: `b49abc2b7949cf2f62b22aea705ee18a0757a66f673e274838f79d633a515a91`
- `RULES_RELEASE_DIAGNOSTIC`: `b0e1137a1497818a416129e71584dda19050a6b0395b086b63c961736c52b622`
- `AGENT_NO_PROGRESS`: `df940ac11a7f203c261f64b52a10f1809daa02136a6caf213032e24c7dabeb3f`
- `LITERAL_REFERENCE`: `f58219d191275be86a5c3c8b003f6197e664139e7ec21070b81aff1c645582eb`
- `REFERENCE_FORMATS`: `e5bb05cd4e2894eebebc84d035c740dee270e0d0bdc7b363b3a287aa4941a863`
- `LITERAL_REFERENCE_PARITY`: `27e3397026da0d325690a50aaba027a56436d2b54b44248df95f1b141859c590`
- `INGEST_TELEMETRY`: `a73f50cd7775b4b41d916f277a4573e4b6b3bc210d4aa137b7b912ca4414b5f5`
- `AGENT_QUESTION_CLASSIFICATION`: `ec02743853fad881332f3f1183ac9ae497f63ffe1125b3e87120a1f27a889870`
