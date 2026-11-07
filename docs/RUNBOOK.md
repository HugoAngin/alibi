# Runbook

What to do when something breaks. Humans run these commands, or tell their agent to.

## Board and wake-ups

| Symptom | Do this |
| --- | --- |
| An agent missed the smoke test, or did not react to a tag | Type `/tasks` in its Claude Code session. If no `board.py wait` is running, tell it to start one in the background. If one ran and exited but the agent did not wake, tell it to use `board.py watch` in the Monitor tool, as `RULES.md` describes. Run the smoke test again. |
| A restarted agent does not know what it was asked | `python3 tools/board.py read --mine -n 10`. Messages count as read once they are printed, so `inbox` will not show them again. |
| An agent sits in `wait` while its `need` goes unanswered | It should have used `wait --timeout 900`. Tell it to proceed on the assumption it stated, or post `blocked` to `lead`. |
| An agent floods the board or loops | Stop it with Esc, read its `NOTES.md`, and restart it with a narrower instruction. |
| `board.py` reports GitHub errors | `wait` and `watch` retry on their own. Run `gh auth status`. If the venue Wi-Fi is down, use a phone hotspot. |
| `board.py: unknown recipient` | The role or username is not in `team.json`. Check the spelling, or ask `lead`. |

## Commits and pushes

| Symptom | Do this |
| --- | --- |
| Hook: `no working Python 3 found` | Install Python 3.11+. On Windows, check that `python` or `py -3` runs in Git Bash. |
| Hook: `is owned by` | The file belongs to someone else. Run `git restore --staged <file>` and `git restore <file>`, then post a `need` to the owner. |
| Hook: `over the 10 MB limit` | Run `git restore --staged <file>`, move the file to `data/`, and commit the command that rebuilds it instead. |
| Hook: `looks like ... API key` | Run `git restore --staged <file>`, move the key to `.env`, and read it from the environment. If it is a false positive, the human, not the agent, commits with `--no-verify`. |
| CI posts `BLOCKED` on the board | The file's owner, or `lead`, runs `git revert <sha>` and pushes. If the commit touched several owners' files, `lead` reverts it with `TEAM_OVERRIDE=1` and `[team-override]` in the message. Whoever pushed it posts what it needed to the owner. |
| `git push` is rejected | `main` moved. Run `git pull --rebase origin main` and push again. Never force-push. |
| `git pull --rebase` stops on a conflict | Two people changed one file, which ownership should prevent. Run `git rebase --abort` and tell `lead`. |
| `board.py` refuses `done` | Push first. Generated files you do not want to commit go in `data/` or in `agents/<role>/.gitignore`. |

## Python

| Symptom | Do this |
| --- | --- |
| `ModuleNotFoundError` for a package such as `pandas` | Run code with `uv run --frozen python -m ...` from the repo root, not plain `python`. If the package is not in `pyproject.toml`, post a `need` to `lead`. |
| `ModuleNotFoundError: agents` | Run from the repo root as a module, `python -m agents.<role>.<module>`, not as `python agents/<role>/file.py`. |
| `uv.lock` or `pyproject.toml` shows as changed | Someone ran uv without `--frozen`, or ran pip. Run `git checkout -- uv.lock pyproject.toml`. Only `lead` changes them, with `uv add`. |
| Numbers differ between two machines | Both must use `uv run --frozen` and fixed seeds. Compare the commands in the results records, not the numbers. |

## Results and the lockbox

| Symptom | Do this |
| --- | --- |
| `add`: `commit these first` | The record must point at the code that ran. Commit the files it lists, or move generated files to `data/`, then run `add` again. |
| `add`: `must print its metrics as one JSON object` | End the command's output with one line such as `print(json.dumps({"sharpe": s}))`. Logs before it are fine. |
| `verify`: `is not here or on origin` | The author has not pushed that commit. Ask them to push. |
| `verify`: `error`, but the command works for its author | It depends on something that is not committed and not in `data/`, such as a file in a gitignored folder. The author commits it or moves it to `data/`. |
| `verify`: `fail`, with two different numbers | A missing seed, or the data in `data/` changed since the record was made. Fix the seed, or rebuild the data, then record it again. |
| `PermissionError` mentioning the lockbox | Your code asked for times inside the lockbox. Use the rows `visible()` returns, and leave the lockbox to `eval`. |
| `lockbox: no lockbox set` | `lead` has not written `shared/splits.json`. Ask for it before anyone studies the data. |
| `assert_no_lookahead` fails | The message names each time t where the decision changed when the future was cut off or scrambled. Look for full-sample statistics, `shift(-1)`, and joins on later timestamps. |

## People and limits

| Symptom | Do this |
| --- | --- |
| An agent hits its usage limit | Its human posts an `fyi` so the team knows that role is paused, then switches model or carries on by hand. `NOTES.md` lets it resume later. |
| `lead` has a queue of `need` posts | Its human answers them first. Packages can go in together with one `uv add`. If `lead` stays behind, give the end-to-end runner to `eval`. |
| A laptop dies | Clone on another machine, sign `gh` in to the same account, run `./tools/setup.sh`, and restart the agent from `NOTES.md`. |
