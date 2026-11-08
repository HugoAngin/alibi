"""Estimate Gemini costs from one evaluation run's JSONL client log.

Standard paid Gemini Developer API rates for text apply to recorded prompt and
candidate-plus-thinking tokens. The estimate assumes uncached provider input
because the client does not log provider cache usage. Local cache hits and
budget rejections make no model call. The log records final-response usage
only, so an estimate with unmeasured attempts is reported separately.

Use a separate log for each system and run. Rows with no case_number are
reported as unscoped. model_calls counts uncached invocations logged by the
client, while logged_transport_attempts includes the retry count it recorded.
"""

import argparse
import json
import math
from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

DEFAULT_LOG = Path("data/alibi/llm_log.jsonl")
PRICING_SOURCE = "https://ai.google.dev/gemini-api/docs/pricing"
# Effective UTC dates and USD per million tokens (input, output), verified 2026-11-08.
STANDARD_RATES = {
    "gemini-3.8-flash": [(date(2027, 1, 1), "1.50", "7.50"), (date(2026, 9, 2), "0.75", "3.75")],
    "gemini-4-argon": [(date(2026, 12, 1), "4.00", "20.00"), (date(2026, 10, 13), "2.00", "10.00")],
}
TOKEN_FIELDS = ("prompt_tokens", "output_tokens", "thinking_tokens", "total_tokens")
CALL_OUTCOMES = {"ok", "schema_invalid", "unavailable"}
NON_CALL_OUTCOMES = {"cache_hit", "budget_exceeded"}


def _read_rates(path: Path) -> dict[str, tuple[Decimal, Decimal]]:
    rates = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(rates, dict):
        raise ValueError("rates must be an object keyed by exact model ID")
    result = {}
    for model, rate in rates.items():
        fields = ("input_per_million", "output_per_million")
        if not model or not isinstance(rate, dict) or set(rate) != set(fields):
            raise ValueError(f"invalid rate for {model!r}: expected input_per_million and output_per_million")
        input_rate, output_rate = (rate[field] for field in fields)
        if any(type(value) not in (int, float) or value < 0 or not math.isfinite(value)
               for value in (input_rate, output_rate)):
            raise ValueError(f"rates for {model!r} must be finite non-negative numbers")
        result[model] = (Decimal(str(input_rate)), Decimal(str(output_rate)))
    return result


def _record(row: dict, explicit_rates: dict[str, tuple[Decimal, Decimal]]) -> dict:
    if not isinstance(row, dict):
        raise ValueError("expected a JSON object")
    model, outcome, cached, attempts = (row.get(key) for key in ("model", "outcome", "cached", "attempts"))
    if not isinstance(model, str) or not model or not isinstance(outcome, str):
        raise ValueError("expected a model ID and an outcome")
    if outcome not in CALL_OUTCOMES | NON_CALL_OUTCOMES:
        raise ValueError("invalid outcome")
    if type(cached) is not bool or cached != (outcome == "cache_hit"):
        raise ValueError("cached flag must match the outcome")
    if type(attempts) is not int or attempts < 1:
        raise ValueError("attempts must be a positive integer")
    case_number = row.get("case_number")
    if case_number is not None and type(case_number) is not int:
        raise ValueError("case_number must be an integer when supplied")
    record = {"case_number": case_number, "model": model, "outcome": outcome, "attempts": attempts}
    if outcome in NON_CALL_OUTCOMES:
        return record

    time = row.get("time")
    timestamp = datetime.fromisoformat(time) if isinstance(time, str) else None
    if timestamp is None or timestamp.utcoffset() is None:
        raise ValueError("expected a timezone-aware time")
    day = timestamp.astimezone(timezone.utc).date()
    rate = explicit_rates.get(model)
    if rate is None:
        rate = next(((Decimal(input_rate), Decimal(output_rate))
                     for start, input_rate, output_rate in STANDARD_RATES.get(model, ())
                     if day >= start), None)

    usage = row.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    tokens = {key: usage.get(key) for key in TOKEN_FIELDS}
    tokens = {key: value if type(value) is int and value >= 0 else None for key, value in tokens.items()}
    # S3a uses an all-zero Usage when metadata is absent. A real prompt is nonempty.
    for key in ("prompt_tokens", "total_tokens"):
        if tokens[key] == 0:
            tokens[key] = None
    if all(value == 0 or value is None for value in tokens.values()):
        tokens = dict.fromkeys(TOKEN_FIELDS)
    prompt, output, thinking, total = tokens.values()
    complete = all(value is not None for value in tokens.values())
    inconsistent = complete and total != prompt + output + thinking
    cost = None
    if rate is not None and complete and not inconsistent:
        cost = (prompt * rate[0] + (output + thinking) * rate[1]) / 1_000_000
    return {**record, **tokens, "priced": rate is not None, "cost": cost, "inconsistent": inconsistent}


