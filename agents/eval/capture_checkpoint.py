"""Snapshot an active capture once; replay saved aggregates without cases or gold."""

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import shutil

from alibi.eval import common
from agents.eval.sealed_worker import MODES, failure_rows

MARKER = "checkpoint.complete.json"
NOTICE = ("INCOMPLETE checkpoint: completed-so-far evidence from the same capture. "
          "All main and adversarial denominators are retained; unfinished requests count as unavailable. "
          "This is not a completed candidate comparison. Usage excludes in-flight work and may be unknown.")


def _digest(path):
    return sha256(path.read_bytes()).hexdigest()


def _files(directory):
    return {str(path.relative_to(directory)): _digest(path)
            for path in sorted(directory.rglob("*")) if path.is_file() and path.name != MARKER}


def create_checkpoint(output: Path, cases: list[dict], *, gold_path: Path,
                      adversarial_ids: set[int], signature: dict, scheduling: dict,
                      completed_workers: set[str]) -> Path:
    """Coordinator-only: never mutate source cases or launch a worker.

    Call only with every prediction stream flushed. Worker scratch processes deny
    the whole capture directory and private gold root, including this snapshot.
    An interrupted snapshot remains incomplete and must not be overwritten.
    """
    from agents.eval.run_sealed import score_saved

    checkpoint = output / "checkpoint"
    checkpoint.mkdir(mode=0o700)
    expected = {(case["case_number"], rfc["request_id"]) for case in cases for rfc in case["rfcs"]}
    missing = {}
    for mode in MODES:
        rows = common.read_jsonl(output / f"{mode}.jsonl")
        keys = [(row["case_number"], row["request_id"]) for row in rows]
        if len(keys) != len(set(keys)) or not set(keys) <= expected:
            raise ValueError("checkpoint predictions have duplicate or unexpected request keys")
        seen = set(keys)
        unavailable = [row for case in cases for row in
                       failure_rows(case, mode, "unavailable", "not completed at checkpoint")
                       if (row["case_number"], row["request_id"]) not in seen]
        missing[mode] = len(unavailable)
        common.write_jsonl(checkpoint / f"{mode}.jsonl", [*rows, *unavailable])
    for name in ("probes.jsonl", "probes.jsonl.receipt.json"):
        if (output / name).is_file():
            shutil.copyfile(output / name, checkpoint / name)
    for name in sorted(completed_workers):
        source = output / "capture-workers" / name
        for filename in ("usage.json", "metadata.json", "llm_log.jsonl"):
            if (source / filename).is_file():
                target = checkpoint / "capture-workers" / name
                target.mkdir(parents=True, exist_ok=True, mode=0o700)
                shutil.copyfile(source / filename, target / filename)
    common.write_json(checkpoint / "scheduling.json", scheduling)
    # This read happens in the coordinator only, after the checkpoint is due.
    gold = common.read_gold_file(gold_path)
    scored = [{**case, "gold": gold[case["case_number"]]} for case in cases]
    main = [case for case in scored if case["case_number"] not in adversarial_ids]
    adversarial = [case for case in scored if case["case_number"] in adversarial_ids]
    snapshot = {"incomplete": True, "full_denominator": True, "missing_requests": missing,
                "main_cases": len(main), "adversarial_cases": len(adversarial),
                "created_at": datetime.now(timezone.utc).isoformat(), "gold_sha256": _digest(gold_path)}
    common.write_json(checkpoint / "run.json", {"input": signature, "checkpoint": snapshot})
    score_saved(checkpoint, main, adversarial, kind="frozen")
    scores = common.load_json(checkpoint / "scores.json")
    scores["checkpoint"] = snapshot
    common.write_json(checkpoint / "scores.json", scores)
    for filename in ("report.md", "deck-table.md"):
        path = checkpoint / filename
        path.write_text(NOTICE + "\n\n" + path.read_text(), encoding="utf-8")
    files = _files(checkpoint)
    temporary = checkpoint / "checkpoint.complete.tmp"
    common.write_json(temporary, {"format": "alibi.checkpoint.v1", "incomplete": True,
                                  "full_denominator": True, "files": files})
    temporary.replace(checkpoint / MARKER)
    return checkpoint


def replay_checkpoint(capture: Path) -> dict:
    """Read only the saved checkpoint; neither imports the runner nor reads gold."""
    from agents.eval.frozen_summary import build_summary

    if not (capture / MARKER).is_file():
        raise ValueError("checkpoint evidence is incomplete")
    marker = common.load_json(capture / MARKER)
    if (marker.get("format") != "alibi.checkpoint.v1" or marker.get("incomplete") is not True
            or marker.get("full_denominator") is not True or marker.get("files") != _files(capture)):
        raise ValueError("checkpoint evidence changed or is incomplete")
    summary = build_summary(common.load_json(capture / "scores.json"),
                            common.load_json(capture / "run.json"),
                            common.load_json(capture / "scheduling.json"))
    summary["checkpoint"] = {"incomplete": True, "full_denominator": True}
    summary["interpretation_and_caveats"].insert(0, NOTICE)
    return summary


def main(argv=None):
    from agents.eval.frozen_summary import _numeric

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", type=Path, required=True, help="saved checkpoint directory")
    args = parser.parse_args(argv)
    summary = replay_checkpoint(args.capture)
    print(json.dumps(summary, sort_keys=True, indent=2, allow_nan=False))
    print(json.dumps(_numeric(summary), sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
