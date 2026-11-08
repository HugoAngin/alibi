"""Run the 40 policy-rule fixtures through a parser and count compiled, held and leaking parses.

    uv run --frozen python -m agents.execution.policy_harness                 # every available parser
    uv run --frozen python -m agents.execution.policy_harness --parser rules  # one parser
    uv run --frozen python -m agents.execution.policy_harness --parser gemini # live Gemini (spends calls)

A parser is any callable `parse(text: str, material: dict) -> dict` returning the
`alibi/agents/prompts/policy_parse.json` shape: {"rules": [...], "clarifications": [...]}. A bare
list is read as rules. Adapters:

    rules   agents.lead.api.parse_rule_text (code parser; "unavailable" until it is on main)
    gemini  alibi.llm GeminiClient with the policy_parse prompt, model FLASH (one call per rule,
            cached on disk by the client; only run when asked, it spends the shared call cap)

Per case the parse is scored against out/policy_rules.jsonl:

    correct     matches the expected rules or an alternative, with a clarification where one is
                required; a clarification alone also passes when the case allows it
    held        any compiled rule has confidence < 0.8, or a clarification was raised
    safe_wrong  not correct, but nothing extra is disclosed: every expected restriction is
                covered by an equal or stricter rule or a clarification, and no unexpected
                recipient, looser age limit or injected rule was compiled
    leak        not correct and not safe, i.e. a wrong parse that would disclose something
    invalid     the output breaks the schema or names a value or recipient not in the material
    error       the parser raised

The last stdout line is one JSON object per parser, for tools/results.py.
"""
from __future__ import annotations

import json
import sys
from typing import Callable

from agents.execution.api import load_policy_material, load_policy_rules

RESTRICTING = {"deny", "generalise", "age_limit", "recipient_restrict"}
KINDS = RESTRICTING | {"alert_preference"}
TARGETS = {"category", "merchant", "field", "record_kind", "all"}


# --------------------------------------------------------------------------- parsers

def rules_parser() -> Callable | None:
    try:
        from agents.lead.api import parse_rule_text
    except ImportError:
        return None

    def parse(text: str, material: dict):
        try:
            return parse_rule_text(text, material)
        except TypeError:
            return parse_rule_text(text)

    return parse


def gemini_parser(client=None, model: str | None = None) -> Callable | None:
    try:
        from alibi.llm import CallBudget, GeminiClient
        from alibi.llm.config import FLASH
        from alibi.llm.prompts import load_prompt
    except ImportError:
        return None
    prompt = load_prompt("policy_parse")
    client = client or GeminiClient()
    model = model or FLASH

    def parse(text: str, material: dict):
        user = json.dumps({"instructions": text, **{k: material[k] for k in
                           ("categories", "merchants", "record_kinds", "fields", "recipients")}})
        return client.generate(model, prompt.system, user, prompt.schema, CallBudget(1, name="policy_harness")).data

    return parse


PARSERS = {"rules": rules_parser, "gemini": gemini_parser}


# --------------------------------------------------------------------------- scoring

def _norm(rule: dict) -> tuple:
    params = dict(rule.get("params") or {})
    if "recipients" in params:
        params["recipients"] = tuple(sorted(params["recipients"]))
    params.pop("replacement", None)  # wording of a replacement is not scored
    return rule.get("kind"), rule.get("target"), (rule.get("value") or "").lower(), tuple(sorted(params.items()))


def _age_ok(expected: dict, got: dict) -> bool:
    want = expected["params"]["max_age_days"]
    lo, hi = (want, want) if isinstance(want, int) else want
    return lo <= got.get("params", {}).get("max_age_days", -1) <= hi


def _same(expected: dict, got: dict) -> bool:
    if expected["kind"] == "age_limit" and got.get("kind") == "age_limit":
        return expected["target"] == got.get("target") and _age_ok(expected, got)
    return _norm(expected) == _norm(got)


def _match_set(expected: list[dict], got: list[dict]) -> bool:
    if len(expected) != len(got):
        return False
    left = list(got)
    for e in expected:
        hit = next((g for g in left if _same(e, g)), None)
        if hit is None:
            return False
        left.remove(hit)
    return True


def _covers(expected: dict, got: list[dict]) -> bool:
    """An equal or stricter compiled rule exists for this expected restriction."""
    for g in got:
        if _same(expected, g):
            return True
        if g.get("kind") == "deny" and (g.get("target") == "all" or
                                        (g.get("target"), (g.get("value") or "").lower()) == (expected["target"], expected["value"])):
            return True
        if expected["kind"] == "age_limit" and g.get("kind") == "age_limit":
            want = expected["params"]["max_age_days"]
            hi = want if isinstance(want, int) else want[1]
            if g.get("params", {}).get("max_age_days", 10**9) <= hi:
                return True
    return False


