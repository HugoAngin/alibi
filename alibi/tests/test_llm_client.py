"""GeminiClient behaviour without Gemini: cap, cache, retries, schema checks, logging."""

from __future__ import annotations

import json

import pytest
from google.genai import errors as genai_errors

from alibi.llm.client import (
    BudgetExceeded,
    CallBudget,
    GeminiClient,
    RawResponse,
    SchemaValidationError,
    Unavailable,
    prompt_sha256,
)
from alibi.llm.config import FLASH, FRONTIER

SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}, "n": {"type": "integer", "minimum": 0}},
    "required": ["ok", "n"],
}
SYSTEM = "You are a test."
USER = "secret evidence text that must never be logged"


class Usage:
    prompt_token_count = 7
    candidates_token_count = 3
    thoughts_token_count = 2
    total_token_count = 12


class ScriptedTransport:
    """Returns or raises each scripted item in turn, recording every call."""

    def __init__(self, *script):
        self.script = list(script)
        self.calls = []

    def __call__(self, model, system, user, json_schema, thinking):
        self.calls.append({"model": model, "thinking": thinking, "schema": json_schema})
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return RawResponse(text=item, usage_metadata=Usage())


def api_error(code: int) -> genai_errors.APIError:
    return genai_errors.APIError(code, {"error": {"message": f"simulated {code}", "status": "x"}})


def make_client(tmp_path, transport, cap=10, sleep=None):
    sleeps = []
    client = GeminiClient(
        transport=transport,
        cache_dir=tmp_path / "cache",
        log_path=tmp_path / "log.jsonl",
        budget=CallBudget(cap, name="test-process"),
        sleep=sleep or sleeps.append,
    )
    client.sleeps = sleeps
    return client


def read_log(client):
    return [json.loads(line) for line in client.log_path.read_text().splitlines()]


# ----------------------------------------------------------------------- happy path


def test_generate_returns_validated_json_and_usage(tmp_path):
    transport = ScriptedTransport('{"ok": true, "n": 1}')
    client = make_client(tmp_path, transport)
    result = client.generate(FLASH, SYSTEM, USER, SCHEMA)
    assert result.data == {"ok": True, "n": 1}
    assert result.cached is False
    assert result.usage.prompt_tokens == 7 and result.usage.thinking_tokens == 2
    assert result.prompt_sha256 == prompt_sha256(FLASH, SYSTEM, USER, SCHEMA)
    assert transport.calls[0]["thinking"] == "low"


def test_frontier_uses_medium_thinking(tmp_path):
    transport = ScriptedTransport('{"ok": true, "n": 1}')
    client = make_client(tmp_path, transport)
    client.generate(FRONTIER, SYSTEM, USER, SCHEMA)
    assert transport.calls[0]["thinking"] == "medium"


# ----------------------------------------------------------------------- cap


def test_process_cap_raises_typed_budget_exceeded(tmp_path):
    transport = ScriptedTransport('{"ok": true, "n": 1}', '{"ok": true, "n": 2}', '{"ok": true, "n": 3}')
    client = make_client(tmp_path, transport, cap=2)
    client.generate(FLASH, SYSTEM, "one", SCHEMA)
    client.generate(FLASH, SYSTEM, "two", SCHEMA)
    with pytest.raises(BudgetExceeded):
        client.generate(FLASH, SYSTEM, "three", SCHEMA)
    assert len(transport.calls) == 2, "the capped call must not reach the transport"
    assert read_log(client)[-1]["outcome"] == "budget_exceeded"


def test_task_budget_is_enforced_and_refunds_process_cap(tmp_path):
    transport = ScriptedTransport('{"ok": true, "n": 1}', '{"ok": true, "n": 2}')
    client = make_client(tmp_path, transport, cap=10)
    task = CallBudget(1, name="probe")
    client.generate(FLASH, SYSTEM, "one", SCHEMA, task)
    with pytest.raises(BudgetExceeded):
        client.generate(FLASH, SYSTEM, "two", SCHEMA, task)
    assert task.remaining == 0
    assert client.process_budget.used == 1, "a refused task call must not consume the process cap"


def test_cap_reads_env(monkeypatch):
    from alibi.llm import config

    monkeypatch.setenv(config.CAP_ENV, "7")
    assert config.call_cap() == 7
    monkeypatch.delenv(config.CAP_ENV)
    assert config.call_cap() == 120


# ----------------------------------------------------------------------- cache


