"""Run the frozen Alibi comparison once and replay scoring from retained predictions."""

import argparse
from collections import Counter
from contextlib import ExitStack
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
from queue import Queue
import subprocess
from tempfile import TemporaryDirectory

from agents.data.api import load_case, load_cases
from agents.eval.freeze_pack import _private_path
from agents.eval.capture_reporting import summarize_capture
from agents.eval.headline import summarize as headline_evidence
from agents.eval.probe_capture import missing_demo_probe_kinds, probe_receipt, probe_receipt_path
from agents.eval.score_capture import score_capture
from agents.eval.sealed_worker import MODES, REPO, case_payload, failure_rows, run_case_worker, verify_adapter, verify_file_isolation
from agents.execution.api import probe_ids
from alibi.eval import common, scorer, seal
from alibi.llm import client
from tools.lockbox import is_open

PREDICTION_FILES = {mode: f"{mode}.jsonl" for mode in MODES}
PROBE_FILE = "probes.jsonl"
PROBE_RECEIPT = probe_receipt_path(Path(PROBE_FILE)).name
REPORT_HEADINGS = {
    "public_rehearsal": "Public rehearsal, stubbed model and drafts only",
    "public_live": "Public live adapter check",
    "frozen": "Frozen comparison",
}
SP47_SCOPE = (
    "SP-47 covers the passport store only. In the S4/S2 rehearsal, the app's "
    "`case_N.state.json` and the mock bank's `state.json` held delivered packages in plaintext."
)


def _digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _probe_rows(path: Path | None) -> list[dict]:
    if path is None:
        return []
    rows = common.read_jsonl(path)
    ids = {row["probe_id"] for row in rows}
    if ids != set(probe_ids()) or len(ids) != len(rows):
        raise ValueError("probe evidence needs one outcome for each catalogue ID")
    if any(row["outcome"] not in ("blocked", "not_blocked", "not_run", "error") for row in rows):
        raise ValueError("probe evidence contains an invalid outcome")
    return rows


def _extra_capture_files(output: Path) -> dict:
    paths = [output / "scheduling.json", output / "progress.json"]
    paths += sorted(path for path in (output / "capture-workers").rglob("*") if path.is_file())
    paths += sorted(path for path in (output / "checkpoint").rglob("*") if path.is_file())
    return {str(path.relative_to(output)): _digest(path) for path in paths}


