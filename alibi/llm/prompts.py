"""Load a role prompt and its output schema from alibi/agents/prompts/.

    from alibi.llm.prompts import load_prompt
    p = load_prompt("challenger")
    client.generate(FRONTIER, p.system, json.dumps(material), p.schema, budget)

The .md file is the system prompt verbatim. The sibling .json file is the JSON schema the
model must return, passed to Gemini as response_json_schema and checked again locally.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

PROMPT_DIR = Path(__file__).resolve().parents[1] / "agents" / "prompts"
ROLES = ("matcher", "detective", "challenger", "curator", "policy_parse")


@dataclass(frozen=True)
class Prompt:
    name: str
    system: str
    schema: dict
    path: Path


@lru_cache(maxsize=None)
def load_prompt(name: str) -> Prompt:
    md = PROMPT_DIR / f"{name}.md"
    js = PROMPT_DIR / f"{name}.json"
    if not md.is_file() or not js.is_file():
        raise FileNotFoundError(f"prompt {name!r} needs both {md.name} and {js.name} in {PROMPT_DIR}")
    return Prompt(
        name=name,
        system=md.read_text(encoding="utf-8"),
        schema=json.loads(js.read_text(encoding="utf-8")),
        path=md,
    )
