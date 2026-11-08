"""One function per Gemini role: prompt in, validated typed output out, never an exception.

``run_matcher``, ``run_detective``, ``run_challenger`` and ``run_curator`` share one contract::

    run_<role>(client, material, input_dict, *, budget=None, model=None, second_opinion=None)
        -> RoleResult

Each call loads the role's prompt and schema (``alibi.llm.prompts.load_prompt``), serialises
``input_dict`` as the user message, calls ``client.generate`` (the real ``GeminiClient``, the
``FakeClient`` or a replay client; they share the signature), parses the answer into the
role's pydantic model and runs ``validate_refs`` so every cited ``record_id``/``revision``
names something in the retrieved ``Material``. Undeclared top-level fields are reported, not
used. The Matcher, Detective and Curator run on ``config.FLASH``; the Challenger on
``config.FRONTIER``.

Any ``LLMError`` (budget, outage, schema) or a pydantic ``ValidationError`` becomes a
``RoleError(role, kind, message)`` with a typed ``kind``, so the runner can degrade a claim to
``unresolved`` with a stated reason instead of crashing.

``second_opinion`` is an optional hook ``callable(role, input_dict, output_model) -> (agree,
note)`` (provided by ``alibi.agents.gemma``, which this module never imports). When it
disagrees, the result is ``distrusted``: the runner then treats a Matcher ``linked`` as
``ambiguous`` and Detective claims as ``unresolved``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

from pydantic import BaseModel, ValidationError

from alibi.agents.material import Material
from alibi.agents.models import ValidationFailure, parse_output, undeclared_fields, validate_refs
from alibi.llm import client as llm
from alibi.llm.config import FLASH, FRONTIER
from alibi.llm.prompts import load_prompt

SecondOpinion = Callable[[str, dict[str, Any], BaseModel], tuple[bool, str]]

ROLE_MODEL = {"matcher": FLASH, "detective": FLASH, "challenger": FRONTIER, "curator": FLASH}

KIND_BUDGET = "budget_exceeded"
KIND_UNAVAILABLE = "unavailable"
KIND_SCHEMA = "schema"
KIND_ERROR = "error"


class RoleError(Exception):
    """A role could not produce a usable answer. ``kind`` is a typed reason
    (``budget_exceeded | unavailable | schema | error``); the message names no payload.

    ``model_calls`` and ``tokens`` carry what the failed call cost (``call_cost``): a schema
    failure or an unparseable answer was still a billed model call; a transport failure or a
    budget refusal was not."""

    def __init__(self, role: str, kind: str, message: str, *, model_calls: int = 0, tokens: int = 0,
                 result: Any = None):
        super().__init__(f"{role}: {kind}: {message}")
        self.role, self.kind, self.message = role, kind, message
        self.model_calls, self.tokens, self.result = model_calls, tokens, result

    @property
    def outcome(self) -> str:
        """The matching ``TraceEvent.outcome``."""
        return self.kind if self.kind in ("budget_exceeded", "unavailable") else "error"


def call_cost(result: Any) -> tuple[int, int]:
    """``(model_calls, tokens)`` of one ``client.generate`` result under the shared rule: a
    result served from cache or a replay counts no model call; anything the model answered
    counts one, with its ``usage.total_tokens``. The one-pass baseline applies the same rule."""
    if result is None:
        return 0, 0
    usage = getattr(result, "usage", None)
    tokens = int(getattr(usage, "total_tokens", 0) or 0)
    return (0 if getattr(result, "cached", False) else 1), tokens


@dataclass
class RoleResult:
    """A role's validated answer plus what validation removed and what the call cost."""

    role: str
    output: BaseModel                       # cleaned: unknown refs dropped
    raw: dict[str, Any]                     # the answer as returned
    failures: list[ValidationFailure] = field(default_factory=list)
    undeclared: list[str] = field(default_factory=list)
    result: Any = None                      # alibi.llm.client.Result
    model_id: str = ""
    distrusted: bool = False
    opinion_note: str = ""

    @property
    def cached(self) -> bool:
        return bool(getattr(self.result, "cached", False))

    @property
    def model_calls(self) -> int:
        return call_cost(self.result)[0]

    @property
    def tokens(self) -> int:
        return call_cost(self.result)[1]


def render_input(input_dict: dict[str, Any]) -> str:
    """The user message: the role input as indented JSON (non-JSON values become strings)."""
    return json.dumps(input_dict, ensure_ascii=False, indent=1, default=str)