def _parallel_capture(output: Path, cases: list[dict], isolation: dict, signature: dict | None = None) -> None:
    # Imported only after module initialization: isolated_candidate also uses score_saved.
    from agents.eval.isolated_candidate import LIMITS, run_isolated
    from agents.eval.parallel_capture import schedule

    systems = {mode: dict(attempted=0, completed=0, failed=0, not_started=0) for mode in MODES}
    checkpoint_written = False
    completed_workers = set()
    with ExitStack() as stack:
        roots = [Path(stack.enter_context(TemporaryDirectory(prefix="alibi-capture-lane-")))
                 for _ in range(isolation["workers"])]
        lanes = Queue()
        for root in roots:
            lanes.put(root)
        destinations = {mode: stack.enter_context((output / filename).open("x", encoding="utf-8"))
                        for mode, filename in PREDICTION_FILES.items()}

        def run_job(job):
            mode, case = job
            lane = lanes.get()
            try:
                return run_isolated(
                    case, mode, candidate=isolation["candidate"], private_root=isolation["private_root"],
                    credential=isolation["credential"],
                    output=output / "capture-workers" / f"{mode}-{case['case_number']}",
                    scratch_parent=lane, denied_scratch=tuple(root for root in roots if root != lane),
                    capture_root=output, deadline=isolation["deadline"],
                )
            finally:
                lanes.put(lane)

        def maybe_checkpoint(aggregate):
            nonlocal checkpoint_written
            checkpoint_at = isolation.get("checkpoint_at")
            finished = aggregate["completed"] + aggregate["errors"] + aggregate["not_started"]
            if (checkpoint_at is not None and not checkpoint_written and finished < aggregate["total"]
                    and datetime.now(timezone.utc) >= checkpoint_at):
                from agents.eval.capture_checkpoint import create_checkpoint
                for destination in destinations.values():
                    destination.flush()
                create_checkpoint(output, cases, gold_path=isolation["gold_path"],
                                  adversarial_ids=isolation["adversarial_ids"], signature=signature or {},
                                  scheduling={"aggregate": aggregate, "systems": systems, "limits": dict(LIMITS)},
                                  completed_workers=completed_workers)
                checkpoint_written = True
                print(json.dumps({"checkpoint_saved": True, "incomplete": True,
                                  "full_denominator": True}), flush=True)

        def progress(outcome, aggregate):
            mode, case = outcome["job"]
            counts = systems[mode]
            if outcome["status"] == "not_started":
                counts["not_started"] += 1
                detail = ("capture_integrity: infrastructure failure stopped new jobs"
                          if outcome["reason"] == "capture_integrity" else "deadline reached before job started")
                rows = failure_rows(case, mode, "unavailable", detail)
            else:
                counts["attempted"] += 1
                if outcome["status"] == "error":
                    rows = failure_rows(case, mode, "error", "isolated worker raised " + outcome["error"])
                else:
                    rows, _metadata = outcome["result"]
                    completed_workers.add(f"{mode}-{case['case_number']}")
                counts["failed" if any(row.get("failure") for row in rows) else "completed"] += 1
            for row in rows:
                destinations[mode].write(json.dumps(row, allow_nan=False) + "\n")
            destinations[mode].flush()
            snapshot = {"aggregate": aggregate, "systems": systems}
            common.write_json(output / "progress.json", snapshot)
            print(json.dumps({"capture_progress": snapshot}, sort_keys=True), flush=True)
            maybe_checkpoint(aggregate)

        jobs = [(mode, case) for case in cases for mode in MODES]
        result = schedule(jobs, run_job, workers=isolation["workers"],
                          deadline=isolation["deadline"], progress=progress, stop_on_error=True,
                          heartbeat=maybe_checkpoint if isolation.get("checkpoint_at") is not None else None)
        summary = {"aggregate": result["metadata"], "systems": systems, "limits": LIMITS}
        common.write_json(output / "progress.json", summary)
        common.write_json(output / "scheduling.json", summary)


