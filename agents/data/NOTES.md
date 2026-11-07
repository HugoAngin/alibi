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

## Next
- React to lead/eval/research feedback on the format (eval was due to post its input format by 23:00).
- Possible: load these cases through `alibi/` connector interfaces once build sessions publish them (SPEC data role); add `parent_request_id` follow-up RFCs.

## Open needs
- None. The 21:3xZ need for the spec was answered at 22:27Z (docs/SPEC.md, c7d0d8e).
