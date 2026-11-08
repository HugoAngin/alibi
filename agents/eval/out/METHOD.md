# Alibi evaluation method

The evaluator authored 32 sealed cases across eight scenario families and 12 adversarial cases. Cases are ledger-first synthetic inputs. The answer key is maintained separately at the request level. The private pack also contains an adversarial step plan. The release seal will record canonical digests of the case and gold contents, the candidate commit, model configuration, and scoring rules.

Each case passes only when all its requests pass. The official scorer tests status, facts, citations, disclosure, and recipient binding. The independent check uses per-request privacy restrictions and source revisions. Report disagreements between the two. Rules and one-pass Gemini share fixed, predeclared retrieval. Full Alibi can retrieve adaptively from the same evidence boundary. Compare final disclosure and pre-policy proposals separately. Count failures and abstentions in the denominators.

## Input and source audit

Run `uv run --frozen python -m agents.eval.audit_inputs` for the public cases. Eval can add `--inputs` with the private prepared case directory. The audit compares `without_gold`, `known_at`, and the request context against independently evaluated timestamps. It checks named lookups for time and subject scope and scans the Python runtime for direct gold keys, development case paths, and development case loaders.

The rehearsal covered 80 public cases and 94 requests, plus 44 private cases and 92 requests. It checked 50 public and 162 private record lookups, with no mismatches. All public gold labels were stripped. The worker payload also omitted evaluator titles, variants, family labels, and case labels. The scan found no direct runtime references in the 57 published Python files it checked. The references it did find were in evaluation and test code. `alibi/adapter.py` was absent, so rerun with `--require-adapter` at freeze and inspect the adapter's live traces. The macOS sandbox test verifies that the subprocess cannot open the gold file. Static scanning covers direct references and should be read alongside that process check.

Before freeze, the evaluator moved the ignored authoring archive out of the checkout and into the protected private directory. SHA-256 comparisons verified that all 84 authoring files and three prepared release files retained their contents. The former archive path had no tracked history. The worker sandbox denied access to the prepared gold file and a relocated authoring file.

## What could fool us

| Risk | Check run | Observed result and remaining check |
| --- | --- | --- |
| Gold leakage | Compare loader outputs against independently parsed receipt and RFC times. Exercise the subprocess with an allowlisted case and a denied gold path. | Public and private view checks passed. macOS denied the synthetic gold file. Recheck the actual S5 adapter and frozen gold path at freeze. |
| Generator and rule overlap | Run the published deterministic baseline against the separately authored private cases during authoring. | It produced all 76 sealed-request predictions with zero schema errors and scored below the ceiling. Keep the case-level results private until the scored run. |
| Dependence within families | Count families separately from variants. Use case-family resampling for the interval and paired differences. | The pack has eight sealed families. The official scorer uses 2,000 family resamples when eight or more groups are present. The actual interval awaits the frozen run. |
| Injection carriers | Check that the probe requires a landed marker, a structured refusal and a corresponding audit entry. Exercise an allowed foreign read as the negative control. | The synthetic transport tests distinguished refusal, missing audit evidence, and an allowed read. Actual case and tool outcomes await the live adapter. |
| Cache reuse across systems | Rehearse the three system paths in separate workers and scratch stores. Use the independent one-pass client with its cache disabled. | The public rehearsal retained 94 request outcomes per system. Check the live adapter's cache and call telemetry before drawing comparisons. |
| Rerun cherry-picking | Bind a manifest to one capture directory, hash the retained predictions, and replay the scorer. Try altering a prediction and starting the same capture in a second directory. | Public replay returned identical metrics without starting workers. Both the altered capture and second directory were rejected. A frozen interruption stays in the run record. |

The public rehearsal exercised the runners and scorers with drafts and a stubbed Gemini transport. Benchmark figures and live security outcomes will come from the frozen candidate.

## Clean checkout rehearsal

A fresh depth-one clone from GitHub at `306559d` on the evaluator Mac had no local `.env` and used the frozen Python dependencies. Clone took 1.72 seconds. The combined Python suite took 24.65 seconds wall time, with 1,455 passes, two skips, 26 expected failures, and two `google-genai` syntax warnings. The public runner took 12.19 seconds wall time for 80 cases and 94 requests. The clone stayed clean. This run used the Mac's installed `uv` and package cache.

Hugo independently repeated the same test and 80-case public runner on the presenter laptop from a fresh clone at `9d0933f`. The suite recorded 1,455 passes, two skips and 26 expected failures in 30.3 seconds. The dry run took 17.6 seconds. His run found seven missing links in the second-platform cases. Both baselines now use the published connector mapping for deliveries and lookups at `e0f566d`. The live-stack rehearsal remains open. [Presenter laptop record](../../data/out/rehearsal_2026-11-08.md)

The live comparison reports the shared backend probe coverage alongside the official fixed denominators. The deck records how many probes returned a definite outcome, how many could not run, and how many errored. Keep the raw catalogue JSONL for per-probe evidence.
