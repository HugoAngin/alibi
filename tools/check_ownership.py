#!/usr/bin/env python3
"""Report files changed by someone who does not own them.

Exit codes: 0 clean, 1 violations found, 2 could not check.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

import team as roster

OVERRIDE = "[team-override]"


def git(*args: str) -> str:
    done = subprocess.run(["git", *args], cwd=roster.ROOT, capture_output=True, text=True)
    if done.returncode:
        print(f"git {' '.join(args)} failed: {done.stderr.strip()}", file=sys.stderr)
        raise SystemExit(2)
    return done.stdout


def violations(team: dict, login: str, paths: list[str]) -> list[str]:
    problems = []
    for path in paths:
        owner = roster.owner_of(team, path)
        if not roster.same_login(owner["github"], login):
            name = owner["github"] or "nobody yet"
            problems.append(f"{path} is owned by {owner['role']} ({name})")
    return problems


def staged_paths() -> list[str]:
    output = git("diff", "--cached", "--name-only", "--no-renames", "-z")
    return [path for path in output.split("\0") if path]


def range_paths(commit_range: str) -> list[str]:
    paths: set[str] = set()
    for sha in git("rev-list", "--no-merges", commit_range).split():
        if OVERRIDE in git("log", "-1", "--format=%B", sha):
            continue
        output = git("diff-tree", "--root", "--no-commit-id", "--name-only",
                     "--no-renames", "-r", "-z", sha)
        paths.update(path for path in output.split("\0") if path)
    return sorted(paths)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--staged", action="store_true", help="check the staged files")
    mode.add_argument("--range", help="check commits in a git range, such as A..B")
    parser.add_argument("--login", help="defaults to the configured login")
    args = parser.parse_args()

    if args.staged and os.environ.get("TEAM_OVERRIDE") == "1":
        return
    login = args.login or roster.current_login()
    paths = staged_paths() if args.staged else range_paths(args.range)
    problems = violations(roster.load(), login, paths)
    if problems:
        print(f"{login} changed files they do not own:")
        for problem in problems:
            print(f"- {problem}")
        print("Ask the owner on the Message Board instead. See RULES.md.")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
