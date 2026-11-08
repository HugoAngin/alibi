"""Score retained public rules captures before/after a fix, without rerunning models."""

import argparse
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from agents.data.api import load_case
from alibi.eval import scorer


def evaluate(capture: Path, candidate: str):
    rows, seen, failures, digests = [], set(), 0, {}
    for worker in sorted((capture / "capture").iterdir()):
        meta = json.loads((worker / "metadata.json").read_text())
        number = meta["case_number"]
        if (type(number) is not int or number not in range(1, 81) or number in seen
                or meta["system"] != "rules" or meta["candidate"] != candidate):
            raise ValueError("capture is not the expected complete public rules candidate")
        seen.add(number)
        failures += bool(meta["failures"])
        path = worker / "predictions.json"
        payload = path.read_bytes()
        predictions = json.loads(payload)
        case = load_case(number)
        if ({row["request_id"] for row in predictions} != {r["request_id"] for r in case["rfcs"]}
                or any(row["case_number"] != number for row in predictions)):
            raise ValueError("public request denominator differs")
        rows.extend(predictions)
        digests[worker.name] = sha256(payload).hexdigest()
    if seen != set(range(1, 81)):
        raise ValueError("capture must contain all 80 public cases")
    with TemporaryDirectory(prefix="alibi-public-score-") as temp:
        path = Path(temp) / "rules.jsonl"
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        system = scorer.load_predictions(path, "rules")
        scored = scorer.run([load_case(n) for n in range(1, 81)], [system], {})[0]
    return {"candidate": candidate, "public_only": True, "worker_failure_groups": failures,
            "metrics": scored.metrics,
            "prediction_set_sha256": sha256(json.dumps(digests, sort_keys=True).encode()).hexdigest()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for side in ("before", "after"):
        parser.add_argument("--" + side, type=Path, required=True)
        parser.add_argument("--" + side + "-candidate", required=True)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    result = {side: evaluate(getattr(args, side), getattr(args, side + "_candidate"))
              for side in ("before", "after")}
    result["scope"] = "Post-freeze public development comparison; original unseen results unchanged."
    encoded = json.dumps(result, indent=2, sort_keys=True, allow_nan=False)
    if args.out:
        with args.out.open("x") as handle:
            handle.write(encoded + "\n")
    print(encoded)
    metrics = {f"{side}_{key}": value for side in ("before", "after")
               for key, value in result[side]["metrics"].items() if type(value) in (int, float)}
    metrics.update({side + "_worker_failure_groups": result[side]["worker_failure_groups"]
                    for side in ("before", "after")})
    print(json.dumps(metrics, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
