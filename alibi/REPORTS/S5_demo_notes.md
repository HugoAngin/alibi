# S5 demo track: runner notes for the lead

Scratch notes from the demo track (item 3), 2026-11-08 ~08:00-08:35Z, worktree `av-s5`.
Everything below was run from the repo root with `uv run --frozen`; no Gemini key was set and
no network call left the machine. Measured lines are quoted verbatim (tokens never appear in
any of them).

## Files

- `alibi/demo.py` (new): the one-process runner and the driver subcommands.
- `alibi/demo_services.py`: `--passport-rfc-url` plumbing only (passes `rfc_url=` to
  `create_bank_app`, which the bank grew during this session; a bank without it is refused with
  a one-line `SystemExit`, nothing is faked).
- `alibi/tests/test_demo.py` (new): 18 tests, one with real sockets on 8190-8192 (skips when a
  port is busy).

## What the runner does

`uv run --frozen python -m alibi.demo --timeout 600 [--case 1] [--reset] [--as-of <utc>] [--replay]`

1. Normalises `--as-of` (default `2026-10-25T00:00:00Z`); a bad value exits before any file is
   created. `--reset` deletes `data/alibi/demo/case_<n>/` (app database and sidecar) and the bank
   state file `data/alibi/demo/case_<n>/bank_state.json`, and prints `{"reset": true, "removed": [...]}`.
2. Without `--reset`, if the kept bank state file names this case, the stack **resumes** at the
   later of its `as_of` and `--as-of` (`resume_clock`), the bank is not re-seeded (its inboxes
   and dispositions survive) and the URL line carries `resumed_from`.
3. Loads the one tokens file (`--tokens`, default `data/alibi/bank_tokens.json`, created if
   missing) and starts the holder app through `alibi.app.main.create_app()` with this process
   environment: `ALIBI_CASE`, `ALIBI_DATA_DIR=data/alibi/demo/case_<n>`, `ALIBI_KEY_DIR`,
   `ALIBI_MERCHANT_URL=http://127.0.0.1:8101`, `ALIBI_BANK_URL=http://127.0.0.1:8102`,
   `ALIBI_API_ORIGIN=http://127.0.0.1:8100`, `ALIBI_TOKENS_FILE`, and `ALIBI_REPLAY=1` only with
   `--replay`. No token value is put in the environment: the app reads the same file the
   services read, and `ALIBI_*_TOKEN` names still win in `load_tokens` for both sides. With both
   URLs set the runtime reports `seed: connectors` and replays nothing from the case file.
4. Builds the merchant and bank with `demo_services.build_apps(--no-seed --passport-url
   {app}/connect/merchant/webhook --passport-rfc-url {app}/connect/bank/rfc --state <bank_state>)`,
   one connector keystore (`--keys`, default `data/alibi/keys`, shared with the app).
5. Starts three uvicorn servers on threads (app 8100, merchant 8101, bank 8102), waits up to 15 s
   for all to bind; a clash prints `{"error": "server did not start", "servers": [...]}` then
   `{"stopped": true, "failed": [...]}` and exits 1.
6. Seeds over HTTP in this order: merchant `POST /admin/seed?case&as_of` with the admin bearer
   (the pushes land in the app through the webhook route), bank `POST /admin/seed` (or, on a
   resume, `POST /admin/advance` only when `--as-of` is later), then `POST {app}/connect/bank/sync`
   (with `ALIBI_HOLDER_TOKEN` as bearer if the environment has one).
7. Prints the URL line, serves until `--timeout` or Ctrl-C (checked every 0.5 s), stops the
   servers, closes the passport store, prints `{"stopped": true, "failed": []}`.

Driver subcommands (each one JSON line, exit 0/1; `--app-url/--merchant-url/--bank-url/--tokens`
override the defaults; a subcommand never creates the tokens file):

- `python -m alibi.demo status`: app `/health`, merchant `GET /`, bank `GET /`.
- `python -m alibi.demo advance --to <utc>`: merchant and bank `POST /admin/advance` with the
  admin bearer, then the app's feed sync; reports the records the merchant pushed and their
  outcomes, and the sync summary (which includes the monitor tick: alerts raised/kept).
