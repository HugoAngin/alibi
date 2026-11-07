# Notes

Role: `data` (HugoAngin). Writes only in `agents/data/`.

## Assignment (lead Knull, 21:40Z 2026-11-07, post 6713363970)
Draft development case ledgers as JSON in `agents/data/out`: clean, bundled payment, balance-release vs settlement, pending correction, duplicate record, missing proof. Nobody edits `alibi/`.

## Done
- `ledgers.py` generates 25 dev cases (1–25, below lockbox 1000) to `out/cases/` and `out/index.json`. Case 1 is the canonical Maya fixture (240000/18000/6000/216000) with a bank_a RFC and a bank_b reuse RFC.
- `api.py`: `load_index`, `load_case`, `load_cases(family)`, `canonical_case`, `without_gold`, `known_at` (point-in-time). Loads go through `tools.lockbox.visible`.
- `test_ledgers.py` (79 pass) re-derives every gold label independently from deliveries, and checks contract shapes, point-in-time evidence and that out/ matches the generator.
- `README.md` documents the layout, families and the assumptions made without the spec.

## Decisions
- `alibi/SPEC.md` exists only on Arnav's local `s1` branch (not pushed), so I worked from `shared/CONTRACTS.md`. The assumptions (matching rule, claim status rule, fees, placeholder question IDs, unsigned records) are listed in README.md.

## Next
- Adjust to SPEC or eval's input format when lead/eval reply.
- Possible additions: a `self_supplied` import family, and follow-up RFCs with `parent_request_id`.

## Open needs
- 21:3xZ 2026-11-07, to lead: push alibi/SPEC.md or point me at it. Proceeding on CONTRACTS.md meanwhile.
