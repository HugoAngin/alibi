"""Policy parser chain for ``/rules``: Gemini, then execution's deterministic compiler, then core.

    from alibi.app.policy_chain import parse_rule_text
    parsed = parse_rule_text("Don't show what I bought.")
    parsed.status          # 'resolved' | 'unresolved'   (alibi.core.policy.RuleParse)
    parsed.rule            # one typed core rule (DenyRule | GeneraliseRule | AgeRule | RecipientRule)
    parsed.source          # 'gemini' | 'execution' | 'core': the layer that answered
    parsed.tried           # layers that ran, in order
    parsed.notes           # layer -> why it fell through

The layers, in order:

1. ``alibi.agents.policy_parse.parse_rule_text`` with a Gemini client. It runs only when the
   caller asks for it: a client is passed in (tests pass ``alibi.llm.fake.FakeClient``), or
   ``use_gemini=True`` and ``GEMINI_API_KEY`` is exported in the process environment with
   ``ALIBI_REPLAY`` off. The default (``use_gemini=None``) runs the deterministic layers only,
   so library callers (``agents.lead.api``, execution's harness and tests) never reach the
   network whatever the shell holds; ``/rules`` (``alibi.app.actions.interpret_rule``) is the
   one caller that opts in. Nothing here reads the repo-root ``.env`` file: unlike
   ``alibi.llm.client.load_api_key`` the gate is the exported variable alone, so start the app
   from a shell that exports ``GEMINI_API_KEY`` (``alibi/RUNBOOK.md``). Whenever the layer does
   not run, ``.skipped["gemini"]`` says why, and the app shows that reason to the holder.
2. ``agents.execution.api.compile_policy``, the team's deterministic text compiler, whose typed
   proposals are mapped onto core rules with ``alibi.agents.policy_parse.map_to_core``.
3. ``alibi.core.policy.parse_rule_text``, the whole-sentence core parser.

A layer's answer is accepted only when it compiles exactly one typed core rule with nothing
left to clarify: no clarification, no dropped or held proposal, no alert preference, and every
proposal expressible in core. Anything else falls through to the next layer. When every layer
falls through, the result is the core parser's unresolved ``RuleParse`` (held for
clarification) carrying which layers were tried and why each stopped.

Safety: the chain never widens disclosure. Each layer only ever yields rules that restrict
(deny, generalise, age limit, recipient allowlist), values are checked against the passport
vocabulary before mapping (``validate_rule``), a text that yields several rules is held so no
restriction is silently dropped behind the single ``rule`` slot, and the holder confirms the
interpretation before the rule is saved (``alibi.app.actions.interpret_rule``).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pydantic import ValidationError

from alibi.agents.models import PolicyParams, PolicyRuleProposal
from alibi.agents.policy_parse import (
    CORE_KIND_MAP,
    describe_rule,
    map_to_core,
    normalise_material,
    validate_rule,
)
from alibi.agents.policy_parse import parse_rule_text as gemini_parse_rule_text
from alibi.core.canon import make_id
from alibi.core.policy import RuleParse
from alibi.core.policy import parse_rule_text as core_parse_rule_text
from alibi.core.schemas import PolicyRule

LAYERS = ("gemini", "execution", "core")
API_KEY_ENV = "GEMINI_API_KEY"
REPLAY_ENV = "ALIBI_REPLAY"
_REPLAY_ON = {"1", "true", "yes"}
#: Execution's compiled kinds -> the parser-schema kinds the core mapping expects.
_EXECUTION_KINDS = {"age": "age_limit", "recipient": "recipient_restrict"}
#: Team record kinds that share one core kind, so a rule on one also covers the other(s).
_MERGED_KINDS = {
    team: sorted(other for other, core in CORE_KIND_MAP.items() if core == core_kind and other != team)
    for team, core_kind in CORE_KIND_MAP.items()
}
_GENERIC_CORE_REASON = "This is not a supported rule yet."


@dataclass
class ChainParse(RuleParse):
    """``RuleParse`` plus which layer answered and what the other layers said."""

    source: str = "core"
    tried: list[str] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)
    notes: dict[str, str] = field(default_factory=dict)
    proposals: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status, "interpretation": self.interpretation, "reason": self.reason,
            "rule": self.rule.to_json_dict() if self.rule is not None else None,
            "source": self.source, "tried": list(self.tried), "skipped": dict(self.skipped),
            "notes": dict(self.notes), "proposals": [dict(p) for p in self.proposals],
        }


@dataclass
class _Layer:
    """One layer's outcome: a single core rule with its interpretation, or why it fell through."""

    rule: PolicyRule | None = None
    interpretation: str = ""
    note: str = ""          # internal: why the layer fell through
    why: str = ""           # holder-facing sentence for the held-rule reason, when there is one
    proposals: list[dict[str, Any]] = field(default_factory=list)


