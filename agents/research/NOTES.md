# Notes

## Assignment
From `lead` (Arnav, `Knull`), board post 6713363970 at 2026-11-07 21:40Z: deck skeleton mapped to the five rubric rows in `agents/research/out`. Nobody edits `alibi/`. The spec snapshot is `docs/SPEC.md` (alibi/SPEC.md is not on main).

## Done
- 2026-11-07: `out/DECK.md`, 17 slides plus appendix. Each slide has a working title, claim, evidence and supplier. It includes a rubric coverage table, the TASK-required slides (user, data, benchmark, results, limits, controls), the demo timing map, claims the deck must not make, and a placeholder registry (PH-01 to PH-45) mapping each number to a results key, system, split and owner.
- Demo and data slides use cases 1, 2, 25 and 39 from `agents/data/out/cases/`. I checked the amounts, refs and timings against the files.

## Decisions
- Numbers stay `{{PH-nn}}` until `eval` verifies them. Development numbers are labelled as such and never shown as held-out.
- Slides 13 and 15 have no drafted headline. It is written only after the numbers land, including if Alibi does not beat a baseline.

## Gaps raised in DECK.md (lead decides)
- Pre-policy unsafe proposals and lookup counts are required by SPEC Evaluation but are not in the CONTRACTS results keys.
- eval needs to tag lookup-dependent sealed cases for slide 15.
- Gemini model ID is not pinned in `docs/TASK.md`.

## Next
- Work loop: `board.py wait` in the background, act on posts addressed to research.

## Open needs
(none)
