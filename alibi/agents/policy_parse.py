"""Gemini-backed policy parser: plain-English privacy instructions -> typed rule proposals.

    from alibi.agents.policy_parse import parse_rule_text, parse_rules
    parse = parse_rule_text("Don't share my account number.", client=GeminiClient())
    parse.status        # 'resolved' | 'unresolved' | 'held'
    parse.rules         # [PolicyRuleProposal] in the team's typed shape (compile_policy input)
    parse.core_rules    # [DenyRule | GeneraliseRule | AgeRule | RecipientRule] for alibi.core

The model only translates. Code decides what survives: every ``value`` must be in the passport
vocabulary for its target, every recipient must be a configured recipient, and each kind must
make sense with its target. A rule that fails any check is dropped and the text becomes
``unresolved``, with the reason in ``dropped``. A clarification makes the text ``unresolved``;
a surviving rule with confidence below ``HOLD_CONFIDENCE`` makes it ``held``. Nothing in the
instruction text can change these checks: the text is data, sent to the model under
``instructions`` and otherwise never interpreted here. ``LLMError`` and pydantic errors never
escape; they become ``unresolved`` with a reason, so a caller can always store the outcome.

Core mapping (``core_rules``) covers what ``alibi.core.policy`` can enforce today. Category and
merchant targets, bank-row kinds, the ``counterparty_name``/``destination``/``release_reference``
fields, recipient restrictions narrower than ``all`` and alert preferences are not expressible
there; each such proposal is listed in ``core_reasons`` (see ``alibi/NEEDS.md``).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import ValidationError

from alibi.agents.models import (
    Clarification,
    PolicyParams,
    PolicyParseOutput,
    PolicyRuleProposal,
    undeclared_fields,
)
from alibi.core.canon import make_id
from alibi.core.schemas import AgeRule, DenyRule, GeneraliseRule, PolicyRule, RecipientRule
from alibi.llm.client import CallBudget, LLMError
from alibi.llm.config import FLASH
from alibi.llm.prompts import load_prompt

ROLE = "policy_parse"
HOLD_CONFIDENCE = 0.8
MATERIAL_KEYS = ("categories", "merchants", "record_kinds", "fields", "recipients")
VOCAB_BY_TARGET = {"category": "categories", "merchant": "merchants", "field": "fields",
                   "record_kind": "record_kinds"}
#: Which params each kind carries; anything else the model returns is stripped.
PARAMS_BY_KIND = {"deny": (), "generalise": ("replacement",), "age_limit": ("max_age_days",),
                  "recipient_restrict": ("recipients",), "alert_preference": ("alert_kind", "enabled")}

#: Team field names -> core field paths. Missing names are not expressible in core yet.
CORE_FIELD_MAP = {
    "amount_minor": "amount_pence", "item": "items.description", "category": "items.category",
    "message": "text", "description": "description", "payment_reference": "payment_ref",
    "account_ref": "account_ref", "occurred_at": "occurred_at", "currency": "currency",
}
#: Team record kinds -> core RecordKind values. Bank rows have no core kind.
CORE_KIND_MAP = {
    "sale": "order", "purchase": "receipt", "refund": "refund", "fee": "fee", "payout": "payout",
    "settlement": "settlement", "receipt": "receipt", "balance_release": "message",
}
CORE_MAX_AGE_DAYS = 3650

_IDENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}$")

Status = Literal["resolved", "unresolved", "held"]


@dataclass
class PolicyParse:
    """Outcome of parsing one instruction text. ``held`` is parallel to ``rules``."""

    status: Status
    rules: list[PolicyRuleProposal] = field(default_factory=list)
    held: list[bool] = field(default_factory=list)
    clarifications: list[Clarification] = field(default_factory=list)
    plain_summary: str = ""
    confidence: float = 1.0
    core_rules: list[PolicyRule] = field(default_factory=list)
    core_reasons: list[str] = field(default_factory=list)
    interpretation: str = ""
    reason: str | None = None
    dropped: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    text: str = ""
    model: str = FLASH
    cached: bool = False
    calls: int = 0

    def as_parser_output(self) -> dict[str, Any]:
        """The validated answer in the shape ``agents.execution.api.compile_policy`` accepts."""
        return {
            "rules": [rule.model_dump(exclude_none=True) for rule in self.rules],
            "clarifications": [c.model_dump() for c in self.clarifications],
            "plain_summary": self.plain_summary,
        }

    def as_dict(self) -> dict[str, Any]:
        out = self.as_parser_output()
        out.update({
            "status": self.status, "held": list(self.held), "confidence": self.confidence,
            "core_rules": [r.model_dump(mode="json") for r in self.core_rules],
            "core_reasons": list(self.core_reasons), "interpretation": self.interpretation,
            "reason": self.reason, "dropped": list(self.dropped), "warnings": list(self.warnings),
            "text": self.text, "model": self.model, "cached": self.cached, "calls": self.calls,
        })
        return out


# --------------------------------------------------------------------------- material and prompt


def default_material() -> dict[str, list[str]]:
    """The team passport vocabulary from ``agents.execution.api.load_policy_material``."""
    from agents.execution.api import load_policy_material

    return normalise_material(load_policy_material())


def normalise_material(material: dict[str, Any]) -> dict[str, list[str]]:
    """Keep only the five vocabulary lists, lower-cased, deduplicated, in order."""
    out: dict[str, list[str]] = {}
    for key in MATERIAL_KEYS:
        seen: list[str] = []
        for value in material.get(key) or []:
            lowered = str(value).strip().lower()
            if lowered and lowered not in seen:
                seen.append(lowered)
        out[key] = seen
    return out


def build_user_json(text: str, material: dict[str, list[str]]) -> str:
    """The user message: the instructions as data plus the vocabulary the rules may name."""
    payload: dict[str, Any] = {"instructions": text}
    payload.update({key: list(material.get(key, [])) for key in MATERIAL_KEYS})
    return json.dumps(payload, ensure_ascii=False, indent=1)


# --------------------------------------------------------------------------- validation


def _sanitise_rule_id(raw: str, text: str, index: int, rule_id: str | None, count: int) -> str:
    if rule_id:
        return rule_id if count == 1 else f"{rule_id}-{index + 1}"
    raw = (raw or "").strip()
    if raw and _IDENT.match(raw):
        return raw
    return make_id("rule", text, index)


def validate_rule(rule: PolicyRuleProposal, material: dict[str, list[str]]
                  ) -> tuple[PolicyRuleProposal | None, str | None]:
    """Check one proposal against the vocabulary and the kind/target table.

    Returns ``(cleaned, None)`` or ``(None, reason)``. Values and recipients are lower-cased;
    params not used by the kind are stripped.
    """
    kind, target = rule.kind, rule.target
    value = rule.value.strip().lower()
    label = f"{rule.rule_id} ({kind}/{target})"

    if target == "all":
        value = ""
    else:
        vocab = material.get(VOCAB_BY_TARGET[target], [])
        if not value:
            return None, f"{label}: no value given for target {target}"
        if value not in vocab:
            return None, f"{label}: {value!r} is not a {target} in the passport"

    params = rule.params
    kept: dict[str, Any] = {}
    if kind == "age_limit":
        if target != "all":
            return None, f"{label}: an age limit applies to everything, target must be 'all'"
        days = params.max_age_days
        if not isinstance(days, int) or isinstance(days, bool) or days <= 0:
            return None, f"{label}: age limit needs max_age_days above 0"
        kept["max_age_days"] = days
    elif kind == "recipient_restrict":
        recipients = [str(r).strip().lower() for r in (params.recipients or []) if str(r).strip()]
        if not recipients:
            return None, f"{label}: recipient restriction names no recipient"
        configured = material.get("recipients", [])
        unknown = sorted(set(r for r in recipients if r not in configured))
        if unknown:
            return None, f"{label}: {', '.join(unknown)} is not a configured recipient"
        recipients = sorted(set(recipients))
        if set(recipients) >= set(configured):
            return None, f"{label}: names every configured recipient, so it restricts nothing"
        kept["recipients"] = recipients
    elif kind == "generalise":
        if target != "field":
            return None, f"{label}: only a field can be generalised"
        if params.replacement is not None and params.replacement.strip():
            kept["replacement"] = params.replacement.strip()
    elif kind == "alert_preference":
        if target != "all":
            return None, f"{label}: an alert preference has target 'all'"
        alert_kind = (params.alert_kind or "").strip().lower()
        if not alert_kind or not re.match(r"^[a-z][a-z0-9_]*$", alert_kind):
            return None, f"{label}: alert preference needs an alert_kind"
        if params.enabled is None:
            return None, f"{label}: alert preference needs enabled true or false"
        kept["alert_kind"], kept["enabled"] = alert_kind, params.enabled
    elif kind != "deny":  # pragma: no cover - the enum already rejects other kinds
        return None, f"{label}: unsupported kind"

    cleaned = rule.model_copy(update={"value": value, "params": PolicyParams(**kept)})
    return cleaned, None


# --------------------------------------------------------------------------- core mapping


def _core_rule(rule: PolicyRuleProposal) -> tuple[PolicyRule | None, str | None]:
    """Map one validated proposal to a core rule, or say why core cannot express it."""
    kind, target, value = rule.kind, rule.target, rule.value
    label = f"{rule.rule_id} ({kind}/{target}{'=' + value if value else ''})"
    base = {"rule_id": rule.rule_id, "source_text": rule.source_text or None}
    if kind == "deny":
        if target == "field":
            path = CORE_FIELD_MAP.get(value)
            if path is None:
                return None, f"{label}: field {value!r} has no core field (not expressible in core yet)"
            return DenyRule(**base, fields=[path]), None
        if target == "record_kind":
            core_kind = CORE_KIND_MAP.get(value)
            if core_kind is None:
                return None, f"{label}: bank rows have no core record kind (not expressible in core yet)"
            return DenyRule(**base, kinds=[core_kind]), None
        if target == "all":
            return None, f"{label}: core deny rules need fields or kinds; denying everything is not expressible"
        return None, f"{label}: core rules cannot target a {target} yet (needs alibi/core/policy.py)"
    if kind == "generalise":
        path = CORE_FIELD_MAP.get(value)
        if path is None:
            return None, f"{label}: field {value!r} has no core field (not expressible in core yet)"
        level = "category" if path == "items.description" else "redact"
        return GeneraliseRule(**base, fields=[path], level=level), None
    if kind == "age_limit":
        days = rule.params.max_age_days or 0
        if days > CORE_MAX_AGE_DAYS:
            return None, f"{label}: core age limits stop at {CORE_MAX_AGE_DAYS} days"
        return AgeRule(**base, max_age_days=days), None
    if kind == "recipient_restrict":
        if target != "all":
            return None, (f"{label}: core recipient rules apply to the whole package, "
                          f"not to one {target} (needs alibi/core/policy.py)")
        return RecipientRule(**base, allow=list(rule.params.recipients or [])), None
    return None, f"{label}: alert preferences are not disclosure rules; core has no home for them"


def map_to_core(rules: list[PolicyRuleProposal]) -> tuple[list[PolicyRule], list[str]]:
    """Core rules for the expressible proposals and a reason for each one that is not."""
    core: list[PolicyRule] = []
    reasons: list[str] = []
    for rule in rules:
        try:
            mapped, reason = _core_rule(rule)
        except ValidationError as exc:
            mapped, reason = None, f"{rule.rule_id}: core rejected the mapping: {_first_error(exc)}"
        if mapped is not None:
            core.append(mapped)
        if reason:
            reasons.append(reason)
    return core, reasons


# --------------------------------------------------------------------------- interpretation


def describe_rule(rule: PolicyRuleProposal) -> str:
    """One plain sentence for a validated rule, built from its fields, never from free text."""
    what = {"category": f"records in category '{rule.value}'", "merchant": f"records from '{rule.value}'",
            "field": f"the field '{rule.value}'", "record_kind": f"{rule.value} records",
            "all": "everything"}[rule.target]
    p = rule.params
    if rule.kind == "deny":
        return f"Never share {what}."
    if rule.kind == "generalise":
        repl = f" as '{p.replacement}'" if p.replacement else ""
        return f"Share {what} only in generalised form{repl}."
    if rule.kind == "age_limit":
        return f"Share nothing older than {p.max_age_days} days."
    if rule.kind == "recipient_restrict":
        return f"Only {', '.join(p.recipients or [])} may receive {what}."
    state = "on" if p.enabled else "off"
    return f"Turn {p.alert_kind} alerts {state}."


def _interpretation(rules: list[PolicyRuleProposal], held: list[bool], dropped: list[str],
                    clarifications: list[Clarification]) -> str:
    parts = [describe_rule(r) + (" (held for review)" if h else "") for r, h in zip(rules, held)]
    if clarifications:
        parts.append(f"{len(clarifications)} point(s) need clarifying before any rule takes effect.")
    if dropped:
        parts.append(f"{len(dropped)} proposed rule(s) were dropped as invalid.")
    if not parts:
        return "No rule was compiled from this text."
    return " ".join(parts)


def _first_error(exc: ValidationError) -> str:
    errors = exc.errors()
    if not errors:
        return "invalid"
    err = errors[0]
    loc = ".".join(str(p) for p in err.get("loc", ()))
    return f"{loc}: {err.get('msg', 'invalid')}" if loc else str(err.get("msg", "invalid"))


# --------------------------------------------------------------------------- entry points


def _unresolved(text: str, reason: str, model: str, **extra: Any) -> PolicyParse:
    return PolicyParse(status="unresolved", reason=reason, interpretation=f"Not compiled: {reason}",
                       text=text, model=model, **extra)


def parse_rule_text(text: str, *, client: Any, material: dict[str, Any] | None = None,
                    rule_id: str | None = None, model: str | None = None,
                    budget: CallBudget | None = None) -> PolicyParse:
    """Parse one instruction text with one model call and validate the answer in code.

    ``client`` has ``GeminiClient.generate``'s signature (``FakeClient`` in tests).
    ``material`` defaults to the team vocabulary. ``rule_id`` names the surviving rule(s)
    (``<rule_id>`` or ``<rule_id>-<n>``). Never raises for model or validation failures.
    """
    model = model or FLASH
    text = text if isinstance(text, str) else str(text)
    vocab = normalise_material(material) if material is not None else default_material()
    prompt = load_prompt(ROLE)
    user = build_user_json(text, vocab)

    try:
        result = client.generate(model, prompt.system, user, prompt.schema, budget)
    except LLMError as exc:
        return _unresolved(text, f"{type(exc).__name__}: {exc}", model)
    calls = 0 if getattr(result, "cached", False) else 1
    cached = bool(getattr(result, "cached", False))
    data = result.data if isinstance(result.data, dict) else {}

    warnings = [f"undeclared field ignored: {name}" for name in undeclared_fields(ROLE, data)]
    try:
        output = PolicyParseOutput.model_validate(data)
    except ValidationError as exc:
        return _unresolved(text, f"answer did not match the policy_parse schema: {_first_error(exc)}",
                           model, warnings=warnings, cached=cached, calls=calls)

    rules: list[PolicyRuleProposal] = []
    dropped: list[str] = []
    for proposal in output.rules:
        cleaned, reason = validate_rule(proposal, vocab)
        if cleaned is None:
            dropped.append(reason or f"{proposal.rule_id}: invalid")
        else:
            rules.append(cleaned)
    count = len(rules)
    seen: set[str] = set()
    for index, rule in enumerate(rules):
        ident = _sanitise_rule_id(rule.rule_id, text, index, rule_id, count)
        if ident in seen:
            ident = make_id("rule", text, index)
        seen.add(ident)
        rule.rule_id = ident

    held = [rule.confidence < HOLD_CONFIDENCE for rule in rules]
    clarifications = list(output.clarifications)
    core_rules, core_reasons = map_to_core(rules)
    confidence = min((rule.confidence for rule in rules), default=1.0)

    reason: str | None = None
    if dropped or clarifications:
        status: Status = "unresolved"
        reason = "; ".join(dropped) if dropped else "the model asked for clarification"
        if dropped and clarifications:
            reason += "; and the model asked for clarification"
    elif any(held):
        status = "held"
        reason = f"confidence {confidence:.2f} is below {HOLD_CONFIDENCE}"
    else:
        status = "resolved"

    return PolicyParse(
        status=status, rules=rules, held=held, clarifications=clarifications,
        plain_summary=output.plain_summary, confidence=confidence, core_rules=core_rules,
        core_reasons=core_reasons, interpretation=_interpretation(rules, held, dropped, clarifications),
        reason=reason, dropped=dropped, warnings=warnings, text=text, model=model, cached=cached,
        calls=calls,
    )


def parse_rules(texts: list[str], *, client: Any, material: dict[str, Any] | None = None,
                rule_id: str | None = None, model: str | None = None,
                budget: CallBudget | None = None) -> list[PolicyParse]:
    """Parse several texts, one call each, sharing ``budget``. Budget exhaustion is recorded
    on the remaining texts as ``unresolved``, never raised."""
    vocab = normalise_material(material) if material is not None else default_material()
    out: list[PolicyParse] = []
    for index, text in enumerate(texts):
        ident = f"{rule_id}-{index + 1}" if rule_id and len(texts) > 1 else rule_id
        out.append(parse_rule_text(text, client=client, material=vocab, rule_id=ident, model=model,
                                   budget=budget))
    return out