def _capture_once(output: Path, cases: list[dict], signature: dict, *, dry: bool, timeout: float,
                  blocked: tuple[Path, ...], probes: Path | None, lock: Path | None = None,
                  isolation: dict | None = None) -> None:
    files = (*PREDICTION_FILES.values(), PROBE_FILE)
    frozen = not dry and lock is not None
    if frozen:
        files += (PROBE_RECEIPT,)
    marker = output / "run.json"
    if output.exists():
        if lock is not None and (not lock.is_file() or common.load_json(lock) != {"run": str(output)}):
            raise RuntimeError("the frozen manifest is bound to a different capture directory")
        if not marker.is_file():
            raise RuntimeError("prior capture is incomplete; preserve its evidence and report the interruption")
        record = common.load_json(marker)
        if record.get("input") != signature or record.get("files") != {name: _digest(output / name) for name in files}:
            raise RuntimeError("retained capture or its inputs changed")
        if isolation is not None and record.get("extra_files") != _extra_capture_files(output):
            raise RuntimeError("retained worker evidence changed")
        return

    if isolation is not None and isolation["deadline"] <= datetime.now(timezone.utc):
        raise ValueError("capture deadline has already passed")
    probe_rows = _probe_rows(probes)
    if frozen:
        if probes is None or not probe_rows or not probe_receipt_path(probes).is_file():
            raise ValueError("capture the full probe catalogue with --candidate-evidence before the frozen run")
        receipt = common.load_json(probe_receipt_path(probes))
        if receipt != probe_receipt(signature["commit"], probe_rows):
            raise ValueError("probe evidence does not match the frozen candidate or recorded outcomes")
        missing = missing_demo_probe_kinds({row["probe_id"]: row["outcome"] for row in probe_rows})
        if missing:
            raise ValueError(f"candidate evidence needs definite demo outcomes for {', '.join(sorted(missing))}")
    if not dry:
        env_source = isolation["credential"] if isolation is not None else os.environ.get("ALIBI_EVAL_ENV_FILE")
        if env_source:
            credential_file = Path(env_source).resolve()
            client.load_api_key(credential_file)
            blocked = (*blocked, credential_file)
        else:
            client.load_api_key()
        if isolation is None:
            verify_adapter(blocked)
    for case in cases:
        case_payload(case)
    if lock is not None:
        try:
            with lock.open("x", encoding="utf-8") as handle:
                json.dump({"run": str(output)}, handle)
        except FileExistsError as exc:
            raise RuntimeError("a capture already started for this manifest; preserve and report its evidence") from exc
        lock.chmod(0o600)
    output.mkdir(mode=0o700)
    common.write_jsonl(output / PROBE_FILE, probe_rows)
    if frozen:
        common.write_json(output / PROBE_RECEIPT, receipt)
    if isolation is not None:
        _parallel_capture(output, cases, isolation, signature)
    else:
        for mode, filename in PREDICTION_FILES.items():
            with (output / filename).open("x", encoding="utf-8") as destination:
                for case in cases:
                    rows = run_case_worker(case, mode, dry_run=dry, timeout=timeout, blocked=blocked)
                    for row in rows:
                        destination.write(json.dumps(row, allow_nan=False) + "\n")
                    destination.flush()
    temporary = output / "run.tmp"
    record = {"input": signature, "files": {name: _digest(output / name) for name in files}}
    if isolation is not None:
        record["extra_files"] = _extra_capture_files(output)
    common.write_json(temporary, record)
    temporary.replace(marker)


def _gold_rows(cases: list[dict]) -> list[dict]:
    rows = []
    for case in cases:
        gold = case["gold"]
        for rfc in case["rfcs"]:
            request_id = rfc["request_id"]
            if "request_checks" in gold:
                rows.append(gold["request_checks"][request_id])
            else:
                rows.append({"case_number": case["case_number"], "request_id": request_id,
                             "answers": gold["answers"][request_id], "links": gold.get("links", []),
                             "forbidden_refs": gold.get("forbidden_refs", [])})
    return rows


def _independent_rows(cases: list[dict], system: scorer.System) -> list[dict]:
    rows = []
    for case in cases:
        versioned = "request_checks" in case["gold"]
        for rfc in case["rfcs"]:
            key = (case["case_number"], rfc["request_id"])
            pred = system.predictions.get(key)
            if not pred or pred["failure"]:
                continue
            package = pred["package"]
            claims = scorer.claims_by_question(pred)
            observed = common.observable(case, rfc["issued_at"]) if versioned else {}
            revisions = {}
            for attachment in (package or {}).get("attachments", []):
                revision = (attachment.get("content") or {}).get("revision")
                if type(revision) is int and revision > 0:
                    revisions[attachment["ref"]] = revision

            def reference(ref):
                if not versioned or ref not in observed:
                    return ref
                revision = revisions.get(ref, common.active_version(observed[ref]).get("revision", 1))
                return {"source_id": ref, "revision": revision}

            refs = {item["ref"] for item in (package or {}).get("attachments", [])}
            refs.update(ref for claim in claims.values() for ref in claim["evidence_refs"])
            audience = (package or {}).get("audience", rfc["audience"])
            if package and package.get("request_id", key[1]) != key[1]:
                continue
            rows.append({
                "case_number": key[0], "request_id": key[1], "audience": audience,
                "answers": {qid: {**claim, "evidence_refs": list(map(reference, claim["evidence_refs"]))}
                            for qid, claim in claims.items()},
                "disclosed_refs": [reference(ref) for ref in sorted(refs)],
                "package_body": None if package is None and not refs else {**(package or {}), "claims": claims},
                "links": pred.get("links", []),
            })
    return rows


