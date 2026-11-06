# winthrutheory

Team repo for the Agentverse hackathon, Jane Street finance track. Five people each run one agent, and each agent owns one folder. Agents hand work to each other through a pinned GitHub issue, so nobody has to send a text.

## Setup

You need `git`, Python 3.11+, the [GitHub CLI](https://cli.github.com) signed in with `gh auth login`, and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/eliohd/winthrutheory-eceb0cdd-submission-repo.git
```

```bash
cd winthrutheory-eceb0cdd-submission-repo && ./tools/setup.sh
```

Start your agent from the repo root. In Claude Code, that is:

```bash
claude "Read the rules, find your role, and start the work loop."
```

Your agent will stop at permission prompts unless you approve its board and git commands, so choose a permission mode you are comfortable with before you leave it running.

## Layout

| Path | Owner | Purpose |
| --- | --- | --- |
| `RULES.md` | lead | Rules every agent reads |
| `team.json` | lead | Who owns which folder |
| `docs/TASK.md` | lead | The task, once announced |
| `docs/PLAYBOOK.md` | lead | What the humans do and when |
| `shared/` | lead | Contracts between agents |
| `agents/<role>/` | that role | Each agent's work and brief |
| `tools/board.py` | lead | Message Board client |

## How handoffs work

1. An agent finishes, pushes, and runs `board.py post done --to <teammate> "..."`.
2. That comment tags the teammate on the pinned issue.
3. The teammate's agent is sitting in `board.py wait`, which checks every 20 seconds. It sees the tag, exits, and the agent carries on.

Tags also trigger normal GitHub notifications for the humans. Switch off email for this repo if that gets noisy.

## Tests

```bash
python3 -m unittest discover -s tools -p 'test_*.py'
```
