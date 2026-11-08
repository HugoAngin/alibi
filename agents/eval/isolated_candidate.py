"""Public rehearsal of a pinned adapter with evaluator-side isolation safeguards.

The scored source is an unchanged git archive. Only input field mapping and
cost presentation occur outside it. This CLI never opens the lockbox.
"""

import argparse
from contextlib import redirect_stdout
from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tarfile
from tempfile import TemporaryDirectory
from time import perf_counter

from agents.data.api import load_case
from agents.eval.llm_cost import summarise_log
from agents.eval.run_sealed import score_saved
from agents.eval.sealed_worker import case_payload, failure_rows, _complete_rows, _worker_env
from tools.lockbox import is_open

REPO = Path(__file__).resolve().parents[2]
LIMITS = {"agents": 180, "one_pass": 90, "rules": 30}
BOOTSTRAP = """import sys,json
from contextlib import redirect_stdout
sys.path.insert(0,sys.argv[1])
from alibi.adapter import run_case
case=json.load(sys.stdin)
with redirect_stdout(sys.stderr):
    rows=run_case(case,mode=sys.argv[2])
print(json.dumps(rows,allow_nan=False))
"""


def mapped_payload(case):
    value = deepcopy(case_payload(case))
    if "holder_policy" in case and "privacy_policy" in case and case["holder_policy"] != case["privacy_policy"]:
        raise ValueError("conflicting policy fields")
    if "privacy_policy" in value:
        value["holder_policy"] = value.pop("privacy_policy")
    elif "holder_policy" in case:
        value["holder_policy"] = deepcopy(case["holder_policy"])
    # Recheck the newly accepted alias without ever handing a gold field to the adapter.
    from agents.eval.sealed_worker import _contains_gold
    if _contains_gold(value):
        raise ValueError("nested gold in worker payload")
    return value


def sandbox(denied, write_denied=()):
    exe = shutil.which("sandbox-exec")
    if exe is None:
        raise RuntimeError("verified macOS sandbox is required")
    profile = "(version 1)(allow default)"
    for path in denied:
        profile += f"(deny file-read* file-write* (subpath {json.dumps(str(path.resolve()))}))"
    for path in write_denied:
        profile += f"(deny file-write* (subpath {json.dumps(str(path.resolve()))}))"
    return [exe, "-p", profile]


def normalize_cost(rows, mode):
    rows = deepcopy(rows)
    for row in rows:
        cost = row.setdefault("cost", {})
        if mode != "rules":
            cost.pop("cost_usd_estimate", None)
            row["cost_note"] = "Per-request USD unknown; retained per-case usage is priced separately."
    return rows


