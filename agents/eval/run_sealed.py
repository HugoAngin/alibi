"""Run the frozen Alibi comparison once and replay scoring from retained predictions."""

import argparse
from collections import Counter
from hashlib import sha256
from importlib.util import find_spec
import json
import os
from pathlib import Path

from agents.data.api import load_case, load_cases
from agents.eval.freeze_pack import _private_path
from agents.eval.probe_capture import probe_receipt, probe_receipt_path
from agents.eval.score_capture import score_capture
from agents.eval.sealed_worker import MODES, REPO, case_payload, run_case_worker, verify_file_isolation
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


def _capture_once(output: Path, cases: list[dict], signature: dict, *, dry: bool, timeout: float,
                  blocked: tuple[Path, ...], probes: Path | None, lock: Path | None = None) -> None:
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
        return

    probe_rows = _probe_rows(probes)
    if frozen:
        if probes is None or not probe_rows or not probe_receipt_path(probes).is_file():
            raise ValueError("capture the full probe catalogue with --candidate-evidence before the frozen run")
        receipt = common.load_json(probe_receipt_path(probes))
        if receipt != probe_receipt(signature["commit"], probe_rows):
            raise ValueError("probe evidence does not match the frozen candidate or recorded outcomes")
    if not dry and find_spec("alibi.adapter") is None:
        raise RuntimeError("the candidate does not contain alibi.adapter.run_case")
    if not dry:
        client.load_api_key()
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
    for mode, filename in PREDICTION_FILES.items():
        with (output / filename).open("x", encoding="utf-8") as destination:
            for case in cases:
                rows = run_case_worker(case, mode, dry_run=dry, timeout=timeout, blocked=blocked)
                for row in rows:
                    destination.write(json.dumps(row, allow_nan=False) + "\n")
                destination.flush()
    temporary = output / "run.tmp"
    common.write_json(temporary, {"input": signature, "files": {name: _digest(output / name) for name in files}})
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
    if probes:
        counts = Counter(row["outcome"] for row in probe_rows)
        coverage = {"catalogue": len(probe_rows), "observed": len(outcomes),
                    "not_run": counts["not_run"], "error": counts["error"]}
        table += (f"\n\nShared backend probe coverage was {coverage['observed']}/{coverage['catalogue']}. "
                  f"{coverage['not_run']} were not run and {coverage['error']} errored. "
                  "The official probe scores use the full catalogue denominators.")
    (output / "deck-table.md").write_text(f"# {REPORT_HEADINGS[kind]}\n\n{table}\n", encoding="utf-8")
    (output / "report.md").write_text(scorer.markdown_report(official), encoding="utf-8")
    common.write_json(output / "scores.json", {
        "mode": kind,
        "probe_coverage": coverage,
        "official": {score.system.name: score.metrics for score in official}, "independent": independent,
        "adversarial": {score.system.name: score.metrics for score in attack_scores},
        "independent_adversarial": independent_scores(adversarial) if adversarial else {},
    })
    headline = scorer.results_line(official[0])
    for baseline in official[1:]:
        headline.update({f"{baseline.system.name}_{key}": value for key, value in scorer.results_line(baseline).items()})
    return headline


def _path_argument(parser, option):
    flag = option.replace("_", "-")
    env = os.environ.get(f"ALIBI_FROZEN_{option.upper()}")
    parser.add_argument(f"--{flag}", type=Path, default=Path(env) if env else None)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    public = parser.add_mutually_exclusive_group()
    public.add_argument("--dry-run", action="store_true", help="use public cases and stubbed model output")
    public.add_argument("--public-live", action="store_true", help="run public cases through the real adapter")
    parser.add_argument("--case", type=int, action="append", help="public case selection")
    parser.add_argument("--timeout", type=float, default=90, help="seconds per case and system")
    for option in ("out", "cases", "gold", "manifest", "model_config", "private_root", "probes"):
        _path_argument(parser, option)
    args = parser.parse_args(argv)
    if args.out is None or not (0 < args.timeout < 3600):
        parser.error("set an external --out directory and a valid timeout")
    output = _private_path(args.out)
    blocked = ()
    adversarial = []
    lock = None
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

    _capture_once(output, cases, signature, dry=args.dry_run, timeout=args.timeout, blocked=blocked,
                  probes=args.probes, lock=lock)
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
