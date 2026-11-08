"""Allowlisted post-capture stderr summary; never export case values or raw traces."""

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import re


MESSAGES = {
    "payload failed schema validation:": "payload failed schema validation: kind [enum mismatch]; extra fields rejected",
    "import refused:": "import refused: event [MASKED] has a kind that is not a case kind",
}
FRAMES = {
    ("alibi/core/ingest.py", 402, "import_self_supplied"),
    ("alibi/connectors/self_supplied.py", 126, "import_record"),
}


def summarise(capture):
    groups = defaultdict(lambda: {"cases": set(), "systems": Counter()})
    missing, unclassified, failures = Counter(), Counter(), Counter()
    for worker in (capture / "capture-workers").iterdir():
        metadata = worker / "metadata.json"
        if not metadata.is_file():
            continue
        meta = json.loads(metadata.read_text())
        if not meta.get("failures"):
            continue
        mode = meta["system"]
        if mode not in ("agents", "one_pass", "rules"):
            raise ValueError("unknown system in retained metadata")
        failures[mode] += 1
        stderr = worker / "worker.stderr.log"
        if not stderr.is_file() or not stderr.stat().st_size:
            missing[mode] += 1
            continue
        value = stderr.read_text()
        messages = re.findall(r"^alibi\.core\.ingest\.IngestError:\s*(.*)$", value, re.M)
        frames = re.findall(r'File "[^"\n]*/(alibi/[^"\n]+\.py)", line (\d+), in (\w+)', value)
        frame = (frames[-1][0], int(frames[-1][1]), frames[-1][2]) if frames else None
        message = messages[-1] if messages else ""
        if frame not in FRAMES or not any(message.startswith(prefix) for prefix in MESSAGES):
            unclassified[mode] += 1
            continue
        if frame[0] == "alibi/core/ingest.py":
            valid = message.startswith("payload failed schema validation: kind: Input should be ") and "Extra inputs are not permitted" in message
            safe = MESSAGES["payload failed schema validation:"]
        else:
            valid = bool(re.fullmatch(r"import refused: event \S+ has a kind that is not a case kind", message))
            safe = MESSAGES["import refused:"]
        if not valid:
            unclassified[mode] += 1
            continue
        key = (*frame, safe)
        groups[key]["cases"].add(meta["case_number"])
        groups[key]["systems"][mode] += 1
    return {
        "kind": "sanitised import-error summary; dynamic values omitted",
        "groups": [{"exception_class": "alibi.core.ingest.IngestError", "file": key[0],
                    "line": key[1], "function": key[2], "masked_message": key[3],
                    "distinct_cases": len(row["cases"]), "system_groups": dict(row["systems"])}
                   for key, row in sorted(groups.items())],
        "failed_groups": dict(failures), "failed_groups_without_stderr": dict(missing),
        "unclassified_groups": dict(unclassified), "case_or_gold_reads": 0,
        "scope": "Observed retained failures only; absent original stderr remains unknown. No retry justification is inferred.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    protected = args.capture.parent if (args.capture / "resume-lock.json").is_file() else args.capture
    if args.out and args.out.resolve().is_relative_to(protected.resolve()):
        parser.error("summary output must be outside the retained capture")
    report = summarise(args.capture)
    encoded = json.dumps(report, indent=2, sort_keys=True)
    if args.out:
        with args.out.open("x") as handle:
            handle.write(encoded + "\n")
    print(encoded)
    print(json.dumps({f"{kind}_{mode}": count for kind in (
        "failed_groups", "failed_groups_without_stderr", "unclassified_groups")
                     for mode, count in report[kind].items()}, sort_keys=True))


if __name__ == "__main__":
    main()
