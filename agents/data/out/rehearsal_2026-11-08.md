# Second-machine rehearsal (data, Hugo's laptop), 2026-11-08 ~02:25Z

Requested by eval (post 6716141918). Run from a fresh clone of `main`, separate from the working checkout. No `.env` was present.

| Item | Value |
| --- | --- |
| Commit | `9d0933fd0fb5f4112cfbd895724ddf08cef65540` ("eval: plan Modal replay and record clean checkout rehearsal") |
| Machine | macOS 15 (Darwin 24.6.0), Apple silicon |
| uv / Python | uv 0.13.7 (Homebrew), Python 3.14.8 (from uv) |
| `git clone` | 1.3 s |
| `uv sync --frozen` | 0.46 s wall. Packages came from the local uv cache; a machine with an empty cache downloads them first. |
| `uv run --frozen python -m pytest -q tools agents/data agents/execution agents/eval alibi` | **1455 passed, 2 skipped, 26 xfailed**, 2 warnings, **30.3 s wall** (pytest reports 29.2 s) |
| `uv run --frozen python -m agents.eval.run_sealed --dry-run --out <new dir outside the clone>` | exit 0, **17.6 s wall**. It wrote `agents.jsonl`, `one_pass.jsonl`, `rules.jsonl`, `probes.jsonl` (empty), `report.md`, `deck-table.md`, `run.json`. Input hash `2874adb7…c8b583`. |
| Clone after both runs | `git status` clean; nothing written inside the clone |

## Dry-run counts

- Each system scored **80 cases**, **94 requests**, **12 families**: cases 1–80, plus 14 second requests (Bank B) in cases 1–2, 51–60 and 79–80.
- In the dry run, all three systems record a failure on every request (94 of 94: `unavailable`, "shared policy and delivery gate required"). So safe_complete is 0/80 everywhere, and agents link nothing (58 unmatched, 0 lookups). This fits "stubbed model and drafts only" and is not a new failure.
- rules and one_pass: match precision 1.000, recall 0.810, link-state accuracy 0.863, 78 lookups, 1956 observed inputs, 0 forbidden items, 0 unsafe proposals. probes: 0 attempted.

## New findings

1. **No new test failures and no setup gaps.** The only warnings are two `SyntaxWarning`s from inside `google-genai`'s `types.py` (invalid escape `\/`) under Python 3.14. They are harmless.
2. **The rules baseline misses 11 link states. Seven are every linked second-platform case.** I compared `rules.jsonl` with the gold links:
   - Cases 42, 43, 46, 47 (hidden_reference: orders and weekday, date only, email layout, CSV layout). These are expected and match data's regex probe.
   - Cases 71, 72, 73, 76, 77, 78, 80 (second_platform, gold `linked`). The baseline returns `unmatched` because it does not map the second platform's schema: a paid `weekly_statement` with `transfer_ref`, `net_payable_pence` and `paid_at` never becomes a payout candidate. Gold applies the pinned rules after `agents.data.api.to_contract()`.
   - This is eval's decision. Either the rules baseline gets the same per-connector mapping (a production rules system would have an adapter for each platform, and SPEC says ordinary reference parsing belongs to the baseline), or the README and deck state that the baseline has no second-platform adapter. Otherwise the second-platform rows compare Alibi against a baseline that cannot read the data.
