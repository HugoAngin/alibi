"""Run eval probes and emit the official scorer's JSONL result format."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

from agents.eval.http_probe_runtime import attempt
from agents.eval.probe_capture import missing_demo_probe_kinds, probe_receipt, probe_receipt_path
from alibi.eval import common, seal
from alibi.eval.probes import Harness, ProbeResult


def _target(config: dict, probe_id: str) -> tuple[dict, dict]:
    override = config.get("probes", {}).get(probe_id, {})
    common = config.get("base_urls", {})
    local = override.get("base_urls", {})
    urls = {**common, **local}
    for key in ("routes", "fixtures"):
        urls[key] = {**common.get(key, {}), **local.get(key, {})}
    return urls, {**config.get("tokens", {}), **override.get("tokens", {})}


def _storage_probe(layout: str | None) -> ProbeResult:
    if layout is None:
        return ProbeResult("SP-47", "not_run", "no confirmed storage layout supplied for the local core probe")
    with TemporaryDirectory(prefix="alibi-eval-sp47-") as temp:
        run = subprocess.run([sys.executable, "-m", "agents.execution.at_rest_probe"], check=True,
                             env={**os.environ, "TMPDIR": temp}, capture_output=True, text=True, timeout=45)
    metrics = json.loads(run.stdout.strip().splitlines()[-1])
    if metrics["sp47_controls_passed"] != metrics["sp47_controls"] or metrics["sp47_controls"] < 1:
        return ProbeResult("SP-47", "error", "local storage controls did not pass")
    outcome = {-1: "not_run", 0: "not_blocked", 1: "blocked"}[metrics[f"sp47_{layout}_blocked"]]
    return ProbeResult("SP-47", outcome, f"isolated passport store, {layout} layout")


def _registered_attempt(config: dict, probe_id: str, kind: str, chosen: set[str]):
    def run(_target_arg):
        if probe_id not in chosen:
            return ProbeResult(probe_id, "not_run", "not selected")
        try:
            if probe_id == "SP-47":
                return _storage_probe(config.get("storage_layout"))
            base_urls, tokens = _target(config, probe_id)
            observed = attempt(probe_id, base_urls, tokens)
            outcome = observed["outcome"]
            detail = observed.get("reason", "")
            if kind == "control" and outcome in ("blocked", "not_blocked"):
                # Eval's low-level attempt reports whether the check passed. The scorer
                # expects the opposite wire label for a positive acceptance control.
                passed = outcome == "blocked"
                outcome = "not_blocked" if passed else "blocked"
                detail = "acceptance postconditions met" if passed else "acceptance postconditions failed"
            if outcome == "not_run" and not detail:
                detail = "expected evidence was inconclusive"
            exchanges = [f"{entry['method']} HTTP {entry.get('status', 'unavailable')}"
                         for entry in observed["exchanges"]]
            return ProbeResult(probe_id, outcome, detail, audit=exchanges)
        except Exception as exc:  # Every failed attempt stays in the official result file.
            # The official harness includes tracebacks on errors. Keep config and credentials out of those rows.
            return ProbeResult(probe_id, "error", f"probe raised {type(exc).__name__}")

    return run


def build_probe_harness(config: dict, ids: list[str] | None = None) -> Harness:
    """Register the full catalogue, including explicit outcomes for unselected IDs."""
    harness = Harness()
    chosen = set(harness.probes if ids is None else ids)
    if not isinstance(config, dict) or not all(isinstance(config.get(key, {}), dict)
                                                for key in ("base_urls", "tokens", "probes")):
        raise ValueError("config needs object-valued base_urls, tokens and probes")
    if config.get("storage_layout") not in (None, "default", "configured"):
        raise ValueError("storage_layout must be default or configured")
    unknown = chosen.union(config.get("probes", {})) - harness.probes.keys()
    if unknown:
        raise ValueError(f"unknown catalogue IDs: {sorted(unknown)}")
    for probe_id, probe in harness.probes.items():
        harness.register(probe_id)(_registered_attempt(config, probe_id, probe["kind"], chosen))
    return harness


def _validate_candidate_origins(config: dict, probe_ids) -> None:
    """Candidate captures can exercise writes only against disposable loopback services."""
    for probe_id in probe_ids:
        try:
            base, _ = _target(config, probe_id)
        except (AttributeError, TypeError):
            raise ValueError(f"{probe_id} has an invalid service configuration") from None
        for service, value in base.items():
            if service in {"routes", "fixtures"}:
                continue
            try:
                parsed = urlsplit(value) if isinstance(value, str) else urlsplit("")
                valid = (parsed.scheme in {"http", "https"}
                         and parsed.hostname in {"127.0.0.1", "::1", "localhost"}
                         and parsed.username is None and parsed.password is None
                         and parsed.port != 0 and not parsed.query and not parsed.fragment)
            except ValueError:
                valid = False
            if not valid:
                raise ValueError(f"{probe_id} needs loopback service origins for candidate evidence")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="git-ignored config for a disposable synthetic stack")
    parser.add_argument("--ids", nargs="+", help="exercise only these catalogue IDs")
    parser.add_argument("--results", type=Path, required=True, help="official scorer input JSONL")
    parser.add_argument("--candidate-evidence", action="store_true",
                        help="record a full probe run against a disposable stack started from this checkout")
    args = parser.parse_args(argv)
    receipt = probe_receipt_path(args.results)
    if receipt.exists():
        parser.error("use a new results path to preserve the candidate capture")
    if args.candidate_evidence:
        if not args.config or args.ids or args.results.exists():
            parser.error("candidate evidence requires a config, the full catalogue and a new results path")
        commit, dirty = seal.git_commit("HEAD")
        if dirty:
            parser.error("record candidate evidence from a clean checkout")
    config = json.loads(args.config.read_text(encoding="utf-8")) if args.config else {}
    harness = build_probe_harness(config, args.ids)
    if args.candidate_evidence:
        try:
            _validate_candidate_origins(config, harness.probes)
        except ValueError as exc:
            parser.error(str(exc))
    results = harness.run()
    print(harness.table(results))
    print(json.dumps(harness.summary(results), sort_keys=True))
    if args.candidate_evidence and seal.git_commit("HEAD") != (commit, False):
        parser.error("candidate checkout changed during probe capture")
    if args.candidate_evidence:
        missing = missing_demo_probe_kinds({result.probe_id: result.outcome for result in results})
        if missing:
            parser.error(f"candidate evidence needs definite demo outcomes for {', '.join(sorted(missing))}")
        args.results.parent.mkdir(parents=True, exist_ok=True)
        args.results.touch(exist_ok=False)
    harness.write_results(results, args.results)
    if args.candidate_evidence:
        metadata = probe_receipt(commit, common.read_jsonl(args.results))
        with receipt.open("x", encoding="utf-8") as handle:
            json.dump(metadata, handle)


if __name__ == "__main__":
    main()
