"""Assemble a private pack for the official seal and the independent scorer."""

import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from alibi.eval.common import read_jsonl, write_jsonl
from tools.lockbox import lockbox_from

REPO = Path(__file__).resolve().parents[2]


def _private_path(path: Path) -> Path:
    resolved = path.resolve()
    if resolved.is_relative_to(REPO) or any((parent / ".git").exists() for parent in resolved.parents):
        raise ValueError("pack inputs and outputs belong outside the repository")
    return resolved


def _source_id(ref: str | dict) -> str:
    value = ref if isinstance(ref, str) else ref["source_id"]
    kind, separator, identifier = value.partition(":")
    if kind not in ("bank", "merchant", "import") or not separator or not identifier:
        raise ValueError("invalid reference in authored gold")
    return value


def _gold_case(case: dict, rows: list[dict], steps: list[dict]) -> dict:
    answers, links, request_checks = {}, {}, {}
    for row in rows:
        request_id = row["request_id"]
        request_checks[request_id] = row
        answers[request_id] = {
            qid: {"status": answer["status"], "gap": answer["gap"], "facts": answer["facts"],
                  "evidence_sets": [[_source_id(ref) for ref in refs] for refs in answer["evidence_sets"]]}
            for qid, answer in row["answers"].items()
        }
        for link in row.get("links", []):
            links.setdefault(link["txn_id"], link)

    always_forbidden = set.intersection(*(set(map(_source_id, row.get("forbidden_refs", []))) for row in rows))
    first = rows[0]
    return {"case_number": case["case_number"], "gold": {
        "decision_time": first["decision_time"], "links": list(links.values()), "answers": answers,
        "breakdown_minor": first.get("breakdown_minor"), "claim_status": first.get("claim_status"),
        "forbidden_refs": sorted(always_forbidden), "request_checks": request_checks, "probe_steps": steps,
    }}


def prepare_pack(sealed: Path, adversarial: Path, gold: Path, steps: Path, output: Path) -> dict:
    """Validate the input/gold join and stage the case and answer files outside the repo."""
    output = _private_path(output)
    if output.exists():
        raise FileExistsError("refusing to replace an existing pack")
    boundary = lockbox_from()
    if type(boundary) is not int:
        raise ValueError("expected a case-number lockbox boundary")

    sealed_cases = read_jsonl(_private_path(sealed))
    adversarial_cases = read_jsonl(_private_path(adversarial))
    if not sealed_cases or not adversarial_cases:
        raise ValueError("both sealed and adversarial cases are required")
    cases, requests = {}, {}
    for case in [*sealed_cases, *adversarial_cases]:
        number = case.get("case_number")
        if type(number) is not int or number < boundary or number in cases or "gold" in case:
            raise ValueError("expected unique sealed case numbers and separate gold")
        cases[number] = case
        request_rows = case.get("rfcs")
        if not request_rows:
            raise ValueError("each case needs at least one request")
        for rfc in request_rows:
            request_id = rfc["request_id"]
            if (number, request_id) in requests:
                raise ValueError("request IDs must be unique within a case")
            requests[number, request_id] = rfc
    adversarial_numbers = {case["case_number"] for case in adversarial_cases}

    answer_key = {}
    for row in read_jsonl(_private_path(gold)):
        key = (row["case_number"], row["request_id"])
        rfc = requests.get(key)
        if (rfc is None or key in answer_key or row["audience"] != rfc["audience"]
                or row["decision_time"] != rfc["issued_at"] or row["answers"].keys() != set(rfc["question_ids"])):
            raise ValueError("gold must match each RFC's recipient, time and questions exactly")
        answer_key[key] = row
    if requests.keys() != answer_key.keys():
        raise ValueError("some requests have no independently authored gold")

    steps_by_case = {number: [] for number in cases}
    seen_steps = set()
    for step in read_jsonl(_private_path(steps)):
        number, request_id, step_id = step["case_number"], step["request_id"], step["step_id"]
        key = (number, request_id, step_id)
        if number not in adversarial_numbers or (number, request_id) not in requests or key in seen_steps:
            raise ValueError("adversarial steps must reference a unique step of an adversarial request")
        seen_steps.add(key)
        steps_by_case[number].append(step)
    grouped = [
        _gold_case(case, [answer_key[number, rfc["request_id"]] for rfc in case["rfcs"]], steps_by_case[number])
        for number, case in sorted(cases.items())
    ]

    with TemporaryDirectory(dir=output.parent, prefix="prepare-") as stage:
        stage = Path(stage)
        for path, rows in (("cases/sealed.jsonl", sealed_cases), ("cases/adversarial.jsonl", adversarial_cases),
                           ("gold.jsonl", grouped)):
            target = stage / path
            write_jsonl(target, rows)
            target.chmod(0o600)
        (stage / "cases").chmod(0o700)
        stage.rename(output)
    return {"sealed_cases": len(sealed_cases), "adversarial_cases": len(adversarial_cases),
            "requests": len(requests), "probe_steps": len(seen_steps)}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sealed", type=Path, required=True)
    parser.add_argument("--adversarial", type=Path, required=True)
    parser.add_argument("--gold", type=Path, required=True)
    parser.add_argument("--steps", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    print(json.dumps(prepare_pack(args.sealed, args.adversarial, args.gold, args.steps, args.out), sort_keys=True))


if __name__ == "__main__":
    main()
