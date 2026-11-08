"""Regression tests for the fixes to agents/eval/out/REDTEAM.md (S3a client findings), NEEDS.md
item 5 (usage on a schema failure) and the S5 log scope (case_number, system, run_id).

Everything runs offline against a scripted transport; no key, no network, no data/alibi/.
"""

from __future__ import annotations

import inspect
import json
import math

import pytest

from agents.eval.llm_cost import summarise_log
from alibi.agents import roles as agent_roles
from alibi.agents.material import Material, challenger_input
from alibi.agents.policy_parse import parse_rule_text
from alibi.agents.roles import RoleError
from alibi.api import load_fixture, open_store
from alibi.core.schemas import RecordKind
from alibi.llm import FakeClient, Scope
from alibi.llm import config
from alibi.llm.client import (
    BudgetExceeded,
    CallBudget,
    GeminiClient,
    RawResponse,
    SchemaValidationError,
    Unavailable,
    Usage,
    gemini_transport,
    parse_json,
)
from alibi.llm.config import FLASH
from alibi.llm.prompts import ROLES, load_prompt
from alibi.llm.schema import open_objects, unsupported_keywords, validate

SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}, "score": {"type": "number", "minimum": 0, "maximum": 1}},
    "required": ["ok", "score"],
    "additionalProperties": False,
}
SYSTEM = "You are a test."
USER = "secret evidence text that must never be logged"


class Meta:
    prompt_token_count = 100
    candidates_token_count = 20
    thoughts_token_count = 5
    total_token_count = 125


class Scripted:
    """Returns or raises each item in turn and records every transport attempt."""

    def __init__(self, *script):
        self.script = list(script)
        self.calls = []

    def __call__(self, model, system, user, json_schema, thinking):
        self.calls.append(json_schema)
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return RawResponse(text=item, usage_metadata=Meta())


class Busy(Exception):
    code = 503


def make_client(tmp_path, transport, *, cap=10, **kwargs):
    return GeminiClient(transport=transport, cache_dir=tmp_path / "cache", log_path=tmp_path / "log.jsonl",
                        budget=CallBudget(cap, name="test-process"), sleep=lambda _: None, **kwargs)


def rows(client):
    return [json.loads(line) for line in client.log_path.read_text(encoding="utf-8").splitlines()]


def challenger_answer(**extra_args):
    return {
        "claim_id": "c1", "proposed_status": "unresolved", "bank_settlement_established": False,
        "payout_reference": "PO-0002-01", "confirming_refs": [], "conflicting_refs": [],
        "missing_evidence": ["Bank settlement record"],
        "next_action": {"type": "lookup_settlement",
                        "args": {"payout_reference": "PO-0002-01", "merchant_id": "mock-marketplace", **extra_args},
                        "reason": "Check whether the payout settled."},
        "trace": [], "rationale": "The visible message describes a release to a platform balance.",
    }


# ------------------------------------------------------------------ 1. undeclared fields


@pytest.mark.parametrize("role", ROLES + ("one_pass",))
def test_every_object_in_every_role_schema_is_closed(role):
    schema = load_prompt(role).schema
    assert open_objects(schema) == [], f"{role}.json leaves objects open to undeclared fields"
    assert unsupported_keywords(schema) == [], f"{role}.json uses a keyword the validator ignores"


@pytest.mark.parametrize(
    ("role", "mutate"),
    [
        ("challenger", lambda a: a["next_action"]["args"].__setitem__("subject_ref", "other-holder")),
        ("challenger", lambda a: a.__setitem__("verdict", "trust me")),
        ("challenger", lambda a: a["next_action"].__setitem__("urgent", True)),
    ],
)
def test_undeclared_fields_fail_validation_at_every_level(role, mutate):
    answer = challenger_answer()
    mutate(answer)
    problems = validate(answer, load_prompt(role).schema)
    assert problems and all("unexpected property" in p for p in problems)


def test_policy_parse_rejects_undeclared_params_through_the_client(tmp_path):
    prompt = load_prompt("policy_parse")
    answer = {"rules": [{"rule_id": "r1", "kind": "deny", "target": "category", "value": "medical",
                         "params": {"allow_auto_share": True}, "source_text": "Do not share medical items.",
                         "confidence": 0.9}], "clarifications": [], "plain_summary": "Medical items withheld."}
    client = make_client(tmp_path, Scripted(json.dumps(answer)))
    with pytest.raises(SchemaValidationError) as info:
        client.generate(FLASH, prompt.system, "{}", prompt.schema)
    assert any("allow_auto_share" in p for p in info.value.problems)
    assert "allow_auto_share" not in client.log_path.read_text(encoding="utf-8")