def run_role(role: str, client: Any, material: Material, input_dict: dict[str, Any], *,
             budget: Any = None, model: str | None = None,
             second_opinion: SecondOpinion | None = None,
             shareable: set[tuple[str, int]] | None = None) -> RoleResult:
    """Run one role end to end (see module doc). Raises ``RoleError`` only."""
    if role not in ROLE_MODEL:
        raise RoleError(role, KIND_ERROR, "unknown role")
    prompt = load_prompt(role)
    model_id = model or ROLE_MODEL[role]
    user = render_input(input_dict)
    try:
        result = client.generate(model_id, prompt.system, user, prompt.schema, budget)
    except llm.BudgetExceeded as exc:
        raise RoleError(role, KIND_BUDGET, str(exc)) from exc
    except llm.Unavailable as exc:
        raise RoleError(role, KIND_UNAVAILABLE, str(exc)) from exc
    except llm.SchemaValidationError as exc:
        # The model answered and the answer failed the schema: a billed call with no usable output.
        raise RoleError(role, KIND_SCHEMA, f"answer failed the schema ({len(exc.problems)} problem(s))",
                        model_calls=1,
                        tokens=int(getattr(getattr(exc, "usage", None), "total_tokens", 0) or 0)) from exc
    except llm.LLMError as exc:
        raise RoleError(role, KIND_ERROR, type(exc).__name__) from exc
    calls, tokens = call_cost(result)
    data = getattr(result, "data", None)
    if not isinstance(data, dict):
        raise RoleError(role, KIND_SCHEMA, "answer is not a JSON object", model_calls=calls, tokens=tokens,
                        result=result)
    try:
        output = parse_output(role, data)
    except ValidationError as exc:
        raise RoleError(role, KIND_SCHEMA, f"answer failed the output model ({exc.error_count()} error(s))",
                        model_calls=calls, tokens=tokens, result=result) from exc
    cleaned, failures = validate_refs(output, material, role=role, shareable=shareable)
    distrusted, note = False, ""
    if second_opinion is not None:
        try:
            agree, note = second_opinion(role, input_dict, cleaned)
            distrusted, note = not bool(agree), str(note or "")
        except Exception as exc:  # noqa: BLE001 - a broken second opinion never blocks the run
            distrusted, note = False, f"second opinion unavailable ({type(exc).__name__})"
    return RoleResult(role=role, output=cleaned, raw=data, failures=failures,
                      undeclared=undeclared_fields(role, data), result=result,
                      model_id=model_id, distrusted=distrusted, opinion_note=note)


def run_matcher(client: Any, material: Material, input_dict: dict[str, Any], *,
                budget: Any = None, model: str | None = None,
                second_opinion: SecondOpinion | None = None) -> RoleResult:
    """Matcher (FLASH): a link proposal for one ambiguous bank entry. Output ``MatcherOutput``."""
    return run_role("matcher", client, material, input_dict, budget=budget, model=model,
                    second_opinion=second_opinion)


def run_detective(client: Any, material: Material, input_dict: dict[str, Any], *,
                  budget: Any = None, model: str | None = None,
                  second_opinion: SecondOpinion | None = None) -> RoleResult:
    """Detective (FLASH): alerts and one candidate claim per question. Output ``DetectiveOutput``."""
    return run_role("detective", client, material, input_dict, budget=budget, model=model,
                    second_opinion=second_opinion)


def run_challenger(client: Any, material: Material, input_dict: dict[str, Any], *,
                   budget: Any = None, model: str | None = None,
                   second_opinion: SecondOpinion | None = None) -> RoleResult:
    """Challenger (FRONTIER): a revised status and at most one retrieval. Output ``ChallengerOutput``."""
    return run_role("challenger", client, material, input_dict, budget=budget, model=model,
                    second_opinion=second_opinion)


def run_curator(client: Any, material: Material, input_dict: dict[str, Any], *,
                budget: Any = None, model: str | None = None,
                second_opinion: SecondOpinion | None = None,
                shareable: set[tuple[str, int]] | None = None) -> RoleResult:
    """Curator (FLASH): disclosures limited to ``shareable`` ``{(record_id, revision)}``, private
    exclusions and gaps. Output ``CuratorOutput``."""
    return run_role("curator", client, material, input_dict, budget=budget, model=model,
                    second_opinion=second_opinion, shareable=shareable)


__all__ = ["ROLE_MODEL", "RoleError", "RoleResult", "call_cost", "render_input", "run_challenger",
           "run_curator", "run_detective", "run_matcher", "run_role"]
