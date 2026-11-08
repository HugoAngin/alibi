# Evidence engine (eval prototype)

`agents/eval/evidence_engine.py` is a deterministic, advisory planner for one RFC. For each requested credit it lists the prerequisite checks (pass, fail or missing) and the exact source revisions behind them. It computes the integer residual where that is valid. It then plans a bounded set of lookups from references and gaps it can see, runs them through the request's scoped `find_evidence` callback, and records what each lookup changed. It does not sign, approve, apply policy or decide verdicts. The backend gates keep all of those.

**Status.** Lead approved this as a pure function over a case without gold (board comment 6718679896). It is **not** wired into the app runner. Wiring it in is Arnav's decision. If he does not accept it, it stays out of the frozen candidate. If he does, execution must rerun the final evaluation independently, because eval wrote the engine.

**Recorded reproduction.** Validation record `b130ba6b03` runs the committed implementation at `268d2997661434773553781475a297ab14feaf97`, declares five explored versions, and passed `tools/results.py verify --id b130ba6b03` in a clean checkout. The final engine and baseline-focused suite passed 183 tests; the full eval suite passed 536 with 16 existing expected failures before the rebase onto the current team changes. This is author-run reproducibility, not independent approval or a sealed evaluation. Earlier uncommitted development runs are disclosed below; only the final version has this reproducible results record.

**API cost.** No live Gemini comparison was run. Lookup counts are connector calls, not model calls or tokens. The engine makes no model calls itself, but adding it ahead of unchanged agent prompts does not establish savings. Integration should measure the existing usage logs for both pipelines on the same inputs, recording model calls, input/output/thinking tokens, latency and correctness. Use its checks and targeted evidence to avoid redundant model rounds only where the existing backend verdict and oversight gates still pass. Do not replace those gates with the draft answers below.

## API

```python
from agents.eval.api import evidence_report, run_engine, diff_reports, LookupCache

report = evidence_report(case_without_gold, "rfc-0001-01", budget=6)   # builds request_context() itself
report = run_engine(view, find_evidence, budget=6, cache=LookupCache(),  # app or adapter form
                    source_version=connector_watermark)                  # optional; enables reuse across calls
report.to_dict()                      # JSON-serialisable; every dataclass is frozen
diff_reports(old_report, new_report)  # changed checks, answers and dependency revisions
```

`evidence_report` refuses a case that still contains `gold`. It also refuses a sealed case number unless the lockbox is open, and that number is the only use it makes of the case number. The engine never reads family, variant, title, label or `view["merchant_lookup"]`. Lookup-only records arrive only through `find_evidence`.

Abridged output for Case 1, Bank A:

```json
{"request_id": "rfc-0001-01", "calls": 1, "budget": 6, "cache_hits": 0,
 "actions": [{"action_id": "L1", "key": "payout_reference", "value": "PO-0001-01",
              "tier": "exact_reference", "reason": "bank credit carries this payment reference",
              "justified_by": ["bank:txn-0001-004"], "state": "executed", "outcome": "ok",
              "returned": [{"source_id": "merchant:rec-0001-021", "revision": 1, "provenance": "merchant_record"}]}],
 "trace": [{"step": 0, "changes": [["txn-0001-004/settlement.record", null, "missing"],
                                   ["txn-0001-004/arithmetic.residual", null, "pass"], "..."]},
           {"step": 1, "action_ids": ["L1"],
            "changes": [["txn-0001-004/settlement.record", "missing", "pass"],
                        ["txn-0001-004/settlement.unique", "missing", "pass"], "..."],
            "link_states": [["txn-0001-004", "unmatched", "linked"]]}],
 "credits": [{"txn_id": "txn-0001-004", "link_state": "linked", "payout_record_ids": ["rec-0001-021"],
              "residual": {"basis": "settlement", "credit_minor": 216000, "sales_minor": 240000,
                           "refunds_minor": 18000, "fees_minor": 6000, "net_minor": 216000, "residual_minor": 0},
              "diagnosed_gap": null, "retrieval_complete": true, "checks": ["..."]}],
 "answers": {"q.source_of_funds": {"status": "supported", "gap": null,
             "facts": {"payout_references": ["PO-0001-01"], "settled_at": ["2026-10-27T16:00:00Z"]},
             "evidence_refs": [{"source_id": "bank:txn-0001-004", "revision": 1, "provenance": "bank_record"},
                               {"source_id": "merchant:rec-0001-021", "revision": 1, "provenance": "merchant_record"}],
             "hold_reasons": []}}}
```

