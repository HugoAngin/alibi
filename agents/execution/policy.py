"""Reference policy compiler and enforcement filter (shared/CONTRACTS.md, Policy row).

    from agents.execution.api import compile_policy, apply_policy

    compiled = compile_policy("Don't show what I bought.")               # plain text, code parser
    compiled = compile_policy({"kind": "age_limit", "target": "all", ...}) # one typed rule
    compiled = compile_policy(gemini_output)                              # parser output dict
    # -> {"rules": [typed], "unresolved": [{source_text, question}], "alerts": [...],
    #     "held": bool, "version": "pol-<sha12>"}

    result = apply_policy(package, compiled, "bank_a", records=record_index(case), now=decision_time)
    # -> {"held": bool, "package": filtered | None, "gaps": [...], "private_review": {...}}

Typed rule (contract names): {"rule_id", "kind": deny | generalise | age | recipient,
"target": category | merchant | field | record_kind | all, "value", "params", "source_text",
"confidence", "held"}. Parser names `age_limit` and `recipient_restrict` are accepted and
renamed. `alert_preference` is not a disclosure rule; it is returned under "alerts".

The text compiler is deterministic and conservative: it compiles only phrasings it recognises
against the passport vocabulary, and anything vague, unsupported, injected or naming something
outside the vocabulary becomes `unresolved`. It never guesses.

Enforcement, in order: unresolved or held rules hold the whole package (nothing to approve);
record rules withhold whole records (deny wins over every other rule; a record whose metadata a
rule needs but cannot be found is withheld); field rules drop or generalise fields; a claim that
cites withheld evidence becomes `unresolved` with a generic gap, and its rationale, calculation
and timing are dropped; reconciliation that depends on withheld records is dropped. Gap text is
identical whatever was withheld. A final scan of every string fails closed if a withheld record
id, a denied value or withheld free text is still present.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime, timedelta

from agents.execution.api import load_policy_material

KIND_ALIASES = {"age_limit": "age", "recipient_restrict": "recipient"}
DISCLOSURE_KINDS = {"deny", "generalise", "age", "recipient"}
TARGETS = {"category", "merchant", "field", "record_kind", "all"}
HOLD_CONFIDENCE = 0.8

# Same wording as eval's reference (agents/eval/sharing_privacy.py) so both agree on the wire.
WITHHELD_GAP = "Some supporting evidence is withheld at the account holder's request."
WITHHELD_RATIONALE = "Part of the evidence for this answer is withheld, so it is not shown."
GAP_CODE = "withheld_by_holder"
DEFAULT_REPLACEMENT = "withheld"


class DisclosureError(Exception):
    """The filtered package still contains withheld material. Fail closed."""


# --------------------------------------------------------------------------- compile

def _rule_id(rule: dict) -> str:
    key = json.dumps({k: rule[k] for k in ("kind", "target", "value", "params")}, sort_keys=True)
    return "r-" + hashlib.sha256(key.encode()).hexdigest()[:8]


def _typed(raw: dict, material: dict, source_text: str = "") -> tuple[dict | None, dict | None, dict | None]:
    """Validate one typed rule. Returns (rule, alert, unresolved)."""
    kind = KIND_ALIASES.get(raw.get("kind"), raw.get("kind"))
    target = raw.get("target")
    value = (raw.get("value") or "").lower()
    params = dict(raw.get("params") or {})
    text = raw.get("source_text") or source_text
    if kind == "alert_preference":
        return None, {"alert_kind": params.get("alert_kind"), "enabled": params.get("enabled"), "source_text": text,
                      "held": raw.get("confidence", 1.0) < HOLD_CONFIDENCE}, None
    vocab = {"category": material["categories"], "merchant": material["merchants"], "field": material["fields"],
             "record_kind": material["record_kinds"], "all": [""]}
    problem = None
    if kind not in DISCLOSURE_KINDS:
        problem = f"rule kind {kind!r} is not supported"
    elif target not in TARGETS:
        problem = f"target {target!r} is not supported"
    elif value not in vocab[target]:
        problem = f"{value!r} is not something in your passport"
    elif kind == "recipient" and (not params.get("recipients")
                                  or any(r not in material["recipients"] for r in params["recipients"])):
        problem = "that recipient is not one of your connected banks"
    elif kind == "age" and not (isinstance(params.get("max_age_days"), int) and params["max_age_days"] >= 0):
        problem = "the age limit needs a whole number of days"
    if problem:
        return None, None, {"source_text": text, "question": f"Could not apply this rule: {problem}. What did you mean?"}
    if kind == "generalise":
        params.setdefault("replacement", DEFAULT_REPLACEMENT)
    rule = {"kind": kind, "target": target, "value": value, "params": params, "source_text": text,
            "confidence": float(raw.get("confidence", 1.0))}
    rule["held"] = rule["confidence"] < HOLD_CONFIDENCE
    rule["rule_id"] = raw.get("rule_id") or _rule_id(rule)
    return rule, None, None


def compile_policy(item, material: dict | None = None) -> dict:
    """Compile plain text, one typed rule, a list of typed rules, or a parser output dict."""
    material = material or load_policy_material()
    rules, alerts, unresolved = [], [], []
    if isinstance(item, str):
        parsed = parse_text(item, material)
        raw_rules, unresolved = parsed["rules"], list(parsed["unresolved"])
    elif isinstance(item, dict) and "rules" in item:
        raw_rules = item["rules"]
        unresolved = [{"source_text": c.get("source_text", ""), "question": c.get("question", "")}
                      for c in item.get("clarifications", []) + item.get("unresolved", [])]
    elif isinstance(item, dict):
        raw_rules = [item]
    else:
        raw_rules = list(item)
    for raw in raw_rules:
        rule, alert, problem = _typed(raw, material)
        if rule:
            rules.append(rule)
        if alert:
            alerts.append(alert)
        if problem:
            unresolved.append(problem)
    version = "pol-" + hashlib.sha256(json.dumps(
        [{k: r[k] for k in ("kind", "target", "value", "params")} for r in rules], sort_keys=True).encode()).hexdigest()[:12]
    held = bool(unresolved) or any(r["held"] for r in rules)
    return {"rules": rules, "unresolved": unresolved, "alerts": alerts, "held": held, "version": version}


# --------------------------------------------------------------------------- text parser

_INJECTION = re.compile(r"\b(ignore (your|all|previous)|system\s*:|set confidence|skip clarif|mark everything|"
                        r"full access|override)\b", re.I)
_UNSUPPORTED = re.compile(r"\b(automatically|auto[- ]?send|whatever (it|they) asks?|can see everything|"
                          r"see everything|share everything|my ex\b|my mum|my dad|my partner|as a total|"
                          r"in total|totals? only)\b", re.I)
_VAGUE = re.compile(r"\b(private|embarrassing|personal|sensitive stuff|the bad stuff)\b", re.I)
_EXCEPTION = re.compile(r"\b(except|apart from|other than|but not)\b", re.I)
_ABSOLUTE_DATE = re.compile(r"\b(before|after|since|from) (january|february|march|april|may|june|july|august|"
                            r"september|october|november|december|last year|this year|\d{4})\b|"
                            r"\b(last|this) year\b", re.I)
_DEFAULT = re.compile(r"^\s*only share what'?s (needed|necessary)\.?\s*$", re.I)
_BANK = re.compile(r"\bbank ([a-z])\b", re.I)
_AGE = re.compile(r"\bolder than (a|an|one|\d+) (day|week|month|year)s?\b", re.I)
_LAST_N = re.compile(r"\b(?:only )?the last (\d+) (day|week|month|year)s?\b", re.I)
_UNIT_DAYS = {"day": 1, "week": 7, "month": 30, "year": 365}
_WORDNUM = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "six": 6, "twelve": 12}
_QUOTED = re.compile(r"""['"‘“]([^'"’”]{1,40})['"’”]""")
_GENERALISE = re.compile(r"\b(just (say|call (it|them))|call (it|them)|replace (it|them) with|instead say)\b", re.I)
_DENY = re.compile(r"\b(don'?t|do not|dont|never|no|hide|leave out|keep .* out|without|exclude|shouldn'?t|can'?t|"
                   r"not)\b", re.I)
_ALERT_OFF = re.compile(r"\b(stop|don'?t|no more) (alerting|warning|telling|pestering|notifying)\b", re.I)
_ALERT_ON = re.compile(r"\b(warn|alert|tell|notify) me (whenever|when|if)\b", re.I)

# Phrase -> (target, value). Longest phrases are tried first. Words outside the passport
# vocabulary (for example "medical" when the category is "health") are deliberately absent:
# the parser asks rather than maps a synonym.
LEXICON = {
    "what i bought": ("record_kind", "purchase"), "my purchases": ("record_kind", "purchase"),
    "purchases": ("record_kind", "purchase"),
    "health purchases": ("category", "health"), "health": ("category", "health"), "rent": ("category", "rent"), "transport": ("category", "transport"),
    "groceries": ("category", "groceries"),
    "account number": ("field", "account_ref"), "account numbers": ("field", "account_ref"),
    "the pharmacy": ("merchant", "synthetic-pharmacy"), "synthetic pharmacy": ("merchant", "synthetic-pharmacy"),
    "synthetic grocer": ("merchant", "synthetic-grocer"), "synthetic lettings": ("merchant", "synthetic-lettings"),
    "marketplace's messages": ("field", "message"), "marketplace messages": ("field", "message"),
    "messages": ("field", "message"),
    "item names": ("field", "item"), "what the items were": ("field", "item"), "items": ("field", "item"),
    "who i paid": ("field", "counterparty_name"),
    "payment references": ("field", "payment_reference"), "payment reference": ("field", "payment_reference"),
    "my sales": ("record_kind", "sale"), "sales": ("record_kind", "sale"),
    "my refunds": ("record_kind", "refund"), "refunds": ("record_kind", "refund"),
    "where the money was sent": ("field", "destination"),
    "bank descriptions": ("field", "description"), "descriptions": ("field", "description"),
}
_LEX = sorted(LEXICON, key=len, reverse=True)


def _find_target(text: str):
    low = text.lower()
    for phrase in _LEX:
        if re.search(r"(?<![a-z])" + re.escape(phrase) + r"(?![a-z])", low):
            return LEXICON[phrase]
    return None


def _days(n: str, unit: str) -> int:
    num = int(n) if n.isdigit() else _WORDNUM.get(n.lower(), 0)
    return num * _UNIT_DAYS[unit.lower()]


def _clauses(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+|;\s*|,\s*but\s+|,\s*and\s+|\s+and\s+(?=nothing|only|never|don'?t)", text.strip())
    return [p.strip() for p in parts if p.strip()]


def _parse_clause(clause: str, material: dict) -> tuple[list[dict], list[dict]]:
    ask = lambda q: ([], [{"source_text": clause, "question": q}])
    low = clause.lower()
    if _INJECTION.search(clause):
        return ask("This reads like an instruction to the system rather than a privacy rule, so it was not applied.")
    if _UNSUPPORTED.search(clause):
        return ask("Alibi can't compile this kind of rule (automatic sharing, sharing everything, totals only, "
                   "or a rule about a person). Can you say which records or fields to restrict?")
    if _EXCEPTION.search(clause):
        return ask("Rules with exceptions are ambiguous. Which records should stay shareable?")
    banks = [b.lower() for b in _BANK.findall(clause)]
    unknown = [b for b in banks if f"bank_{b}" not in material["recipients"]]
    if unknown:
        return ask(f"Bank {unknown[0].upper()} is not one of your connected banks. Which bank did you mean?")
    if _ALERT_OFF.search(clause):
        kind = "unusual_inflow" if re.search(r"deposit|inflow|money coming in", low) else None
        if kind:
            return [{"kind": "alert_preference", "target": "all", "value": "",
                     "params": {"alert_kind": kind, "enabled": False}, "source_text": clause, "confidence": 0.75}], []
        return ask("Which alerts should be switched off?")
    if _ALERT_ON.search(clause):
        if re.search(r"merchant.*(isn'?t|not) verified|unverified merchant", low):
            return [{"kind": "alert_preference", "target": "all", "value": "",
                     "params": {"alert_kind": "merchant_verification_gap", "enabled": True},
                     "source_text": clause, "confidence": 0.9}], []
        return ask("Which alerts do you want?")
    last_n = _LAST_N.search(clause)
    if last_n and banks:
        return ask("Age limits apply to every bank. Should this limit apply to all banks, or only restrict this bank?")
    age = _AGE.search(clause) or last_n
    if age:
        if _ABSOLUTE_DATE.search(clause):
            return ask("Alibi limits evidence by age in days. How many days back should be shareable?")
        return [{"kind": "age_limit", "target": "all", "value": "",
                 "params": {"max_age_days": _days(age.group(1), age.group(2))}, "source_text": clause,
                 "confidence": 0.95}], []
    if re.search(r"\b(six months|half a year)\b", low):
        return [{"kind": "age_limit", "target": "all", "value": "", "params": {"max_age_days": 182},
                 "source_text": clause, "confidence": 0.9}], []
    if _ABSOLUTE_DATE.search(clause):
        return ask("Alibi limits evidence by age in days. How many days back should be shareable?")
    if _DEFAULT.match(clause):
        return [], []
    found = _find_target(clause)
    if found is None:
        if _VAGUE.search(clause) or _DENY.search(clause):
            return ask("Which records or fields does this rule cover?")
        return [], []
    target, value = found
    if _VAGUE.search(clause) and target != "record_kind":
        return ask("Which records or fields does this rule cover?")
    terse = len(clause.split()) <= 3
    conf = 0.75 if terse else 0.92
    # recipient rules: "only Bank A sees X", "Bank A can see X but Bank B can't", "Bank B shouldn't see X"
    if banks:
        allowed = set()
        if re.search(r"\bonly bank ([a-z])\b", low):
            allowed = {f"bank_{re.search(r'only bank ([a-z])', low).group(1)}"}
        else:
            for b in banks:
                seg = low.split(f"bank {b}", 1)[1][:30]
                if not re.search(r"^\s*(shouldn'?t|can'?t|cannot|must not|doesn'?t|should not|never)", seg):
                    allowed.add(f"bank_{b}")
            negated = {f"bank_{b}" for b in banks} - allowed
            if negated and not allowed:
                allowed = set(material["recipients"]) - negated
        if not allowed:
            return ask("Which bank may receive this?")
        return [{"kind": "recipient_restrict", "target": target, "value": value,
                 "params": {"recipients": sorted(allowed)}, "source_text": clause, "confidence": conf}], []
    if _GENERALISE.search(clause):
        q = _QUOTED.search(clause)
        return [{"kind": "generalise", "target": target, "value": value,
                 "params": {"replacement": q.group(1) if q else DEFAULT_REPLACEMENT}, "source_text": clause,
                 "confidence": conf}], []
    if _DENY.search(clause) or terse:
        return [{"kind": "deny", "target": target, "value": value, "params": {}, "source_text": clause,
                 "confidence": conf}], []
    return ask("Should this be hidden from every bank, or only some?")


def parse_text(text: str, material: dict | None = None) -> dict:
    """Plain text -> {"rules": [parser-schema rules], "unresolved": [...]}. Code only, no model."""
    material = material or load_policy_material()
    rules, unresolved = [], []
    for clause in _clauses(text):
        r, u = _parse_clause(clause, material)
        rules += r
        unresolved += u
    return {"rules": rules, "unresolved": unresolved}


def parse_rule_text(text: str, material: dict | None = None) -> dict:
    """Parser-schema output for agents.execution.policy_harness: {"rules", "clarifications"}."""
    parsed = parse_text(text, material)
    return {"rules": parsed["rules"], "clarifications": parsed["unresolved"]}


# --------------------------------------------------------------------------- records

def record_index(case: dict) -> dict:
    """record_id / txn_id -> metadata the filter needs, from a data case (all deliveries + lookups)."""
    idx = {}
    events = [d["event"] for d in case.get("merchant_deliveries", [])]
    events += [x["event"] for x in case.get("merchant_lookup", [])]
    events += [i.get("event", i) for i in case.get("imports", [])]
    for e in events:
        rid = e.get("record_id")
        if not rid:
            continue
        cur = idx.get(rid)
        if cur and cur["revision"] > e.get("revision", 1):
            continue
        idx[rid] = {"record_id": rid, "revision": e.get("revision", 1), "record_kind": e.get("kind"),
                    "category": e.get("category"), "merchant": e.get("issuer_id"),
                    "at": e.get("occurred_at"), "fields": {k: v for k, v in e.items()}}
    for d in case.get("bank_deliveries", []):
        row = d["row"]
        kind = "bank_credit" if row.get("amount_minor", 0) > 0 else "bank_debit"
        merchant = re.sub(r"[^a-z0-9]+", "-", (row.get("counterparty_name") or "").lower()).strip("-")
        merchant = re.sub(r"-(ltd|limited|payments)$", "", merchant)
        idx[row["txn_id"]] = {"record_id": row["txn_id"], "revision": row.get("revision", 1), "record_kind": kind,
                              "category": None, "merchant": merchant, "at": row.get("booked_at"),
                              "fields": dict(row)}
    return idx


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _record_decision(meta: dict | None, rules: list[dict], recipient: str, now: datetime) -> str | None:
    """Return the rule_id that withholds the whole record, or None to keep it. Deny is checked first."""
    def hits(rule):
        t, v = rule["target"], rule["value"]
        if t == "all":
            return True
        if meta is None:
            return True  # cannot classify: fail closed
        if t == "record_kind":
            return meta["record_kind"] == v
        if t == "category":
            return meta["category"] == v
        if t == "merchant":
            return bool(meta["merchant"]) and (meta["merchant"] == v or v.startswith(meta["merchant"]) or meta["merchant"].startswith(v))
        return False

    for rule in (r for r in rules if r["kind"] == "deny" and r["target"] != "field"):
        if hits(rule):
            return rule["rule_id"]
    for rule in (r for r in rules if r["kind"] == "recipient" and r["target"] != "field"):
        if hits(rule) and recipient not in rule["params"]["recipients"]:
            return rule["rule_id"]
    for rule in (r for r in rules if r["kind"] == "age"):
        if meta is None or not meta.get("at"):
            return rule["rule_id"]
        if _parse_time(meta["at"]) < now - timedelta(days=rule["params"]["max_age_days"]):
            return rule["rule_id"]
    return None


def _field_actions(rules: list[dict], recipient: str) -> dict[str, tuple[str, str, str | None]]:
    """field -> (action, rule_id, replacement); deny beats recipient beats generalise."""
    out: dict[str, tuple[str, str, str | None]] = {}
    rank = {"deny": 0, "recipient": 1, "generalise": 2}
    for r in sorted((r for r in rules if r["target"] == "field"), key=lambda r: rank.get(r["kind"], 9)):
        f = r["value"]
        if f in out:
            continue
        if r["kind"] == "deny":
            out[f] = ("drop", r["rule_id"], None)
        elif r["kind"] == "recipient" and recipient not in r["params"]["recipients"]:
            out[f] = ("drop", r["rule_id"], None)
        elif r["kind"] == "generalise":
            out[f] = ("replace", r["rule_id"], r["params"].get("replacement", DEFAULT_REPLACEMENT))
    return out


# --------------------------------------------------------------------------- apply

def _strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _strings(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _strings(v)


def _summary(meta: dict | None, fallback: str, field_actions: dict) -> str:
    if meta is None:
        return fallback
    kind = meta["record_kind"] or "record"
    label = "Bank credit" if kind == "bank_credit" else ("Bank debit" if kind == "bank_debit" else kind)
    amount = meta["fields"].get("amount_minor")
    if amount is None or "amount_minor" in field_actions:
        return label
    return f"{label} · {abs(amount)} minor units {meta['fields'].get('currency', '')}".rstrip()


def apply_policy(package: dict, compiled: dict, recipient: str, *, records: dict, now) -> dict:
    """Filter an analyst-package-shaped dict (agents.eval.api.build_analyst_preview) for one recipient."""
    if isinstance(now, str):
        now = _parse_time(now)
    if compiled.get("held"):
        return {"held": True, "package": None, "gaps": [], "version": compiled.get("version"),
                "private_review": {"reason": "policy has unresolved or low-confidence rules awaiting your confirmation",
                                   "unresolved": compiled.get("unresolved", []),
                                   "held_rules": [r["rule_id"] for r in compiled.get("rules", []) if r["held"]]}}
    rules = compiled["rules"]
    pkg = copy.deepcopy(package)
    field_actions = _field_actions(rules, recipient)

    ids = {s["record_id"] for s in pkg.get("source_trust", [])}
    for q in pkg.get("evidence_by_question", []):
        ids |= {e["record_id"] for e in q.get("evidence", [])}
    for line in (pkg.get("reconciliation") or {}).get("lines", []):
        ids |= {s.split(" ")[0] for s in line.get("source_ids", [])}
    withheld = {}
    for rid in sorted(ids):
        why = _record_decision(records.get(rid), rules, recipient, now)
        if why:
            withheld[rid] = why

    excluded = [{"record_id": rid, "rule_id": why} for rid, why in withheld.items()]
    generalised = [{"field": f, "rule_id": a[1]} for f, a in field_actions.items() if a[0] == "replace"]
    dropped_fields = [{"field": f, "rule_id": a[1]} for f, a in field_actions.items() if a[0] == "drop"]

    pkg["source_trust"] = [s for s in pkg.get("source_trust", []) if s["record_id"] not in withheld]
    for s in pkg["source_trust"]:
        s["summary"] = _summary(records.get(s["record_id"]), s.get("summary", ""), field_actions)

    gaps = []
    affected = set()
    for q in pkg.get("evidence_by_question", []):
        before = q.get("evidence", [])
        q["evidence"] = [e for e in before if e["record_id"] not in withheld]
        for e in q["evidence"]:
            e["summary"] = _summary(records.get(e["record_id"]), e.get("summary", ""), field_actions)
        if len(q["evidence"]) != len(before) or (q.get("calculation") and "amount_minor" in field_actions):
            affected.add(q["question_id"])
            q["claim"] = {"status": "unresolved", "rationale": WITHHELD_RATIONALE}
            q["calculation"], q["timing"], q["gap"] = None, None, WITHHELD_GAP
            gaps.append({"question_id": q["question_id"], "description": WITHHELD_GAP, "reason_code": GAP_CODE})
    for cs in pkg.get("claim_status", []):
        if cs["question_id"] in affected:
            cs["status"], cs["summary"] = "unresolved", WITHHELD_RATIONALE

    recon = pkg.get("reconciliation")
    if recon:
        uses_withheld = any(s.split(" ")[0] in withheld for line in recon.get("lines", []) for s in line.get("source_ids", []))
        if uses_withheld or "amount_minor" in field_actions:
            pkg["reconciliation"] = {"title": "Reconciliation not shown: it relies on withheld evidence.", "lines": [],
                                     "expression": "", "result": None, "computed_by": recon.get("computed_by"),
                                     "matches": None, "compared_to": None, "difference": None,
                                     "computed_at": recon.get("computed_at"), "withheld": True}
    pkg["gaps"] = list(pkg.get("gaps", [])) + gaps

    # Fail closed: nothing withheld may survive anywhere in the package. The request's own scope
    # (txn_ids the bank named) is not a disclosure, so it is exempt from the id scan.
    scope = set((pkg.get("package") or {}).get("txn_ids") or [])
    banned = set(withheld) - scope
    for rid in withheld:
        meta = records.get(rid) or {}
        for k in ("item", "message", "description", "counterparty_name"):
            v = (meta.get("fields") or {}).get(k)
            if isinstance(v, str) and len(v) >= 6:
                banned.add(v)
    for r in rules:
        if r["kind"] in ("deny", "recipient") and r["target"] in ("category", "merchant") and r["value"]:
            if r["kind"] == "deny" or recipient not in r["params"]["recipients"]:
                banned.add(r["value"])
    for text in _strings(pkg):
        low = text.lower()
        for b in banned:
            if b.lower() in low:
                raise DisclosureError("filtered package still contains withheld material")

    return {"held": False, "package": pkg, "gaps": gaps, "version": compiled.get("version"),
            "private_review": {"excluded": excluded, "dropped_fields": dropped_fields, "generalised": generalised,
                               "withheld_count": len(withheld)}}