# --------------------------------------------------------------------------- helpers


def replay_on(env: dict[str, str] | None = None) -> bool:
    env = os.environ if env is None else env
    return str(env.get(REPLAY_ENV, "")).strip().lower() in _REPLAY_ON


def gemini_enabled(env: dict[str, str] | None = None) -> bool:
    """True only when the key is exported in the environment and replay is off. The ``.env``
    file is deliberately not consulted: tests and replays must never reach Gemini. Use
    ``gemini_skip_reason`` for the holder-facing explanation when this is False."""
    env = os.environ if env is None else env
    if not (env.get(API_KEY_ENV) or "").strip():
        return False
    return not replay_on(env)


def gemini_skip_reason(env: dict[str, str] | None = None) -> str | None:
    """Why the Gemini layer will not run under ``env`` (None when it would)."""
    env = os.environ if env is None else env
    if replay_on(env):
        return f"{REPLAY_ENV} is on; a recorded run never calls Gemini"
    if not (env.get(API_KEY_ENV) or "").strip():
        return (f"{API_KEY_ENV} is not exported in the app's environment (the app does not read "
                f".env; export the key in the shell that starts it)")
    return None


def default_material() -> dict[str, Any]:
    from agents.execution.api import load_policy_material

    return load_policy_material()


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip(" .!?;:,\"'")


def _plain(reason: str) -> str:
    """Strip the rule label and the file pointer from a validation or mapping reason."""
    text = re.sub(r"^r[-_][A-Za-z0-9_]+ \([^)]*\): ", "", reason)
    text = re.sub(r"^rule_[A-Za-z0-9]+ \([^)]*\): ", "", text)
    return re.sub(r" \(needs [^)]*\)", "", text).strip()


def _rule_id_for(text: str, rule: PolicyRule) -> str:
    data = rule.model_dump(mode="json", exclude={"rule_id", "source_text"})
    return make_id("rule", _normalise(text), data)


def _with_identity(rule: PolicyRule, text: str, rule_id: str | None) -> PolicyRule:
    ident = rule_id if rule_id is not None else _rule_id_for(text, rule)
    return rule.model_copy(update={"rule_id": ident, "source_text": text})


#: Holder-facing names for the core field paths the team vocabulary maps onto.
_FIELD_LABELS = {
    "items.description": "Item names", "items.category": "Item categories", "text": "Message text",
    "payment_ref": "Payment references", "account_ref": "Account references",
    "description": "Bank descriptions", "amount_pence": "Amounts", "currency": "The currency",
    "occurred_at": "Dates and times",
}


def _label(path: str) -> str:
    return _FIELD_LABELS.get(path, f"The field '{path}'")


def _join(words: list[str]) -> str:
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1]


def _interpret(proposal: PolicyRuleProposal, rule: PolicyRule) -> str:
    """Plain English for the holder, in the core parser's voice, built from the typed rule
    (never from the instruction text) and honest about what core does with the proposal."""
    if rule.type == "age":
        return (f"Nothing dated more than {rule.max_age_days} days before the package is built "
                f"will be shared.")
    if rule.type == "recipient":
        names = _join([f"Bank {a.split('_')[-1].upper()}" if a.startswith("bank_") else a for a in rule.allow])
        return f"Only {names} may receive your evidence; any other recipient gets nothing."
    if rule.type == "deny" and rule.fields:
        return f"{_join([_label(f) for f in rule.fields])} will not be shared with any recipient."
    if rule.type == "deny":
        kinds = [k.value for k in rule.kinds]
        merged = _MERGED_KINDS.get(proposal.value) or []
        if merged:
            return (f"{proposal.value.capitalize()} records will not be shared with any recipient. "
                    f"Alibi keeps {proposal.value} and {_join(merged)} records together as "
                    f"{_join(kinds)} records, so this rule covers both.")
        return f"{_join(kinds).capitalize()} records will not be shared with any recipient."
    # generalise
    words = proposal.params.replacement
    if rule.level == "redact":
        label = _join([_label(f) for f in rule.fields])
        said = f" Alibi cannot yet replace it with '{words}', so it is withheld instead." if words else ""
        return f"{label} will be withheld from every recipient.{said}"
    if rule.fields == ["items.description"]:
        instead = f" (not by '{words}')" if words else ""
        return (f"Item names will be replaced by their categories{instead}; an item without a "
                f"category will have its name withheld.")
    return f"{_join([_label(f) for f in rule.fields])} will be shared at {rule.level} level only."


