# Alibi runbook

Everything runs from the repo root with the team's locked environment. Nothing here needs a real
account, real money or a real bank: every record is labelled synthetic and both connection layers
are simulated services. Times are UTC, money is integer pence.

## Install

```bash
git clone <repo> && cd <repo>
uv sync --frozen            # one Python environment from pyproject.toml and uv.lock; never pip install
cp .env.example .env        # then fill the two names below
```

`.env` names (values never printed, never committed):

| Name | Used for |
| --- | --- |
| `GEMINI_API_KEY` | Gemini calls by the agents, the one-pass baseline and the policy parser. Read by `alibi.llm` from the environment, falling back to the repo-root `.env`. The holder app's `/rules` Gemini layer runs only when the key is **exported** in the shell that starts the app. |
| `ALIBI_MASTER_KEY` | Fernet key that encrypts the passport store. Optional: without it the app writes `data/alibi/app/master.key` beside the databases. |

Other environment names (all optional, documented in `alibi/app/UI.md` and `alibi/connectors/SERVICES.md`):
`ALIBI_CASE`, `ALIBI_DATA_DIR`, `ALIBI_KEY_DIR`, `ALIBI_NOW`, `ALIBI_REPLAY`, `ALIBI_API_ORIGIN`,
`ALIBI_MERCHANT_URL`, `ALIBI_BANK_URL`, `ALIBI_TOKENS_FILE`, `ALIBI_HOLDER_TOKEN`,
`ALIBI_BANK_A_TOKEN`, `ALIBI_BANK_B_TOKEN`, `ALIBI_FEED_TOKEN`, `ALIBI_ADMIN_TOKEN`, `ALIBI_LLM_CAP`.

## Tests

```bash
uv run --frozen python -m pytest alibi -q                                   # the backend, no network, no Gemini
uv run --frozen python -m pytest -q alibi agents/execution agents/eval agents/data tools   # the whole team
uv run --frozen python -m pytest -q --runxfail agents/eval/test_redteam_llm.py agents/eval/test_redteam_core.py   # eval's red team, all fixed
```

Windows: set `PYTHONUTF8=1` for commands that print non-ASCII.

## Start the demo stack

**pending** (item 3): `uv run --frozen python -m alibi.demo --timeout 600 [--case 1] [--reset]`.

Until then, two processes:

```bash
uv run --frozen python -m alibi.demo_services --case 1 --timeout 3600     # mock merchant 8101, mock bank 8102
uv run --frozen python -m alibi.app --timeout 3600                        # holder app and analyst consoles on 8100
```

| Where | URL | Credential |
| --- | --- | --- |
| Holder (Maya) | `http://127.0.0.1:8100/passport` | none in the demo; `ALIBI_HOLDER_TOKEN` when set (bearer or the `alibi_holder` cookie set once by `/passport?token=`) |
| Request page | `http://127.0.0.1:8100/requests/rfc-0001-01` | holder |
| Bank A console | `http://127.0.0.1:8100/bank/bank_a/inbox?token=<bank_a token>` once, then the bare URL | `bank_a` token: `ALIBI_BANK_A_TOKEN`, else the `bank_a` entry of the tokens file |
| Bank B console | `http://127.0.0.1:8100/bank/bank_b/inbox?token=<bank_b token>` | `bank_b` token |
| Mock merchant | `http://127.0.0.1:8101/` | lookup routes open; `/admin/*` need the `admin` token from a loopback client |
| Mock bank | `http://127.0.0.1:8102/` | recipient routes need `bank_a` / `bank_b`; `/feed/*` the `feed` token; `/admin/*` the `admin` token |
| Health | `http://127.0.0.1:8100/health` | none |

Tokens live in one file, `data/alibi/bank_tokens.json` (`bank_a`, `bank_b`, `feed`, `admin`),
created with random values on first use and shared by the app and the services; the environment
names above override. The file path is printed, never a value.

A bank's RFC expires: `rfc-0001-01` at 2026-11-09T09:00Z, `rfc-0001-02` at 2026-11-11T14:00Z.
After those dates set `ALIBI_NOW` (a fixed UTC clock) or regenerate the fixtures.

