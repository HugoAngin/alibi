# Notes

## Assignment
From `lead` (Arnav, `Knull`), board post 6713363970 at 2026-11-07 21:40Z: deck skeleton mapped to the five rubric rows in `agents/research/out`. Nobody edits `alibi/`. The spec snapshot is `docs/SPEC.md` (alibi/SPEC.md is not on main).

## Done
- 2026-11-07: `out/DECK.md`, 17 slides plus appendix. Each slide has a working title, claim, evidence and supplier. It includes a rubric coverage table, the TASK-required slides (user, data, benchmark, results, limits, controls), the demo timing map, claims the deck must not make, and a placeholder registry (PH-01 to PH-45) mapping each number to a results key, system, split and owner.
- Demo and data slides use cases 1, 2, 25 and 39 from `agents/data/out/cases/`. I checked the amounts, refs and timings against the files.

## Decisions
- Numbers stay `{{PH-nn}}` until `eval` verifies them. Development numbers are labelled as such and never shown as held-out.
- Slides 13 and 15 have no drafted headline. It is written only after the numbers land, including if Alibi does not beat a baseline.

## Gaps raised in DECK.md
- All three closed by lead in c44079b (22:44Z): results keys `unsafe_proposals`, `lookups`, `observed_inputs`; sealed gold keeps `requires_retrieval`; models `gemini-3.8-flash` and `gemini-4-argon` pinned. DECK.md registry updated to match.

## Frontend (from lead, post 6714508805, 23:33Z)
- Research now owns `alibi/app/templates/` (team.json, setup.sh rerun). Data owns `alibi/app/static/`.
- Phase 1 done: change list in `out/FRONTEND_NOTES.md` (25 items, tagged T template / L lead / D data). Reviewed s4a 6466584 from a git-archive copy in scratch, not by switching this checkout's branch. Baseline `pytest alibi`: 22 passed.
- Phase 2: edit templates only after lead announces the S4 merge (~00:45). Then pull, rerun setup.sh, run `uv run --frozen python -m alibi.app --timeout 600` and `pytest alibi -q` before each commit.
- 23:36Z: execution (6714534193) and data (6714542491, a `need` to research) posted their notes. Both are merged into the work order at the top of `out/FRONTEND_NOTES.md`. data's requested hooks (step classes, review-group sections, data-label, home order) are items A1–A3 and 13.
- The L and D items are not posted yet. Elio approves each board post that tags someone.

## Next
- Work loop: `board.py wait` in the background, act on posts addressed to research.

## Open needs
(none)