- `python -m alibi.demo rfc bank_a [--request-id rfc-0001-01]` / `rfc bank_b`: `POST /rfc` on the
  bank with that recipient's token; the bank forwards it to the app (`forwarded`). If a bank had
  no forwarder (`forwarded: null`), the subcommand would post the returned `rfc` to
  `{app}/connect/bank/rfc` itself and mark `forwarded_by: runner`; on this tree the bank forwards,
  so that path was not exercised live. Exit 0 only when `forwarded.status == "accepted"`.

## Measured runs

All measured on ports 8190-8192 because **port 8100 is held by another worktree's app** (its
`/health` reports `db: ...\agentverse2\.claude\worktrees\mobile-landing-ui-redesign-371276\data\alibi\app\case_1.db`,
`bank_url: in-process`); it does not show as LISTENING in this shell's `netstat`, so it is not a
process of this session and was left alone. The 8100-8102 run was attempted twice:

```
{"reset": true, "removed": ["data\\alibi\\demo\\case_1"]}
ERROR:    [Errno 10048] error while attempting to bind on address ('127.0.0.1', 8100): ...
{"error": "server did not start", "servers": ["app"]}
{"stopped": true, "failed": ["app"]}
```

That is the port-clash path working (exit 1, no URL line, everything stopped). Rerun on 8100
once that process is gone: `timeout 60 uv run --frozen python -m alibi.demo --timeout 25 --case 1 --reset`.

Same stack on 8190-8192 (`--app-port 8190 --merchant-port 8191 --bank-port 8192`, real
`data/alibi/demo/case_1`, real tokens file), `--timeout 25 --case 1 --reset`, 08:22:54Z. URL line:

```
{"app": "http://127.0.0.1:8190", "merchant": "http://127.0.0.1:8191", "bank": "http://127.0.0.1:8192", "holder": "http://127.0.0.1:8190/passport", "consoles": {"bank_a": {"url": "http://127.0.0.1:8190/bank/bank_a/inbox", "token": "ALIBI_BANK_A_TOKEN or tokens file entry bank_a"}, "bank_b": {"url": "http://127.0.0.1:8190/bank/bank_b/inbox", "token": "ALIBI_BANK_B_TOKEN or tokens file entry bank_b"}}, "tokens_file": "data\\alibi\\bank_tokens.json", "case": 1, "as_of": "2026-10-25T00:00:00Z", "data_dir": "data\\alibi\\demo\\case_1", "bank_state": "data\\alibi\\demo\\case_1\\bank_state.json", "keys": "data\\alibi\\keys", "passport_seed": "connectors", "seeds": {"merchant_pushed": 19, "merchant_outcomes": {"inserted": 19, "duplicate": 0, "corrected": 0, "stale": 0, "refused": 0, "error": 0, "not_delivered": 0}, "bank": {"ok": true, "as_of": "2026-10-25T00:00:00Z", "recipients": ["bank_a", "bank_b"], "subject_ref": "maya-001"}, "feed": {"ok": true, "location": "/passport#transactions", "pages": 1, "inserted": 3, "duplicate": 0, "corrected": 0, "stale": 0, "cursor": "c:3", "tick": {"raised": [], "resolved": [], "kept": [], "at": "2026-11-08T08:22:54.529486Z"}}}, "replay": false, "reset": {"reset": true, "removed": ["data\\alibi\\demo\\case_1"]}, "timeout": 25.0}
```

(`resumed_from: null` is in the line since the resume change.) From a second shell within the 25 s:

```
status  -> {"app": {"status": "ok", ..., "db": "data\\alibi\\demo\\case_1\\case_1.db", "bank_url": "http://127.0.0.1:8192", "merchant_url": "http://127.0.0.1:8191", "seed": "connectors", "api_origin": "http://127.0.0.1:8190", "tokens_file": "data\\alibi\\bank_tokens.json", "holder_auth": "demo"}, "merchant": {"service": "mock-marketplace", "issuer_id": "mock-marketplace", "kid": "merchant_mock", "case_number": 1, "as_of": "2026-10-25T00:00:00Z"}, "bank": {"service": "mock-bank", "case_number": 1, "as_of": "2026-10-25T00:00:00Z", "recipients": ["bank_a", "bank_b"]}}   exit 0
advance --to 2026-10-26T18:05:00Z -> {"advanced_to": "2026-10-26T18:05:00Z", "merchant": {"ok": true, "as_of": "2026-10-26T18:05:00Z", "pushed": 1, "outcomes": {"inserted": 1}, "records": ["rec-0001-020@1"]}, "bank": {"ok": true, "as_of": "2026-10-26T18:05:00Z"}, "feed": {"ok": true, ..., "inserted": 0, ..., "cursor": "c:3", ...}}   exit 0
rfc bank_a (at that clock) -> {"recipient": "bank_a", "request_id": null, "ok": false, "status": 404, "code": "unknown_request", "reason": "no unissued request for this recipient"}   exit 1 (correct: not issued until 2026-11-02T09:00Z)
runner exit 0, last line {"stopped": true, "failed": []}; netstat :819 LISTENING empty afterwards.
```

Full sequence (`--timeout 70 --reset`, 08:23:58Z), every call exit 0:

```
advance --to 2026-10-26T18:05:00Z -> merchant pushed ["rec-0001-020@1"] inserted; feed inserted 0
advance --to 2026-10-28T07:30:00Z -> merchant pushed 0; feed inserted 1 (cursor c:4); tick raised ["alrt_917fd4e28ea3bbf7a6f7e72a"]
advance --to 2026-11-02T09:00:00Z -> merchant 0; feed 0; tick kept the alert
rfc bank_a -> {"recipient": "bank_a", "request_id": "rfc-0001-01", "reissued": false, "issued_at": "2026-11-02T09:00:00Z", "expires_at": "2026-11-09T09:00:00Z", "scope": {"txn_ids": ["txn-0001-004"]}, "forwarded": {"status": "accepted", "request_id": "rfc-0001-01", "audience": "bank_a"}, "holder_url": "http://127.0.0.1:8190/requests/rfc-0001-01"}
advance --to 2026-11-04T14:00:00Z -> merchant 0; feed 0
rfc bank_b -> {"recipient": "bank_b", "request_id": "rfc-0001-02", ..., "scope": {"period": {"from": "2026-10-01T00:00:00Z", "to": "2026-11-01T00:00:00Z"}}, "forwarded": {"status": "accepted", "request_id": "rfc-0001-02", "audience": "bank_b"}, ...}
GET /requests/rfc-0001-01 (Accept: application/json) -> 200; GET /passport JSON -> requests ['rfc-0001-01', 'rfc-0001-02'], 4 transactions, alerts ['unexplained_credit']
```

Persistence (08:31Z): `--reset` start, `advance --to 2026-10-28T07:30:00Z`, stop; restart **without**
`--reset`: `"as_of": "2026-10-28T07:30:00Z", "resumed_from": "2026-10-28T07:30:00Z"`, merchant
`"pushed": 20, "outcomes": {"duplicate": 20}`, bank `{"ok": true, "resumed": true}`, feed
`{"ok": true, "inserted": 0, "cursor": "c:4"}`, tick kept the alert. Before the resume logic a
restart re-seeded the bank at `--as-of` while the passport kept cursor `c:4`, and the sync
reported `502 feed_unavailable ... HTTP 400` (cursor beyond the re-seeded feed); the line now
carries a `hint` for that state, which only arises when the bank state file is deleted but the
app directory kept.

Tests: `uv run --frozen python -m pytest -q -p no:cacheprovider alibi/tests/test_demo.py alibi/tests/test_demo_services.py`
-> `26 passed in 23.16s`.

## The demo script, mark by mark (answers to the five [verify] items)

