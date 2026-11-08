"""Resume only the groups authorized by sealed-run rules 7 and 8, once."""

import argparse
from collections import defaultdict
from contextlib import ExitStack
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
from queue import Queue
import shutil
import subprocess
from tempfile import TemporaryDirectory

from alibi.eval import common, seal
from alibi.llm import client
from agents.eval.isolated_candidate import LIMITS, run_isolated
from agents.eval.parallel_capture import schedule
from agents.eval.probe_capture import probe_receipt
from agents.eval.run_sealed import PREDICTION_FILES, _aware_deadline, _digest, _extra_capture_files, _pinned_candidate, score_saved
from agents.eval.sealed_worker import MODES, REPO, failure_rows, _complete_rows, case_payload
from tools.lockbox import is_open

CANDIDATE = "58bf0d4a199f5ad3833fa9bde751d31140219975"
DEADLINE = datetime(2026, 11, 8, 12, 50, tzinfo=timezone.utc)
UNSTARTED = {"type": "unavailable", "detail": "capture_integrity: infrastructure failure stopped new jobs"}
PERMISSION = {"type": "error", "detail": "isolated worker raised PermissionError"}
DISCLOSURE = (
    "This continues the interrupted first capture under lead rules 7 and 8. The original 67 returned "
    "groups, including candidate child failures, are preserved unchanged. Only the 62 originally "
    "unstarted groups and, with verified public proof, the three scheduler PermissionError groups "
    "are eligible. The original operator suspension affected timing and timeout evidence. Early "
    "generic child-exit causes remain unconfirmed and those failures are not retried."
)


def _original(capture):
    record = common.load_json(capture / "run.json")
    required = {*PREDICTION_FILES.values(), "probes.jsonl", "probes.jsonl.receipt.json"}
    if set(record.get("files", {})) != required or record["files"] != {
            name: _digest(capture / name) for name in required}:
        raise ValueError("original capture predictions or probes changed")
    if record.get("extra_files") != _extra_capture_files(capture):
        raise ValueError("original worker evidence changed")
    signature = record["input"]
    if (signature.get("commit") != CANDIDATE or signature.get("candidate") != CANDIDATE
            or signature.get("workers") != 4 or signature.get("limits") != LIMITS
            or _aware_deadline(signature["deadline"]) != DEADLINE):
        raise ValueError("original candidate, limits, workers or deadline do not match authorization")
    probe_rows = common.read_jsonl(capture / "probes.jsonl")
    if common.load_json(capture / "probes.jsonl.receipt.json") != probe_receipt(CANDIDATE, probe_rows):
        raise ValueError("original probe receipt differs from the candidate")
    scheduling = common.load_json(capture / "scheduling.json")
    aggregate = scheduling["aggregate"]
    for key, value in dict(total=132, attempted=70, completed=67, errors=3, not_started=62,
                           aborted=True, abort_reason="PermissionError").items():
        if aggregate.get(key) != value:
            raise ValueError("original scheduling counts do not match authorized interruption")
    return record, scheduling


def select_groups(capture, cases):
    """Return exact original bytes plus narrowly classified case/system groups."""
    expected = {(case["case_number"], rfc["request_id"]) for case in cases for rfc in case["rfcs"]}
    raw, groups = {}, {}
    selected = {"unstarted": set(), "permission": set(), "preserved": set()}
    for mode, filename in PREDICTION_FILES.items():
        raw[mode] = (capture / filename).read_bytes().splitlines(keepends=True)
        by_case = defaultdict(list)
        seen = set()
        for line in raw[mode]:
            row = json.loads(line)
            key = (row["case_number"], row["request_id"])
            if key in seen or key not in expected or row.get("system", mode) != mode:
                raise ValueError("original rows do not match the complete request set")
            seen.add(key)
            by_case[row["case_number"]].append(row)
        if seen != expected:
            raise ValueError("original capture omitted request denominator rows")
        for number, rows in by_case.items():
            key = (mode, number)
            groups[key] = rows
            kind = ("unstarted" if all(row.get("failure") == UNSTARTED for row in rows) else
                    "permission" if all(row.get("failure") == PERMISSION for row in rows) else "preserved")
            selected[kind].add(key)
    if {key: len(value) for key, value in selected.items()} != dict(unstarted=62, permission=3, preserved=67):
        raise ValueError("authorized 62/3/67 group classification does not match original rows")
    return raw, groups, selected


