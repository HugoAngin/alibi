# Fresh-clone RUNBOOK run, replay mode, no key (lead 10:16Z, post 6720250667)

Driver: data (Hugo's laptop, macOS). 2026-11-08 11:15:42–11:19Z. A fresh `git clone` of main at `fa0c481` (`sealed-candidate-46-gfa0c481`), followed `alibi/RUNBOOK.md` exactly as written. No Gemini key: `.env` copied from `.env.example` with both values empty, and `GEMINI_API_KEY` unset in the shell.

## Steps and results

| Step (RUNBOOK) | Command | Result |
| --- | --- | --- |
| Install | `uv sync --frozen`; `cp .env.example .env` | 1 s (packages cached on this laptop). |
| Tests | `uv run --frozen python -m pytest alibi -q` | **2 failed, 1373 passed** in 66 s. `test_bank_service::test_malformed_json_bodies_are_refusals_not_server_errors` expects "nested" and gets "the package needs a package_id identifier". `test_demo::test_smoke_three_servers_answer_and_the_process_exits_zero` fails with ports 8100–8102 free, so it is not a port clash. The 3 dispatch-page failures from QA pass 2 are gone (research applied the S5 template patch, `639a170`). |
| Start | `uv run --frozen python -m alibi.demo_services --case 1 --timeout 3600` then `ALIBI_REPLAY=1 uv run --frozen python -m alibi.app --timeout 3600` | Both up. `/health`: `mode: rules-only`, `replay: true`, **`seed: case_adapter`, `bank_url: in-process`, `merchant_url: case_adapter`**. |
| Holder | `http://localhost:8100/passport` | Alert, both RFCs and all 4 transactions present at start, preloaded from the case file. |
| Bank A | Run → Approve → **Send to Bank A** | Both questions Supported; 21 included, 0 excluded. The Send button is now in the approval card. Delivered `pkg_2e2c1ce54c9e91bc842c6afb`, receipt accepted. |
| Bank B | Run → Approve → Send to Bank B | Supported; 10 included. Delivered `pkg_229fcca8b89dfca3e336e811`. |
| Bank inboxes | `/bank/bank_a/inbox`, `/bank/bank_b/inbox` with the tokens file | Each lists only its own package, authentication `valid`. Bank A 2 supported, Bank B 1 supported; both requests `answered`. |
| Verify | `uv run --frozen python -m agents.eval.verify_package pkg_a.json --registry data/alibi/keys/trust_registry.json --recipient bank_a` (JSON from `/requests/rfc-0001-01/export.json`) | Exit 0, `status: verified`, package signature valid, both claims Supported. |
| Replay with agents | Reset per RUNBOOK (delete `data/alibi/app/case_1.db` and `case_1.state.json`), then `ALIBI_REPLAY=1 ALIBI_MODE=agents uv run --frozen python -m alibi.app` | Detective 4, Challenger 3, Curator 2, Matcher not called; "model calls 0, lookups 1, tokens 31799; REPLAY: served from recording case1_agents.json". Both claims Supported. |

## What the RUNBOOK gets wrong

1. **The documented start does not connect the app to the services.** With the two RUNBOOK commands, the app seeds from the case file (`case_adapter`) and uses an in-process bank, so the merchant on 8101 and the bank on 8102 run but are unused. Everything (the release, the credit, the alert and both RFCs) is present at 0:00, and nothing arrives live. The connector path is `alibi.demo`, which the RUNBOOK still marks "**pending** (item 3)" although it is on main and works (QA pass 2). Fix: replace the "Until then, two processes" block with `uv run --frozen python -m alibi.demo --timeout 600 --case 1 --reset [--replay]` and its `advance`/`rfc` driver steps, or set `ALIBI_MERCHANT_URL`/`ALIBI_BANK_URL` in the two-process version.
2. **Replay mode shows no Gemini unless `ALIBI_MODE=agents` is set.** `ALIBI_REPLAY=1` alone gives `mode: rules-only`, and every role reads "skipped". The RUNBOOK's Gemini section names `ALIBI_REPLAY` but never `ALIBI_MODE` (QA pass 2, P0-B). Both variables together work, as the last row shows.
3. **On the RUNBOOK path the clock problem does not occur**: the in-process bank shares the app's clock, so Bank A delivered first time. On the `alibi.demo` connector path it does occur (QA pass 2, P0-A): run `python -m alibi.demo advance --to <UTC now + 10 min>` before the first Send.
4. Reset instructions are right for the two-process start. A sent request cannot be re-run ("Not available while the investigation is complete"), so changing mode after a run needs that reset.

## For the backup video

The video should use the connector path (records arriving live) with both variables set:

```bash
ALIBI_MODE=agents uv run --frozen python -m alibi.demo --timeout 900 --case 1 --reset --replay
```

Then add the clock advance before the first Send. Data's final `demo_script.md` will carry these commands.