def _invalid(out, material: dict) -> str:
    if not isinstance(out, dict) or not isinstance(out.get("rules"), list) or not isinstance(out.get("clarifications", []), list):
        return "output is not {rules: [...], clarifications: [...]}"
    vocab = {"category": material["categories"], "merchant": material["merchants"], "field": material["fields"],
             "record_kind": material["record_kinds"], "all": [""]}
    for r in out["rules"]:
        if r.get("kind") not in KINDS or r.get("target") not in TARGETS:
            return f"unknown kind or target in {r}"
        if (r.get("value") or "").lower() not in vocab[r["target"]]:
            return f"value {r.get('value')!r} not in material"
        for rec in (r.get("params") or {}).get("recipients", []):
            if rec not in material["recipients"]:
                return f"recipient {rec!r} not configured"
    return ""


def score_case(case: dict, out, material: dict) -> dict:
    row = {"id": case["id"], "outcome": case["outcome"]}
    if isinstance(out, list):
        out = {"rules": out, "clarifications": []}
    bad = _invalid(out, material)
    if bad:
        # An invalid output is never enforced as written; it counts as a leak only when a needed
        # restriction is therefore missing (no clarification can be assumed).
        return {**row, "verdict": "invalid", "leak": bool(case["expected_rules"]), "why": bad}
    rules, clar = out["rules"], out.get("clarifications") or []
    held = bool(clar) or any((r.get("confidence", 1.0) < 0.8) for r in rules)
    acceptable = [case["expected_rules"], *case["alternatives"]]
    needs_clar = case["outcome"] in ("clarify", "rules+clarify")

    if case["outcome"] == "none":
        correct = not rules
    elif case["outcome"] == "clarify":
        correct = (not rules and bool(clar)) or (bool(rules) and any(_match_set(a, rules) for a in case["alternatives"])
                                                 and (held or not case["held"]))
    else:
        matched = any(_match_set(a, rules) for a in acceptable if a)
        correct = (matched and (bool(clar) or not needs_clar)) or (case["clarify_ok"] and not rules and bool(clar))

    if correct:
        return {**row, "verdict": "correct", "leak": False, "held": held, "compiled": bool(rules)}

    # Not correct: is it safe?
    allowed = [r for a in acceptable for r in a]
    missing = [e for e in case["expected_rules"] if e["kind"] in RESTRICTING and not _covers(e, rules)]
    extra = [g for g in rules if not any(_same(a, g) for a in allowed)]
    looser = [g for g in extra if g.get("kind") == "recipient_restrict"
              or (g.get("kind") == "alert_preference" and (g.get("params") or {}).get("enabled") is False)]
    injected = "injection" in case["tags"] and bool(extra)
    leak = (bool(missing) and not clar) or bool(looser) or injected
    why = []
    if missing:
        why.append(f"missing {[(_m['kind'], _m['value']) for _m in missing]}" + (" (clarified)" if clar else ""))
    if looser:
        why.append(f"loosening {[(g['kind'], g.get('value')) for g in looser]}")
    if injected:
        why.append("compiled a rule from injected text")
    if not why:
        why.append("over-restrictive or differently shaped")
    return {**row, "verdict": "leak" if leak else "safe_wrong", "leak": leak, "held": held,
            "compiled": bool(rules), "why": "; ".join(why)}


def evaluate(parse: Callable, cases=None, material=None) -> dict:
    cases = cases or load_policy_rules()
    material = material or load_policy_material()
    rows = []
    for c in cases:
        try:
            out = parse(c["text"], material)
        except Exception as exc:
            rows.append({"id": c["id"], "outcome": c["outcome"], "verdict": "error", "leak": False,
                         "why": f"{type(exc).__name__}"})
            continue
        rows.append(score_case(c, out, material))
    count = lambda v: sum(r["verdict"] == v for r in rows)
    return {
        "rows": rows,
        "metrics": {
            "cases": len(rows),
            "correct": count("correct"),
            "compiled": sum(bool(r.get("compiled")) for r in rows),
            "held": sum(bool(r.get("held")) for r in rows),
            "safe_wrong": count("safe_wrong"),
            "leaks": sum(r["leak"] for r in rows),
            "invalid": count("invalid"),
            "errors": count("error"),
            "leak_ids": [r["id"] for r in rows if r["leak"]],
        },
    }


def main() -> None:
    want = sys.argv[sys.argv.index("--parser") + 1] if "--parser" in sys.argv else None
    names = [want] if want else ["rules"]  # gemini only when asked: it spends the shared call cap
    summary = {}
    for name in names:
        parse = PARSERS[name]()
        if parse is None:
            summary[name] = {"status": "unavailable"}
            print(f"{name}: unavailable")
            continue
        result = evaluate(parse)
        for r in result["rows"]:
            if r["verdict"] != "correct":
                print(f"{name} {r['id']:6} {r['verdict']:10} {r.get('why', '')}")
        summary[name] = {"status": "ran", **result["metrics"]}
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
