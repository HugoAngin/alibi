# Answer accuracy: holder-stated breakdown, amount mismatch, by-issuer period facts

This is a staged native patch that has not been committed. It is `ANSWER_ACCURACY.patch` (sha256 `3517b18b47f8…cc01b6d`), built against the combined fixes tree `alibi-all-fixes-full-um0dauzf`, which has the issuer, dependency, classification, lookup and timing fixes. Apply it with `git apply` from the repo root. It applies cleanly to a fresh copy of that tree, and the result matches the tested files byte for byte. It doesn't depend on `PENDING_GAP.patch`.

It is public development evidence only. No hidden or private inputs were used, and gold and the scorer are unchanged. Nothing checks a case ID. This is not a fresh unseen result.

## Changes (question semantics only; retrieval and matching untouched)

1. **The holder's stated breakdown is checked as an untrusted claim** (cases 8, 24 and 76).
   - `core.investigate_rules.run_investigation(..., holder_claim=None)`, `api.run_investigation(..., holder_claim=None)` (rules) and `one_pass.run_one_pass(..., holder_claim=None)` take the case's `holder_claim`. The adapter passes it in rules and one-pass mode.
   - `_stated_breakdowns` keeps only a claim that has a `txn_id` and four integer `stated_minor` figures. Anything else is ignored.
   - A claim is never evidence and never cited. It only changes a breakdown that is already **supported**. If the records' breakdown differs from the stated one, the status becomes `contradicted`. The same issuer evidence, calculation and `breakdown_minor` facts are kept, and the statement names the differing fields.
   - With no claim, or a matching one, the answer is unchanged. Source of funds never depends on the claim.
   - `core.facts.derive_facts` now states `breakdown_minor` for a contradicted breakdown that has a calculation, so model modes keep the records that contradict the claim. Every other non-supported status still states nothing.
2. **A reference-named settlement of a different amount is `unresolved` with the `amount_mismatch` gap, not `contradicted`** (cases 34, 38, 66 and 75).
   - In `_verdict`, a different amount leaves part of the credit unexplained. It does not conflict with a specific claim, per the `ClaimStatus` rule in `shared/CONTRACTS.md` and `alibi/SPEC.md`.
   - The failed-status and later-settlement conflicts are unchanged.
   - Because the claim is now unresolved, the unresolved-evidence rule moves its refs to `examined_refs`. Unnecessary items fall from 47 to 0, and all 47 came from these four cases.
3. **Period totals that span several issuers state `facts.by_issuer`** (cases 79 and 80).
   - Rules `period_total` and the shared `facts._period_facts` (one-pass and agents) split the same counted records by `issuer_id`. Each issuer gets every requested kind, including zero values.
   - It is emitted only when there are two or more issuers. The model path emits nothing if any term's issuer can't be read, so a partial split is never stated.

Two existing tests assumed the old amount-differs → `contradicted` rule. Their expected status is now `UNRESOLVED`. The `amount_mismatch` gap code and the detail sentence are still asserted.

## Measurements (public 80, offline)

| System | Before | After | Gained | Lost | Forbidden | Unnecessary |
| --- | --- | --- | --- | --- | --- | --- |
| rules | 41/80 | **50/80** | 8, 24, 34, 38, 66, 75, 76, 79, 80 | none | 0 → 0 | 47 → 0 |
| one_pass, `FakeClient` (empty proposal) | 45/80 | **50/80** | 8, 24, 76, 79, 80 | none | 0 → 0 | 12 → 12 |

Rules citation accuracy went from 0.968 to 1.000, and gap recall from 0.354 to 0.500. One-pass cases 34, 38, 66 and 75 still fail. Their gap is `settlement_not_found` because one-pass never retrieves the referenced payout, which is retrieval work outside this lane.

Tests: the whole `alibi/tests` suite gives **1435 passed** on the patched tree. That includes the 9 new tests in `alibi/tests/test_answer_accuracy.py`. These run on mutated copies of the public control case: matching, contradicted, other-credit, malformed or absent claims, an amount mismatch, a single issuer with no split, the issuer split with empty kinds, the no-partial-split rule, and the one-pass parity check.

Reproduce with a copy of the combined tree plus this patch and the shared `.venv`:

```bash
PYTHONPATH=$PWD ALIBI_DATA_DIR=/tmp/aa/data .venv/bin/python -m alibi.adapter --mode rules --cases 1-80 --out /tmp/aa/data/pred
PYTHONPATH=$PWD .venv/bin/python -m alibi.eval.scorer --predictions /tmp/aa/data/pred/rules.jsonl
PYTHONPATH=$PWD ALIBI_DATA_DIR=/tmp/aa/data .venv/bin/python -m pytest -q alibi/tests
```

## Not covered

- Agents mode doesn't receive `holder_claim` yet. Its runner signature is unchanged, and it only gets the shared `by_issuer` and contradicted-facts derivation.
- The one-pass path in `api.run_investigation` doesn't forward `holder_claim`. Only the adapter's direct `run_one_pass` call does.
- None of these numbers is registered with `results.py`.