# ------------------------------------------------------------------ 2. non-finite numbers


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_parse_json_refuses_nonfinite_literals(literal):
    with pytest.raises(ValueError):
        parse_json('{"score": %s}' % literal)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_validator_rejects_nonfinite_numbers(value):
    problems = validate({"ok": True, "score": value}, SCHEMA)
    assert problems and "non-finite" in problems[0]
    # Even a bare number schema with no bounds refuses it.
    assert validate(value, {"type": "number"})


@pytest.mark.parametrize("literal", ["NaN", "Infinity"])
def test_client_turns_nonfinite_answer_into_schema_error_with_usage(tmp_path, literal):
    client = make_client(tmp_path, Scripted('{"ok": true, "score": %s}' % literal))
    with pytest.raises(SchemaValidationError) as info:
        client.generate(FLASH, SYSTEM, USER, SCHEMA)
    assert info.value.usage.total_tokens == 125
    assert rows(client)[-1]["outcome"] == "schema_invalid"
    assert not list(client.cache_dir.glob("*.json")), "a rejected answer is never cached"


def test_fake_client_refuses_nonfinite_scripted_confidence():
    prompt = load_prompt("policy_parse")
    answer = {"rules": [{"rule_id": "r1", "kind": "deny", "target": "category", "value": "medical", "params": {},
                         "source_text": "x", "confidence": math.nan}], "clarifications": [], "plain_summary": "x"}
    with pytest.raises(SchemaValidationError):
        FakeClient({"*": answer}).generate(FLASH, prompt.system, "{}", prompt.schema)


# ------------------------------------------------------------------ 3. retries spend budget


@pytest.mark.parametrize(("process_cap", "task_cap"), [(1, 5), (5, 1)], ids=["process", "task"])
def test_a_cap_of_one_means_exactly_one_transport_attempt(tmp_path, process_cap, task_cap):
    transport = Scripted(Busy(), Busy(), Busy())
    client = make_client(tmp_path, transport, cap=process_cap)
    task = CallBudget(task_cap, name="probe")
    with pytest.raises(Unavailable) as info:
        client.generate(FLASH, SYSTEM, USER, SCHEMA, task)
    assert len(transport.calls) == 1
    assert "no budget left to retry" in str(info.value)
    row = rows(client)[-1]
    assert row["outcome"] == "unavailable" and row["attempts"] == 1
    # The one attempt is accounted for exactly once on each budget.
    assert client.process_budget.used == 1 and task.used == 1


def test_each_retry_spends_both_budgets(tmp_path):
    transport = Scripted(Busy(), Busy(), '{"ok": true, "score": 0.5}')
    client = make_client(tmp_path, transport, cap=10)
    task = CallBudget(3, name="probe")
    result = client.generate(FLASH, SYSTEM, USER, SCHEMA, task)
    assert result.attempts == 3 and len(transport.calls) == 3
    assert client.process_budget.used == 3 and task.used == 3
    assert rows(client)[-1]["attempts"] == 3


def test_retry_refused_by_task_budget_refunds_the_process_cap(tmp_path):
    transport = Scripted(Busy(), Busy())
    client = make_client(tmp_path, transport, cap=10)
    with pytest.raises(Unavailable):
        client.generate(FLASH, SYSTEM, USER, SCHEMA, CallBudget(1, name="probe"))
    assert client.process_budget.used == 1, "the refused retry must not be charged to the process cap"
    assert len(transport.calls) == 1


def test_first_attempt_refused_by_budget_still_logs_budget_exceeded(tmp_path):
    client = make_client(tmp_path, Scripted(), cap=0)
    with pytest.raises(BudgetExceeded):
        client.generate(FLASH, SYSTEM, USER, SCHEMA)
    assert rows(client)[-1]["outcome"] == "budget_exceeded"


# ------------------------------------------------------------------ 4. cache revalidation


