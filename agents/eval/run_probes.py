"""Run eval probes and emit the official scorer's JSONL result format."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

from agents.eval.http_probe_runtime import attempt
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
    return ProbeResult("SP-47", outcome, f"isolated local core storage probe, {layout} layout")


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


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="git-ignored config for a disposable synthetic stack")
    parser.add_argument("--ids", nargs="+", help="exercise only these catalogue IDs")
    parser.add_argument("--results", type=Path, required=True, help="official scorer input JSONL")
    args = parser.parse_args(argv)
    config = json.loads(args.config.read_text(encoding="utf-8")) if args.config else {}
    harness = build_probe_harness(config, args.ids)
    results = harness.run()
    harness.write_results(results, args.results)
    print(harness.table(results))
    print(json.dumps(harness.summary(results), sort_keys=True))


if __name__ == "__main__":
    main()