def _measured_costs(score: scorer.SystemScore) -> None:
    rows = [request.cost for request in score.requests]
    for key in ("model_calls", "tokens", "cost_usd_estimate"):
        if any(key not in row and (key == "model_calls" or row.get("model_calls") != 0) for row in rows):
            score.metrics[key] = None
    if score.metrics["cost_usd_estimate"] is None:
        score.notes = [note for note in score.notes if "cost_usd_estimate is reported as 0" not in note]
        score.notes.append(f"{score.system.name}: cost is unknown because some requests lack complete measurements.")


def score_saved(output: Path, cases: list[dict], adversarial: list[dict], *, kind: str) -> dict:
    systems = [scorer.load_predictions(output / filename, mode) for mode, filename in PREDICTION_FILES.items()]
    retained = summarize_capture(output, systems)
    probe_file = output / PROBE_FILE
    probe_rows = common.read_jsonl(probe_file)
    outcomes = scorer.load_probe_results(probe_file)
    probes = scorer.probe_metrics(outcomes) if probe_rows and kind != "public_rehearsal" else {}
    official = scorer.run(cases, systems, probes)
    attack_scores = scorer.run(adversarial, systems, probes) if adversarial else []
    for score in [*official, *attack_scores]:
        _measured_costs(score)
    def independent_scores(group):
        gold = _gold_rows(group)
        return {system.name: score_capture(group, gold, _independent_rows(group, system), outcomes)
                for system in systems}

    independent = independent_scores(cases)
    table = scorer.comparison_table(official)
    table += "\n| Independent safe-complete | " + " | ".join(
        f"{independent[system.name]['metrics']['safe_complete']}/{len(cases)}" for system in systems
    ) + " |"
    coverage = None
    report = scorer.markdown_report(official)
    scope = ("Headline correctness measures investigation (draft-evidence) claims. "
             "It does not establish recipient correctness after policy review. "
             "The frozen baselines have unequal retrieval access.")
    table += "\n\n" + scope
    report += "\n\n" + scope + "\n"
    for system in systems:
        divergence = retained["claim_divergence_by_system"][system.name]
        line = (f"{system.name}: package_claims differ on {divergence['differing_rfcs']} RFCs; "
                f"{divergence['policy_filtering_rfcs']} have an explicit policy-filtering reason, "
                f"{divergence['unknown_reason_rfcs']} have an unknown reason. "
                f"{divergence['unknown_comparison_rfcs']} RFC comparisons have unavailable fields.")
        table += "\n\n" + line
        report += "\n" + line + "\n"
    if (output / "scheduling.json").exists():
        retained["scheduling"] = common.load_json(output / "scheduling.json")
        if retained["scheduling"]["aggregate"].get("aborted"):
            warning = "Capture stopped on an infrastructure error; this is an incomplete capture, not a completed candidate comparison."
            table += "\n\n" + warning
            report += "\n" + warning + "\n"
    if probes:
        counts = Counter(row["outcome"] for row in probe_rows)
        coverage = {"catalogue": len(probe_rows), "observed": len(outcomes),
                    "not_run": counts["not_run"], "error": counts["error"]}
        table += (f"\n\nShared backend probe coverage was {coverage['observed']}/{coverage['catalogue']}. "
                  f"{coverage['not_run']} were not run and {coverage['error']} errored. "
                  "The official probe scores use the full catalogue denominators.")
        table += f"\n\n{SP47_SCOPE}"
        report = f"{report.rstrip()}\n\n{SP47_SCOPE}\n"
    (output / "deck-table.md").write_text(f"# {REPORT_HEADINGS[kind]}\n\n{table}\n", encoding="utf-8")
    (output / "report.md").write_text(report, encoding="utf-8")
    common.write_json(output / "scores.json", {
        "mode": kind,
        "retained_capture": retained,
        "headline": headline_evidence(official, attack_scores, independent, outcomes if probes else {}, kind),
        "probe_coverage": coverage,
        "official": {score.system.name: score.metrics for score in official}, "independent": independent,
        "adversarial": {score.system.name: score.metrics for score in attack_scores},
        "independent_adversarial": independent_scores(adversarial) if adversarial else {},
    })
    headline = scorer.results_line(official[0])
    for baseline in official[1:]:
        headline.update({f"{baseline.system.name}_{key}": value for key, value in scorer.results_line(baseline).items()})
    for system in systems:
        headline[f"{system.name}_package_claims_divergent_rfcs"] = retained["claim_divergence_by_system"][system.name]["differing_rfcs"]
    return headline


