"""Isolated build stage for the Alibi Modal replay (agents/eval/out/MODAL_REPLAY.md).

Copies a strict allowlist of tracked files from one selected commit into a new,
empty directory outside the repository, for a separately labelled replay of
public synthetic data. Files are read from the commit's git objects, never from
the working tree, so untracked, ignored and locally modified files cannot get
in. Nothing is deployed, no network is used and no environment value is read.

Two modes, kept apart:

* ``dry`` stages any commit name (default ``HEAD``) for rehearsal. It lists the
  release-required files the commit lacks under ``missing`` and is never a
  release.
* ``release`` takes a full commit SHA only. It refuses unless every
  release-required file is tracked at that commit: the lockfile, the S5 runbook
  ``alibi/RUNBOOK.md``, public Cases 1 and 2, and their replay recordings.

Both modes refuse before writing anything if an allowlisted file, or any entry
under a staged root, is a symlink, a submodule or has a secret-shaped name, or
if a staged path looks private (sealed, gold, lockbox, scratch, ``data/``).
After writing, the stage is walked again and must match the manifest exactly.

stdout gets one JSON line: the candidate SHA, the mode and the file manifest
(relative path, size, sha256). Refusals go to stderr and exit 1.

    uv run --frozen python -m agents.eval.deploy.replay_stage dry --dest "$(mktemp -d)"
    uv run --frozen python -m agents.eval.deploy.replay_stage release \\
        --candidate "$(git rev-parse HEAD)" --dest "$(mktemp -d /tmp/alibi-modal-stage.XXXXXX)"
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Iterable, Sequence

REPO = Path(__file__).resolve().parents[3]
MODES = ("dry", "release")

#: Runtime files the replay container needs, as full-match regexes on tracked
#: POSIX paths. Tests, eval and sealing code, reports and docs stay out.
RUNTIME = (
    r"pyproject\.toml",
    r"uv\.lock",
    r"\.python-version",
    r"alibi/RUNBOOK\.md",
    r"alibi/[a-z0-9_]+\.py",
    r"alibi/(?:agents|app|bank|baselines|connectors|core|fixtures|llm)/[a-z0-9_]+\.py",
    r"alibi/agents/prompts/[a-z0-9_]+\.(?:md|json)",
    r"alibi/app/templates/(?:[a-z0-9_]+/)*[a-z0-9_]+\.html",
    r"alibi/app/static/(?:[a-z0-9_-]+/)*[a-z0-9_-]+(?:\.[a-z0-9_-]+)*\.(?:css|js|svg|png|ico|woff2?)",
    r"tools/(?:lockbox|team)\.py",
    r"shared/splits\.json",
    r"agents/data/api\.py",
    r"agents/data/out/(?:index|merchant_registry)\.json",
    r"agents/execution/(?:api|policy)\.py",
    r"agents/execution/out/policy_material\.json",
    r"agents/research/deploy/(?:modal_app|router)\.py",
    r"agents/eval/deploy/(?:router|s5_host|s5_modal)\.py",
)

#: Public synthetic inputs: development cases 1 to 80 only (shared/CONTRACTS.md,
#: "Shared deployments run public cases 1 to 80 only"), fixtures and recordings.
PUBLIC = (
    r"agents/data/out/cases/case_00(?:0[1-9]|[1-7][0-9]|80)\.json",
    r"alibi/fixtures/[a-z0-9_]+\.json",
    r"alibi/fixtures/replays/[a-z0-9_]+\.json",
)

ALLOWLIST = tuple(re.compile(p) for p in RUNTIME + PUBLIC)

RUNBOOK = "alibi/RUNBOOK.md"

#: Release refuses unless each of these is tracked at the candidate. ``--require``
#: adds to this list and can never remove from it.
REQUIRED_RELEASE = (
    "pyproject.toml",
    "uv.lock",
    RUNBOOK,
    "agents/data/out/cases/case_0001.json",
    "agents/data/out/cases/case_0002.json",
    "alibi/fixtures/replays/maya_supported.json",
    "alibi/fixtures/replays/maya_contradiction.json",
)

#: Every tracked entry under these roots is checked for symlinks, submodules
#: and secret-shaped names, whether or not it is allowlisted.
SCAN_ROOTS = ("alibi/", "tools/", "shared/", "agents/data/", "agents/execution/",
              "agents/research/deploy/", "agents/eval/deploy/")

REGULAR = ("100644", "100755")

SECRET_NAME = re.compile(
    r"\.env(?:\..*)?"
    r"|\.(?:netrc|npmrc|pypirc|pgpass|htpasswd)"
    r"|id_(?:rsa|dsa|ecdsa|ed25519)(?:\.pub)?"
    r"|.*\.(?:pem|key|p12|pfx|jks|keystore|kdbx|gpg|db|sqlite|sqlite3|db-wal|db-shm|db-journal)"
    r"|.*(?:secret|credential|passw|privkey|private[_-]?key|api[_-]?key|token).*"
)
PRIVATE_DIRS = frozenset(
    {"sealed", "gold", "lockbox", "private", "secrets", "scratch", "tmp",
     ".git", ".venv", "node_modules", "__pycache__", ".pytest_cache"}
)
PRIVATE_WORDS = re.compile(r"sealed|gold|lockbox|private|llm_log")
FULL_SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
SECRET_CONTENT = re.compile(
    rb"(?<![A-Za-z0-9_-])(?:AIza[A-Za-z0-9_-]{35}|AQ\.[A-Za-z0-9_-]{40,})(?![A-Za-z0-9_-])"
    rb"|-----BEGIN (?:[A-Z0-9]+ )?PRIVATE KEY-----"
)

# Variables that would point git at another repository or object store.
_GIT_LOCATION_ENV = frozenset(
    {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
     "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR", "GIT_NAMESPACE"}
)


class StageError(Exception):
    """Refused: the stage would not be a clean, public, allowlisted snapshot."""

    def __init__(self, problems: Iterable[str]):
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


@dataclass(frozen=True)
class Entry:
    mode: str
    kind: str
    oid: str
    path: str


@dataclass(frozen=True)
class Plan:
    candidate: str
    mode: str
    files: tuple[Entry, ...]
    missing: tuple[str, ...]


def allowed(path: str) -> bool:
    return any(rx.fullmatch(path) for rx in ALLOWLIST)


def secret_shaped(path: str) -> bool:
    """True if the file name, or any directory above it, looks like a secret or a local database."""
    return any(SECRET_NAME.fullmatch(part) for part in path.lower().split("/"))


def private_shaped(path: str) -> bool:
    parts = path.lower().split("/")
    if parts[0] == "data" or any(part in PRIVATE_DIRS for part in parts[:-1]):
        return True
    name = parts[-1]
    return not name.endswith(".py") and bool(PRIVATE_WORDS.search(name))


def _git(repo: Path, *args: str, stdin: bytes | None = None) -> bytes:
    env = {k: v for k, v in os.environ.items() if k not in _GIT_LOCATION_ENV}
    try:
        done = subprocess.run(["git", "-C", str(repo), *args], input=stdin, capture_output=True, env=env)
    except OSError as exc:
        raise StageError([f"git could not run: {exc.strerror or exc}"]) from None
    if done.returncode != 0:
        lines = done.stderr.decode("utf-8", "replace").strip().splitlines()
        raise StageError([f"git {args[0]} failed: {lines[-1] if lines else f'exit {done.returncode}'}"])
    return done.stdout


def resolve_candidate(repo: Path, candidate: str, mode: str) -> str:
    """The commit SHA to stage. Release accepts a full SHA only, never a ref name."""
    if mode == "release" and not FULL_SHA.fullmatch(candidate or ""):
        raise StageError([f"release needs the candidate as a full commit SHA, got {candidate!r}"])
    if not candidate or candidate.startswith("-") or any(ch.isspace() or ord(ch) < 32 for ch in candidate):
        raise StageError([f"candidate {candidate!r} is not a commit name"])
    try:
        sha = _git(repo, "rev-parse", "--verify", "--quiet", f"{candidate}^{{commit}}").decode("ascii").strip()
    except StageError:
        raise StageError([f"candidate {candidate!r} is not a commit in {repo}"]) from None
    if mode == "release" and sha != candidate:
        raise StageError([f"candidate {candidate!r} resolved to a different commit {sha}"])
    return sha


def tracked(repo: Path, sha: str) -> list[Entry]:
    """Every entry of the commit's tree, recursively, from git objects only."""
    entries = []
    for record in _git(repo, "ls-tree", "-r", "-z", "--full-tree", sha).split(b"\0"):
        if not record:
            continue
        meta, _, raw = record.partition(b"\t")
        mode, kind, oid = meta.decode("ascii").split(" ")
        entries.append(Entry(mode, kind, oid, raw.decode("utf-8", "surrogateescape")))
    return entries