def _proof(path, digest):
    if path is None and digest is None:
        return None
    if path is None or digest is None or _digest(path) != digest:
        raise ValueError("public PermissionError proof requires the matching SHA256")
    value = common.load_json(path)
    if (value.get("candidate_commit") != CANDIDATE or
            any(value.get(key) is not True for key in
                ("public_only", "permission_error_reproduced", "fix_verified"))):
        raise ValueError("public PermissionError proof does not establish the authorized fix")
    return digest


def _retained_files(output):
    return {str(path.relative_to(output)): _digest(path) for path in sorted(output.rglob("*"))
            if path.is_file() and path != output / "run.json"}


def _replay(output, original_hash):
    record = common.load_json(output / "run.json")
    if (record["input"].get("original_run_sha256") != original_hash
            or record.get("files") != _retained_files(output)
            or common.load_json(output / "resume-lock.json") != {"original_run_sha256": original_hash}):
        raise ValueError("retained resume evidence changed")
    return common.load_json(output / "metrics.json")


def _capture(output, capture, cases, selected, original_groups, *, private_root, credential, deadline):
    eligible = selected["eligible"]
    replacements = {}
    not_started = set()
    system_counts = {mode: dict(attempted=0, completed=0, failed=0, not_started=0) for mode in MODES}
    with ExitStack() as stack:
        roots = [Path(stack.enter_context(TemporaryDirectory(prefix="alibi-resume-lane-"))) for _ in range(4)]
        lanes = Queue()
        for root in roots:
            lanes.put(root)
        destinations = {mode: stack.enter_context((output / f"{mode}.attempts.jsonl").open("x")) for mode in MODES}

        def run(job):
            mode, case = job
            lane = lanes.get()
            try:
                rows, metadata = run_isolated(case, mode, candidate=CANDIDATE, private_root=private_root,
                    credential=credential, output=output / "capture-workers" / f"{mode}-{case['case_number']}",
                    scratch_parent=lane, denied_scratch=tuple(root for root in roots if root != lane),
                    capture_root=capture, deadline=deadline)
                return _complete_rows(rows, case, mode), metadata
            finally:
                lanes.put(lane)

        def progress(outcome, aggregate):
            mode, case = outcome["job"]
            counts = system_counts[mode]
            if outcome["status"] == "not_started":
                counts["not_started"] += 1
                key = (mode, case["case_number"])
                not_started.add(key)
                rows = (original_groups[key] if key in selected["permission"] else
                        failure_rows(case, mode, "unavailable", "resume stopped: " + outcome["reason"]))
            else:
                counts["attempted"] += 1
                rows = (outcome["result"][0] if outcome["status"] == "completed" else
                        failure_rows(case, mode, "error", "isolated worker raised " + outcome["error"]))
                counts["failed" if any(row.get("failure") for row in rows) else "completed"] += 1
            replacements[(mode, case["case_number"])] = rows
            for row in rows:
                destinations[mode].write(json.dumps(row, allow_nan=False) + "\n")
            destinations[mode].flush()
            summary = {"aggregate": aggregate, "systems": system_counts}
            common.write_json(output / "progress.json", summary)
            print(json.dumps({"resume_progress": summary}, sort_keys=True), flush=True)

        jobs = [(mode, case) for case in cases for mode in MODES if (mode, case["case_number"]) in eligible]
        result = schedule(jobs, run, workers=4, deadline=deadline, progress=progress, stop_on_error=True)
    summary = {"aggregate": result["metadata"], "systems": system_counts, "limits": LIMITS}
    common.write_json(output / "resume-scheduling.json", summary)
    return replacements, summary, not_started


