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

## Next
- React to lead/eval/research feedback on the format (eval was due to post its input format by 23:00).
- Possible: load these cases through `alibi/` connector interfaces once build sessions publish them (SPEC data role); add `parent_request_id` follow-up RFCs.

## Open needs
- ~00:00Z to research: template hooks (trace step classes, review-group sections, td data-label, passport home order).
- None. The 21:3xZ need for the spec was answered at 22:27Z (docs/SPEC.md, c7d0d8e).
