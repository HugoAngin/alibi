# Notes

## Assignment
Turn the task into a plan, keep the agents moving, and own the submission. See `BRIEF.md`. `docs/TASK.md` is not filled in yet, and no agent has an assignment.

## Handover (2026-11-07)
Elio (`eliohd`) is handing `lead` to Arnav (`Knull`) and taking `research`. The `team.json` commit has to come from `Knull`, because the commit hook blocks `eliohd` from making it. Tested in a scratch clone only, not yet done on `main`.

## Next for the new lead
- Update the team table in `docs/SUBMISSION.md`, and "Elio gives the task" at `docs/PLAYBOOK.md:23`.
- Tell everyone to pull, so that `--to lead` reaches `Knull`. Only `eliohd` and `Knull` need to rerun `./tools/setup.sh`.
- Decide whether to fix this gap: the ownership check uses the roster from the commit it is checking, so any teammate can make themselves lead and pass both the hook and CI.
- `eliohd` keeps repo admin, because the repo is under that personal account and `Knull` has push access only. Elio has to make the repo public at submission.

## Open needs
- Handover commit from `Knull`, posted 2026-11-07 20:33 UTC. Until it lands, `eliohd` stays lead.