def _proposal_from_execution(raw: dict[str, Any]) -> PolicyRuleProposal | None:
    """Execution's compiled typed rule -> the parser-schema proposal, or None when malformed."""
    kind = _EXECUTION_KINDS.get(raw.get("kind"), raw.get("kind"))
    params = {k: v for k, v in (raw.get("params") or {}).items() if v is not None}
    try:
        return PolicyRuleProposal(
            rule_id=str(raw.get("rule_id") or "r"), kind=kind, target=raw.get("target"),
            value=str(raw.get("value") or ""), params=PolicyParams(**params),
            source_text=str(raw.get("source_text") or ""),
            confidence=float(raw.get("confidence", 1.0)),
        )
    except (ValidationError, TypeError, ValueError):
        return None


def _accept(proposals: list[PolicyRuleProposal], text: str, rule_id: str | None,
            vocab: dict[str, list[str]]) -> _Layer:
    """Exactly one validated, core-expressible proposal becomes the layer's rule."""
    shown = [p.model_dump(exclude_none=True) for p in proposals]
    if not proposals:
        return _Layer(note="compiled no rule", proposals=shown)
    cleaned: list[PolicyRuleProposal] = []
    for proposal in proposals:
        ok, reason = validate_rule(proposal, vocab)
        if ok is None:
            return _Layer(note=f"refused: {reason}", why=_plain(reason), proposals=shown)
        cleaned.append(ok)
    listed = " ".join(describe_rule(p) for p in cleaned)
    core_rules, reasons = map_to_core(cleaned)
    if reasons or len(core_rules) != len(cleaned):
        return _Layer(note="not expressible in core policy: " + "; ".join(reasons),
                      why=f"Alibi read this as: {listed} Its sharing filter cannot apply that yet "
                          f"({'; '.join(_plain(r) for r in reasons)}).", proposals=shown)
    if len(core_rules) > 1:
        return _Layer(note=f"compiled {len(core_rules)} rules from one text ({listed}) and Alibi "
                           f"saves one rule at a time",
                      why=f"Alibi read this as {len(core_rules)} rules: {listed} Add each as its own rule.",
                      proposals=shown)
    rule = _with_identity(core_rules[0], text, rule_id)
    return _Layer(rule=rule, interpretation=_interpret(cleaned[0], rule), proposals=shown)


# --------------------------------------------------------------------------- layers


def _gemini_layer(text: str, client: Any, vocab: dict[str, list[str]], rule_id: str | None) -> _Layer:
    parse = gemini_parse_rule_text(text, client=client, material=vocab, rule_id=rule_id)
    if parse.status != "resolved":
        return _Layer(note=parse.reason or f"status {parse.status}",
                      proposals=[p.model_dump(exclude_none=True) for p in parse.rules])
    return _accept(list(parse.rules), text, rule_id, vocab)


def _execution_layer(text: str, material: dict[str, Any], vocab: dict[str, list[str]],
                     rule_id: str | None) -> _Layer:
    from agents.execution.api import compile_policy

    compiled = compile_policy(text, material)
    unresolved = compiled.get("unresolved") or []
    if unresolved:
        questions = "; ".join(str(u.get("question") or "needs clarification") for u in unresolved)
        return _Layer(note=f"asked for clarification: {questions}", why=questions,
                      proposals=list(compiled.get("rules") or []))
    if compiled.get("alerts"):
        return _Layer(note="an alert preference is not a disclosure rule",
                      proposals=list(compiled.get("rules") or []))
    if compiled.get("held"):
        return _Layer(note="compiled with low confidence",
                      why="Alibi is not confident enough in its reading of this text to apply it.",
                      proposals=list(compiled.get("rules") or []))
    proposals = []
    for raw in compiled.get("rules") or []:
        proposal = _proposal_from_execution(raw)
        if proposal is None:
            return _Layer(note=f"compiled rule did not match the parser schema: {raw!r}"[:300],
                          proposals=list(compiled.get("rules") or []))
        proposals.append(proposal)
    return _accept(proposals, text, rule_id, vocab)


def _core_layer(text: str, rule_id: str | None) -> tuple[RuleParse, _Layer]:
    parse = core_parse_rule_text(text, rule_id=rule_id)
    if parse.status == "resolved" and parse.rule is not None:
        return parse, _Layer(rule=parse.rule, interpretation=parse.interpretation)
    return parse, _Layer(note=parse.reason or "unresolved")