Each check has a `basis`:

- `observed` means a field of a bank row or merchant record as it was delivered or returned. The engine does not verify signatures, so `observed` does not mean issuer-signed.
- `computed` means integer arithmetic over observed fields.
- `asserted` means the holder's claim or a self-supplied import. Its provenance is unverified, whatever trust the import itself claims.

The residual has one of three `basis` values:

- `settlement`: the components of a linked payout.
- `unlinked_payout`: a reference-bound payout that fails the rule, for example one that was pending.
- `balance_release`: only a release to the platform balance names the components.

Answers are drafts from the same rules as the fixed baseline (`baselines._rules`). Then **holds** apply. Any of the following forces an `unresolved` draft with no facts or citations, and the reason goes in `hold_reasons`:

- `identity_check_failed`: a quarantined or colliding identity touches the credit.
- `retrieval_incomplete`: a lookup returned an error or a refusal, or was skipped for budget.
- `self_supplied_evidence`: a supported draft would cite any `import:` record.

`diagnosed_gap` is a separate gap code read from the checks, and it does not change the draft.

## Identity, matching and retrieval

- **Order-independent identity.** Every distinct body is kept, and the active revision is computed from that set, so the order of deliveries and lookups cannot change the report. Tests shuffle 7 public cases with 3 seeds each.
- **Quarantine.** Merchant identity is (issuer, record_id). It is quarantined (excluded as `conflicting_identity` and withheld from matching) in two situations: one (issuer, event_id) carries different bytes, or one (issuer, record_id, revision) carries different content. Arrival order never decides which copy wins. The same revision redelivered under a new `event_id` with identical content is a duplicate (public Case 23). A quarantined component is never refetched, because a lookup cannot settle the conflict. Bank rows follow the same rule by (txn_id, revision), and imports by (record_id, revision).
- **Collisions.** The same record ID from two issuers is withheld as an `identity_collision`.
- **Other exclusions.** These are `superseded_revision`, `foreign_subject` and `after_decision_time` (a record that arrives or occurs after `issued_at`), plus `malformed`. A timestamp without a UTC offset counts as `malformed` and never raises an error.
- **Imports.** An import stays `import:` and `self_supplied`. It never replaces a merchant record with the same ID, and it cannot fill a quarantined one.
- **Matching is scan-based, not indexed.** Records are stored by identity, but each evaluation runs the shared candidate rule (`settlement.match_credit`) over the whole visible and retrieved pool for each credit. The tied-document and reference-bound helpers are linear scans too. There is no secondary index by amount, reference or date. On the public set the engine ingests about 21 inputs per request on average (1958 over 94 requests), and nothing has been measured at scale. `observed_inputs` counts the records ingested, not candidate comparisons, which are not instrumented.
- **Lookup tiers.** Each tier runs only while the credit is unresolved:
  1. The credit's own `payment_reference` is always fetched once, even if a pushed payout already links, so a correction served only by the endpoint is not missed.
  2. Description tokens, then the holder's cited reference.
  3. References in documents tied to the credit. A tie is a line stating the credit amount or reference, a same-amount balance release in the 5-day window, or a document whose only amount is the credit amount.
  4. One `covers_record_id` call for a component named by a tied document. It is skipped if a known payout already covers that component.
  5. At most two `payout_date` calls, for dates written in tied documents inside the window.
  6. `record_id` calls for missing components of a linked payout.