def test_edited_cache_entry_is_ignored_and_replaced_by_a_fresh_call(tmp_path):
    prompt = load_prompt("challenger")
    good = challenger_answer()
    transport = Scripted(json.dumps(good), json.dumps(good))
    client = make_client(tmp_path, transport)
    first = client.generate(FLASH, prompt.system, "{}", prompt.schema)
    path = client.cache_dir / f"{first.prompt_sha256}.json"
    entry = json.loads(path.read_text(encoding="utf-8"))
    entry["data"]["next_action"]["type"] = "dump_passport"
    path.write_text(json.dumps(entry), encoding="utf-8")

    replay = client.generate(FLASH, prompt.system, "{}", prompt.schema)
    assert replay.cached is False and replay.data == good
    assert len(transport.calls) == 2
    assert json.loads(path.read_text(encoding="utf-8"))["data"] == good, "the fresh answer overwrites the bad entry"
    assert [r["outcome"] for r in rows(client)] == ["ok", "ok"]


def test_cache_entry_is_checked_against_the_schema_in_force_now(tmp_path):
    transport = Scripted('{"ok": true, "score": 0.5}', '{"ok": true, "score": 0.5}')
    client = make_client(tmp_path, transport)
    client.generate(FLASH, SYSTEM, USER, SCHEMA)
    # Same prompt, same schema: served from cache.
    assert client.generate(FLASH, SYSTEM, USER, SCHEMA).cached is True
    # Hand-write a cache file whose data has a non-finite float: ignored, not returned.
    key = client.generate(FLASH, SYSTEM, USER, SCHEMA).prompt_sha256
    path = client.cache_dir / f"{key}.json"
    path.write_text('{"model": "m", "data": {"ok": true, "score": NaN}, "usage": {}}', encoding="utf-8")
    fresh = client.generate(FLASH, SYSTEM, USER, SCHEMA)
    assert fresh.cached is False and fresh.data["score"] == 0.5
    assert len(transport.calls) == 2


def test_malformed_cache_entry_is_a_miss(tmp_path):
    transport = Scripted('{"ok": true, "score": 0.5}', '{"ok": true, "score": 0.5}')
    client = make_client(tmp_path, transport)
    key = client.generate(FLASH, SYSTEM, USER, SCHEMA).prompt_sha256
    (client.cache_dir / f"{key}.json").write_text('{"model": "m", "usage": {"prompt_tokens": "lots"}}', encoding="utf-8")
    assert client.generate(FLASH, SYSTEM, USER, SCHEMA).cached is False
    assert Usage.from_dict({"prompt_tokens": "lots", "total_tokens": 3, "extra": 1}) == Usage(total_tokens=3)


# ------------------------------------------------------------------ 5. usage on schema failure


def test_schema_validation_error_carries_the_billed_usage(tmp_path):
    client = make_client(tmp_path, Scripted('{"ok": "yes", "score": 0.1}'))
    with pytest.raises(SchemaValidationError) as info:
        client.generate(FLASH, SYSTEM, USER, SCHEMA)
    assert info.value.usage == Usage(prompt_tokens=100, output_tokens=20, thinking_tokens=5, total_tokens=125)
    assert rows(client)[-1]["usage"]["total_tokens"] == 125


@pytest.mark.parametrize("text", ["", "not json"])
def test_unparseable_answers_also_carry_usage(tmp_path, text):
    client = make_client(tmp_path, Scripted(text))
    with pytest.raises(SchemaValidationError) as info:
        client.generate(FLASH, SYSTEM, USER, SCHEMA)
    assert info.value.usage.total_tokens == 125


def test_schema_validation_error_defaults_to_zero_usage():
    assert SchemaValidationError(["x"]).usage == Usage()
    fake = FakeClient({"*": {"ok": "yes"}})
    with pytest.raises(SchemaValidationError) as info:
        fake.generate(FLASH, "", "x", {"type": "object", "properties": {"ok": {"type": "boolean"}}})
    assert info.value.usage == fake.usage


# ------------------------------------------------------------------ 6. log scope


def test_unscoped_rows_carry_null_scope_fields(tmp_path):
    client = make_client(tmp_path, Scripted('{"ok": true, "score": 0.5}'))
    client.generate(FLASH, SYSTEM, USER, SCHEMA)
    row = rows(client)[0]
    assert {"case_number": None, "system": None, "run_id": None}.items() <= row.items()


