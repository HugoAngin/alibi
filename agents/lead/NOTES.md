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
- Answer board posts to `lead`. A `need` to all teammates (21:11Z, 2026-11-07) asked them to pull, rerun setup.sh and post what they pick up; no detailed per-agent assignments yet.
- Work loop restarted 2026-11-07 ~21:20Z: inbox had nothing new (all three posts to Knull already handled). Waiting on the board.
- `eliohd` keeps repo admin and must make the repo public at submission.

## For Arnav
Requests that need a change inside `alibi/` are queued here.

(none yet)

## Open needs
(none)
