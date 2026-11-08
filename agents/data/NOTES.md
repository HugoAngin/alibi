# Notes

Role: `data` (HugoAngin). Writes only in `agents/data/`.

## Assignment (lead Knull, 21:40Z 2026-11-07, post 6713363970)
Draft development case ledgers as JSON in `agents/data/out`: clean, bundled payment, balance-release vs settlement, pending correction, duplicate record, missing proof. Nobody edits `alibi/`. The spec is `docs/SPEC.md` (snapshot; `alibi/SPEC.md` wins when it lands on main).

## Done
- v1 (7396400): 25 cases from CONTRACTS.md, before the spec was readable.
- v2: 40 cases aligned to docs/SPEC.md. Cases 1–2 are the demo pair (Maya 216000; balance release pushed, payout only by lookup; 1 supported, 2 pending at booking, so contradicted). Families: demo, clean, bundled_payment, balance_vs_settlement, pending_correction, duplicate_record, missing_proof, self_supplied_import. Every case has distractors.
- `api.py`: `load_index`, `load_case`, `load_cases(family)`, `canonical_case`, `without_gold`, `known_at(case, at, include_lookup=False)` and `lookup(case, at, payout_reference|record_id)` (simulated merchant retrieval). Loads go through `tools.lockbox.visible`.
- `test_ledgers.py`: 165 pass. It re-derives all gold independently at decision time.
- `README.md`: layout, families, gold rules, open assumptions.

## Frontend (lead 23:33Z, post 6714508805, commit df8cb9d)
- data now owns `alibi/app/static/` (the stylesheet). Phase 1: review s4a screens and write `agents/data/out/FRONTEND_NOTES.md` (done). Phase 2 begins once the S4 merge is announced (~00:45Z): edit `alibi/app/static/alibi.css` on main and run `pytest alibi` before every commit.
- s4a is checked out in a scratch worktree (scratchpad/s4a), never in this shared checkout. Previews are served at http://127.0.0.1:8765.
- CSS draft (alibi_css_phase2.patch) was applied to alibi/app/static/alibi.css after the S4 merge; the patch file was deleted at 08:5xZ per lead post 6719455424.
- Research's notes treat my case 1 as the canonical fixture (txn-0001-004, PO-0001-01).

## hidden_reference family (lead 23:40Z, post 6714579064)
- Added cases 41-50 (10 cases; cases 1-40 byte-identical). Five patterns: decoy in release / reference only in order confirmation; no reference (orders + weekday, date only); withdrawal-ID alias mapped by a merchant statement or Maya's CSV import; unfamiliar layouts (email PO/nnnn/nn, semicolon CSV without the PO prefix); two references told apart by amount or timing.
- api.lookup gained covers_record_id and payout_date keys. The gold candidate rule also accepts withdrawal_id as the credit's reference. Gold gains retrieval_path and clue_refs for this family.
- naive_regex_probe in the tests: 40/40 on the originals, 6/10 on hidden_reference.

## Lead work order (00:46Z, post 6715221388)
- (1) demo_script.md done (8b99d67). (2) case2_walkthrough.md done (a8ee960). (3) data_card.md done (README-ready). (4) Stylesheet patch: waiting for the S4 merge.
- Fixed case 2/20: a pending payout is available only from its status_as_of time (c339e2a).

## Lead work order 2 (01:03Z, post 6715391986)
- (1) Injection family cases 51-60 done (a8105fa). (2) Alert family cases 61-70, gold.alerts, out/merchant_registry.json and api.merchant_lookup done. (3) Stylesheet patch: after the S4 merge. (4) Clean-clone RUNBOOK dry run as Driver: after S5's RUNBOOK lands.

## Lead work order 3 (01:29Z, post 6715633720)
- (1) Second platform, cases 71-80: done. issuer second-platform, schema sp-2.1, weekly statements, SPLAT Wnn refs; api.to_contract is the reference mapping; cases 79-80 have Maya on both platforms.
- (2) Receipts: 12 PNGs plus receipts.json done (10 pushed in da8289f, 11-12 with the second platform).
- (3) The injection and alert families were already done. (4) Stylesheet patch applied to alibi/app/static/alibi.css after the S4 merge (576 alibi tests pass).

## Lead work order 4 (01:57Z, post 6715894632)
- (1) The alert family was already done (11abc05). (2) Deck figures: agents/data/figures.py writes out/figures/fig1-4. (3) Throughput set: 200 cases 101-300 in out/throughput, api.load_throughput. (4) Driver run of demo_script.md twice from a clean clone: after S5 lands.