def test_scope_from_constructor_is_on_every_row(tmp_path):
    scope = Scope(case_number=7, system="alibi", run_id="run-2026-11-08-a")
    client = make_client(tmp_path, Scripted(Busy(), '{"ok": true, "score": 0.5}', '{"ok": "no"}'), scope=scope)
    client.generate(FLASH, SYSTEM, USER, SCHEMA)        # one retry, then ok
    client.generate(FLASH, SYSTEM, USER, SCHEMA)        # cache hit
    with pytest.raises(SchemaValidationError):
        client.generate(FLASH, SYSTEM, "other", SCHEMA)  # schema_invalid
    with pytest.raises(BudgetExceeded):
        make_client(tmp_path, Scripted(), cap=0, scope=scope).generate(FLASH, SYSTEM, "third", SCHEMA)
    logged = rows(client)
    assert [r["outcome"] for r in logged] == ["ok", "cache_hit", "schema_invalid", "budget_exceeded"]
    assert all((r["case_number"], r["system"], r["run_id"]) == (7, "alibi", "run-2026-11-08-a") for r in logged)


def test_scoped_context_manager_sets_and_restores(tmp_path):
    client = make_client(tmp_path, Scripted('{"ok": true, "score": 0.5}', '{"ok": true, "score": 0.5}',
                                            '{"ok": true, "score": 0.5}'), scope=Scope(system="alibi", run_id="r1"))
    with client.scoped(case_number=1) as same:
        assert same is client
        client.generate(FLASH, SYSTEM, "case one", SCHEMA)
        with client.scoped(case_number=2, system="gemini_onepass"):
            client.generate(FLASH, SYSTEM, "case two", SCHEMA)
        assert client.scope == Scope(case_number=1, system="alibi", run_id="r1")
    assert client.scope == Scope(system="alibi", run_id="r1")
    client.generate(FLASH, SYSTEM, "after", SCHEMA)
    logged = rows(client)
    assert [(r["case_number"], r["system"]) for r in logged] == [(1, "alibi"), (2, "gemini_onepass"), (None, "alibi")]


def test_scoped_restores_after_an_exception(tmp_path):
    client = make_client(tmp_path, Scripted(Busy(), Busy(), Busy()))
    with pytest.raises(Unavailable):
        with client.scoped(case_number=3, system="alibi", run_id="r"):
            client.generate(FLASH, SYSTEM, USER, SCHEMA)
    assert client.scope == Scope()
    assert rows(client)[-1]["case_number"] == 3


def test_set_scope_clears_with_none_and_returns_previous(tmp_path):
    client = make_client(tmp_path, Scripted(), scope=Scope(case_number=4, system="rules", run_id="r"))
    previous = client.set_scope(case_number=None)
    assert previous == Scope(case_number=4, system="rules", run_id="r")
    assert client.scope == Scope(case_number=None, system="rules", run_id="r")


@pytest.mark.parametrize("bad", [{"case_number": "7"}, {"case_number": True}, {"system": 3}, {"run_id": 1}])
def test_scope_rejects_values_llm_cost_cannot_read(bad):
    with pytest.raises(TypeError):
        Scope(**bad)


