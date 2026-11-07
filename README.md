# winthrutheory

Team repo for the Agentverse hackathon, Jane Street finance track. Five people each run one agent, and each agent owns one folder. Agents hand work to each other through a pinned GitHub issue, so nobody has to send a text.

At T-1h this page is replaced by the project write-up. The template is in [`docs/SUBMISSION.md`](docs/SUBMISSION.md).

## Setup

You need `git`, Python 3.11+, the [GitHub CLI](https://cli.github.com) signed in with `gh auth login`, and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/eliohd/winthrutheory-eceb0cdd-submission-repo.git
```

```bash
cd winthrutheory-eceb0cdd-submission-repo && ./tools/setup.sh
```

Run `./tools/setup.sh` again whenever you pull a change to `tools/` or `team.json`, then restart your agent. It:

- links this clone to your role and writes `CLAUDE.local.md`, which tells your agent who it is and which Python command works on your machine
- installs the commit hook, and sets `git pull` to stash and restore local edits
- writes Claude Code rules to `.claude/settings.local.json`: the board and git commands run without prompts, force-push, `--no-verify` and reading `.env` are blocked, and edits outside your folder are refused. Rules you added yourself are kept.
- installs the locked Python packages into `.venv/`

Start your agent from the repo root. In Claude Code, that is:

```bash
claude "Read the rules, find your role, and start the work loop."
```

Running code your agent wrote still asks first, unless you choose a permission mode that does not. Pick one you are comfortable leaving unattended.

### Windows

- Install [Git for Windows](https://git-scm.com/download/win) and run `setup.sh` from **Git Bash**, not PowerShell.
- `python3` is often missing or opens the Microsoft Store. Setup and the commit hook fall back to `python` or `py -3`, and `CLAUDE.local.md` tells your agent which one to use.
- The deny rules that setup writes turn off Claude Code's PowerShell tool, so your agent runs commands in Git Bash, which is what `RULES.md` assumes.

## Check the handoff works

Do this once everyone's agent is running, and again after anyone restarts.

1. `lead` posts: `python3 tools/board.py post need --to all "Smoke test. Reply with board.py post fyi 'awake: <your role>', then go back to waiting."`
2. Watch the Message Board. Within two minutes there should be one `awake` reply per role.
3. Missing a role? That agent did not wake when `board.py wait` exited. Its human tells it to use `board.py watch` in the Monitor tool instead, as `RULES.md` describes, and you run the test again.

## Layout

| Path | Owner | Purpose |
| --- | --- | --- |
| `RULES.md` | lead | Rules every agent reads |
| `team.json` | lead | Who owns which folder |
| `docs/TASK.md` | lead | The task, once announced |
| `docs/PLAYBOOK.md` | lead | What the humans do and when |
| `docs/RUNBOOK.md` | lead | What to do when something breaks |
| `docs/SUBMISSION.md` | lead | Deadline facts, Devfolio text and the final README |
| `shared/` | lead | Contracts between agents |
| `agents/<role>/` | that role | Each agent's work and brief |
| `tools/board.py` | lead | Message Board client |

## How handoffs work

1. An agent finishes, pushes, and runs `board.py post done --to <teammate> "..."`.
2. That comment tags the teammate on the pinned issue.
3. The teammate's agent is sitting in `board.py wait`, which checks every 20 seconds. It sees the tag, exits, and the agent carries on.

Only `--to` tags anyone. Any other `@name` in a message is put in backticks, so a stray mention cannot wake an agent. Tags also trigger normal GitHub notifications for the humans. Switch off email for this repo if that gets noisy.

## Tests

```bash
python3 -m unittest discover -s tools -p 'test_*.py'
```
