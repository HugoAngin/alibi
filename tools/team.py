"""Roster and ownership lookups, read from team.json.

Run as a script, it writes this clone's Claude Code permission rules for one role:
    python3 tools/team.py local-settings <role>
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCAL_SETTINGS = ROOT / ".claude" / "settings.local.json"
# Commands every agent runs all day. Allowing them stops agents stalling on prompts.
ALLOW = ["Bash(python3 tools/board.py *)", "Bash(python tools/board.py *)",
         "Bash(py -3 tools/board.py *)", "Bash(git add agents/*)", "Bash(git commit -m *)",
         "Bash(git pull --rebase origin main)", "Bash(git push origin main)",
         "Bash(git fetch *)", "Bash(uv sync --frozen)"]
# What RULES.md forbids, plus reading the secrets file into a transcript.
DENY = ["Bash(git push --force*)", "Bash(git push -f*)", "Bash(git push * --force*)",
        "Bash(git push * -f)", "Bash(git push * -f *)", "Bash(git push * +*)",
        "Bash(git add -A*)", "Bash(git add --all*)", "Bash(git add -u*)", "Bash(git add .)",
        "Bash(git commit -a*)", "Bash(git commit --all*)", "Bash(git commit -n*)",
        "Bash(git commit --no-verify*)", "Bash(git commit * --no-verify*)", "Read(/.env)"]
# Paths lead owns. Claude Code refuses edits to them before the commit hook would.
LEAD_PATHS = ["/shared/**", "/docs/**", "/tools/**", "/.github/**", "/.githooks/**",
              "/RULES.md", "/README.md", "/CLAUDE.md", "/AGENTS.md", "/team.json",
              "/pyproject.toml", "/uv.lock", "/.gitignore", "/.env.example"]


def load(path: Path = ROOT / "team.json") -> dict:
    return json.loads(path.read_text())


def same_login(a: str | None, b: str | None) -> bool:
    return bool(a and b) and a.lower() == b.lower()


def member_for_login(team: dict, login: str) -> dict | None:
    return next((m for m in team["members"] if same_login(m["github"], login)), None)


def owner_of(team: dict, path: str) -> dict:
    """Return the member owning path. The longest prefix wins, otherwise lead."""
    matches = [
        (len(prefix), member)
        for member in team["members"]
        for prefix in member["paths"]
        if path.startswith(prefix)
    ]
    if matches:
        return max(matches, key=lambda match: match[0])[1]
    return next(m for m in team["members"] if m["role"] == "lead")


def current_login() -> str:
    """Return the login set by setup.sh, falling back to the signed-in gh user."""
    configured = subprocess.run(
        ["git", "config", "--get", "team.login"],
        cwd=ROOT, capture_output=True, text=True,
    ).stdout.strip()
    if configured:
        return configured
    return subprocess.run(
        ["gh", "api", "user", "--jq", ".login"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def edit_denies(team: dict, role: str) -> list[str]:
    """Claude Code deny rules for the files this role does not own."""
    rules = [f"Edit(/{prefix}**)" for m in team["members"] if m["role"] != role
             for prefix in m["paths"]]
    if role != "lead":
        rules += [f"Edit({path})" for path in LEAD_PATHS]
    return rules


def is_generated(rule: str) -> bool:
    lead_rules = {f"Edit({path})" for path in LEAD_PATHS}
    return rule.startswith("Edit(/agents/") or rule in lead_rules or rule in ALLOW + DENY


def with_rules(settings: dict, allow: list[str], deny: list[str]) -> dict:
    """Swap in this script's rules and keep everything the person added themselves."""
    permissions = settings.setdefault("permissions", {})
    for key, rules in (("allow", allow), ("deny", deny)):
        kept = [rule for rule in permissions.get(key, []) if not is_generated(rule)]
        permissions[key] = kept + rules
    return settings


def write_local_settings(team: dict, role: str, path: Path = LOCAL_SETTINGS) -> None:
    try:
        settings = json.loads(path.read_text())
    except FileNotFoundError:
        settings = {}
    except json.JSONDecodeError:
        raise SystemExit(f"{path} is not valid JSON. Fix it, then run setup again.")
    settings = with_rules(settings, ALLOW, DENY + edit_denies(team, role))
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(settings, indent=2) + "\n")


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "local-settings":
        raise SystemExit("usage: team.py local-settings <role>")
    write_local_settings(load(), sys.argv[2])
