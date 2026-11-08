Staged audit only. DO NOT APPLY during the candidate5 capture: its original scoring commitment must remain unchanged. Any future scorer revision needs separate versioned results. The copied regression script is `agents/eval/out/scorer_alternatives_regression.py`.

# Scorer alternative-evidence audit

Candidate tested: `af751cdb001be8a4debe76683736f33ee2bdd00d` (immutable source archive).
Scope: public contracts and newly authored tiny synthetic cases only. No private cases/gold/secrets, private captures, answer/retrieval engine work, shared source changes, gold edits or threshold changes.

## Reproduced finding

`alibi/eval/scorer.py:350-358` independently selects one acceptable evidence set per question using `max(sets, key=(overlap, -size))`, then unions those choices. Independent choices can overlap and leave another valid question's evidence outside the union. Those refs become `unnecessary`, causing safe-complete to fail even though each question cites exactly one accepted alternative and the total disclosure is precisely their union.

1. Q1 accepts {a,b} OR {c,d}; Q2 accepts {a,b}. Q1 cites {c,d}; Q2 cites {a,b}. All four refs are observable and none forbidden. Current result: FAIL, c and d unnecessary. Reverse Q1's alternative order: PASS. Required facts/status/gaps and predictions are unchanged. This proves order dependence on semantically equivalent evidence-set lists.
2. Q1 accepts {a} OR {b,c}; Q2 accepts {b,c}. Q1 cites {a}; Q2 cites {b,c}. Current result: FAIL, a unnecessary, regardless of alternative order. The larger Q1 alternative wins even though it is already justified by Q2.

These are one-case, one-request schema-valid predictions passed through `build_system` and `score_case`, not merely isolated helper assertions. All refs are synthetic `merchant:a` through `merchant:d`, authored in the test. No benchmark case is consulted.

## Policy distinction

The strict all-or-nothing endpoint is intentional: one unnecessary item causes failure, and citing both alternatives for a single question remains unnecessary. Do not relax that policy or accept the union of every alternative.

The public contract says extra evidence is allowed when justified by another question, and defines unnecessary refs as outside the chosen acceptable evidence sets (`alibi/eval/predictions.md`, Claims and Package/disclosure). These counterexamples satisfy that semantic rule. However, the same document explicitly describes the current per-question best-coverage heuristic. This is therefore a demonstrated heuristic/contract-intent mismatch and order-dependence bug, rather than evidence that the implementation differs from its literal documented heuristic. Owner should confirm joint selection as the intended clarification before changing the scoring version.

## Suggested correction, not applied

`scorer-audit-suggested.patch` replaces independent greedy choices with the possible unions obtained by choosing ONE alternative per question, then selects the union with maximum disclosed coverage and the existing smaller-size preference. It includes a deterministic tie-breaker. No status, fact, citation, forbidden, observable, empty, denominator or threshold check changes. The same helper also affects pre-policy unnecessary counts.

Number of combinations is the product of alternatives across questions (deduplicated by union); the published question set has only three questions. This is a minimal suggested owner patch, not a production performance claim. It should receive the owner's usual full public scorer regression suite and scoring-version decision. No captured run should silently be rescored or replaced; any scorer revision needs separate versioned reporting.

## Tests

Unchanged candidate: **3 failed, 6 passed**. These failures are the two counterexamples above (the short-alternative example is checked in both orders).
Proposed patch executed in an in-memory module, with snapshot source unchanged: **9 passed**.

Passing controls cover either alternative for one question, rejection of both redundant alternatives for one question, unjustified extra evidence, forbidden evidence, and incomplete per-question citations despite sufficient package attachments.

Commands, run in the disposable source snapshot with existing dependencies and isolated Python/cache state:

```
uv run --frozen --no-sync python -m pytest -q -p no:cacheprovider test_scorer_audit.py
AUDIT_PATCHED=1 uv run --frozen --no-sync python -m pytest -q -p no:cacheprovider test_scorer_audit.py
```

For owner reproduction in a fresh disposable checkout of the SHA: copy `scorer-audit-test.py` as `test_scorer_audit.py` into its root; run the first command; apply `scorer-audit-suggested.patch` in that disposable checkout; run the first command again. The optional AUDIT_PATCHED mode was used only for this audit and reads `proposed_scorer.txt` in the retained disposable snapshot.

Artifacts beside this report:
- `scorer-audit-test.py`: public synthetic regression tests.
- `scorer-audit-original-tests.txt`: unchanged candidate failure details.
- `scorer-audit-proposed-tests.txt`: in-memory proposed change result.
- `scorer-audit-suggested.patch`: owner-applicable minimal diff.
- `scorer-audit-snapshot.txt`: exact SHA and disposable snapshot path.

Optional diagnostic fields were left out after the coordinator narrowed scope. No claim is made about benchmark impact or private-result causes.
