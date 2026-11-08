"""Gemma as an independent second parser for the matcher and detective roles.

The primary roles run on Gemini. A second, unrelated model re-reads the same prompt and
material and must reach the same structured answer before the runner treats a proposal
as confirmed. Disagreement, or a missing second opinion, never silently trusts the
primary answer: both come back as ``agree=False`` with a reason.

    from alibi.agents.gemma import make_second_opinion
    second = make_second_opinion(client)            # None when the key sees no Gemma model
    if second is not None:
        agree, note = second("matcher", matcher_input, matcher_output)

Comparison rules
----------------
* matcher: ``decision`` and ``(chosen_record_id, chosen_revision)`` must be equal.
* detective: per ``question_id`` the ``status`` and the set of ``(record_id, revision)``
  refs with role ``confirms`` must be equal; ``context`` and ``conflicts`` refs may differ.
* Any ``LLMError`` or pydantic ``ValidationError`` -> ``(False, "second parser unavailable: ...")``.

CLI (needs GEMINI_API_KEY; at most one real call; exit 2 without a key):

    uv run --frozen python -m alibi.agents.gemma --probe
"""

from __future__ import annotations

import json
import sys
from typing import Any, Iterable, Optional

from pydantic import BaseModel, ValidationError

from alibi.agents.models import DetectiveOutput, MatcherOutput, parse_output
from alibi.llm import config
from alibi.llm.client import (
    CallBudget,
    GeminiClient,
    LLMError,
    MissingApiKey,
    RawResponse,
    load_api_key,
)
from alibi.llm.prompts import load_prompt

DEFAULT_ROLES: tuple[str, ...] = ("matcher", "detective")

PROBE_SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
PROBE_SYSTEM = "Answer with JSON only."
PROBE_USER = "Set ok to true."


# --------------------------------------------------------------------------- transport


def is_gemma(model: str) -> bool:
    return "gemma" in (model or "").lower()


def thinking_aware_transport(api_key: Optional[str] = None, genai_client: Any = None):
    """The team transport, except that Gemma models get no ``thinking_config``.

    Confirmed live on 2026-11-08: ``gemma-4-26b-a4b-it`` answers structured output but returns
    400 "Thinking level is not supported for this model" when a thinking level is sent, which
    the default ``alibi.llm.client.gemini_transport`` always does. Gemini models keep their
    configured level. ``genai_client`` can be injected for tests.
    """
    from google.genai import types

    if genai_client is None:
        from google import genai
        genai_client = genai.Client(api_key=api_key or load_api_key())
    levels = {
        "minimal": types.ThinkingLevel.MINIMAL,
        "low": types.ThinkingLevel.LOW,
        "medium": types.ThinkingLevel.MEDIUM,
        "high": types.ThinkingLevel.HIGH,
    }

    def call(model: str, system: str, user: str, json_schema: dict, thinking: str) -> RawResponse:
        cfg = types.GenerateContentConfig(
            system_instruction=system or None,
            response_mime_type="application/json",
            response_json_schema=json_schema,
            thinking_config=None if is_gemma(model)
            else types.ThinkingConfig(thinking_level=levels[thinking]),
        )
        resp = genai_client.models.generate_content(model=model, contents=user, config=cfg)
        return RawResponse(text=resp.text, usage_metadata=resp.usage_metadata)

    return call


def live_client(api_key: Optional[str] = None, **kwargs: Any) -> GeminiClient:
    """A ``GeminiClient`` that can talk to Gemini and Gemma models alike (see above)."""
    return GeminiClient(transport=thinking_aware_transport(api_key), **kwargs)


# --------------------------------------------------------------------------- model discovery


def _strip(name: str) -> str:
    return (name or "").removeprefix("models/")


def list_gemma_models(names: Optional[Iterable[str]] = None) -> list[str]:
    """Every model id containing 'gemma', from ``names`` or from the live model listing.

    Listing imports google.genai lazily and needs the key. Any failure gives ``[]``.
    """
    try:
        if names is None:
            from google import genai  # imported lazily so tests never need it

            client = genai.Client(api_key=load_api_key())
            names = [getattr(m, "name", "") or "" for m in client.models.list()]
        return [_strip(n) for n in names if "gemma" in _strip(n).lower()]
    except Exception:  # noqa: BLE001 - discovery must never break the caller
        return []


def find_gemma_model(names: Optional[Iterable[str]] = None) -> Optional[str]:
    """The first model id containing 'gemma' (without a ``models/`` prefix), else ``None``."""
    found = list_gemma_models(names)
    return found[0] if found else None


# --------------------------------------------------------------------------- comparison


def _matcher_key(out: MatcherOutput) -> tuple[str, str, int]:
    return (out.decision, out.chosen_record_id, out.chosen_revision)


