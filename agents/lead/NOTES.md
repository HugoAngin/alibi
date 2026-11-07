# Notes

## Assignment
Turn the task into a plan, keep the agents moving, and own the submission. See `BRIEF.md`. The task is Alibi; the spec is `alibi/SPEC.md`. Arnav's instruction to this agent on 2026-11-07: answer `need` and `blocked` posts about contracts, dependencies and scope. Edit only `docs/`, `shared/`, `pyproject.toml`, `uv.lock` and this file. Never edit `alibi/` (build sessions own it) and never run `setup.sh`. Post `fyi` after changing a shared file.

## Machine notes
- This checkout is `~/agentverse2` on branch `main`. Push only with `git push origin main`. Never push the `s1`, `s3a` or `s4a` branches; those are build worktrees owned by build sessions.
- `GITHUB_TOKEN` in the environment is a fine-grained PAT that cannot see the private repo. Run the board as `env -u GITHUB_TOKEN python3 tools/board.py ...` so `gh` uses the keyring login.

## Done (2026-11-07)
- Handover landed on `main` in 7d771eb; `Knull` is lead, `eliohd` is research. Elio's step-7 post (`done --to eliohd "Lead handover pushed."`) was addressed to Arnav and has not been posted.
- Filled `docs/TASK.md` from the spec. Fixed the team table in `docs/SUBMISSION.md` and the playbook line 23.
- Wrote `shared/CONTRACTS.md` (pinned decisions and results keys), `shared/splits.json` (lockbox is case number 1000) and updated `shared/README.md`.

## Decisions
- Deadline: hacking ends 13:00, team submits by 12:30 London per the spec.
- Lockbox is a case number, so `tools/lockbox.py` works unchanged.
- Open gap, not fixed: the ownership check reads the roster from the commit it checks, so a teammate could make themselves lead. Accepted for the hackathon; CI still reports it.

## Next
- Answer board posts to `lead`. A `need` to all teammates (21:11Z, 2026-11-07) asked them to pull, rerun setup.sh and post what they pick up.
- Assignments broadcast from the Knull account at 21:40Z 2026-11-07 (post 6713363970, another Knull session, not this agent), commit 0320ca7:
  - eval: author sealed cases and gold from SPEC's Evaluation section; keep them out of the repo until freeze; post the case input format wanted by 23:00.
  - data: draft development case ledgers (clean, bundled payment, balance-release vs settlement, pending correction, duplicate record, missing proof) as JSON in `agents/data/out`.
  - execution: security probe list and analyst disposition flow as a checklist in `agents/execution/out`.
  - research (Elio): deck skeleton mapped to the five rubric rows in `agents/research/out`.
  - Nobody edits `alibi/`. Needs go to lead.
- Work loop restarted 2026-11-07 ~21:20Z; three 15-minute waits to ~22:10Z saw no replies from teammates yet.
- 22:25Z: data (HugoAngin) picked up fixtures and loaders and asked for the spec. Answered with `docs/SPEC.md` snapshot.
- 22:3xZ: data pushed 25 dev case ledgers (7396400); execution pushed probes, analyst flow and scoring api (4a3d4c4). Pinned the disposition record in `shared/CONTRACTS.md` at execution's request. 
- 22:38Z: data posted done (c9472fe): 40 dev cases in `agents/data/out/cases/`, loaders `agents.data.api` (`load_cases`, `known_at`, `lookup`, `without_gold`), 165 tests pass. No action for lead. Still no post from research (eliohd).
- 22:39Z: eval picked up sealed cases and gold, proposed JSONL per case with gold in a separate evaluator-only file. Pinned Case schema, Questions and matching, and App adapter rows in `shared/CONTRACTS.md`.
- 22:43Z: research posted the deck skeleton (bd33ff6) with three gaps. Answered: added results keys `unsafe_proposals`, `lookups`, `observed_inputs`; made `requires_retrieval` explicit for sealed gold; pinned Gemini IDs `gemini-3.8-flash` and `gemini-4-argon` from s3a's `alibi/llm/config.py` in TASK.md and CONTRACTS.md. If a build session changes the models, lead must update both files. All four agents have now reported.
- 23:36Z: execution reviewed the s4a analyst screens (6c1d877). Pinned `form` on trust/evidence items in CONTRACTS; queued the five L items for Arnav.
- 23:37Z: data asked which fixture is canonical. Pinned data's case 1 (IDs now in the Canonical fixture row); queued regenerating the s4a UI sample from it. Waiting on the board.
- `eliohd` keeps repo admin and must make the repo public at submission.

## For Arnav
Requests that need a change inside `alibi/` are queued here.

- 2026-11-07 22:25Z, data (HugoAngin) post 6713839852: `alibi/SPEC.md` is not on `main` or any remote branch; it exists only on the local `s3a` and `s4a` worktrees. The 21:40Z broadcast told teammates it was on `s1`, but `s1` is at ffbafb4 and has no `alibi/`. Lead published a verbatim snapshot as `docs/SPEC.md` (lead-owned) so data is unblocked. Please land `alibi/SPEC.md` on `main` from a build session; the snapshot then becomes redundant and lead will delete it.
- 2026-11-07 22:37Z, execution (sirExile44) post 6713951719: asks which build session owns the console UI vs the verifier code in `alibi/`. Their acceptance checklist is `agents/execution/out/ANALYST_FLOW.md`. From branch names, `s4a` has the static UI templates and `UI.md`; the verifier owner is not visible from here.
- 2026-11-07 22:39Z, eval (t7acct) post 6713972641: needs the runnable app adapter (case without gold in, one package per RFC out) when it lands in `alibi/`. Pinned the case schema and eval's sealed format in `shared/CONTRACTS.md` meanwhile; the adapter's import path needs linking in CONTRACTS once a build session publishes it.
- 2026-11-07 23:36Z, execution (sirExile44) post 6714534193, `agents/execution/out/FRONTEND_NOTES.md` items marked L for the s4a analyst screens (routes, variables, sample data in `alibi/`):
  - Item 3: `ctx_analyst_package` sample `att_03` (the redacted extract) is badged `issuer_verified`. Lead pinned the contract: trust stays at three values and items carry `form: full_record | redacted_extract`. Add `form` to `alibi/UI.md` and fix the sample so the extract is not `issuer_verified`.
  - Item 4: the disposition route must take `analyst` from the authenticated session and ignore the posted `analyst_name`.
  - Item 5: the stored disposition field is `reason` per CONTRACTS; map the form's `note` to `reason` or rename both in UI.md and the route.
  - Item 7: add `timing` to each `evidence_by_question` item: `None | {payout_ref, released_to_balance_at, bank_settled_at, credit_booked_at, settled_before_booking}`.
  - Item 8: connect `request_more` to the follow-up: one combined POST, or the follow-up route records a `request_more` disposition, and collect `question_ids` on whichever form posts it.
- 2026-11-07 23:37Z, data (HugoAngin) post 6714542612: the s4a UI preview sample (`ctx_analyst_package`: txn_0001, ord_1001, PO-7781, stl_9001 as a separate settlement kind, ResaleHub, booking 2026-11-03) disagrees with the canonical fixture. Lead pinned data's case 1 as canonical (IDs in CONTRACTS). Regenerate the UI sample and the mock merchant/bank fixtures from `agents.data.api.canonical_case()`; drop the separate settlement kind, since the contract has `payout` with `status`/`settled_at` and `balance_release`.

## Open needs
(none)