def plan(repo: Path, candidate: str, mode: str, require: Sequence[str] = ()) -> Plan:
    """Decide what to stage, or raise StageError with every problem found. Writes nothing."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, got {mode!r}")
    sha = resolve_candidate(Path(repo), candidate, mode)
    entries = tracked(Path(repo), sha)
    by_path = {e.path: e for e in entries}
    problems: list[str] = []

    for e in sorted(entries, key=lambda e: e.path):
        picked = allowed(e.path)
        if not (picked or e.path.startswith(SCAN_ROOTS)):
            continue
        if e.mode == "120000":
            problems.append(f"{e.path}: symlink")
        elif e.kind != "blob" or e.mode not in REGULAR:
            problems.append(f"{e.path}: not a regular file (git mode {e.mode})")
        if secret_shaped(e.path):
            problems.append(f"{e.path}: secret-shaped filename")
        if picked and private_shaped(e.path):
            problems.append(f"{e.path}: private path")

    for path in require:
        if not allowed(path):
            problems.append(f"{path}: required but not on the allowlist")
    required = list(dict.fromkeys([*REQUIRED_RELEASE, *require]))
    missing = tuple(p for p in required if p not in by_path)
    if mode == "release":
        problems.extend(f"{p}: required for release but not tracked at {sha}" for p in missing)

    files = tuple(sorted((e for e in entries if allowed(e.path)), key=lambda e: e.path))
    if not files:
        problems.append(f"no allowlisted files are tracked at {sha}")
    if problems:
        raise StageError(problems)
    return Plan(sha, mode, files, missing)


def _toplevel(repo: Path) -> Path:
    return Path(_git(repo, "rev-parse", "--show-toplevel").decode("utf-8").strip()).resolve()


def _check_dest(root: Path, dest: Path) -> None:
    """Require a new or empty destination outside Git worktrees."""
    if dest.is_symlink():
        raise StageError([f"destination {dest} is a symlink"])
    resolved = dest.resolve()
    if resolved == root or root in resolved.parents or any(
        (path / ".git").exists() for path in (resolved, *resolved.parents)
    ):
        raise StageError([f"destination {dest} is inside the repository"])
    if dest.exists():
        if not dest.is_dir():
            raise StageError([f"destination {dest} is not a directory"])
        if any(dest.iterdir()):
            raise StageError([f"destination {dest} is not empty"])
        return
    if not dest.parent.is_dir():
        raise StageError([f"the parent of destination {dest} does not exist"])


def _read_blobs(repo: Path, oids: Iterable[str]) -> dict[str, bytes]:
    unique = sorted(set(oids))
    if not unique:
        return {}
    out = _git(repo, "cat-file", "--batch", stdin=("\n".join(unique) + "\n").encode("ascii"))
    blobs, pos = {}, 0
    for oid in unique:
        end = out.find(b"\n", pos)
        header = out[pos:end].decode("ascii", "replace").split(" ") if end >= 0 else []
        if len(header) != 3 or header[0] != oid or header[1] != "blob" or not header[2].isdigit():
            raise StageError([f"git object {oid} is not a readable blob"])
        size = int(header[2])
        data = out[end + 1:end + 1 + size]
        if len(data) != size:
            raise StageError([f"git object {oid} was truncated"])
        blobs[oid] = data
        pos = end + 1 + size + 1
    return blobs


def _write(dest: Path, files: Sequence[Entry], blobs: dict[str, bytes]) -> dict[str, tuple[int, str]]:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    written = {}
    for entry in files:
        data = blobs[entry.oid]
        target = dest.joinpath(*entry.path.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        with os.fdopen(os.open(target, flags, 0o600), "wb") as fh:
            fh.write(data)
        os.chmod(target, 0o755 if entry.mode == "100755" else 0o644)
        written[entry.path] = (len(data), hashlib.sha256(data).hexdigest())
    return written


def _verify(dest: Path, expected: dict[str, tuple[int, str]]) -> list[str]:
    """Walk the stage without following links; it must hold exactly the manifest."""
    problems, found = [], {}
    for top, dirs, names in os.walk(dest):
        for name in sorted(dirs + names):
            full = Path(top, name)
            rel = full.relative_to(dest).as_posix()
            mode = os.lstat(full).st_mode
            if stat.S_ISLNK(mode):
                problems.append(f"{rel}: symlink in the stage")
            elif stat.S_ISREG(mode):
                data = full.read_bytes()
                found[rel] = (len(data), hashlib.sha256(data).hexdigest())
            elif not stat.S_ISDIR(mode):
                problems.append(f"{rel}: not a regular file in the stage")
    problems += [f"{p}: in the stage but not in the manifest" for p in sorted(found.keys() - expected.keys())]
    problems += [f"{p}: in the manifest but not in the stage" for p in sorted(expected.keys() - found.keys())]
    problems += [f"{p}: differs from the candidate" for p in sorted(found.keys() & expected.keys())
                 if found[p] != expected[p]]
    return problems


def stage(dest: Path | str, *, mode: str, candidate: str = "HEAD", repo: Path | str = REPO,
          require: Sequence[str] = ()) -> dict:
    """Stage the candidate into ``dest`` and return the manifest. Raises StageError on any refusal."""
    repo = Path(repo)
    chosen = plan(repo, candidate, mode, require)
    dest = Path(os.path.abspath(dest))
    _check_dest(_toplevel(repo), dest)
    blobs = _read_blobs(repo, [e.oid for e in chosen.files])
    secret_files = [entry.path for entry in chosen.files if SECRET_CONTENT.search(blobs[entry.oid])]
    if secret_files:
        raise StageError([f"{path}: credential-shaped content" for path in secret_files])
    try:
        with TemporaryDirectory(dir=dest.parent, prefix=f".{dest.name}-") as temp:
            staged = Path(temp) / "snapshot"
            staged.mkdir(mode=0o700)
            written = _write(staged, chosen.files, blobs)
            problems = _verify(staged, written)
            if problems:
                raise StageError(problems)
            os.replace(staged, dest)
    except OSError as exc:
        raise StageError([f"publishing the stage failed: {exc.strerror or exc}"]) from None
    result = {
        "candidate": chosen.candidate,
        "mode": chosen.mode,
        "files": [{"path": e.path, "size": written[e.path][0], "sha256": written[e.path][1]} for e in chosen.files],
    }
    if chosen.mode == "dry":
        result["missing"] = list(chosen.missing)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m agents.eval.deploy.replay_stage",
        description="Stage an allowlisted, tracked-only snapshot of one commit for the public Modal replay.",
    )
    modes = parser.add_subparsers(dest="mode", required=True)
    dry = modes.add_parser("dry", help="rehearsal stage of any commit; reports missing release files")
    dry.add_argument("--candidate", default="HEAD", help="commit name to stage (default HEAD)")
    release = modes.add_parser("release", help="release stage; full SHA and every required file")
    release.add_argument("--candidate", required=True, help="full commit SHA")
    for sub in (dry, release):
        sub.add_argument("--dest", required=True, type=Path, help="new or empty directory outside the repo")
        sub.add_argument("--require", action="append", default=[], metavar="PATH",
                         help="extra allowlisted path that must be tracked (repeatable)")
        sub.add_argument("--repo", type=Path, default=REPO, help="repository to read (default: this checkout)")
    args = parser.parse_args(argv)
    try:
        result = stage(args.dest, mode=args.mode, candidate=args.candidate, repo=args.repo, require=args.require)
    except StageError as exc:
        for problem in exc.problems:
            print(f"replay_stage: refused: {problem}", file=sys.stderr)
        return 1
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