def _detective_key(out: DetectiveOutput) -> dict[str, tuple[frozenset[str], frozenset[tuple[str, int]]]]:
    """question_id -> (statuses, confirming refs). Several claims on one question are merged."""
    merged: dict[str, tuple[set[str], set[tuple[str, int]]]] = {}
    for claim in out.claims:
        statuses, refs = merged.setdefault(claim.question_id, (set(), set()))
        statuses.add(claim.status)
        refs.update((r.record_id, r.revision) for r in claim.refs if r.role == "confirms")
    return {q: (frozenset(s), frozenset(r)) for q, (s, r) in merged.items()}


def compare_outputs(role: str, primary: BaseModel, second: BaseModel) -> tuple[bool, str]:
    """Role-specific agreement check. Returns ``(agree, difference)``; difference is '' on agreement."""
    if role == "matcher":
        a, b = _matcher_key(primary), _matcher_key(second)  # type: ignore[arg-type]
        if a == b:
            return True, ""
        return False, f"matcher decision {a[0]}/{a[1]}@{a[2]} vs {b[0]}/{b[1]}@{b[2]}"
    if role == "detective":
        a, b = _detective_key(primary), _detective_key(second)  # type: ignore[arg-type]
        if a == b:
            return True, ""
        differing = sorted(q for q in set(a) | set(b) if a.get(q) != b.get(q))
        return False, "detective differs on question(s) " + ", ".join(differing)
    return False, f"no comparison rule for role {role!r}"


# --------------------------------------------------------------------------- second opinion


class SecondOpinion:
    """Re-run a role's prompt on a Gemma model and compare with the primary output.

    ``client`` has the ``GeminiClient.generate`` signature (the FakeClient works). ``budget``
    is an optional per-task ``CallBudget`` passed to every call. Every comparison is appended
    to ``.log`` as a dict with role, model, agree, note and (when made) the prompt hash.
    """

    def __init__(self, client: Any, model_id: str, *, roles: Iterable[str] = DEFAULT_ROLES,
                 budget: Optional[CallBudget] = None):
        self.client = client
        self.model_id = model_id
        self.roles = tuple(roles)
        self.budget = budget
        self.log: list[dict[str, Any]] = []

    def __call__(self, role: str, input_dict: dict[str, Any], output_model: BaseModel
                 ) -> tuple[bool, str]:
        if role not in self.roles:
            return True, "not checked"
        entry: dict[str, Any] = {"role": role, "model": self.model_id}
        prompt = load_prompt(role)
        user = json.dumps(input_dict, ensure_ascii=False, indent=1)
        try:
            result = self.client.generate(self.model_id, prompt.system, user, prompt.schema,
                                          self.budget)
            entry["prompt_sha256"] = result.prompt_sha256
            second = parse_output(role, result.data)
        except (LLMError, ValidationError) as exc:
            agree, note = False, f"second parser unavailable: {type(exc).__name__} on {self.model_id}"
            entry.update(agree=agree, note=note, error=type(exc).__name__)
            self.log.append(entry)
            return agree, note
        agree, difference = compare_outputs(role, output_model, second)
        note = (f"{self.model_id} agrees on {role}" if agree
                else f"{self.model_id} disagrees: {difference}")
        entry.update(agree=agree, note=note)
        self.log.append(entry)
        return agree, note


def make_second_opinion(client: Any, names: Optional[Iterable[str]] = None, *,
                        roles: Iterable[str] = DEFAULT_ROLES,
                        budget: Optional[CallBudget] = None) -> Optional[SecondOpinion]:
    """A ``SecondOpinion`` on the first Gemma model, or ``None`` when the key sees none."""
    model_id = find_gemma_model(names)
    if model_id is None:
        return None
    return SecondOpinion(client, model_id, roles=roles, budget=budget)


# --------------------------------------------------------------------------- CLI


def probe() -> int:
    """List Gemma ids and make at most one structured call on the first. Never prints the key."""
    report: dict[str, Any] = {"gemma_models": [], "chosen": None, "structured_output_ok": False,
                              "error": None}
    try:
        key = load_api_key()
    except MissingApiKey as exc:
        report["error"] = f"MissingApiKey: {exc}"
        print(json.dumps(report, indent=1))
        return 2
    models = list_gemma_models()
    report["gemma_models"] = models
    if not models:
        report["error"] = "no Gemma model listed on this key"
        print(json.dumps(report, indent=1))
        return 1
    chosen = models[0]
    report["chosen"] = chosen
    try:
        client = live_client(key, use_cache=False)
        result = client.generate(chosen, PROBE_SYSTEM, PROBE_USER, PROBE_SCHEMA,
                                 CallBudget(1, name="gemma-probe"))
        report["structured_output_ok"] = result.data.get("ok") is True
    except LLMError as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    print(json.dumps(report, indent=1))
    return 0 if report["structured_output_ok"] else 1


def main(argv: Optional[list[str]] = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args == ["--probe"]:
        return probe()
    print("usage: python -m alibi.agents.gemma --probe", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
