#!/usr/bin/env bash
# Run once per clone. Connects this clone to your role and installs the commit hook.
set -euo pipefail
cd "$(dirname "$0")/.."

command -v gh >/dev/null || { echo "Install the GitHub CLI: https://cli.github.com"; exit 1; }
gh auth status >/dev/null 2>&1 || { echo "Run: gh auth login"; exit 1; }

login="$(gh api user --jq .login)"
role="$(LOGIN="$login" python3 -c '
import json, os
team = json.load(open("team.json"))
me = os.environ["LOGIN"].lower()
print(next((m["role"] for m in team["members"] if (m["github"] or "").lower() == me), ""))
')"
[ -n "$role" ] || { echo "$login is not in team.json. Ask lead to add you."; exit 1; }

git config team.login "$login"
git config core.hooksPath .githooks

cat > CLAUDE.local.md <<LOCAL
You are the \`$role\` agent, GitHub username \`$login\`. You write only inside \`agents/$role/\`.

@agents/$role/BRIEF.md
@agents/$role/NOTES.md
LOCAL

if command -v uv >/dev/null; then uv sync --quiet; else echo "uv not found, skipping the Python environment."; fi
echo "Ready: $login is $role. Start your agent from this folder."
