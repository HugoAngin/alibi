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
- CSS draft finished: agents/data/out/alibi_css_phase2.patch (made against s4a 6466584; 22 alibi tests pass). After the S4 merge announcement: pull, run setup.sh, apply the patch to alibi/app/static/alibi.css (by hand if S4 changed the CSS), run pytest alibi, commit alibi/app/static/, post done to lead.
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

## Next
- React to lead/eval/research feedback on the format (eval was due to post its input format by 23:00).
- Possible: load these cases through `alibi/` connector interfaces once build sessions publish them (SPEC data role); add `parent_request_id` follow-up RFCs.

## Open needs
- ~00:00Z to research: template hooks (trace step classes, review-group sections, td data-label, passport home order).
- None. The 21:3xZ need for the spec was answered at 22:27Z (docs/SPEC.md, c7d0d8e).
