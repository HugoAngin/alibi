"""Seal the evaluation set: write the commitments before the frozen run, verify them after.

    uv run --frozen python -m alibi.eval.seal write --cases data/sealed --gold data/sealed_gold.jsonl \\
        --candidate HEAD --model-config data/model_config.json --purpose sealed --frozen-by eval \\
        --systems rules one_pass alibi --out data/sealed_manifest.json
    uv run --frozen python -m alibi.eval.seal verify data/sealed_manifest.json
    uv run --frozen python -m alibi.eval.seal verify data/sealed_manifest.json --cases ... --gold ...

Sealed cases are JSONL files (one case object per line, without gold); gold is the separate
evaluator-only file read by ``alibi.eval.common.read_gold_file``. The manifest records a
sha256 per case and per gold entry over canonical JSON (sorted keys, no whitespace, UTF-8),
so a checkout with different line endings verifies the same while any changed value does not.
It also records the candidate commit, the model configuration and a digest of the scoring
rules (scorer, helpers, prediction schema). This tool never prints case content.
"""
from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from alibi.eval import jsonschema_lite
from alibi.eval.common import (EVAL_DIR, REPO_ROOT, EvalError, canonical_sha256, load_json,
                               load_schema, read_gold_file, read_jsonl, sealed_files, write_json)
from alibi.eval.scorer import SCORING_RULES_VERSION

MANIFEST_SCHEMA_VERSION = "alibi.eval.manifest.v2"
SCORING_FILES = ("scorer.py", "common.py", "jsonschema_lite.py", "schemas/prediction.schema.json")
SCORING_COMMAND = ("uv run --frozen python -m alibi.eval.scorer --sealed <cases> --gold <gold> "
                   "--predictions <files> --names <names> --probe-results <probes>")