def test_identical_call_hits_disk_cache_and_spends_nothing(tmp_path):
    transport = ScriptedTransport('{"ok": true, "n": 5}')
    client = make_client(tmp_path, transport, cap=1)
    first = client.generate(FLASH, SYSTEM, USER, SCHEMA)
    second = client.generate(FLASH, SYSTEM, USER, SCHEMA)  # cap is 1; this must not spend
    assert first.cached is False and second.cached is True
    assert second.data == first.data
    assert len(transport.calls) == 1
    assert client.process_budget.used == 1
    outcomes = [e["outcome"] for e in read_log(client)]
    assert outcomes == ["ok", "cache_hit"]

    # A fresh client over the same directory also hits the cache.
    other = make_client(tmp_path, ScriptedTransport(), cap=0)
    assert other.generate(FLASH, SYSTEM, USER, SCHEMA).cached is True


def test_cache_key_covers_model_system_user_and_schema(tmp_path):
    base = prompt_sha256(FLASH, SYSTEM, USER, SCHEMA)
    assert prompt_sha256(FRONTIER, SYSTEM, USER, SCHEMA) != base
    assert prompt_sha256(FLASH, SYSTEM + "x", USER, SCHEMA) != base
    assert prompt_sha256(FLASH, SYSTEM, USER + "x", SCHEMA) != base
    assert prompt_sha256(FLASH, SYSTEM, USER, {**SCHEMA, "required": ["ok"]}) != base
    reordered = {"required": SCHEMA["required"], "properties": SCHEMA["properties"], "type": "object"}
    assert prompt_sha256(FLASH, SYSTEM, USER, reordered) == base


def test_invalid_answer_is_not_cached(tmp_path):
    transport = ScriptedTransport('{"ok": "yes"}', '{"ok": true, "n": 1}')
    client = make_client(tmp_path, transport)
    with pytest.raises(SchemaValidationError):
        client.generate(FLASH, SYSTEM, USER, SCHEMA)
    result = client.generate(FLASH, SYSTEM, USER, SCHEMA)
    assert result.cached is False and result.data["n"] == 1


# ----------------------------------------------------------------------- retries


def test_retries_twice_on_429_then_raises_unavailable(tmp_path):
    transport = ScriptedTransport(api_error(429), api_error(503), api_error(429), '{"ok": true, "n": 1}')
    client = make_client(tmp_path, transport)
    with pytest.raises(Unavailable) as info:
        client.generate(FLASH, SYSTEM, USER, SCHEMA)
    assert len(transport.calls) == 3, "one call plus two retries"
    assert client.sleeps == [2.0, 6.0]
    assert "429" in str(info.value)
    assert read_log(client)[-1]["outcome"] == "unavailable"


def test_recovers_after_one_503(tmp_path):
    transport = ScriptedTransport(api_error(503), '{"ok": true, "n": 9}')
    client = make_client(tmp_path, transport)
    result = client.generate(FLASH, SYSTEM, USER, SCHEMA)
    assert result.data["n"] == 9 and result.attempts == 2
    assert client.sleeps == [2.0]


def test_non_retryable_error_fails_immediately(tmp_path):
    transport = ScriptedTransport(api_error(400), '{"ok": true, "n": 1}')
    client = make_client(tmp_path, transport)
    with pytest.raises(Unavailable):
        client.generate(FLASH, SYSTEM, USER, SCHEMA)
    assert len(transport.calls) == 1 and client.sleeps == []


# ----------------------------------------------------------------------- schema validation


@pytest.mark.parametrize(
    "text",
    [
        '{"ok": true}',  # missing required n
        '{"ok": "true", "n": 1}',  # wrong type
        '{"ok": true, "n": -1}',  # below minimum
        "not json at all",
        "",
    ],
)
def test_schema_failures_raise_typed_error(tmp_path, text):
    client = make_client(tmp_path, ScriptedTransport(text))
    with pytest.raises(SchemaValidationError) as info:
        client.generate(FLASH, SYSTEM, USER, SCHEMA)
    assert info.value.problems
    assert read_log(client)[-1]["outcome"] == "schema_invalid"


# ----------------------------------------------------------------------- log hygiene


def test_log_has_required_fields_and_no_prompt_text(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-fake-key-for-test")
    client = make_client(tmp_path, ScriptedTransport('{"ok": true, "n": 1}'))
    client.generate(FLASH, SYSTEM, USER, SCHEMA)
    raw = client.log_path.read_text()
    assert USER not in raw and SYSTEM not in raw and "AIza-fake-key-for-test" not in raw
    entry = read_log(client)[0]
    assert set(entry) >= {"time", "model", "prompt_sha256", "usage", "cached", "outcome"}
    assert entry["model"] == FLASH and entry["cached"] is False and entry["outcome"] == "ok"
    assert entry["usage"]["total_tokens"] == 12
