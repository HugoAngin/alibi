# Freeze procedure

1. Author the sealed and adversarial cases from scenario families separate from the development and demo cases, as JSONL without gold, with gold in one evaluator-only file keyed by case number (`alibi.eval.common.read_gold_file`); number them from `lockbox_from` (1000) upward and keep both files out of the repo and out of builder reach until the freeze.
2. Freeze the candidate: one commit on `main`, the prompts and `alibi/llm/config.py` it uses, and a model configuration JSON (model IDs, thinking levels, call and token caps, prompt digests).
3. Write the manifest: `uv run --frozen python -m alibi.eval.seal write --cases <dir> --gold <gold> --candidate <commit> --model-config <json> --purpose sealed --frozen-by eval --systems rules one_pass alibi --out <manifest>`; it commits sha256 digests of every case and gold entry (canonical JSON), the candidate commit, the model configuration and the scoring rules.
4. Commit the manifest and post `done` to lead before running any frozen system.
5. Run each frozen system on the same cases from `known_at(case, issued_at)` plus `lookup()` only, keeping every timeout and failure in its predictions file; never rerun to improve a number, and disclose any rerun.
6. Run the probe harness against the frozen build: `uv run --frozen python -m alibi.eval.probes --run --attempts <module> --results data/probes.jsonl`.
7. Verify before scoring: `uv run --frozen python -m alibi.eval.seal verify <manifest>` must print `verified`; a changed case, gold entry, scorer or model configuration is a problem to disclose, not to fix quietly.
8. Score once with the lockbox open: `python3 tools/results.py add --split lockbox --variants <n> --command "uv run --frozen python -m alibi.eval.scorer --sealed <dir> --gold <gold> --predictions <alibi> <one_pass> <rules> --names alibi one_pass rules --probe-results data/probes.jsonl --by-family --report <md> --json <json>"`; record one run per system with the same keys.
9. Commit the sealed cases, gold, manifest, predictions, report and `agents/eval/out/results.jsonl` after that single look, and run `tools/results.py verify`.
10. Any post-freeze fix needs a fresh holdout for a new unseen claim; the first look stays in the report as it was.