def _summarise(records: list[dict]) -> dict:
    calls = [row for row in records if row["outcome"] in CALL_OUTCOMES]
    token_totals = {}
    for field in TOKEN_FIELDS:
        values = [row[field] for row in calls]
        token_totals[field] = None if None in values else sum(values)
    priced_costs = [row["cost"] for row in calls if row["cost"] is not None]
    priced_cost = float(sum(priced_costs))
    untracked_attempts = sum(row["attempts"] > 1 or row["outcome"] == "schema_invalid" for row in calls)
    return {
        "log_rows": len(records), "model_calls": len(calls),
        "logged_transport_attempts": sum(row["attempts"] for row in calls),
        "cache_hits": sum(row["outcome"] == "cache_hit" for row in records),
        "budget_exceeded": sum(row["outcome"] == "budget_exceeded" for row in records),
        "unavailable_rows": sum(row["outcome"] == "unavailable" for row in calls),
        "rows_with_potential_unmeasured_attempts": untracked_attempts,
        "rows_missing_tokens": sum(any(row[key] is None for key in TOKEN_FIELDS) for row in calls),
        "rows_with_inconsistent_tokens": sum(row["inconsistent"] for row in calls),
        "unknown_pricing_models": sorted({row["model"] for row in calls if not row["priced"]}),
        **token_totals, "tokens": token_totals["total_tokens"],
        "cost_usd_estimate": priced_cost if len(priced_costs) == len(calls) and not untracked_attempts else None,
        "priced_rows_cost_usd_estimate": priced_cost if priced_costs else None,
    }


def summarise_log(path: Path, rates_path: Path | None = None) -> dict:
    """Read one run's log and group only rows that carry an explicit case number."""
    rates = _read_rates(rates_path) if rates_path is not None else {}
    records = []
    by_case = defaultdict(list)
    with path.open(encoding="utf-8") as source:
        for number, line in enumerate(source, 1):
            if not line.strip():
                continue
            try:
                record = _record(json.loads(line), rates)
            except ValueError as exc:
                raise ValueError(f"{path}:{number}: {exc}") from exc
            records.append(record)
            by_case[record["case_number"]].append(record)
    unscoped = by_case.pop(None, [])
    return {
        "pricing_source": PRICING_SOURCE,
        "pricing_basis": "Standard paid text rates, assuming uncached provider input",
        "explicit_rates_usd_per_million": {
            model: {"input_per_million": float(rate[0]), "output_per_million": float(rate[1])}
            for model, rate in rates.items()
        },
        "totals": _summarise(records),
        "by_case": [{"case_number": number, **_summarise(by_case[number])} for number in sorted(by_case)],
        "unscoped": _summarise(unscoped),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG, help=f"one run's JSONL log (default: {DEFAULT_LOG})")
    parser.add_argument("--rates", type=Path, help="JSON of exact model IDs to input_per_million and output_per_million in USD")
    args = parser.parse_args()
    try:
        report = summarise_log(args.log, args.rates)
        print(json.dumps(report, sort_keys=True, allow_nan=False))
    except (OSError, ValueError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