# --------------------------------------------------------------------------- entry point


def parse_rule_text(text: str, *, material: dict[str, Any] | None = None, client: Any = None,
                    now: datetime | None = None, rule_id: str | None = None,
                    use_gemini: bool | None = None) -> ChainParse:
    """Turn one plain-English rule into one typed core rule, or hold it for clarification.

    ``material`` is the passport vocabulary (``agents.execution.api.load_policy_material``
    shape); ``client`` is a Gemini-shaped client (``FakeClient`` in tests) and forces the
    Gemini layer on. ``use_gemini=True`` asks for the live layer, which then runs only when
    ``gemini_enabled()`` holds (key exported, replay off) and otherwise records the reason in
    ``.skipped["gemini"]``; ``False`` switches it off (the rules page restates active rules
    this way without spending calls); the default ``None`` does not request it, so a plain
    ``parse_rule_text(text)`` from a library caller or a test never builds a client. ``now`` is
    accepted for signature parity with the other parsers and is not needed to parse a rule.
    ``rule_id`` names the rule; by default the id is deterministic from the text and the
    compiled rule, so the same text gives the same id whichever layer answered.
    """
    tried: list[str] = []
    skipped: dict[str, str] = {}
    notes: dict[str, str] = {}
    proposals: list[dict[str, Any]] = []
    whys: list[str] = []
    if not isinstance(text, str) or not text.strip():
        core, _ = _core_layer(text if isinstance(text, str) else "", rule_id)
        return ChainParse(core.status, None, core.interpretation, core.reason, source="core",
                          tried=["core"], notes={"core": core.reason or "unresolved"})
    source_text = text.strip()
    material = material if material is not None else default_material()
    vocab = normalise_material(material)

    # 1. Gemini, only with a client in hand or when asked for with a key exported (never during
    #    replay). A default call runs the deterministic layers only.
    if use_gemini is False:
        run_gemini, skipped["gemini"] = False, "switched off"
    elif client is not None:
        run_gemini = True
    elif use_gemini is None:
        run_gemini, skipped["gemini"] = False, "not requested (deterministic layers only)"
    elif gemini_enabled():
        run_gemini = True
    else:
        run_gemini, skipped["gemini"] = False, gemini_skip_reason() or "unavailable"
    if run_gemini:
        if client is None:
            from alibi.llm.client import GeminiClient

            client = GeminiClient()
        tried.append("gemini")
        try:
            layer = _gemini_layer(source_text, client, vocab, rule_id)
        except Exception as exc:  # noqa: BLE001 - a model failure never blocks the next layer
            layer = _Layer(note=f"{type(exc).__name__}: {exc}"[:300])
        proposals = layer.proposals or proposals
        if layer.rule is not None:
            return ChainParse("resolved", layer.rule, layer.interpretation, None, source="gemini",
                              tried=tried, skipped=skipped, notes=notes, proposals=layer.proposals)
        notes["gemini"] = layer.note
        if layer.why:
            whys.append(layer.why)

    # 2. Execution's deterministic compiler, mapped onto core rules.
    tried.append("execution")
    try:
        layer = _execution_layer(source_text, material, vocab, rule_id)
    except Exception as exc:  # noqa: BLE001 - never let a compiler fault block the core parser
        layer = _Layer(note=f"{type(exc).__name__}: {exc}"[:300])
    proposals = layer.proposals or proposals
    if layer.rule is not None:
        return ChainParse("resolved", layer.rule, layer.interpretation, None, source="execution",
                          tried=tried, skipped=skipped, notes=notes, proposals=layer.proposals)
    notes["execution"] = layer.note
    if layer.why:
        whys.append(layer.why)

    # 3. The core whole-sentence parser decides, and its reason is what the holder sees.
    tried.append("core")
    core, layer = _core_layer(source_text, rule_id)
    if layer.rule is not None:
        return ChainParse("resolved", layer.rule, layer.interpretation, None, source="core",
                          tried=tried, skipped=skipped, notes=notes, proposals=proposals)
    notes["core"] = layer.note
    reason = core.reason or "No rule was created from this text."
    if reason.startswith(_GENERIC_CORE_REASON) and whys:
        reason = f"{reason} {whys[-1]}"
    return ChainParse("unresolved", None, core.interpretation, reason, source="core", tried=tried,
                      skipped=skipped, notes=notes, proposals=proposals)