def _path_argument(parser, option):
    flag = option.replace("_", "-")
    env = os.environ.get(f"ALIBI_FROZEN_{option.upper()}")
    parser.add_argument(f"--{flag}", type=Path, default=Path(env) if env else None)


def _aware_deadline(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("deadline must be an aware ISO datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("deadline must include a timezone")
    return parsed


def _pinned_candidate(ref: str) -> tuple[str, str]:
    """Bind a clean evaluator checkout separately from the immutable app tag."""
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=REPO, text=True).strip()

    if git("status", "--porcelain", "--untracked-files=all"):
        raise ValueError("the evaluator checkout must be clean")
    candidate = git("rev-parse", "--verify", f"{ref}^{{commit}}")
    approved_tag = ref if ref in ("sealed-candidate-2", "sealed-candidate-3") else "sealed-candidate"
    tagged = git("rev-parse", "--verify", f"refs/tags/{approved_tag}^{{commit}}")
    if candidate != tagged:
        raise ValueError(f"candidate-ref must resolve to the {approved_tag} tag")
    return candidate, git("rev-parse", "HEAD")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    public = parser.add_mutually_exclusive_group()
    public.add_argument("--dry-run", action="store_true", help="use public cases and stubbed model output")
    public.add_argument("--public-live", action="store_true", help="run public cases through the real adapter")
    parser.add_argument("--case", type=int, action="append", help="public case selection")
    parser.add_argument("--timeout", type=float, default=90, help="seconds per case and system")
    parser.add_argument("--candidate-ref", help="run a declared sealed-candidate tag with isolated workers")
    parser.add_argument("--workers", type=int, choices=range(1, 5), default=4)
    parser.add_argument("--deadline", type=_aware_deadline, help="aware ISO deadline for new captures")
    parser.add_argument("--checkpoint-at", type=_aware_deadline,
                        help="one full-denominator completed-so-far snapshot during the frozen capture")
    for option in ("out", "cases", "gold", "manifest", "model_config", "private_root", "probes"):
        _path_argument(parser, option)
    args = parser.parse_args(argv)
    if args.checkpoint_at is not None and (not args.candidate_ref or args.dry_run or args.public_live):
        parser.error("checkpoint-at requires an isolated frozen capture")
    if args.out is None or not (0 < args.timeout < 3600):
        parser.error("set an external --out directory and a valid timeout")
    output = _private_path(args.out)
    blocked = ()
    adversarial = []
    lock = None
    isolation = None
    candidate = evaluator = None
    if args.candidate_ref:
        if args.dry_run or args.deadline is None or args.private_root is None:
            parser.error("candidate-ref requires a live capture, private root and aware deadline")
        try:
            candidate, evaluator = _pinned_candidate(args.candidate_ref)
        except (ValueError, subprocess.CalledProcessError) as exc:
            parser.error(str(exc))
        credential = os.environ.get("ALIBI_EVAL_ENV_FILE")
        if not credential:
            parser.error("isolated captures require ALIBI_EVAL_ENV_FILE")
        isolation = {"candidate": candidate, "private_root": _private_path(args.private_root),
                     "credential": Path(credential).resolve(), "workers": args.workers, "deadline": args.deadline}
    kind = "frozen"
    if args.dry_run:
        kind = "public_rehearsal"
        cases = [load_case(number) for number in args.case] if args.case else load_cases()
        signature = {"dry": True, "inputs": common.canonical_sha256(cases), "timeout": args.timeout}
    elif args.public_live:
        kind = "public_live"
        if is_open() or not args.case or args.private_root is None:
            parser.error("public live checks require explicit --case and --private-root with the lockbox closed")
        root = _private_path(args.private_root)
        if not root.is_dir() or output.is_relative_to(root):
            parser.error("use an existing private root and a separate run directory")
        commit, dirty = seal.git_commit("HEAD")
        if candidate is not None:
            commit = candidate
        if dirty:
            parser.error("run the public live check from a clean candidate checkout")
        cases = [load_case(number) for number in dict.fromkeys(args.case)]
        blocked = (root, REPO / "data", REPO / "agents" / "data" / "out" / "cases", output)
        signature = {"kind": kind, "commit": commit, "inputs": common.canonical_sha256(cases),
                     "timeout": args.timeout}
    else:
        if not is_open():
            parser.error("use the authorized tools/results.py lockbox command")
        if args.case or not all((args.cases, args.gold, args.manifest, args.model_config, args.private_root)):
            parser.error("frozen runs require the manifest, cases, gold, model config and private root")
        root = _private_path(args.private_root)
        input_path, gold_path = _private_path(args.cases), _private_path(args.gold)
        if not all(path.is_relative_to(root) for path in (input_path, gold_path)) or output.is_relative_to(root):
            parser.error("the private root must cover the cases and gold and exclude the run directory")
        manifest = common.load_json(args.manifest)
        commit, dirty = seal.git_commit("HEAD")
        if candidate is not None:
            commit = candidate
        if manifest.get("purpose") != "sealed" or dirty or manifest["candidate"]["commit"] != commit:
            parser.error("run from the clean sealed candidate checkout")
        problems, _ = seal.verify_manifest(args.manifest, input_path, gold_path, args.model_config)
        if problems:
            parser.error("frozen commitments failed verification")
        cases = common.load_sealed_cases(input_path, require_gold=False)
        attack_ids = {case["case_number"] for case in common.read_jsonl(input_path / "adversarial.jsonl")}
        if not attack_ids or not attack_ids < {case["case_number"] for case in cases}:
            parser.error("expected a separate adversarial file in the prepared pack")
        blocked = (root, REPO / "data", REPO / "agents" / "data" / "out" / "cases", output)
        verify_file_isolation(blocked, gold_path)
        signature = {"dry": False, "commit": commit, "manifest": _digest(args.manifest), "timeout": args.timeout}
        lock = gold_path.parent / f"capture-{manifest['set_sha256']}.json"
        if isolation is not None and args.checkpoint_at is not None:
            isolation.update(checkpoint_at=args.checkpoint_at, gold_path=gold_path, adversarial_ids=attack_ids)

    if isolation is not None:
        from agents.eval.isolated_candidate import LIMITS
        signature.update({"candidate": candidate, "evaluator_commit": evaluator,
                          "workers": args.workers, "deadline": args.deadline.isoformat(),
                          "limits": dict(LIMITS), "inherited_llm_cap": False})
        if args.checkpoint_at is not None:
            signature["checkpoint_at"] = args.checkpoint_at.isoformat()
    _capture_once(output, cases, signature, dry=args.dry_run, timeout=args.timeout, blocked=blocked,
                  probes=args.probes, lock=lock, isolation=isolation)
    if kind == "frozen":
        problems, _ = seal.verify_manifest(args.manifest, input_path, gold_path, args.model_config)
        if problems or _digest(args.manifest) != signature["manifest"]:
            parser.error("frozen commitments changed during capture")
        gold = common.read_gold_file(gold_path)
        cases = [{**case, "gold": gold[case["case_number"]]} for case in cases]
        adversarial = [case for case in cases if case["case_number"] in attack_ids]
        cases = [case for case in cases if case["case_number"] not in attack_ids]
    print(json.dumps(score_saved(output, cases, adversarial, kind=kind), sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
