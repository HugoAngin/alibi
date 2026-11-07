#!/usr/bin/env bash
# Run once per clone, and again after pulling changes to tools/ or team.json.
# Connects this clone to your role, installs the commit hook, and writes Claude Code
# permission rules for your role to .claude/settings.local.json (your own approvals are kept).
# On Windows, run it from Git Bash.
set -euo pipefail
cd "$(dirname "$0")/.."

command -v gh >/dev/null || { echo "Install the GitHub CLI: https://cli.github.com"; exit 1; }
gh auth status >/dev/null 2>&1 || { echo "Run: gh auth login"; exit 1; }

py=""
for candidate in "python3" "python" "py -3"; do
  if $candidate -c 'import sys; sys.exit(sys.version_info < (3, 8))' >/dev/null 2>&1; then
    py="$candidate"
    break
  fi
done
[ -n "$py" ] || { echo "Install Python 3.11+ so that python3, python or py works."; exit 1; }

login="$(gh api user --jq .login)"
role="$(LOGIN="$login" $py -c '
import json, os
team = json.load(open("team.json"))
me = os.environ["LOGIN"].lower()
print(next((m["role"] for m in team["members"] if (m["github"] or "").lower() == me), ""))
')"
[ -n "$role" ] || { echo "$login is not in team.json. Ask lead to add you."; exit 1; }

git config team.login "$login"
git config core.hooksPath .githooks
git config rebase.autoStash true

cat > CLAUDE.local.md <<LOCAL
You are the \`$role\` agent, GitHub username \`$login\`. You write only inside \`agents/$role/\`.
On this machine, run the board as \`$py tools/board.py\` and project code as \`uv run --frozen python -m ...\`.

@agents/$role/BRIEF.md
@agents/$role/NOTES.md
LOCAL

$py tools/team.py local-settings "$role"
echo "Claude Code: board and git commands allowed, force-push and --no-verify blocked,"
echo "and edits outside agents/$role/ refused. See .claude/settings.local.json."

if command -v uv >/dev/null; then uv sync --frozen --quiet; else echo "uv not found, skipping the Python environment."; fi
echo "Ready: $login is $role. Start your agent from this folder."
