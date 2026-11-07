# Playbook for the humans

## Before the agents start

- [ ] Confirm the deadline and team size with the organisers. Devfolio says hacking ends at **13:00 on Sunday** and teams are **2 to 4**. The event site says **18:00** and **3 to 5**. Until they answer, plan to 13:00, and check now that all five of you can join one Devfolio project. Details are in [`SUBMISSION.md`](SUBMISSION.md).
- [ ] Everyone accepts the repo invite, clones, and runs `./tools/setup.sh`. On Windows, use Git Bash.
- [ ] Everyone confirms their role in `team.json`.
- [ ] Check the event rules on pre-written code and AI tools. This repo should hold only workflow tooling until the task is announced.

## First 30 minutes

1. Elio gives the task to `lead`, which fills in `docs/TASK.md`.
2. Spend 10 minutes together agreeing the approach. This is the most valuable human decision of the event.
3. If the five roles do not fit the task, rename them now, before agents start. Update `team.json`, then `git mv` the folders, and commit with `TEAM_OVERRIDE=1` and `[team-override]` in the message. Only lead's account can use these. Everyone then runs `./tools/setup.sh` again.
4. `lead` writes the contracts in `shared/`, covering the data, the interface and the results format that [`shared/README.md`](../shared/README.md) lists.
5. `lead` adds the packages the approach needs in one go with `uv add`, so agents do not queue a `need` for each one.
6. `lead` posts each agent's assignment with `done --to all`. Each agent copies its assignment into its `NOTES.md`.
7. Each human starts their agent, then `lead` runs the smoke test in the README. Nobody leaves an agent unattended until their role has answered it.

## During the event

- Get a thin version working end to end first, then improve the weakest part.
- One human owns sponsor time. In the first two hours, find the Jane Street, Google DeepMind and NVIDIA people, ask what they want to see, and write their answers into `docs/TASK.md` under **What judges want**, where every agent reads them.
- If the Google DeepMind or NVIDIA prizes are open to this track, decide early whether to use their tools, and give that to one role.
- `lead` is the bottleneck, because every contract change and package goes through it. Its human answers posts to `lead` first. If it falls behind, hand the end-to-end runner to `eval`, which has to run everything anyway.
- Each human watches their own agent and steps in if it stalls. [`RUNBOOK.md`](RUNBOOK.md) covers the usual failures.
- Check the board about every 30 minutes for stuck `need` and `blocked` posts: `python3 tools/board.py read -n 30`.
- To roll out a change to `RULES.md` or `tools/`, `lead` pushes it. Then each human stops their agent, pulls, runs `./tools/setup.sh`, and restarts the agent with "Read your NOTES.md and continue the work loop." An agent that is not restarted keeps the old rules.

## Before the deadline

Work back from the confirmed deadline. If it is 13:00 on Sunday, T-3h is 10:00.

- **T-3h:** Stop adding features. Only fix and verify from here on.
- **T-2h:** `eval` reruns every reported number from a clean clone.
- **T-1h:** Write the README and the Devfolio entry from [`SUBMISSION.md`](SUBMISSION.md), finish the demo, then rehearse it.
- **T-30m:** Submit. Run the pre-submit checks in [`SUBMISSION.md`](SUBMISSION.md), set the repo visibility the organisers require, and open the submission as an outsider would.
