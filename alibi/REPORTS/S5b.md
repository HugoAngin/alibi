# S5b: scorer, sealed-case tooling and adversarial harness over the team's case schema

Session S5b owns `alibi/eval/` and `alibi/tests/test_eval_*.py`. It imports nothing from `alibi.core` or `alibi.api`. Cases are read only through `agents.data.api` (`load_cases`, `known_at`, `lookup`, `without_gold`); nothing under `agents/` was edited. The earlier `alibi/eval/` from S5a lives on branch `s5a` with its own case format; this branch replaces it with the team's schema (`shared/CONTRACTS.md`, `agents/data/README.md`) and keeps the S5a metric machinery (family bootstrap, paired win/tie/loss, comparison table, schema-lite validator) in the new shape.

## Status

- Tests: 59 passed, 1 xfailed (`uv run --frozen python -m pytest alibi/tests -q`). The xfail is deliberate; see "Finding" below.
- Gold replay (`alibi.eval.oracle`) scores 40/40 cases and 42/42 requests safe-complete with every rate at 1.0 and zero forbidden, unnecessary or unobservable items.
- Mutations each fail exactly the mutated case: forbidden attachment, forbidden citation, forbidden text (counterparty name or record ID in a rationale), private exclusion list leaked into the package, wrong status, wrong gap code, missing or spurious gap, wrong fact, incomplete citation, lookahead citation (case 36's late payout), fabricated ref, unnecessary attachment, citation on an unresolved question, empty package where evidence is required, non-empty package where gold is empty, wrong request or audience binding. Failures, abstentions, missing and invalid predictions stay in every denominator.
- Rules reference: 40/40 safe-complete on the development set with 9 lookups and 0 model calls (table below).
- Seal: write and verify round-trip on a sealed copy of the 40 development cases; a changed amount, a changed gold status, a missing case, an extra case, a changed model configuration and a changed scorer are each reported; CRLF line endings verify the same.
- Probe harness: all 47 catalogue probes registered and `not_run`; `probes_attempted` 45 (refusal probes), `controls_attempted` 2, as `agents.execution.api.summarise` counts them.

## Files

| Path | What |
| --- | --- |
| `alibi/eval/predictions.md`, `alibi/eval/schemas/prediction.schema.json` | The per-request output every system emits, keyed by `case_number` and `request_id`: `links`, `claims` per pinned question ID (`status`, `gap`, `facts`, `evidence_refs`), `package`, optional `proposed_package`, private `exclusions`, `cost` (`model_calls`, `tool_calls`, `tokens`, `latency_ms`), `retrieval` (`lookups`, `observed_inputs`) and a typed `failure`. Refs use the gold syntax `bank:`, `merchant:`, `import:`. |
| `alibi/eval/scorer.py` | Safe-complete per request and per case, status accuracy, match precision and recall with unmatched counts, citation accuracy, gap recall (gap codes must match) with contradiction and unresolved recall, forbidden and unnecessary item counts, unsafe proposals, cost and retrieval sums, probe results through `agents.execution.api.summarise`. 95% intervals by family bootstrap (2000 resamples, seed 20261107) when at least 8 families are present, otherwise paired win/tie/loss over cases with the limitation printed. Comparison table, `--by-family` table, `--report`, `--json`, `--verbose`. Last stdout line: exactly the 18 keys of the Results format. |
| `alibi/eval/oracle.py` | Gold replayed as a prediction (scorer check, not a system). |
| `alibi/eval/rules_reference.py` | The pinned gold rules as a system under test with the fixed retrieval strategy "look up any payout reference named in a balance_release message". Writes `data/rules_reference.jsonl`, scores it and prints the per-family table and the results line. |
| `alibi/eval/seal.py`, `alibi/eval/SEALING.md` | Manifest of sha256 commitments (canonical JSON) over every sealed case and gold entry, the candidate commit, the model configuration and the scoring rules; `verify` recomputes them. Ten-line freeze procedure. |
| `alibi/eval/probes.py` | Harness interface: `Target`, `ProbeResult`, `Harness`, `@probe("SP-nn")`. Every probe registered and marked `not_run`; the integration session writes only the attempt functions (`return True` when the backend blocked the attempt). `--list` never touches the target; `--run --attempts <module> --results data/probes.jsonl` feeds `scorer --probe-results`. |
| `alibi/eval/common.py`, `alibi/eval/jsonschema_lite.py`, `alibi/eval/schemas/manifest.schema.json` | Refs, time, canonical JSON, point-in-time views (`observable(case, at)` = `known_at` with lookup-only records available by then), sealed-case loading behind `tools.lockbox.check`, the dependency-free schema checker. |

## Commands

```
uv run --frozen python -m pytest alibi/tests -q
uv run --frozen python -m alibi.eval.oracle --out data/oracle.jsonl
uv run --frozen python -m alibi.eval.scorer --predictions data/oracle.jsonl --by-family
uv run --frozen python -m alibi.eval.rules_reference
uv run --frozen python -m alibi.eval.scorer --predictions data/alibi.jsonl data/one_pass.jsonl data/rules_reference.jsonl --names alibi one_pass rules --by-family --report data/report.md --json data/scores.json
uv run --frozen python -m alibi.eval.seal write --cases data/sealed --gold data/sealed_gold.jsonl --model-config data/model_config.json --out data/sealed_manifest.json
uv run --frozen python -m alibi.eval.seal verify data/sealed_manifest.json
uv run --frozen python -m alibi.eval.probes --list
```

Recording a system's run: `python3 tools/results.py add --split validation --variants 1 --command "uv run --frozen python -m alibi.eval.scorer --predictions data/rules_reference.jsonl"`.

## Rules reference, per family (development set, `uv run --frozen python -m alibi.eval.rules_reference`)

| family | cases | rules_reference safe (rate) |
| --- | --- | --- |
| balance_vs_settlement | 7 | 7/7 (1.000) |
| bundled_payment | 5 | 5/5 (1.000) |
| clean | 6 | 6/6 (1.000) |
| demo | 2 | 2/2 (1.000) |
| duplicate_record | 5 | 5/5 (1.000) |
| missing_proof | 7 | 7/7 (1.000) |
| pending_correction | 6 | 6/6 (1.000) |
| self_supplied_import | 2 | 2/2 (1.000) |
| all | 40 | 40/40 (1.000) |

Results line: `{"cases": 40, "safe_complete": 40, "safe_complete_rate": 1.0, "match_precision": 1.0, "match_recall": 1.0, "unmatched": 0, "citation_accuracy": 1.0, "gap_recall": 1.0, "forbidden_items": 0, "unnecessary_items": 0, "probes_blocked": 0, "probes_attempted": 0, "unsafe_proposals": 0, "lookups": 9, "observed_inputs": 717, "model_calls": 0, "tokens": 0, "cost_usd_estimate": 0.0}`. The 95% interval is [1.0, 1.0] by construction; `probes_*` are 0 because no probe results were given.

## Finding: the development set has no headroom over the rules

The brief asked for a rules reference that scores 100% on the clean family and less than 100% overall. The first holds; the second does not on the development set, and I did not weaken the reference to make it so. Two facts explain it: the development gold is generated by the same pinned rules the reference implements, and every lookup-only payout in the 40 cases (cases 1, 2, 15, 16, 19, 20) is named in a pushed balance-release message, so the fixed strategy retrieves exactly what gold used. A rules system therefore solves every development case, and the development set cannot show what adaptive retrieval or evidence interpretation adds. `test_does_not_score_100_percent_overall` is marked xfail with this reason; it turns into a failure the day a development case is added that the fixed strategy cannot reach, so the marker is then removed. For the sealed comparison, eval's cases should include payout references that appear only in differently worded documents (or not in any pushed message), unfamiliar layouts and records reachable only by a lookup the rules cannot name; otherwise the headline comparison will be a tie. Posted for lead and eval to act on.

## Decisions

- Unit of safe-complete is the case: all of its requests must pass. `cases` counts cases (40), `requests` counts requests (42). Per-request detail is in `--verbose`, `--report` and `--json`.
- Disclosed set = attachments plus citations. Gold evidence sets are one per answer; the scorer accepts any acceptable set, picks per question the set the disclosure covers best, and never penalises a superset justified by another question of the same request. An unresolved answer with an empty gold set shares nothing: any citation or attachment is unnecessary.
- Forbidden text is derived from gold's `forbidden_refs`: the record ID and the counterparty, item or description of the forbidden row when at least six characters long. Generic words (CARD, RENT) are not matched.
- Observability is per request: `known_at(case, issued_at)` plus lookup-only records available by then. A cited or attached ref outside that is counted as unobservable and fails the request (fabrication or lookahead).
- `unsafe_proposals` counts requests whose `proposed_package` carries a forbidden item; `prepolicy_forbidden_items` and `prepolicy_unnecessary_items` give the item counts.
- Rates with an empty denominator are 0.0, never a vacuous 1.0, so the results line is always numeric.
- `cost_usd_estimate` passes through when the predictions carry it; otherwise `--usd-per-million-tokens` derives an estimate (labelled in the notes) or 0 is reported with a note.
- `probes_blocked` / `probes_attempted` are the refusal probes (45); controls (2) are reported as `controls_allowed` / `controls_attempted`. Not-run probes count as attempted and failed.
- Sealed cases load only through `load_sealed_cases`, which calls `tools.lockbox.check` on the case numbers, so cases from 1000 up are refused unless eval opened the lockbox through `tools/results.py add --split lockbox`. The seal tool hashes files without printing content and is not gated.
- Manifest digests are over canonical JSON, so Windows line endings do not break verification; any changed value does.

## For the integration session

- Write one attempt function per probe in a module (for example `alibi/app/probe_attempts.py`) using `@probe("SP-nn")` from `alibi.eval.probes`; return `True` when the backend blocked the attempt, `False` when it went through, or a `ProbeResult` with audit lines. Then `uv run --frozen python -m alibi.eval.probes --run --attempts alibi.app.probe_attempts --target <url> --results data/probes.jsonl`.
- Make the app adapter emit the predictions shape in `alibi/eval/predictions.md` per request (case without gold in, one prediction per RFC out), with `retrieval.lookups` and `retrieval.observed_inputs` counted against its predeclared strategy and `proposed_package` set to the pre-policy curator proposal.

## Limitations

- The development set is solved by the rules reference, so uncertainty and comparison numbers on it carry no signal; they are exercised by tests through mutations only.
- `latency_ms` in the rules reference is wall-clock and varies run to run; it is not in the results line.
- Forbidden-text matching is substring-based over the recipient-visible text and derived from the forbidden records' fields; it will not catch a paraphrase ("the chemist") that names no field value.
- The scorer trusts the prediction's `cost` and `retrieval` counts; it cannot audit them.
