"""Score the /rules parser chain on execution's policy fixtures, read back in team vocabulary.

    PYTHONUTF8=1 uv run --frozen python -m agents.lead.out.policy_harness_chain            # 40 fixture rules
    PYTHONUTF8=1 uv run --frozen python -m agents.lead.out.policy_harness_chain --holdout  # 12 held-out paraphrases

Scoring is agents.execution.policy_harness's own (``evaluate``, ``load_policy_rules``, the holdout
file); only the adapter differs. The chain (``agents.lead.api.parse_rule_text``, that is
``alibi.app.policy_chain``) returns core rules (``alibi.core.schemas.PolicyRule``) whose field
paths and record kinds are core names. Execution's ``rules`` adapter, ``lead_rule_to_fixture``,
reads those core names as if they were team names, so a deny on core ``receipt`` (the team's
``purchase``), a generalise on ``items.description`` (``item``), a deny on ``text`` (``message``) or
on ``payment_ref`` (``payment_reference``) scores as a leak or as invalid although the compiled
rule covers exactly the asked restriction. This adapter translates the names back through
``alibi.agents.policy_parse.CORE_FIELD_MAP`` and ``CORE_KIND_MAP`` before scoring; nothing else
changes. Gemini is never called: the chain is asked for its deterministic layers only.

Per case a non-correct verdict is printed as ``<set> <id> <verdict> <why>``. Then one JSON object
per set, and the last line is one flat JSON object of numbers for ``tools/results.py``
(``chain_*`` for the fixture, ``chain_holdout_*`` for the holdout).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Callable

from agents.execution import policy_harness as harness
from agents.execution.policy_harness import FIELD_ALIASES
from alibi.agents.policy_parse import CORE_FIELD_MAP

#: Core field path -> the team vocabulary name the fixtures use (reverse of CORE_FIELD_MAP).
FIELD_BACK = {core: team for team, core in CORE_FIELD_MAP.items()}
#: Core record kind -> team record kind. ``receipt`` is both ``purchase`` and ``receipt`` in the
#: team vocabulary; the fixtures say ``purchase``. Other kinds share their name.
KIND_BACK = {"order": "sale", "receipt": "purchase", "message": "balance_release"}


def team_field(path: str) -> str:
    if path in FIELD_BACK:
        return FIELD_BACK[path]
    last = path.split(".")[-1]
    return FIELD_ALIASES.get(last, last)


def core_rule_to_fixture(rule: dict) -> list[dict]:
    """``lead_rule_to_fixture`` with core names translated back to the team vocabulary first."""
    translated = dict(rule)
    translated["fields"] = [team_field(str(f)) for f in rule.get("fields") or []]
    translated["kinds"] = [KIND_BACK.get(str(k), str(k)) for k in rule.get("kinds") or []]
    return harness.lead_rule_to_fixture(translated)


def chain_parser() -> Callable:
    from agents.lead.api import parse_rule_text

    def parse(text: str, material: dict):
        parsed = parse_rule_text(text, use_gemini=False)   # deterministic layers only, never Gemini
        if parsed.status != "resolved" or parsed.rule is None:
            return {"rules": [], "clarifications": [{"source_text": text, "question": parsed.reason or ""}]}
        rules = core_rule_to_fixture(parsed.rule.model_dump(mode="json"))
        return {"rules": [dict(r, confidence=0.95) for r in rules], "clarifications": []}

    return parse


def holdout_cases() -> list[dict]:
    path = Path(harness.__file__).resolve().parent / "out" / "policy_rules_holdout.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main(argv: list[str] | None = None) -> dict:
    argv = sys.argv[1:] if argv is None else argv
    holdout = "--holdout" in argv
    name = "chain_holdout" if holdout else "chain"
    result = harness.evaluate(chain_parser(), holdout_cases() if holdout else None)
    for row in result["rows"]:
        if row["verdict"] != "correct":
            print(f"{name} {row['id']:6} {row['verdict']:10} {row.get('why', '')}")
    metrics = result["metrics"]
    print(json.dumps({name: {"status": "ran", **metrics}}))
    flat = {f"{name}_{k}": v for k, v in metrics.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}
    print(json.dumps(flat))
    return metrics


if __name__ == "__main__":
    main()
