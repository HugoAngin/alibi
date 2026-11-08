"""Verify retained continuation invariants without cases, gold or model calls."""

import argparse
from collections import defaultdict
import json
from pathlib import Path

from agents.eval.resume_capture import CANDIDATE, PERMISSION, UNSTARTED, _original, _replay
from agents.eval.run_sealed import PREDICTION_FILES, _digest


def _groups(path):
    groups = defaultdict(list)
    seen = set()
    for line in path.read_bytes().splitlines(keepends=True):
        row = json.loads(line)
        key = (row["case_number"], row["request_id"])
        if key in seen:
            raise ValueError("duplicate retained request")
        seen.add(key)
        groups[row["case_number"]].append((line, row.get("failure")))
    return groups


def audit(capture):
    original, _ = _original(capture)
    resumed = capture / "resume"
    _replay(resumed, _digest(capture / "run.json"))
    record = json.loads((resumed / "run.json").read_text())
    if (record["input"]["original_input"] != original["input"]
            or record["input"]["candidate"] != CANDIDATE):
        raise ValueError("continuation input commitment differs")
    counts = dict(preserved_groups=0, unstarted_groups=0, permission_retry_groups=0,
                  combined_groups=0, preserved_request_lines=0, continuation_groups=0)
    for mode, name in PREDICTION_FILES.items():
        before, after = _groups(capture / name), _groups(resumed / name)
        attempts = _groups(resumed / f"{mode}.attempts.jsonl")
        eligible = set()
        if before.keys() != after.keys():
            raise ValueError("continuation group denominator differs")
        for key, rows in before.items():
            counts["combined_groups"] += 1
            if all(failure == UNSTARTED for _, failure in rows):
                counts["unstarted_groups"] += 1
                eligible.add(key)
            elif all(failure == PERMISSION for _, failure in rows):
                counts["permission_retry_groups"] += 1
                eligible.add(key)
            else:
                if [line for line, _ in rows] != [line for line, _ in after[key]]:
                    raise ValueError("an original returned group changed")
                counts["preserved_groups"] += 1
                counts["preserved_request_lines"] += len(rows)
        if attempts.keys() != eligible or any(attempts[key] != after[key] for key in eligible):
            raise ValueError("continuation attempts differ from authorised replacement groups")
        counts["continuation_groups"] += len(attempts)
    expected = dict(preserved_groups=67, unstarted_groups=62,
                    permission_retry_groups=3, combined_groups=132, continuation_groups=65)
    if any(counts[key] != value for key, value in expected.items()):
        raise ValueError("continuation differs from authorised group counts")
    return dict(counts, evidence_hashes_verified=True, original_input_unchanged=True,
                candidate_unchanged=True, model_calls=0, case_or_gold_reads=0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(audit(args.capture.resolve(strict=True)), sort_keys=True))


if __name__ == "__main__":
    main()
