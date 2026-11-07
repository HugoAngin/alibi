#!/usr/bin/env python3
"""Record every reported number with the command that made it, and rerun them to check.

    python3 tools/results.py add --split validation --variants 12 --command "uv run --frozen python -m agents.research.backtest --seed 7"
    python3 tools/results.py verify
    python3 tools/results.py report --markdown

The command must print its metrics as one JSON object on its last line of output, such as
{"sharpe": 1.42, "max_drawdown": -0.08}. `add` runs it, so no number is ever typed by hand,
and appends a record to your agents/<role>/out/results.jsonl. `verify` reruns records in a
clean checkout of the commit they came from, with data/ linked in, and appends a verdict
to your agents/<role>/out/verified.jsonl.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import team as roster
from lockbox import OPEN_FLAG, lockbox_from

RESULTS = "out/results.jsonl"
VERDICTS = "out/verified.jsonl"
NEEDS_VARIANTS = {"validation", "lockbox"}


def die(message: str) -> None:
    print(f"results: {message}", file=sys.stderr)
    raise SystemExit(1)


def git(*args: str, cwd: Path = roster.ROOT, check: bool = True) -> str:
    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if check and done.returncode:
        die(f"git {' '.join(args)} failed: {done.stderr.strip()}")
    return done.stdout.strip() if done.returncode == 0 else ""


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def me(team: dict) -> dict:
    member = roster.member_for_login(team, roster.current_login())
    if member is None:
        die("your GitHub login is not in team.json. Run tools/setup.sh.")
    return member


def read_lines(paths) -> list[dict]:
    rows = []
    for path in paths:
        for number, line in enumerate(Path(path).read_text().splitlines(), 1):
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    print(f"results: skipping bad line {number} in {path}", file=sys.stderr)
    return rows


def all_records(root: Path = roster.ROOT) -> list[dict]:
    return read_lines(sorted(root.glob(f"agents/*/{RESULTS}")))


def latest_verdicts(root: Path = roster.ROOT) -> dict[str, dict]:
    verdicts: dict[str, dict] = {}
    for verdict in read_lines(sorted(root.glob(f"agents/*/{VERDICTS}"))):
        verdicts[verdict["id"]] = verdict
    return verdicts


def metrics_from(stdout: str) -> dict[str, float]:
    """Read the metrics object from the last non-empty line the command printed."""
    lines = [line for line in stdout.splitlines() if line.strip()]
    try:
        found = json.loads(lines[-1]) if lines else None
    except json.JSONDecodeError:
        found = None
    if not isinstance(found, dict):
        raise ValueError("the command must print its metrics as one JSON object on its last "
                         'line, such as {"sharpe": 1.42}')
    metrics = {k: v for k, v in found.items()
               if isinstance(v, (int, float)) and not isinstance(v, bool)}
    if not metrics:
        raise ValueError("the JSON on the last line has no numbers in it")
    return metrics


def same(a: float, b: float, rel: float, abs_: float) -> bool:
    if isinstance(a, float) and isinstance(b, float) and math.isnan(a) and math.isnan(b):
        return True
    return math.isclose(a, b, rel_tol=rel, abs_tol=abs_)


def compare(expected: dict, got: dict, rel: float, abs_: float) -> list[str]:
    problems = []
    for name, value in expected.items():
        if name not in got:
            problems.append(f"{name} missing from the rerun")
        elif not same(value, got[name], rel, abs_):
            problems.append(f"{name} was {value}, rerun gave {got[name]}")
    return problems


def record_id(record: dict) -> str:
    keys = ("role", "split", "command", "commit", "metrics", "created")
    blob = json.dumps({k: record.get(k) for k in keys}, sort_keys=True)
    return hashlib.sha1(blob.encode()).hexdigest()[:10]


def lockbox_problem(role: str, records: list[dict], reason: str | None) -> str | None:
    """Why this lockbox look is not allowed, or None if it is."""
    if role != "eval":
        return "only eval scores the lockbox."
    earlier = [r for r in records if r.get("split") == "lockbox"]
    if earlier and not reason:
        return (f"the lockbox was already scored in record {earlier[-1]['id']}. Scoring it again "
                f"makes it a second validation set. If you must, pass --reason, which the "
                f"report shows.")
    return None


def child_env(open_lockbox: bool) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in (OPEN_FLAG, "VIRTUAL_ENV")}
    if open_lockbox:
        env[OPEN_FLAG] = "1"
    return env


def run(command: str, cwd: Path, open_lockbox: bool, timeout: float | None) -> str:
    """Run command through the shell, showing its stderr live, and return its stdout."""
    done = subprocess.run(command, shell=True, cwd=cwd, env=child_env(open_lockbox),
                          stdout=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    if done.returncode:
        raise RuntimeError(f"exited with code {done.returncode}. Last output: "
                           f"{done.stdout.strip()[-300:]}")
    return done.stdout


def dirty(folder: str) -> list[str]:
    """Changes that would make a record point at code other than what ran."""
    changed = git("diff", "--name-only", "HEAD").splitlines()
    untracked = git("ls-files", "--others", "--exclude-standard", "--", folder).splitlines()
    own_logs = {folder + RESULTS, folder + VERDICTS}
    return [path for path in changed + untracked if path not in own_logs]


def cmd_add(args: argparse.Namespace, team: dict) -> None:
    member = me(team)
    role, folder = member["role"], member["paths"][0]
    if args.split in NEEDS_VARIANTS and not args.variants:
        die(f"--variants is required for {args.split}: how many versions you tried before this one.")
    records = all_records()
    if args.split == "lockbox":
        if lockbox_from() is None:
            die("no lockbox is set in shared/splits.json. Ask lead.")
        problem = lockbox_problem(role, records, args.reason)
        if problem:
            die(problem)
    leftovers = dirty(folder)
    if leftovers:
        die("commit these first, so the record points at the code that ran: "
            + ", ".join(leftovers[:10]))
    print(f"results: running {args.command}", file=sys.stderr)
    try:
        stdout = run(args.command, roster.ROOT, args.split == "lockbox", args.timeout)
        print("\n".join(stdout.rstrip().splitlines()[-20:]), file=sys.stderr)
        metrics = metrics_from(stdout)
    except (RuntimeError, ValueError, subprocess.TimeoutExpired) as error:
        die(str(error))
    record = {"role": role, "split": args.split, "metrics": metrics, "variants": args.variants,
              "command": args.command, "commit": git("rev-parse", "HEAD"), "created": now()}
    if args.note:
        record["note"] = args.note
    if args.reason:
        record["reason"] = args.reason
    record = {"id": record_id(record), **record}
    path = roster.ROOT / (folder + RESULTS)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as out:
        out.write(json.dumps(record) + "\n")
    print(json.dumps(record, indent=2))
    print(f"Recorded in {folder + RESULTS}. Commit and push it, then post done to eval.")


def link(source: Path, target: Path) -> None:
    try:
        os.symlink(source, target, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            raise
        subprocess.run(["cmd", "/c", "mklink", "/J", str(target), str(source)],
                       check=True, capture_output=True)


def unlink(target: Path) -> None:
    if target.is_symlink() or (os.name == "nt" and target.is_dir()):
        os.rmdir(target) if os.name == "nt" else os.unlink(target)


def has_commit(commit: str) -> bool:
    return subprocess.run(["git", "cat-file", "-e", f"{commit}^{{commit}}"],
                          cwd=roster.ROOT, capture_output=True).returncode == 0


def checkout(commit: str, into: Path) -> str | None:
    """Make a clean checkout of commit with data/ and .env from this clone. Returns an error."""
    if not has_commit(commit):
        git("fetch", "-q", "origin", check=False)
        if not has_commit(commit):
            return f"commit {commit[:7]} is not here or on origin. Was it pushed?"
    git("worktree", "add", "-q", "--detach", str(into), commit)
    if (roster.ROOT / "data").is_dir():
        link(roster.ROOT / "data", into / "data")
    if (roster.ROOT / ".env").is_file():
        shutil.copyfile(roster.ROOT / ".env", into / ".env")
    return None


def remove_checkout(into: Path) -> None:
    unlink(into / "data")
    (into / ".env").unlink(missing_ok=True)
    git("worktree", "remove", "--force", str(into), check=False)
    git("worktree", "prune", check=False)


def verify_one(record: dict, cwd: Path, args: argparse.Namespace) -> dict:
    verdict = {"id": record["id"], "checked": now()}
    try:
        got = metrics_from(run(record["command"], cwd, record["split"] == "lockbox", args.timeout))
    except subprocess.TimeoutExpired:
        return {**verdict, "verdict": "error", "detail": f"timed out after {args.timeout}s"}
    except (RuntimeError, ValueError) as error:
        return {**verdict, "verdict": "error", "detail": str(error)}
    problems = compare(record["metrics"], got, args.rel, args.abs)
    if problems:
        return {**verdict, "verdict": "fail", "detail": "; ".join(problems)}
    return {**verdict, "verdict": "pass", "detail": ""}


def cmd_verify(args: argparse.Namespace, team: dict) -> None:
    member = me(team)
    records = all_records()
    seen = latest_verdicts()
    chosen = [r for r in records if (args.id and r["id"] in args.id)
              or (not args.id and (args.all or r["id"] not in seen))]
    if not chosen:
        print("Nothing to verify.")
        return
    git("worktree", "prune", check=False)
    verdicts = []
    for commit in dict.fromkeys(r["commit"] for r in chosen):
        batch = [r for r in chosen if r["commit"] == commit]
        scratch = Path(tempfile.mkdtemp(prefix="verify-"))
        into = scratch / "repo"
        try:
            error = checkout(commit, into)
            for record in batch:
                if error:
                    verdicts.append({"id": record["id"], "checked": now(),
                                     "verdict": "error", "detail": error})
                elif record["split"] == "lockbox" and member["role"] != "eval":
                    print(f"results: skipping {record['id']}, only eval opens the lockbox.")
                    continue
                else:
                    print(f"results: rerunning {record['id']} ({record['role']}, "
                          f"{record['split']}) at {commit[:7]}", file=sys.stderr)
                    verdicts.append(verify_one(record, into, args))
                verdicts[-1]["by"] = member["role"]
                print(f"{verdicts[-1]['verdict'].upper():5} {record['id']} {verdicts[-1]['detail']}")
        finally:
            if into.exists():
                remove_checkout(into)
            shutil.rmtree(scratch, ignore_errors=True)
    path = roster.ROOT / (member["paths"][0] + VERDICTS)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as out:
        for verdict in verdicts:
            out.write(json.dumps(verdict) + "\n")
    print(f"Wrote {len(verdicts)} verdicts to {member['paths'][0] + VERDICTS}. Commit and push it.")
    if any(v["verdict"] != "pass" for v in verdicts):
        raise SystemExit(1)


def report_rows(records: list[dict], verdicts: dict[str, dict]) -> list[list[str]]:
    rows = []
    for r in records:
        v = verdicts.get(r["id"])
        status = f"{v['verdict']} ({v.get('by', '?')})" if v else "not verified"
        metrics = ", ".join(f"{k} {r['metrics'][k]:.4g}" for k in sorted(r["metrics"]))
        rows.append([r["role"], r["split"], metrics, str(r.get("variants") or ""),
                     status, r["commit"][:7], f"`{r['command']}`"])
    return rows


def cmd_report(args: argparse.Namespace, team: dict) -> None:
    records, verdicts = all_records(), latest_verdicts()
    header = ["Role", "Split", "Metrics", "Variants", "Verified", "Commit", "Command"]
    rows = report_rows(records, verdicts)
    if args.markdown:
        print("| " + " | ".join(header) + " |")
        print("| " + " | ".join("---" for _ in header) + " |")
        for row in rows:
            print("| " + " | ".join(row) + " |")
    else:
        for row in rows:
            print("  ".join(row))
    looks = [r for r in records if r["split"] == "lockbox"]
    print(f"\nLockbox scored {len(looks)} time(s).")
    for look in looks[1:]:
        print(f"- again in {look['id']}: {look.get('reason', 'no reason given')}")
    unverified = sum(1 for r in records if verdicts.get(r["id"], {}).get("verdict") != "pass")
    if unverified:
        print(f"{unverified} record(s) not verified as passing. Run: python3 tools/results.py verify")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="name", required=True)

    add = commands.add_parser("add", help="run a command and record its metrics")
    add.add_argument("--command", required=True, help="what to run, from the repo root")
    add.add_argument("--split", required=True, help="train, validation, lockbox, or another name")
    add.add_argument("--variants", type=int, help="versions tried before this one")
    add.add_argument("--note", help="anything a reader needs, such as cost assumptions")
    add.add_argument("--reason", help="why the lockbox is being scored again")
    add.add_argument("--timeout", type=float, help="seconds before giving up")
    add.set_defaults(run=cmd_add)

    verify = commands.add_parser("verify", help="rerun records in clean checkouts")
    verify.add_argument("--all", action="store_true", help="rerun records already checked")
    verify.add_argument("--id", nargs="+", help="rerun only these records")
    verify.add_argument("--rel", type=float, default=1e-6, help="relative tolerance")
    verify.add_argument("--abs", type=float, default=1e-9, help="absolute tolerance")
    verify.add_argument("--timeout", type=float, default=3600, help="seconds per command")
    verify.set_defaults(run=cmd_verify)

    report = commands.add_parser("report", help="list records and their verdicts")
    report.add_argument("--markdown", action="store_true", help="a table for the README")
    report.set_defaults(run=cmd_report)
    return parser.parse_args(argv)


def main() -> None:
    args = parse_args()
    args.run(args, roster.load())


if __name__ == "__main__":
    main()