- **Stopping.** A credit stops when it is linked with every component present, when it is ambiguous, or when its own reference resolves to a conflicting payout.
- **Actions.** Actions are deduplicated by (key, value). The default budget is 6 calls per request, and budget skips are recorded as proposed but not executed. Outcomes are `ok`, `not_found`, `refused` or `error`, and the engine keeps only the outcome type, never exception text.
- **Cache.** Within one call, each lookup key is fetched at most once. `LookupCache` reuses results across calls only under the same non-empty `source_version`, which is a connector watermark that changes whenever a served record may have changed. Without one, every call refetches, because the engine cannot see lookup-only revisions. A regression test covers a payout corrected to `failed` that only the endpoint serves. The scope also binds the subject, request, audience, decision time and every visible input with its revision.

## Demo sequence

1. **Amounts reconcile.** Case 1, step 0. The balance release's components give 240000 − 18000 − 6000 = 216000, so `arithmetic.residual` passes on the `balance_release` basis. `settlement.record` and `release.not_settlement` are still missing.
2. **Settlement missing, targeted lookup.** The bank credit carries `PO-0001-01`, so L1 is `payout_reference=PO-0001-01` (1 call). For a no-reference variant, see Case 42: one `covers_record_id` call, justified by the same-amount release.
3. **Confirmed.** In step 1, `settlement.record`, `.status`, `.timing` and `.unique` move from missing to pass, the link goes `unmatched → linked`, and the draft is `supported`. It cites `bank:txn-0001-004@1` and `merchant:rec-0001-021@1`.
4. **Contradicted.** Case 2's holder cites `PO-0002-01`, and the lookup returns a pending payout. `settlement.status` and `holder.reference` fail, and the residual stays `unlinked_payout`. The draft is unresolved with `settlement_pending_at_booking`, after 1 call.
5. **Held.** If the same payout exists only as the holder's import, the credit still links and the residual is 0, but the draft is `unresolved` with `self_supplied_evidence`.

`diff_reports` shows the change between a no-lookup run and a lookup run: the changed checks, the answer status, and `merchant:rec-0001-021: [null, 1]`.

## Benchmark (public cases 1–80, keyless)

```bash
uv run --frozen python -m agents.eval.benchmark_evidence_engine            # stable final JSON line
uv run --frozen python -m agents.eval.benchmark_evidence_engine --timed    # adds CPU replay seconds on stderr
```

Both systems get the same `request_context` view and the same scoped lookup, and both drafts come from `_rules`. Each changed case is attributed to the stricter index (an exclusion the baseline does not apply), to holds, or to retrieval when neither applies. A case can involve both the stricter index and holds. Gold is read only for scoring. The score is agreement on status, gap, facts and evidence set per question. It is not the official scorer, policy gate or package pipeline, and it is not a safe-complete rate.

The run covers 80 cases, 94 requests and 174 answers.

| Metric | Fixed baseline | Engine |
| --- | --- | --- |
| Answers matching on all four fields | 150 | 152 |
| Status correct | 166 | 168 |
| Links exact | 76/80 | 80/80 |
| Cases with every answer exact | 67 | 68 |
| Supported where gold is not | 0 | 0 |
| Gold supported, system not | 8 | 6 |
| Held answers | n/a | 6 |
| Lookup calls | 78 | 81 |

- **Retrieval: 4 improvements, 0 regressions.** Hidden-reference Cases 42, 43, 46 and 47 are each linked by one covering-record lookup justified by a same-amount release.
- **Stricter policy: 3 regressions against public gold.**
  - Case 28 (strict index and hold). Gold accepts the first-arrived copy of a sale redelivered with different bytes. The engine quarantines it, so both answers are unresolved. The fixed baseline's breakdown changes if the two deliveries are swapped. The engine's does not.
  - Cases 39 and 40 (hold). Gold supports the credit from the holder's CSV export. The engine holds it as `self_supplied_evidence`.

  These are deliberate safety choices that disagree with the pinned gold rules. The official scorer would count them as misses, and lead decides whether the policy stays.
- **Lookups.**
  - hidden_reference: 14 → 16. The engine adds calls in 42, 43, 46 and 47, and saves one each in 44 and 45, where the CSV line containing the bank's withdrawal ID names one payout.
  - balance_vs_settlement: 5 → 7. Cases 17 and 18 add a covering lookup that returns `not_found`, which confirms the release-only reading.
  - injection: 11 → 10. Case 53 skips the planted `PO-9999-99`.
  - Other families are unchanged.

  The engine had 12 `not_found` outcomes, which include the bundled `BATCH-…` references; the baseline makes the same calls.
