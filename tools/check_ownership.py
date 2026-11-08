#!/usr/bin/env python3
"""Report files changed by someone who does not own them.

With --staged it also stops files over 10 MB and anything that looks like an API key.

Exit codes: 0 clean, 1 violations found, 2 could not check.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys

import team as roster

OVERRIDE = "[team-override]"
MAX_BYTES = 10 * 1024 * 1024
SECRETS = [
    ("a Google API key", re.compile(r"AIza[0-9A-Za-z_-]{35}")),
    ("an NVIDIA API key", re.compile(r"nvapi-[0-9A-Za-z_-]{20,}")),
    ("an Anthropic API key", re.compile(r"sk-ant-[0-9A-Za-z_-]{20,}")),
    ("an OpenAI-style API key", re.compile(r"\bsk-(?!ant-)(?=[0-9A-Za-z_-]*\d)[0-9A-Za-z_-]{20,}")),
    ("a GitHub token", re.compile(r"\b(?:gh[pousr]_[0-9A-Za-z]{30,}|github_pat_[0-9A-Za-z_]{30,})")),
    ("a Hugging Face token", re.compile(r"\bhf_[0-9A-Za-z]{30,}")),
    ("an AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("a private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
]


def git(*args: str) -> str:
    done = subprocess.run(["git", *args], cwd=roster.ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if done.returncode:
        print(f"git {' '.join(args)} failed: {done.stderr.strip()}", file=sys.stderr)
        raise SystemExit(2)
    return done.stdout


def is_lead(team: dict, login: str) -> bool:
    return any(m["role"] == "lead" and roster.same_login(m["github"], login)
               for m in team["members"])


def violations(team: dict, login: str, paths: list[str]) -> list[str]:
    problems = []
    on_roster = roster.member_for_login(team, login) is not None
    for path in paths:
        if on_roster and roster.is_open(team, path):
            continue
        owner = roster.owner_of(team, path)
        if not roster.same_login(owner["github"], login):
            name = owner["github"] or "nobody yet"
            problems.append(f"{path} is owned by {owner['role']} ({name})")
    return problems


def staged_paths() -> list[str]:
    output = git("diff", "--cached", "--name-only", "--no-renames", "-z")
    return [path for path in output.split("\0") if path]


def range_paths(commit_range: str, honour_override: bool) -> list[str]:
    paths: set[str] = set()
    for sha in git("rev-list", "--no-merges", commit_range).split():
        if honour_override and OVERRIDE in git("log", "-1", "--format=%B", sha):
            continue
        output = git("diff-tree", "--root", "--no-commit-id", "--name-only",
                     "--no-renames", "-r", "-z", sha)
        paths.update(path for path in output.split("\0") if path)
    return sorted(paths)


def oversized(sizes: dict[str, int]) -> list[str]:
    return [f"{path} is {size / 2**20:.1f} MB, over the 10 MB limit. Keep it in data/ "
            f"and commit the command that rebuilds it."
            for path, size in sorted(sizes.items()) if size > MAX_BYTES]


def secrets_in(diff: str) -> list[str]:
    """Name what looks like a key in the added lines of a diff, never the key itself."""
    found: dict[str, set[str]] = {}
    path, in_header = None, False
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            path, in_header = None, True
        elif in_header and line.startswith("+++ "):
            name = line[4:].strip('"')
            path = name[2:] if name.startswith("b/") else None
        elif line.startswith("@@"):
            in_header = False
        elif path and not in_header and line.startswith("+"):
            for label, pattern in SECRETS:
                if pattern.search(line):
                    found.setdefault(path, set()).add(label)
    return [f"{path} has what looks like {label}. Put it in .env and read it from the "
            f"environment."
            for path, labels in sorted(found.items()) for label in sorted(labels)]


def staged_sizes() -> dict[str, int]:
    sizes = {}
    output = git("diff", "--cached", "--name-only", "--no-renames", "--diff-filter=ACM", "-z")
    for path in (p for p in output.split("\0") if p):
        done = subprocess.run(["git", "cat-file", "-s", f":{path}"], cwd=roster.ROOT,
                              capture_output=True, text=True, encoding="utf-8", errors="replace")
        if done.returncode == 0:
            sizes[path] = int(done.stdout)
    return sizes


def staged_diff() -> str:
    return git("diff", "--cached", "--no-color", "--no-ext-diff", "--no-renames",
               "--src-prefix=a/", "--dst-prefix=b/", "--diff-filter=ACM", "-U0")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--staged", action="store_true", help="check the staged files")
    mode.add_argument("--range", help="check commits in a git range, such as A..B")
    parser.add_argument("--login", help="defaults to the configured login")
    args = parser.parse_args()

    team = roster.load()
    login = args.login or roster.current_login()
    lead = is_lead(team, login)
    if args.range:
        problems = violations(team, login, range_paths(args.range, honour_override=lead))
        if problems:
            print(f"{login} pushed changes to files they do not own:")
            for problem in problems:
                print(f"- {problem}")
            raise SystemExit(1)
        return

    override = os.environ.get("TEAM_OVERRIDE") == "1"
    if override and not lead:
        print("TEAM_OVERRIDE only works for lead, so ownership is checked as usual.")
    owned = [] if override and lead else violations(team, login, staged_paths())
    problems = owned + oversized(staged_sizes()) + secrets_in(staged_diff())
    if problems:
        print(f"Commit blocked for {login}:")
        for problem in problems:
            print(f"- {problem}")
        if owned:
            print("For files you do not own, ask the owner on the Message Board. See RULES.md.")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
