Final negative control: named transaction bypasses unsupported-question exclusion
2026-11-08 13:32 London; public synthetic only.

Exact integrated base source: /var/folders/h5/vyz923lx1t7_ll1y6mb_9xbm0000gn/T/alibi-unseen-generalisation-zau4pblt/tree. Own copy /tmp/new-evidence-final-negative-work. BASE_SHA256.txt pins the core source before patch; no committed SHA asserted for this composed scratch.

Failure: classify_question returns SOURCE_OF_FUNDS immediately if text names a requested transaction. It only checks _NOT_SOURCE (credit scores/ratings, loans, mortgages, borrowing, etc.) afterward. As a result, arbitrary-ID questions "What credit score applies to credit-921-004?" and "Does credit-921-004 qualify me for a mortgage?" receive a supported source-of-funds answer instead of an unresolved unsupported-question result. This happens in rules, one_pass and agents. All six end-to-end public-derived controls fail before. The account holder, identifiers and payout reference are renamed; no private input or gold is used. Agent/one-pass model paths use FakeClient only.

Minimal fix: one-line change applies the already-defined _NOT_SOURCE exclusion before the named-transaction branch. It adds no categories, lookup ability, trust changes or disclosure scope. These already-excluded questions now remain unresolved even when they name a real transaction. Ordinary existing source/breakdown/period questions continue to pass.

Validation: 92 passed in 4.27 seconds after the fix, across six new negative controls, existing one-pass classification, new agent question generalization, and deterministic investigation tests. Before-tests.log preserves six false supported results; after-tests.log preserves the complete passing run. No scorer/gold changes and no full80/native-suite duplication.

Command from own copy:
UV_PROJECT_ENVIRONMENT=/Users/tamim/code/agentverse_wtt/.venv uv run --frozen --no-sync python -m pytest -q alibi/tests/test_named_transaction_question_guard.py alibi/tests/test_one_pass_classification.py alibi/tests/test_agents_question_generalization.py alibi/tests/test_investigate_rules.py

Native owner lead. NAMED_TRANSACTION_QUESTION_GUARD.patch contains the one-line production fix and regression tests. Test helpers come from the already-integrated literal-reference and agent-question patches. No shared scratch changes, Git operations, private inputs or live model calls.

Residual risks: keyword classification remains a heuristic, and a named transaction still implies source-of-funds for text outside the explicit excluded-category vocabulary. Pinned contract question IDs remain authoritative by existing design. This narrow patch does not claim arbitrary natural-language question understanding. Exact-query fixes do not invent aliases, normalize provider separators or create missing trusted issuer pins. Broadening those behaviours is outside this stability pass. Coordinator owns the final native regression and any post-hoc reused-data diagnostic; these public tests do not establish unseen-score improvement.