- **Shared rule limits.** Both systems miss 16 further answers. 14 give the generic `settlement_not_found` where gold has a specific gap (Cases 17, 18, 32, 34, 38, 66 and 75), and in Cases 79 and 80 `facts.by_issuer` is missing. `diagnosed_gap` agrees with gold's source-of-funds gap on 78 of 80 credits. This was 79 before the quarantine, which makes Case 28 report `missing_cited_record` where gold has none.
- **Timing.** `--timed` CPU replay (about 0.03 s for the baseline and 0.14 s for the engine across all requests) measures local Python only. It says nothing about live LLM speed or end-to-end speed.

**Variants for `results.py`: 5.** Each engine code state was scored on the full public set.

1. The initial engine.
2. Version 1 plus `diagnosed_gap`.
3. The residual-basis fix.
4. Cache reuse only under `source_version`, strict UTC timestamps, collision exclusions, and attribution metrics.
5. Order-independent quarantine and the self-supplied trust hold.

Versions 1 to 4 gave identical answers. Version 5 changed Cases 28, 39 and 40 as described above. Versions 3, 4 and 5 were each rerun once, and each rerun gave a final line identical to the first. Partial inspection scripts and a three-case subset run used the same versions. The design targets the hidden_reference clues in the public README, so the results are in-sample. No sealed or private data was read.

## Integration hook (proposal, not implemented)

The natural place is `alibi/agents/runner.py`, `_Run.investigate_credit`. Consult the engine when the Challenger proposes `none` or fails while `settlement_candidates` is not `linked`. Spend the existing `ActionBudget`, and keep the code verdict and the backend gates unchanged. The adapter mapping is:

| Engine | `alibi/agents/actions.py` | Note |
| --- | --- | --- |
| `payout_reference` | `lookup_settlement {payout_reference}` | `Material.knows_reference` accepts only references in structured record and txn fields, so document-text and holder-cited references would come back `refused` unless that check is widened. |
| `covers_record_id` | `lookup_payouts_covering {record_id}` | A passport listing filtered in code, not the merchant connector (see `alibi/NEEDS.md`). The material's `lookup_count` does not count it. |
| `payout_date` | `lookup_by_date {payout_date}` | The same listing caveat applies. |
| `record_id` | none | A gateway record fetch is needed. |
| `ok`, `not_found`, `refused` | same | |
| `unavailable`, `error` | `error` | |
| `budget_exceeded` | `skipped_budget` | |

The view also needs a converter from `Material` (`EvidenceRecord` and `BankTxn`) to the contract dicts that `request_context` returns. Alternatively, `alibi.adapter.run_case` can call `run_engine` on the case view before `run_investigation` and pass its planned actions in as hints. To reuse lookups across calls, the gateway must supply a `source_version` watermark. It has none today.

## Limitations

- **Gold disagreement.** The quarantine and the self-supplied hold disagree with the pinned gold rules on Cases 28, 39 and 40.
- **Uniqueness.** It is checked only among visible and retrieved candidates. When a credit has no reference, a competitor the engine never fetched would not make the link ambiguous. The baseline has the same limit.
- **Matching.** It is scan-based and unmeasured at scale.
- **Reference parsing.** Only hyphen and underscore forms are extracted, and exact keys are never rewritten. `PO/0046/02` is not normalised, and Case 46 is solved by the covering lookup instead.
- **Date parsing.** It covers ISO, `DD.MM.YYYY` and `D Month [YYYY]` only.
- **Document ties.** The tying rules are heuristics designed on public cases.
- **Multi-credit RFCs.** None of the public RFCs is multi-credit, so the per-credit loop is untested on public data.
- **Provenance.** Signature and provenance checks belong to the backend. `observed` checks are as delivered.
- **Readiness.** This is a prototype. It is not production-ready, and it has not been run on sealed data or through the app's scorer, policy and package pipeline.