## Eval rehearsal (02:24Z, post 6716141918)
- Done: fresh clone at scratchpad/rehearsal (commit 9d0933f), record in agents/data/out/rehearsal_2026-11-08.md. Finding sent to eval: the rules baseline lacks a second-platform mapping (7 linked SP cases come out unmatched). Reuse this clone for the Driver run after S5.

## Web front end (research 02:46Z, post 6716339131)
- The product UI is the Astro workspace in web/ (apps alibi 4321, merchant 4322, bank 4323; backend 8100). Jinja templates plus alibi.css are the fallback.
- demo_script.md URLs refer to the Jinja routes; update them to the web/ apps once S5 publishes the route table.
- Fixture drifts reported to research at 02:50Z; fixed in df573c5 (bank fillers now use txn-q-*, merchant shows case 1's four orders, tested against receipts.json).

## web/apps/merchant (research proposal 03:43Z, post 6716849163)
- Proposed: data owns web/apps/merchant/ once lead updates team.json, then rerun setup.sh. Task: make it look like a real marketplace and wire connect (forwardAction from @alibi/ui/api) with Maya's case 1 records. Built-in data must match the backend shape, with a test against agents/data/out. Rules: web/docs/contributing.md. No new deps (ask research); run pnpm verify before each commit; research deploys after a done post.
- Blocker: the laptop has Node 20.12 and no pnpm; the workspace needs Node >=22.12 and pnpm 10.33. Asked the human (via the parent session, ~03:50Z) whether to brew install node@22 pnpm. Nothing installed yet.
- Pushed agents/data/out/merchant_view/case_0001.json (073b6dc): the marketplace's own view of case 1 (8 orders with refs and titles, 2 refunds, fees, payout PO-0001-01, release notification, purchase), tested.
- Unverified Astro draft is in the scratchpad (merchant_draft/): src/data/account.ts, tests/account.test.ts, components/SellerNav.astro, pages seller/{index,orders,payouts,messages}, plus a nav link in the layout. Copy into web/apps/merchant once granted, run pnpm format and verify, then wire connect after the S5 route table.
- Plan: seller dashboard (sales, refunds, fees, the balance-release message, payout PO-0001-01 settled 27 Oct), a messages page, a payouts page, connect wired to the backend with a fallback notice, and a fixtures test against case_0001.json.

## Landing hero (research 04:22Z, post 6717182136; spec agents/research/out/HERO_STREAM.md)
- Not yet assigned (lead must grant web/apps/alibi/src/components/site/). Flagged on the board: generic synthetic merchant names instead of Amazon/Deliveroo/Uniqlo unless Elio confirms.
- SVG prototype in scratchpad/hero/index.html. Elements: sources (Mock Marketplace from case 1, 4 generic shops, 2 bank accounts, mule-pattern check) with packets flowing along paths into the engine with its 4 agents; scroll lights the agents and sends the signed package to Bank A (rfc-0001-01). Reduced motion gives a still frame; rendering only runs while visible. Checked at 1440 and 375 (no horizontal scroll; labels small on phone). Port the figure, style and script into HeroScene.astro when granted.

## 04:32Z reroute (research, post 6717263436)
- Lead is out and team.json is frozen. Eval owns the hero; data handed over agents/data/out/hero_prototype/ (eea410a). Generic merchant labels until Elio confirms the real ones.
- Merchant drop-ins staged in agents/data/web_merchant/ for research to copy in and verify (eea410a). Waiting for research's pnpm verify results.

## Driver QA (lead 06:06Z, post 6718047800)
- Lead said "once S2-S4 are on main (I will post when)". Pass 1 ran anyway at 06:07Z on 2842b58, where the templates are still S4a. Findings are in out/QA_0430.md: 4 P0, 8 P1, 14 P2.
- P0 items: the `period.from` 500 on rfc-0001-01's detail and export pages; tokens in bank_tokens.json are refused on 8100 (the app uses env vars or the defaults in config.py); there is no send button after approval.
- Pass 2: rerun the same walk when lead posts that S2-S4 are on main, and update QA_0430.md. Data's own items: the start command and the imports connection in demo_script, and the phone overflow in alibi.css after S4.
- Lead 06:06Z (to eval): "data: merchant worker under agents/data". Hosting stays inside our own folder. Not yet assigned to data directly.

## Lead batch (06:49Z, post 6718410490)
- (1) GEMINI_SHOWCASE.md is done, with an evidence link for each row. vision_check.py scores 3 receipts (rcpt-06, 09, 10) against receipts.json. Eval ran it live (record 73e742c7d7): 20/21 fields; rcpt-09 got the issuer wrong. The showcase cites that record. Hugo later added GEMINI_API_KEY to .env and approved 3 calls; they were NOT spent, because eval's record already covers it. Ask Hugo before making any call. The "measured on sealed cases" section waits on eval's results.jsonl.
- (2) QA pass 2 once S5 lands: the Astro web/ apps and Jinja, both inboxes. (3) Finalise demo_script from RUNBOOK, do two timed dry runs, then record a replay video under 4 min outside the repo and link it from the README. The video needs Hugo to screen-record (macOS, not Xbox Game Bar). (4) Regenerate the figures from the real numbers. (5) Finalise data_card.

## web/ opened (lead 08:26Z, post 6719250999, bf2076d)
- Anyone may push to web/ after running pnpm verify. Data can't yet: the laptop has Node 20.12 and no pnpm, and installing them needs Hugo's approval. The holder app folders web/apps/alibi/src/{components,pages}/app/ stay with lead until 09:30.
- 08:3xZ incident: `git pull --rebase` autostashed another session's uncommitted web/ edits. settings.astro conflicted against lead's redirect; the original is in stash@{0} c07f1f6. The restore command was blocked, so I reported it to the parent session and asked who restores it. **Always use `git pull --rebase --no-autostash`** while others share this checkout.

## Web unified (lead 08:49Z, post 6719455424, bbb14db)
- research (Elio) owns web/apps/alibi/; execution (Mouaad) owns the rest of web/; lead keeps alibi/. Data edits nothing in web/. Demo front end = phone holder app + Mouaad's bank console on the S5 backend; Jinja is the fallback. So QA pass 2 walks those two first.
- Deleted out/alibi_css_phase2.patch as asked. agents/data/web_merchant/ and hero_prototype/ are reference drops only now.
- The uncommitted web/ edits in this shared checkout are not data's; left for their owner. While they exist the checkout can't rebase-pull, so data pushes from a scratch worktree on origin/main.

## S5 not on main (lead 09:49Z, post 6720008019)
- Lead's 09:21Z "S5 on main" post was premature. QA pass 2 is on hold until lead posts a pushed SHA and tag. Meanwhile: data_card.md finalised (394 tests, tables fixed, commit SHAs, vision and merchant-view lines). QA worktree: scratchpad/qa2.

## QA pass 2 (lead 10:16Z, post 6720250667; S5 on main 0b0adb6, tag sealed-candidate c6ea270)
- Done 10:17-10:28Z, in out/QA_0430.md "Pass 2". New P0-A: Bank A dispatch is refused "issued in the future" because the bank's clock lags the app's wall clock. The rejected package ID can't be resent, so the Driver must run `alibi.demo advance --to <now+10m>` before 1:50. New P0-B: `--replay` stays rules-only without ALIBI_MODE=agents; with it, the replay serves case1_agents.json.
- Fixed alibi.css: `.page-title` overflow-wrap (export pages were 419px at 390px). pytest alibi: 5 failed, none CSS (3 await Elio's s5_templates_on_main.patch).
- No s5_static.patch exists. Next: 11:15Z fresh-clone RUNBOOK run in replay mode, no key; then the final demo_script.

- Lead 10:52Z (post 6720581992): demo_script must show the follow-up only on the native bank console route. On the Jinja fallback, skip the follow-up beat (the passport proxy POST /bank/bank_a/requests is a known limit).

- 11:15Z fresh-clone RUNBOOK run done: out/fresh_clone_1115.md. The RUNBOOK's two-process start never connects to the services (seed case_adapter, in-process bank); `alibi.demo` is still marked pending. Replay needs ALIBI_MODE=agents. Both banks delivered; verify_package exit 0. Next: final demo_script.md on `ALIBI_MODE=agents alibi.demo --replay` plus the clock advance before Send.

## Next
- React to lead/eval/research feedback on the format (eval was due to post its input format by 23:00).
- Possible: load these cases through `alibi/` connector interfaces once build sessions publish them (SPEC data role); add `parent_request_id` follow-up RFCs.

## Open needs
- ~00:00Z to research: template hooks (trace step classes, review-group sections, td data-label, passport home order).
- None. The 21:3xZ need for the spec was answered at 22:27Z (docs/SPEC.md, c7d0d8e).