def combine(output, capture, raw, replacements, preserved):
    """Preserved RFC lines are copied byte-for-byte, including original failures."""
    for mode, filename in PREDICTION_FILES.items():
        emitted = set()
        with (output / filename).open("xb") as handle:
            for line in raw[mode]:
                row = json.loads(line)
                key = (mode, row["case_number"])
                if key not in replacements:
                    handle.write(line)
                elif key not in emitted:
                    for replacement in replacements[key]:
                        handle.write((json.dumps(replacement, allow_nan=False) + "\n").encode())
                    emitted.add(key)
    for mode, number in preserved:
        original = capture / "capture-workers" / f"{mode}-{number}"
        if original.is_dir():
            shutil.copytree(original, output / "capture-workers" / original.name)
    for name in ("probes.jsonl", "probes.jsonl.receipt.json"):
        shutil.copyfile(capture / name, output / name)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", type=Path, required=True)
    for name in ("cases", "gold", "manifest", "model-config", "private-root", "permission-proof"):
        parser.add_argument("--" + name, type=Path)
    parser.add_argument("--permission-proof-sha256")
    args = parser.parse_args(argv)
    os.umask(0o077)
    capture = args.capture.resolve(strict=True)
    original, original_schedule = _original(capture)
    original_hash = _digest(capture / "run.json")
    output = capture / "resume"
    if output.exists():
        if not (output / "run.json").is_file():
            raise RuntimeError("resume already started but is incomplete; preserve it, never relaunch")
        print(json.dumps(_replay(output, original_hash), sort_keys=True, allow_nan=False))
        return
    if not is_open():
        parser.error("use authorized tools/results.py add --split lockbox --reason")
    if not all((args.cases, args.gold, args.manifest, args.model_config, args.private_root)):
        parser.error("new resume requires original cases, gold, manifest, model config and private root")
    tagged, evaluator = _pinned_candidate("sealed-candidate-3")
    if tagged != CANDIDATE:
        raise ValueError("candidate tag changed")
    root, inputs, gold_path = (path.resolve(strict=True) for path in (args.private_root, args.cases, args.gold))
    if not inputs.is_relative_to(root) or not gold_path.is_relative_to(root) or capture.is_relative_to(root):
        raise ValueError("private root must contain original inputs and gold, excluding capture")
    manifest = common.load_json(args.manifest)
    if (_digest(args.manifest) != original["input"]["manifest"] or
            manifest.get("purpose") != "sealed" or manifest["candidate"]["commit"] != CANDIDATE):
        raise ValueError("original manifest or candidate changed")
    problems, _ = seal.verify_manifest(args.manifest, inputs, gold_path, args.model_config)
    if problems:
        raise ValueError("original sealed commitments failed verification")
    lock = gold_path.parent / f"capture-{manifest['set_sha256']}.json"
    if common.load_json(lock) != {"run": str(capture)}:
        raise ValueError("original exclusive capture lock differs")
    proof_sha = _proof(args.permission_proof, args.permission_proof_sha256)
    cases = common.load_sealed_cases(inputs, require_gold=False)
    for case in cases:
        case_payload(case)
    raw, groups, selected = select_groups(capture, cases)
    selected["eligible"] = selected["unstarted"] | (selected["permission"] if proof_sha else set())
    deadline = _aware_deadline(original["input"]["deadline"])
    if datetime.now(timezone.utc) >= deadline:
        raise ValueError("original capture deadline has passed")
    credential = Path(os.environ["ALIBI_EVAL_ENV_FILE"]).resolve(strict=True)
    client.load_api_key(credential)
    signature = dict(original_input=original["input"], candidate=CANDIDATE, commit=CANDIDATE,
                     evaluator_commit=evaluator, original_run_sha256=original_hash,
                     manifest=_digest(args.manifest), model_config=_digest(args.model_config),
                     workers=4, limits=LIMITS, deadline=deadline.isoformat(), permission_proof_sha256=proof_sha)
    output.mkdir(mode=0o700)
    with (output / "resume-lock.json").open("x") as handle:
        json.dump({"original_run_sha256": original_hash}, handle)
    common.write_json(output / "authorization.json", {"input": signature, "rules": [7, 8],
        "original_returned_preserved": 67, "original_unstarted": 62,
        "permission_errors_authorized": 3 if proof_sha else 0, "disclosure": DISCLOSURE})
    if proof_sha:
        shutil.copyfile(args.permission_proof, output / "permission-proof.json")
    replacements, resumed, not_started = _capture(output, capture, cases, selected, groups,
                                     private_root=root, credential=credential, deadline=deadline)
    preserved = set(groups) - selected["eligible"]
    if set(replacements) != selected["eligible"] or not selected["preserved"] <= preserved:
        raise ValueError("resume outcomes do not preserve the authorized denominator")
    combine(output, capture, raw, replacements, preserved)
    remaining_unstarted = len(not_started & selected["unstarted"])
    combined = {"aggregate": dict(total=132, attempted=132 - remaining_unstarted,
        completed=67 + resumed["aggregate"]["completed"],
        errors=132 - remaining_unstarted - 67 - resumed["aggregate"]["completed"],
        not_started=remaining_unstarted, initial_workers=4,
        final_workers=resumed["aggregate"]["final_workers"],
        aborted=resumed["aggregate"]["aborted"], deadline_reached=resumed["aggregate"]["deadline_reached"]),
        "systems": {}, "resume": resumed, "original": original_schedule, "limits": LIMITS}
    for mode in MODES:
        rows = common.read_jsonl(output / PREDICTION_FILES[mode])
        grouped = defaultdict(list)
        for row in rows:
            grouped[row["case_number"]].append(row)
        unstarted = sum((mode, number) in not_started & selected["unstarted"] for number in grouped)
        failed = sum(any(r.get("failure") for r in values) for values in grouped.values()) - unstarted
        combined["systems"][mode] = dict(attempted=len(grouped)-unstarted,
            completed=len(grouped)-unstarted-failed, failed=failed, not_started=unstarted)
    common.write_json(output / "scheduling.json", combined)
    _original(capture)
    problems, _ = seal.verify_manifest(args.manifest, inputs, gold_path, args.model_config)
    if problems or _digest(args.manifest) != signature["manifest"]:
        raise ValueError("sealed commitments changed during resume")
    # Only the coordinator reads gold, after all workers have stopped.
    gold = common.read_gold_file(gold_path)
    scored = [{**case, "gold": gold[case["case_number"]]} for case in cases]
    attacks = {case["case_number"] for case in common.read_jsonl(inputs / "adversarial.jsonl")}
    metrics = score_saved(output, [c for c in scored if c["case_number"] not in attacks],
                          [c for c in scored if c["case_number"] in attacks], kind="frozen")
    metrics.update(resume_original_returned_preserved=67, resume_original_unstarted=62,
                   resume_permission_errors_authorized=3 if proof_sha else 0,
                   resume_attempted=resumed["aggregate"]["attempted"],
                   resume_completed=resumed["aggregate"]["completed"],
                   resume_not_started=resumed["aggregate"]["not_started"])
    for filename in ("report.md", "deck-table.md"):
        path = output / filename
        path.write_text(DISCLOSURE + "\n\n" + path.read_text())
    common.write_json(output / "metrics.json", metrics)
    files = _retained_files(output)
    temporary = output / "run.tmp"
    common.write_json(temporary, {"input": signature, "files": files})
    temporary.replace(output / "run.json")
    print(json.dumps(metrics, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