def run_isolated(case, mode, *, candidate, private_root, credential, output, source_repo=REPO):
    """Run one case; retain raw predictions and logs before destroying the source copy."""
    if mode not in LIMITS:
        raise ValueError("unknown mode")
    payload = mapped_payload(case)
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    private_root = private_root.resolve(strict=True)
    credential = credential.resolve(strict=True)
    output = output.resolve()
    if output.is_relative_to(private_root) or output.is_relative_to(source_repo.resolve()):
        raise ValueError("capture must be outside the private input root and checkout")
    start = perf_counter()
    with TemporaryDirectory(prefix="alibi-isolated-") as temp:
        work = Path(temp).resolve()
        source, data = work / "source", work / "runtime"
        source.mkdir()
        data.mkdir(mode=0o700)
        archive = work / "candidate.tar"
        with archive.open("wb") as handle:
            subprocess.run(["git", "archive", candidate], cwd=source_repo, stdout=handle, check=True)
        with tarfile.open(archive) as bundle:
            bundle.extractall(source, filter="data")
        archive.unlink()
        denied = (private_root, credential, output.parent, source / "agents/data/out/cases",
                  source / ".env", source_repo / "data", source_repo / "agents/data/out/cases")
        prefix = sandbox(denied, (source_repo, credential.parent))
        # Denial proof opens directories/descriptors only; it never reads protected contents.
        probe = """import os,sys
for p in sys.argv[1:]:
    try:
        fd=os.open(p,os.O_RDONLY)
    except PermissionError:
        continue
    else:
        os.close(fd);sys.exit(1)
"""
        subprocess.run([*prefix, sys.executable, "-c", probe, str(private_root), str(credential), str(output)],
                       cwd=work, env=_worker_env(str(data), True), check=True, capture_output=True, timeout=10)
        command = [*prefix, sys.executable, "-c", BOOTSTRAP, str(source), mode]
        env = _worker_env(str(data), False)
        if mode == "rules":
            env.pop("GEMINI_API_KEY", None)
        with subprocess.Popen(command, cwd=work, env=env, text=True, stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True) as child:
            failure = None
            try:
                stdout, _ = child.communicate(json.dumps(payload), timeout=LIMITS[mode])
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.communicate()
                failure = "timeout"
            if failure is None and child.returncode:
                failure = "error"
            if failure:
                raw = failure_rows(case, mode, failure, "isolated candidate did not complete")
            else:
                try:
                    raw = _complete_rows(json.loads(stdout), case, mode)
                except (ValueError, TypeError):
                    raw = failure_rows(case, mode, "invalid_output", "invalid candidate predictions")
        log = output / "llm_log.jsonl"
        log_paths = sorted(data.rglob("*.jsonl"))
        log_paths = [p for p in log_paths if p.name == "llm_log.jsonl" or p.parent.name == "llm_logs"]
        with log.open("wb") as handle:
            for path in log_paths:
                handle.write(path.read_bytes())
        usage = summarise_log(log) if log.stat().st_size else {"totals": {
            "model_calls": 0 if mode == "rules" and not failure else None,
            "tokens": 0 if mode == "rules" and not failure else None,
            "cost_usd_estimate": 0 if mode == "rules" and not failure else None}}
        # Raw outputs and replay evidence are retained privately; later workers cannot read them.
        replay = source / "alibi/fixtures/replays" / f"case{case['case_number']}_{mode}.json"
        if replay.is_file() and mode != "rules":
            shutil.copyfile(replay, output / "replay.json")
        (output / "raw.json").write_text(json.dumps(raw, indent=2))
        rows = normalize_cost(raw, mode)
        (output / "predictions.json").write_text(json.dumps(rows, indent=2))
        (output / "usage.json").write_text(json.dumps(usage, indent=2))
        metadata = {"candidate": candidate, "case_number": case["case_number"], "system": mode,
                    "wall_seconds": round(perf_counter() - start, 3), "timeout_seconds": LIMITS[mode],
                    "requests": len(rows), "packages": sum(isinstance(r.get("package"), dict) for r in rows),
                    "failures": [r["failure"]["type"] for r in rows if r.get("failure")],
                    "isolation_preflight": "passed", "policy_alias_mapped": "privacy_policy" in case,
                    "usage_log_sha256": sha256(log.read_bytes()).hexdigest()}
        (output / "metadata.json").write_text(json.dumps(metadata, indent=2))
    return rows, metadata


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--private-root", type=Path, required=True)
    parser.add_argument("--credential", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--case", type=int, action="append", required=True)
    parser.add_argument("--mode", choices=list(LIMITS), action="append")
    parser.add_argument("--restriction", action="store_true", help="public variant denying amount_pence")
    args = parser.parse_args()
    if is_open() or any(not 1 <= n <= 80 for n in args.case):
        parser.error("public cases 1–80 only, with lockbox closed")
    os.umask(0o077)
    candidate = subprocess.check_output(["git", "rev-parse", args.candidate + "^{commit}"], text=True).strip()
    args.out = args.out.resolve()
    args.out.mkdir(mode=0o700, parents=True, exist_ok=False)
    cases = [load_case(n) for n in dict.fromkeys(args.case)]
    if args.restriction:
        for case in cases:
            case["privacy_policy"] = [{"rule_id": "public_amount_denial", "type": "deny", "fields": ["amount_pence"]}]
    modes = args.mode or list(LIMITS)
    from alibi.llm.client import load_api_key
    if any(mode != "rules" for mode in modes):
        load_api_key(args.credential)
    records = []
    for mode in modes:
        with (args.out / f"{mode}.jsonl").open("w") as handle:
            for case in cases:
                rows, meta = run_isolated(case, mode, candidate=candidate, private_root=args.private_root,
                                         credential=args.credential, output=args.out / f"{mode}-{case['case_number']}")
                for row in rows:
                    handle.write(json.dumps(row) + "\n")
                handle.flush()
                records.append(meta)
                print(json.dumps(meta), flush=True)
    (args.out / "run.json").write_text(json.dumps({"kind": "public-live", "candidate": candidate,
        "restriction_variant": args.restriction, "limits": LIMITS, "workers": records}, indent=2))
    (args.out / "probes.jsonl").write_text("")
    if set(modes) == set(LIMITS) and not args.restriction:
        print(json.dumps(score_saved(args.out, cases, [], kind="public_live")), flush=True)


if __name__ == "__main__":
    main()
