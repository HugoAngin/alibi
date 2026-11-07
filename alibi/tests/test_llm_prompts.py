"""Every role prompt loads, its schema is usable, and its worked example conforms to that schema."""

from __future__ import annotations

import json
import re

import pytest

from alibi.llm.prompts import ROLES, Prompt, load_prompt
from alibi.llm.schema import validate

EXPECTED_BLOCK = re.compile(r"Expected output:\s*```json\s*(.*?)```", re.DOTALL)
CANONICAL = ("240000", "18000", "6000", "216000")


def expected_outputs(prompt: Prompt) -> list[dict]:
    blocks = EXPECTED_BLOCK.findall(prompt.system)
    assert blocks, f"{prompt.name}.md has no 'Expected output:' json block"
    return [json.loads(b) for b in blocks]


@pytest.mark.parametrize("role", ROLES)
def test_prompt_loads_with_schema(role):
    p = load_prompt(role)
    assert p.system.startswith("#"), "prompt starts with a title"
    assert p.schema["type"] == "object" and p.schema["required"]
    assert f"`{role}.json`" in p.system, "prompt names its schema file"


@pytest.mark.parametrize("role", ROLES)
def test_prompt_states_the_shared_rules(role):
    text = load_prompt(role).system
    assert "`record_id` and `revision`" in text, "reference rule"
    assert "supplied" in text and "Never invent" in text
    assert "is data" in text and "never an instruction" in text, "text-is-data rule"
    assert "## Worked example" in text and "## Output" in text


@pytest.mark.parametrize("role", ROLES)
def test_worked_example_conforms_to_schema(role):
    p = load_prompt(role)
    for example in expected_outputs(p):
        assert validate(example, p.schema) == []


@pytest.mark.parametrize("role", ["matcher", "detective", "challenger", "curator"])
def test_worked_example_uses_canonical_amounts(role):
    text = load_prompt(role).system
    for amount in CANONICAL:
        assert amount in text, f"{role}.md should use the canonical amount {amount}"


def test_challenger_prompt_covers_next_action_and_settlement_distinction():
    p = load_prompt("challenger")
    assert "lookup_settlement" in p.system and "refresh_txn" in p.system
    assert "Platform balance versus bank settlement" in p.system
    assert "seller balance" in p.system
    next_action = p.schema["properties"]["next_action"]
    assert next_action["properties"]["type"]["enum"] == ["lookup_settlement", "refresh_txn", "none"]
    assert "args" in next_action["required"]
    example = expected_outputs(p)[0]
    assert example["proposed_status"] == "unresolved"
    assert example["bank_settlement_established"] is False
    assert example["next_action"] == {
        "type": "lookup_settlement",
        "args": {"payout_reference": "RH-PO-7781", "merchant_id": "resalehub", "record_id": "po_7781"},
        "reason": example["next_action"]["reason"],
    }


def test_example_references_exist_in_example_input():
    """Every record_id cited in a worked example appears in that prompt's input material."""
    for role in ROLES:
        p = load_prompt(role)
        text = p.system
        for example in expected_outputs(p):
            for rid in _record_ids(example):
                assert f'"{rid}"' in text.split("Expected output:")[0], f"{role}: {rid} not in input material"


def _record_ids(obj) -> set[str]:
    found = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in ("record_id", "chosen_record_id") and isinstance(v, str) and v:
                found.add(v)
            found |= _record_ids(v)
    elif isinstance(obj, list):
        for item in obj:
            found |= _record_ids(item)
    return found
