import json

import pytest

from agents.eval.llm_cost import summarise_log


def log_row(model="gemini-3.8-flash", **overrides):
    return {
        "time": "2026-11-08T00:00:00+00:00",
        "model": model,
        "prompt_sha256": "a" * 64,
        "usage": {"prompt_tokens": 1000, "output_tokens": 100, "thinking_tokens": 200, "total_tokens": 1300},
        "cached": False,
        "outcome": "ok",
        "attempts": 1,
        **overrides,
    }


def write_log(tmp_path, *rows):
    path = tmp_path / "llm_log.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


def test_official_rates_include_thinking_and_exclude_local_cache_and_budget(tmp_path):
    path = write_log(
        tmp_path,
        log_row(case_number=42),
        log_row(case_number=42, outcome="cache_hit", cached=True),
        log_row(case_number=42, outcome="budget_exceeded", usage={}),
        log_row("gemini-4-argon"),  # Actual S3a format has no case number.
    )

    report = summarise_log(path)

    case, = report["by_case"]
    assert case["case_number"] == 42
    assert case["log_rows"] == 3
    assert case["model_calls"] == case["logged_transport_attempts"] == 1
    assert case["cache_hits"] == case["budget_exceeded"] == 1
    assert (case["prompt_tokens"], case["output_tokens"], case["thinking_tokens"], case["total_tokens"]) == (
        1000, 100, 200, 1300,
    )
    assert case["cost_usd_estimate"] == pytest.approx(0.001875)
    assert report["unscoped"]["model_calls"] == 1
    assert report["unscoped"]["cost_usd_estimate"] == pytest.approx(0.005)
    assert report["totals"]["tokens"] == 2600
    assert report["totals"]["cost_usd_estimate"] == pytest.approx(0.006875)


def test_unknown_models_require_exact_explicit_rate(tmp_path):
    model = "gemini-4-argon-unpriced-example"
    log = write_log(tmp_path, log_row(model))
    report = summarise_log(log)
    assert report["by_case"] == []
    assert report["unscoped"]["unknown_pricing_models"] == [model]
    assert report["totals"]["cost_usd_estimate"] is None
    assert report["totals"]["priced_rows_cost_usd_estimate"] is None

    rates = tmp_path / "rates.json"
    rates.write_text(json.dumps({model: {"input_per_million": 1, "output_per_million": 2}}), encoding="utf-8")
    report = summarise_log(log, rates)
    assert report["explicit_rates_usd_per_million"] == {model: {"input_per_million": 1, "output_per_million": 2}}
    assert report["totals"]["unknown_pricing_models"] == []
    assert report["totals"]["cost_usd_estimate"] == pytest.approx(0.0016)


def test_missing_usage_and_retry_cost_remain_unknown(tmp_path):
    empty_usage = dict.fromkeys(log_row()["usage"], 0)
    path = write_log(
        tmp_path,
        log_row(case_number=42, attempts=2),
        log_row(case_number=42, outcome="schema_invalid"),
        log_row(case_number=43, outcome="unavailable", attempts=3, usage=empty_usage),
        log_row(case_number=44, usage={"prompt_tokens": 1000, "output_tokens": 100, "thinking_tokens": 0}),
    )

    retry_case, unavailable_case, incomplete_case = summarise_log(path)["by_case"]

    assert retry_case["model_calls"] == 2
    assert retry_case["logged_transport_attempts"] == 3
    assert retry_case["rows_with_potential_unmeasured_attempts"] == 2
    assert retry_case["cost_usd_estimate"] is None
    assert retry_case["priced_rows_cost_usd_estimate"] == pytest.approx(0.00375)
    assert unavailable_case["unavailable_rows"] == unavailable_case["rows_missing_tokens"] == 1
    assert unavailable_case["output_tokens"] is unavailable_case["tokens"] is unavailable_case["cost_usd_estimate"] is None
    assert incomplete_case["prompt_tokens"] == 1000
    assert incomplete_case["tokens"] is incomplete_case["cost_usd_estimate"] is None


def test_official_argon_rate_changes_on_published_utc_date(tmp_path):
    path = write_log(tmp_path, log_row("gemini-4-argon", time="2026-12-01T00:01:00+00:00"))

    assert summarise_log(path)["totals"]["cost_usd_estimate"] == pytest.approx(0.010)
