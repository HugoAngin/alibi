"""Roster and ownership lookups, read from team.json."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


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
