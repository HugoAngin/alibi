"""FakeClient: the scripted stand-in other sessions use instead of Gemini."""

from __future__ import annotations

import pytest

from alibi.llm.client import BudgetExceeded, CallBudget, SchemaValidationError, Unavailable
from alibi.llm.config import FLASH
from alibi.llm.fake import FakeClient
from alibi.llm.prompts import load_prompt

SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}


def test_scripted_dict_is_matched_on_user_text_and_recorded():
    fake = FakeClient({"challenger": {"ok": True}, "matcher": {"ok": False}})
    r = fake.generate(FLASH, "system", "please run the matcher now", SCHEMA)
    assert r.data == {"ok": False}
    assert fake.call_count == 1 and fake.calls[0].answer == {"ok": False}
    assert r.model == FLASH and r.cached is False


def test_system_prompt_and_fallback_keys():
    fake = FakeClient({"challenger": {"ok": True}, "*": {"ok": False}})
    assert fake.generate(FLASH, "# Challenger\n...", "input", SCHEMA).data == {"ok": True}
    assert fake.generate(FLASH, "# Curator", "input", SCHEMA).data == {"ok": False}


def test_sequence_callable_and_exception_scripts():
    fake = FakeClient({
        "seq": [{"ok": True}, {"ok": False}],
        "fn": lambda model, system, user, schema: {"ok": "fn" in user},
        "boom": Unavailable("simulated outage"),
    })
    assert fake.generate(FLASH, "", "seq", SCHEMA).data["ok"] is True
    assert fake.generate(FLASH, "", "seq", SCHEMA).data["ok"] is False
    with pytest.raises(IndexError):
        fake.generate(FLASH, "", "seq", SCHEMA)
    assert fake.generate(FLASH, "", "fn", SCHEMA).data["ok"] is True
    with pytest.raises(Unavailable):
        fake.generate(FLASH, "", "boom", SCHEMA)
    assert isinstance(fake.calls[-1].error, Unavailable)


def test_answers_are_validated_against_the_schema_unless_disabled():
    fake = FakeClient({"*": {"ok": "yes"}})
    with pytest.raises(SchemaValidationError):
        fake.generate(FLASH, "", "x", SCHEMA)
    assert FakeClient({"*": {"ok": "yes"}}, validate=False).generate(FLASH, "", "x", SCHEMA).data == {"ok": "yes"}


def test_budget_is_honoured():
    fake = FakeClient({"*": {"ok": True}})
    budget = CallBudget(1)
    fake.generate(FLASH, "", "x", SCHEMA, budget)
    with pytest.raises(BudgetExceeded):
        fake.generate(FLASH, "", "x", SCHEMA, budget)


def test_missing_script_is_a_clear_error():
    with pytest.raises(KeyError):
        FakeClient({"matcher": {"ok": True}}).generate(FLASH, "", "curator", SCHEMA)


def test_returned_data_is_a_copy():
    script = {"ok": True}
    fake = FakeClient({"*": script})
    fake.generate(FLASH, "", "x", SCHEMA).data["ok"] = False
    assert script["ok"] is True


def test_worked_example_from_prompt_can_be_scripted():
    """The Challenger example in the prompt file is a valid FakeClient script for that schema."""
    from alibi.tests.test_llm_prompts import expected_outputs

    prompt = load_prompt("challenger")
    example = expected_outputs(prompt)[0]
    fake = FakeClient({"challenger": example})
    out = fake.generate(FLASH, prompt.system, '{"claim": {"claim_id": "c1"}}', prompt.schema).data
    assert out["next_action"]["type"] == "lookup_settlement"
    assert out["bank_settlement_established"] is False