def test_scoped_log_stays_free_of_prompt_text_and_answers(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-fake-key-for-test")
    client = make_client(tmp_path, Scripted('{"ok": true, "score": 0.25}'), api_key="AIza-fake-key-for-test")
    with client.scoped(case_number=1, system="alibi", run_id="r1"):
        client.generate(FLASH, SYSTEM, USER, SCHEMA)
    raw = client.log_path.read_text(encoding="utf-8")
    assert USER not in raw and SYSTEM not in raw and "AIza-fake-key-for-test" not in raw and "0.25" not in raw


def test_llm_cost_groups_the_client_log_by_case_and_reports_unscoped(tmp_path):
    client = make_client(tmp_path, Scripted('{"ok": true, "score": 0.5}', '{"ok": true, "score": 0.5}',
                                            '{"ok": true, "score": 0.5}'))
    with client.scoped(case_number=1, system="alibi", run_id="r1"):
        client.generate(FLASH, SYSTEM, "one", SCHEMA)
        client.generate(FLASH, SYSTEM, "one", SCHEMA)  # cache hit
    with client.scoped(case_number=2, system="alibi", run_id="r1"):
        client.generate(FLASH, SYSTEM, "two", SCHEMA)
    client.generate(FLASH, SYSTEM, "stray", SCHEMA)
    report = summarise_log(client.log_path)
    assert [(c["case_number"], c["model_calls"], c["cache_hits"]) for c in report["by_case"]] == [(1, 1, 1), (2, 1, 0)]
    assert report["unscoped"]["model_calls"] == 1
    assert report["totals"]["model_calls"] == 3 and report["totals"]["tokens"] == 375


def test_fake_client_records_scope_per_call():
    fake = FakeClient({"*": {"ok": True}})
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
    with fake.scoped(case_number=9, system="alibi", run_id="r"):
        fake.generate(FLASH, "", "x", schema)
    fake.generate(FLASH, "", "x", schema)
    assert fake.calls[0].scope == Scope(case_number=9, system="alibi", run_id="r")
    assert fake.calls[1].scope == Scope()


# ------------------------------------------------------------------ 7. one fresh log per system and run


def test_fresh_log_path_creates_an_empty_file_under_llm_logs(tmp_path, monkeypatch):
    monkeypatch.setenv("ALIBI_DATA_DIR", str(tmp_path / "data"))
    path = config.fresh_log_path("alibi", "run-1")
    assert path == tmp_path / "data" / "llm_logs" / "alibi_run-1.jsonl"
    assert path.is_file() and path.stat().st_size == 0
    assert config.log_path() == tmp_path / "data" / "llm_log.jsonl", "the default log is untouched"


def test_fresh_log_path_refuses_to_truncate_a_used_log_unless_told(tmp_path, monkeypatch):
    monkeypatch.setenv("ALIBI_DATA_DIR", str(tmp_path / "data"))
    path = config.fresh_log_path("rules", "r")
    path.write_text('{"row": 1}\n', encoding="utf-8")
    with pytest.raises(FileExistsError):
        config.fresh_log_path("rules", "r")
    assert path.read_text(encoding="utf-8") == '{"row": 1}\n'
    assert config.fresh_log_path("rules", "r", overwrite=True).stat().st_size == 0


def test_fresh_log_path_sanitises_names_and_rejects_empty_ones(tmp_path, monkeypatch):
    monkeypatch.setenv("ALIBI_DATA_DIR", str(tmp_path / "data"))
    path = config.fresh_log_path("gemini_onepass", "2026-11-08T01:02:03+00:00/x")
    assert path.name == "gemini_onepass_2026-11-08T01-02-03-00-00-x.jsonl"
    assert path.parent == tmp_path / "data" / "llm_logs"
    for system, run_id in (("", "r"), ("alibi", " "), ("///", "r")):
        with pytest.raises(ValueError):
            config.fresh_log_path(system, run_id)


def test_for_run_gives_a_fresh_scoped_log(tmp_path, monkeypatch):
    monkeypatch.setenv("ALIBI_DATA_DIR", str(tmp_path / "data"))
    client = GeminiClient.for_run("alibi", "run-7", transport=Scripted('{"ok": true, "score": 0.5}'),
                                  cache_dir=tmp_path / "cache", budget=CallBudget(5), sleep=lambda _: None)
    assert client.log_path == tmp_path / "data" / "llm_logs" / "alibi_run-7.jsonl"
    with client.scoped(case_number=12):
        client.generate(FLASH, SYSTEM, USER, SCHEMA)
    row, = rows(client)
    assert (row["case_number"], row["system"], row["run_id"]) == (12, "alibi", "run-7")
    with pytest.raises(TypeError):
        GeminiClient.for_run("alibi", "run-8", log_path=tmp_path / "x.jsonl")


# ------------------------------------------------------------------ 6. the roles layer behind a permissive client
#
# Closed schemas stop an undeclared field at the client, so it never reaches a role. The roles
# layer keeps its own undeclared-field report for clients that do not validate (a
# FakeClient(validate=False), a replay client). alibi/tests/test_agents_roles.py::
# test_undeclared_fields_are_reported_not_used and alibi/tests/test_agents_policy_parse.py::
# test_undeclared_field_is_ignored_and_reported exercise that layer and now need
# FakeClient(..., validate=False); these tests show both halves of the split.

POLICY_MATERIAL = {
    "label": "SYNTHETIC",
    "categories": ["health", "rent", "transport"],
    "merchants": ["synthetic-pharmacy", "synthetic-grocer"],
    "record_kinds": ["sale", "purchase", "refund", "payout", "bank_credit"],
    "fields": ["amount_minor", "item", "message", "account_ref", "counterparty_name", "description"],
    "recipients": ["bank_a", "bank_b"],
}


@pytest.fixture
def challenger_fx(tmp_path, keystore):
    store = open_store(tmp_path / "p.db")
    fixture = load_fixture(store, "supported", keystore=keystore)
    material = Material(fixture["subject_ref"])
    for record in store.list_sources(fixture["subject_ref"]):
        material.add_record(record)
    txn = store.get_bank_txn("txn_9001")
    material.add_txn(txn)
    inp = challenger_input(material, {"claim_id": "c1", "status": "unresolved"}, [txn],
                           [r for r in material.records() if r.kind != RecordKind.SETTLEMENT],
                           material.records(kinds=[RecordKind.SETTLEMENT]), round_index=0, actions_left=3)
    try:
        yield material, inp
    finally:
        store.close()


def undeclared_challenger_answer():
    return dict(challenger_answer(), payout_reference="PO-5001",
                confirming_refs=[{"record_id": "po_5001", "revision": 1, "why": "reference"}],
                verdict="trust me")


def test_closed_schema_stops_an_undeclared_field_at_the_client(challenger_fx):
    material, inp = challenger_fx
    client = FakeClient({"*": undeclared_challenger_answer()})
    with pytest.raises(RoleError) as info:
        agent_roles.run_challenger(client, material, inp)
    assert info.value.kind == "schema" and info.value.role == "challenger"
    assert isinstance(client.calls[-1].error, SchemaValidationError)
    assert any("verdict" in p for p in client.calls[-1].error.problems)
    # a billed call that failed the schema is still counted (NEEDS.md item 5)
    assert info.value.model_calls == 1 and info.value.tokens == client.usage.total_tokens


def test_roles_layer_still_reports_undeclared_fields_behind_a_permissive_client(challenger_fx):
    material, inp = challenger_fx
    rr = agent_roles.run_challenger(FakeClient({"*": undeclared_challenger_answer()}, validate=False), material, inp)
    assert rr.undeclared == ["verdict"] and not hasattr(rr.output, "verdict")
    assert [r.record_id for r in rr.output.confirming_refs] == ["po_5001"]


def test_policy_parse_still_reports_undeclared_fields_behind_a_permissive_client():
    text = "Do not share my account number."
    data = {"rules": [{"rule_id": "r1", "kind": "deny", "target": "field", "value": "account_ref", "params": {},
                       "source_text": "x", "confidence": 0.95}],
            "clarifications": [], "plain_summary": "Summary.", "grant_access": True}
    strict = parse_rule_text(text, client=FakeClient({text: data}), material=POLICY_MATERIAL)
    assert strict.status == "unresolved" and strict.rules == []
    assert "SchemaValidationError" in strict.reason and "unexpected property" in strict.reason
    permissive = parse_rule_text(text, client=FakeClient({text: data}, validate=False), material=POLICY_MATERIAL)
    assert permissive.status == "resolved" and len(permissive.rules) == 1
    assert permissive.warnings == ["undeclared field ignored: grant_access"]


# ------------------------------------------------------------------ 7. the closed schemas reach Gemini unchanged
#
# Offline check of the SDK side only. google-genai sends response_json_schema through as-is
# (t_json_schema is the identity), unlike response_schema, which it rewrites into its typed
# Schema. The first live run (alibi.llm.probe / try_prompts) must still confirm the service
# accepts additionalProperties:false; no key or network is used here.


def test_transport_sends_the_schema_as_json_schema_not_as_a_typed_schema():
    src = inspect.getsource(gemini_transport)
    assert "response_json_schema=json_schema" in src
    assert "response_schema=" not in src


@pytest.mark.parametrize("role", ROLES + ("one_pass",))
def test_sdk_forwards_the_closed_role_schema_untouched_to_the_developer_api(role):
    types = pytest.importorskip("google.genai.types")
    models = pytest.importorskip("google.genai.models")
    schema = load_prompt(role).schema
    cfg = types.GenerateContentConfig(
        system_instruction="s", response_mime_type="application/json", response_json_schema=schema,
        thinking_config=types.ThinkingConfig(thinking_level=types.ThinkingLevel.LOW))
    request = models._GenerateContentConfig_to_mldev(None, cfg)
    assert request["responseJsonSchema"] == schema
    assert "responseSchema" not in request
    assert open_objects(request["responseJsonSchema"]) == []
    assert '"additionalProperties": false' in json.dumps(request["responseJsonSchema"])
