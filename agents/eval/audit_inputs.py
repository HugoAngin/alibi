"""Audit case visibility and flag runtime source code that could reach gold."""

import argparse
import ast
from datetime import datetime
import json
from pathlib import Path

from agents.data.api import known_at, load_cases, without_gold
from agents.eval.case_io import request_context
from agents.eval.freeze_pack import _private_path
from agents.eval.sealed_worker import REPO, case_payload
from alibi.eval.common import read_jsonl, sealed_files

TIME_FIELDS = {"merchant_deliveries": "received_at", "bank_deliveries": "received_at",
               "imports": "received_at", "rfcs": "issued_at"}
CASE_LOADERS = {"load_case", "load_cases", "canonical_case"}


def _visible(rows: list[dict], time_key: str, until: datetime) -> list[dict]:
    return [row for row in rows if datetime.fromisoformat(row[time_key]) <= until]


def audit_cases(cases: list[dict]) -> dict:
    """Check all RFC views against independently evaluated delivery timestamps."""
    if not cases:
        raise ValueError("no cases to audit")
    counts = dict(cases=len(cases), requests=0, lookup_checks=0, hidden_future_rows=0,
                  lookup_only_rows=0, gold_labels_removed=0, adapter_labels_removed=0)
    for case in cases:
        stripped = without_gold(case)
        if stripped != {key: value for key, value in case.items() if key != "gold"}:
            raise ValueError("without_gold changed something beyond the answer key")
        counts["gold_labels_removed"] += int("gold" in case)
        payload = case_payload(case)
        counts["adapter_labels_removed"] += len(set(stripped) - payload.keys())
        for rfc in case["rfcs"]:
            counts["requests"] += 1
            timestamp = rfc["issued_at"]
            until = datetime.fromisoformat(timestamp)
            expected = {key: _visible(case[key], clock, until) for key, clock in TIME_FIELDS.items()}
            known = known_at(case, timestamp)
            if "gold" in known or known["merchant_lookup"] or any(known[key] != value for key, value in expected.items()):
                raise ValueError("known_at exposes unexpected keys or rows at the request time")
            request, lookup = request_context(payload, rfc["request_id"])
            if (request["rfcs"] != [rfc] or request["merchant_lookup"] or "gold" in request
                    or any(request[key] != expected[key] for key in TIME_FIELDS if key != "rfcs")):
                raise ValueError("request_context exposes the wrong RFC, gold or evidence")

            counts["hidden_future_rows"] += sum(len(case[key]) - len(value) for key, value in expected.items())
            counts["lookup_only_rows"] += len(case["merchant_lookup"])
            ready = _visible(case["merchant_lookup"], "available_from", until)
            events = [item["event"] for item in [*expected["merchant_deliveries"], *ready]]
            for record_id in {item["event"]["record_id"] for item in case["merchant_lookup"]}:
                counts["lookup_checks"] += 1
                matched = {}
                for event in events:
                    if event["record_id"] == record_id:
                        matched.setdefault(event["event_id"], event)
                wanted = [event for event in matched.values() if event["subject_ref"] == rfc["subject_ref"]]
                if lookup(record_id=record_id) != wanted:
                    raise ValueError("lookup exposed the wrong subject, record or time")
    return counts


def _references(source: ast.AST) -> set[tuple[int, str]]:
    matches = set()
    for node in ast.walk(source):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value.casefold() == "gold":
                matches.add((node.lineno, "gold key"))
            elif "agents/data/out/cases" in node.value.replace("\\", "/"):
                matches.add((node.lineno, "development case path"))
        elif isinstance(node, ast.Call):
            name = getattr(node.func, "attr", getattr(node.func, "id", None))
            if name in CASE_LOADERS:
                matches.add((node.lineno, "development case loader"))
        elif isinstance(node, ast.ImportFrom) and node.module == "agents.data.api":
            if any(alias.name in CASE_LOADERS for alias in node.names):
                matches.add((node.lineno, "development case loader import"))
    return matches


def audit_source(root: Path) -> dict:
    runtime_hits, evaluator_hits, files = [], [], 0
    for path in sorted((root / "alibi").rglob("*.py")):
        relative = path.relative_to(root)
        files += 1
        matches = _references(ast.parse(path.read_text(encoding="utf-8"), filename=str(relative)))
        destination = evaluator_hits if relative.parts[1] in ("eval", "tests") else runtime_hits
        destination.extend({"path": str(relative), "line": line, "kind": kind} for line, kind in sorted(matches))
    return {"source_files": files, "adapter_present": (root / "alibi" / "adapter.py").is_file(),
            "runtime_references": runtime_hits, "evaluator_and_test_references": len(evaluator_hits)}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", type=Path, help="evaluator-only input JSONL or prepared cases directory")
    parser.add_argument("--require-adapter", action="store_true", help="fail if the release adapter is absent")
    args = parser.parse_args(argv)
    cases = ([case for path in sealed_files(_private_path(args.inputs)) for case in read_jsonl(path)]
             if args.inputs else load_cases())
    report = {**audit_cases(cases), **audit_source(REPO)}
    print(json.dumps(report, sort_keys=True))
    if report["runtime_references"] or args.require_adapter and not report["adapter_present"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