Reset: delete `data/alibi/app/case_<n>.db` and `case_<n>.state.json` (keep `master.key` unless you
delete every database beside it) and the bank state `data/alibi/bank/state.json`. A restart
**without** a reset replays the case as duplicates and keeps history; that is the persistence
check, not a reload.

## Live demo (the sites drive it)

Two terminals from the repo root:

```bash
uv run --frozen python -m alibi.demo --case 1 --reset     # app 8100, merchant 8101, bank 8102, director 8103
cd web && pnpm demo                                       # alibi 4321, merchant 4322, bank 4323
```

Then, in the browser, with no CLI steps:

1. Marketplace `http://127.0.0.1:4322/seller/payouts/`: **Withdraw**. The director moves every clock
   to the payout landing (10-26, 10-28, then 10-29 08:00), so Maya's alert and Bank A's case
   FC-0147 appear.
2. Bank `http://127.0.0.1:4323/` (passcode printed by `pnpm demo`): open FC-0147, **Request
   customer's alibi**, Send. The clock moves to the request's issue time (11-02 09:00).
3. Maya `http://127.0.0.1:4321/simulate/`: open Bank A's request, Run investigation, Approve, Send.
4. Bank: the case reads "Alibi received"; the inbox opens the context report.
5. **Reset demo** (bank console header, or Maya's simulation bar) puts everything back to step 1 in
   about a second.

Every page polls its site's `/live/` and reloads when the demo's state changes, so all open
screens follow within about 1.5 s. One clock: the holder app reads the mock bank's clock
in-process, so a package is never "issued in the future" and the requests' expiry does not
depend on today's date. Director routes (`/demo/state|withdraw|request|reset`, bearer
`ALIBI_DEMO_TOKEN` or the tokens file's `admin`) are in `alibi/demo_director.py`; hosted, the
router forwards `/demo/*` to it, and the Workers need `ALIBI_DEMO_ORIGIN` and `ALIBI_DEMO_TOKEN`.

## Routes

The holder and analyst route table (method, path, request, response, auth, refusal codes) is
`alibi/app/UI.md`, section "Routes, JSON and credentials". The mock merchant and mock bank routes
are `alibi/connectors/SERVICES.md`. Every route answers JSON under `Accept: application/json` with
`version: "alibi-ui/1"`; refusals are 4xx `{"code", "reason"}`.

## Gemini, replay and the policy chain

```bash
uv run --frozen python -m alibi.llm.probe                 # 2 to 4 calls: lists the models and answers one word each
ALIBI_LLM_CAP=10 uv run --frozen python -m alibi.llm.try_prompts   # the Challenger prompt live, 2 calls
```

Models: `gemini-3.8-flash` (routine roles) and `gemini-4-argon` (Challenger), pinned in
`alibi/llm/config.py`. `ALIBI_LLM_CAP` caps calls per process (default 120). Every call logs one
row (no prompt text, no answers, no keys) with `case_number`, `system`, `run_id` to
`data/alibi/llm_log.jsonl`, or to `data/alibi/llm_logs/<system>_<run_id>.jsonl` for a scored run.

`ALIBI_REPLAY=1` serves recorded Gemini answers from `alibi/fixtures/replays/` and shows the REPLAY
banner on every page; the backend checks, refusals and signatures still run live.

`/rules` parses a plain-English rule through `alibi.app.policy_chain`: Gemini (when the key is
exported and replay is off), then execution's deterministic compiler, then the core parser; the
layer that answered and any layer skipped are shown and audited. Harness:
`PYTHONUTF8=1 uv run --frozen python -m agents.lead.out.policy_harness_chain [--holdout]`.

## Adapter and scorer

**pending** (items 2 and 6): `uv run --frozen python -m alibi.adapter --mode rules|one_pass|agents --cases 1-80 --out data/alibi/predictions`,
then `uv run --frozen python -m alibi.eval.scorer --predictions data/alibi/predictions/<mode>.jsonl --by-family`.

## Probes

**pending** (item 5): `uv run --frozen python -m alibi.eval.probes --run --attempts alibi.app.probe_attempts --target http://127.0.0.1:8100 --results data/alibi/probes.jsonl`.

## Verify a package independently

```bash
uv run --frozen python -m agents.eval.verify_package PACKAGE.json --registry data/alibi/keys/trust_registry.json --recipient bank_a
```