def text_sha256(paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.name.encode("utf-8") + b"\0")
        digest.update(path.read_text(encoding="utf-8").replace("\r\n", "\n").encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def scoring_sha256() -> str:
    return text_sha256([EVAL_DIR / name for name in SCORING_FILES])


def case_entries(path: Path) -> tuple[list[dict], list[dict]]:
    """Per-file and per-case digests of the sealed cases at ``path``."""
    files, entries = [], []
    seen: set[int] = set()
    for file in sealed_files(path):
        rows = read_jsonl(file)
        for index, row in enumerate(rows, 1):
            if not isinstance(row, dict) or not isinstance(row.get("case_number"), int):
                raise EvalError(f"{file}:{index}: not a case object with an integer case_number")
            number = row["case_number"]
            if number in seen:
                raise EvalError(f"{file}:{index}: case {number} appears twice")
            seen.add(number)
            if "gold" in row:
                raise EvalError(f"{file}:{index}: case {number} carries gold; sealed cases must not")
            entries.append({"case_number": number, "family": row.get("family"),
                            "request_ids": [r.get("request_id") for r in row.get("rfcs") or []],
                            "sha256": canonical_sha256(row)})
        files.append({"file": file.name, "cases": len(rows), "sha256": canonical_sha256(rows)})
    if not entries:
        raise EvalError(f"no sealed cases found at {path}")
    entries.sort(key=lambda e: e["case_number"])
    return files, entries


def gold_entries(path: Path) -> tuple[str, list[dict]]:
    gold = read_gold_file(path)
    entries = [{"case_number": number, "sha256": canonical_sha256(gold[number])}
               for number in sorted(gold)]
    return canonical_sha256({str(k): v for k, v in gold.items()}), entries


def model_config_entry(path: Path | None) -> dict:
    content = load_json(path) if path else {}
    if not isinstance(content, dict):
        raise EvalError(f"{path}: model configuration must be a JSON object")
    return {"path": str(path) if path else None, "sha256": canonical_sha256(content),
            "content": content}


def git_commit(ref: str) -> tuple[str, bool]:
    done = subprocess.run(["git", "rev-parse", ref], cwd=REPO_ROOT, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if done.returncode:
        raise EvalError(f"git rev-parse {ref} failed: {done.stderr.strip()}")
    status = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=REPO_ROOT,
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
    return done.stdout.strip(), bool(status.stdout.strip())


def set_sha256(cases: list[dict], gold: list[dict]) -> str:
    parts = sorted([c["sha256"] for c in cases] + [g["sha256"] for g in gold])
    return hashlib.sha256("\n".join(parts).encode("ascii")).hexdigest()


def build_manifest(cases_path: Path, gold_path: Path, *, candidate: str = "HEAD",
                   model_config: Path | None = None, purpose: str = "sealed", frozen_by: str = "",
                   note: str = "", systems: list[str] | None = None, description: str = "",
                   manifest_id: str | None = None) -> dict:
    files, cases = case_entries(cases_path)
    gold_sha, gold = gold_entries(gold_path)
    numbers = {c["case_number"] for c in cases}
    gold_numbers = {g["case_number"] for g in gold}
    if numbers != gold_numbers:
        raise EvalError(f"cases and gold differ: cases without gold {sorted(numbers - gold_numbers)}, "
                        f"gold without cases {sorted(gold_numbers - numbers)}")
    commit, dirty = git_commit(candidate)
    now = datetime.now(timezone.utc)
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "manifest_id": manifest_id or f"{purpose}-{now:%Y%m%dT%H%M%SZ}",
        "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "purpose": purpose,
        "frozen_by": frozen_by,
        "note": note,
        "cases": {"path": str(cases_path), "files": files, "entries": cases},
        "gold": {"path": str(gold_path), "sha256": gold_sha, "entries": gold},
        "set_sha256": set_sha256(cases, gold),
        "candidate": {"commit": commit, "dirty": dirty, "description": description,
                      "systems": systems or []},
        "model_config": model_config_entry(model_config),
        "scoring": {"rules_version": SCORING_RULES_VERSION, "sha256": scoring_sha256(),
                    "files": list(SCORING_FILES), "command": SCORING_COMMAND},
    }
    problems = jsonschema_lite.errors(load_schema("manifest"), manifest)
    if problems:
        raise EvalError("manifest does not match its schema: " + "; ".join(problems))
    return manifest


def verify_manifest(path: Path, cases_path: Path | None = None, gold_path: Path | None = None,
                    model_config: Path | None = None) -> tuple[list[str], list[str]]:
    """Recompute every commitment. Returns (problems, notes); verification passes when
    problems is empty."""
    manifest = load_json(path)
    problems = [f"manifest schema: {p}" for p in jsonschema_lite.errors(load_schema("manifest"), manifest)]
    if problems:
        return problems, []
    notes: list[str] = []
    cases_path = cases_path or Path(manifest["cases"]["path"])
    gold_path = gold_path or Path(manifest["gold"]["path"])
    try:
        _files, now_cases = case_entries(cases_path)
    except EvalError as exc:
        return [f"cases: {exc}"], notes
    sealed = {c["case_number"]: c for c in manifest["cases"]["entries"]}
    current = {c["case_number"]: c for c in now_cases}
    for number, entry in sealed.items():
        found = current.get(number)
        if found is None:
            problems.append(f"case {number}: not found under {cases_path}")
        elif found["sha256"] != entry["sha256"]:
            problems.append(f"case {number}: content changed since sealing")
        elif found["request_ids"] != entry["request_ids"]:
            problems.append(f"case {number}: request ids changed since sealing")
    extra = sorted(set(current) - set(sealed))
    if extra:
        problems.append(f"cases present but not sealed: {extra}")
    try:
        gold_sha, now_gold = gold_entries(gold_path)
    except EvalError as exc:
        problems.append(f"gold: {exc}")
        now_gold, gold_sha = [], ""
    sealed_gold = {g["case_number"]: g["sha256"] for g in manifest["gold"]["entries"]}
    current_gold = {g["case_number"]: g["sha256"] for g in now_gold}
    for number, digest in sealed_gold.items():
        if number not in current_gold:
            problems.append(f"gold for case {number}: not found in {gold_path}")
        elif current_gold[number] != digest:
            problems.append(f"gold for case {number}: changed since sealing")
    extra_gold = sorted(set(current_gold) - set(sealed_gold))
    if extra_gold:
        problems.append(f"gold present but not sealed: {extra_gold}")
    if now_gold and gold_sha != manifest["gold"]["sha256"]:
        problems.append("gold file digest changed since sealing")
    if set_sha256(manifest["cases"]["entries"], manifest["gold"]["entries"]) != manifest["set_sha256"]:
        problems.append("set_sha256 does not match the listed digests")
    if manifest["scoring"]["sha256"] != scoring_sha256():
        problems.append("scoring rules changed since sealing (scorer, helpers or prediction schema); "
                        "disclose the change and its reason")
    config_path = model_config or (Path(manifest["model_config"]["path"])
                                   if manifest["model_config"].get("path") else None)
    if config_path is not None:
        try:
            if model_config_entry(config_path)["sha256"] != manifest["model_config"]["sha256"]:
                problems.append("model configuration changed since sealing")
        except EvalError as exc:
            problems.append(f"model configuration: {exc}")
    else:
        notes.append("no model configuration file to recheck")
    try:
        head, dirty = git_commit("HEAD")
        if head != manifest["candidate"]["commit"]:
            notes.append(f"HEAD {head[:12]} differs from the sealed candidate "
                         f"{manifest['candidate']['commit'][:12]}; score the candidate commit")
        if dirty:
            notes.append("working tree has uncommitted tracked changes")
    except EvalError as exc:
        notes.append(str(exc))
    return problems, notes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    write = commands.add_parser("write", help="write a manifest of commitments")
    write.add_argument("--cases", type=Path, required=True, help="sealed .jsonl file or directory")
    write.add_argument("--gold", type=Path, required=True, help="evaluator-only gold file")
    write.add_argument("--candidate", default="HEAD", help="git ref of the frozen candidate")
    write.add_argument("--model-config", type=Path, help="JSON with model IDs, thinking levels, caps")
    write.add_argument("--purpose", choices=["sealed", "adversarial", "dev"], default="sealed")
    write.add_argument("--frozen-by", default="")
    write.add_argument("--systems", nargs="*", help="frozen systems to run, e.g. rules one_pass alibi")
    write.add_argument("--description", default="", help="what the candidate is")
    write.add_argument("--note", default="")
    write.add_argument("--manifest-id")
    write.add_argument("--out", type=Path, required=True)
    verify = commands.add_parser("verify", help="recompute the commitments and compare")
    verify.add_argument("manifest", type=Path)
    verify.add_argument("--cases", type=Path)
    verify.add_argument("--gold", type=Path)
    verify.add_argument("--model-config", type=Path)
    args = parser.parse_args(argv)

    try:
        if args.command == "write":
            manifest = build_manifest(args.cases, args.gold, candidate=args.candidate,
                                      model_config=args.model_config, purpose=args.purpose,
                                      frozen_by=args.frozen_by, note=args.note, systems=args.systems,
                                      description=args.description, manifest_id=args.manifest_id)
            write_json(args.out, manifest)
            print(f"seal: wrote {args.out}: {len(manifest['cases']['entries'])} cases, "
                  f"{len(manifest['gold']['entries'])} gold entries, candidate "
                  f"{manifest['candidate']['commit'][:12]}{' (dirty)' if manifest['candidate']['dirty'] else ''}, "
                  f"set {manifest['set_sha256'][:12]}")
            if not args.model_config:
                print("seal: no --model-config given; add one before the frozen run", file=sys.stderr)
            return 0
        problems, notes = verify_manifest(args.manifest, args.cases, args.gold, args.model_config)
    except EvalError as exc:
        print(f"seal: {exc}", file=sys.stderr)
        return 2
    for note in notes:
        print(f"seal: note: {note}")
    for problem in problems:
        print(f"seal: {problem}")
    print("seal: " + ("verified" if not problems else f"{len(problems)} problem(s)"))
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