Start (T-10): `uv run --frozen python -m alibi.demo --timeout 600 --case 1 --reset`
(add `--replay` for the fallback). Wait for the URL line. Open `http://127.0.0.1:8100/passport`
(390 px), `http://127.0.0.1:8100/bank/bank_a/inbox` and `.../bank/bank_b/inbox` with the
`bank_a` / `bank_b` entries of `data/alibi/bank_tokens.json` as the credential (the app and the
bank service read the same file). `python -m alibi.demo status` must show
`"seed": "connectors"` and both services at `"as_of": "2026-10-25T00:00:00Z"`.

| Mark | Driver runs | Live effect |
| --- | --- | --- |
| 0:00 | nothing (state after start) | 19 merchant records (8 sales, 2 refunds, 8 fees, the eyewear purchase) via the webhook route; 3 debits via the feed. **Not** yet: `rec-0001-020`, the £2,160 credit, any RFC. So the "alert before any request" is not on screen at 0:00 with the pinned clock (demo_script item 5). To have it at 0:00 instead, start with `--as-of 2026-10-28T07:30:00Z` (credit and alert present, release already in, still no RFC). |
| 0:25 | `python -m alibi.demo advance --to 2026-10-26T18:05:00Z` | merchant pushes `rec-0001-020` r1 (balance release) live through the webhook; feed unchanged. Then `advance --to 2026-10-28T07:30:00Z` brings `txn-0001-004` (the credit) through the feed and the monitor raises `unexplained_credit`. Open `/txn/txn-0001-004`. |
| 0:45 | `python -m alibi.demo advance --to 2026-11-02T09:00:00Z` then `python -m alibi.demo rfc bank_a` | the bank issues `rfc-0001-01` (txn-scoped) and forwards it; `forwarded.status` must read `accepted`. Open `/requests/rfc-0001-01`, click Run investigation. |
| 1:50 | UI | the Send button is the form `#send` in the approval card on `/requests/rfc-0001-01` ("Send to Bank A", posts `/requests/{id}/dispatch`), shown after approval. Dispatch posts the signed package to the bank service's `/deliver` with the recipient's bearer. |
| 2:25 | UI | Bank A package URL: `/bank/bank_a/packages/<package_id>` (the dispatch result carries `package_url`, absolute on `ALIBI_API_ORIGIN`). |
| 2:45 | `python -m alibi.demo advance --to 2026-11-04T14:00:00Z` then `python -m alibi.demo rfc bank_b` | `rfc-0001-02` (period scope) issued and forwarded `accepted`. Open `/requests/rfc-0001-02`. |

History before `rec-0001-020` is seeded by the merchant's `/admin/seed` at the start clock pushing
every delivery with `received_at <= 2026-10-25T00:00:00Z` through the webhook route, and the
bank's seed plus the app's `/connect/bank/sync` for the three debits. Nothing is preloaded from
the case file (`seed: connectors`), so every record on screen arrived through a connector.

Gemini probe: `uv run --frozen python -m alibi.llm.probe` (module present at `alibi/llm/probe.py`;
not run by this track, no key). Under `--replay` (`ALIBI_REPLAY=1`): the banner is on every page
and the Gemini answers are the recorded ones; the webhook ingest, feed sync, RFC intake,
signature checks, approval digest, dispatch, the bank's `/deliver` verification and refusals all
run live, because the runner changes nothing but that one variable.

## Open points for the lead

- The 8100-8102 live run is blocked by the other worktree's app on 8100 (see above). The 8190-8192
  runs exercise the identical code path. Rerun the exact commands once 8100 is free.
- With the pinned default clock the 0:00 screen has no alert yet; either accept the two-step 0:25
  (`advance` to 10-26 then 10-28) or start at `--as-of 2026-10-28T07:30:00Z`. Data's script needs
  one of the two written in.
- The app clock is the wall clock (no `ALIBI_NOW`); QA item 19 (mixed clocks) stands. Setting
  `ALIBI_NOW` would freeze the app clock and break the approval window across advances, so the
  runner does not set it.
- `rfc` fallback (`forwarded_by: runner`) exists for a bank without `rfc_url` and is untested live
  because this bank forwards itself.
- Not covered by this track: the bank consoles under `ALIBI_BANK_URL`, dispatch to `/deliver`,
  the adapter (`alibi.adapter.run_case`), and the full UI pass on the stack (data's QA pass 2).
