# Playbook for the humans

## Before the event

- [ ] Everyone accepts the repo invite, clones, and runs `./tools/setup.sh`.
- [ ] Everyone confirms their role in `team.json`.
- [ ] Run a test handoff: one agent posts `done` to another and that agent wakes.
- [ ] Check the event rules on pre-written code. This repo should hold only workflow tooling until the task is announced.

## First 30 minutes

1. Elio gives the task to `lead`, which fills in `docs/TASK.md`.
2. Spend 10 minutes together agreeing the approach. This is the most valuable human decision of the event.
3. If the five roles do not fit the task, rename them now, before agents start. Update `team.json`, then `git mv` the folders, and commit with `TEAM_OVERRIDE=1` and `[team-override]` in the message.
4. `lead` writes the contracts in `shared/`, rewrites each agent's assignment, and posts `done --to all`.
5. Each human starts their agent.

## During the event

- Get a thin version working end to end first, then improve the weakest part.
- Each human watches their own agent and steps in if it stalls.
- Check the board about every 30 minutes for stuck `need` and `blocked` posts.

## Before the deadline

Work back from the real deadline.

- **T-3h:** Stop adding features. Only fix and verify from here on.
- **T-2h:** `eval` reruns every reported number from a clean clone.
- **T-1h:** Finish the demo and write-up, then rehearse them.
- **T-30m:** Submit. Check the repo visibility the organisers require, and open the submission as an outsider would.
