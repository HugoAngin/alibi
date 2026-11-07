"""Disclosure policy: typed rules compiled into a filter over an allowlisted field set.

What a recipient may see of each source is decided here, in code:

* Extracts are built from the source's ``to_json_dict()`` (timestamps as UTC ``Z`` strings)
  and keep only allowlisted fields: ``RECORD_FIELDS`` for merchant evidence, ``TXN_FIELDS``
  for bank transactions. Message text, subject and account references, provenance and
  signatures never appear in an extract.
* Rules only ever restrict. A rule with ``kinds`` applies only to merchant records of those
  kinds. Bank transactions have no kind, so only unscoped rules reach them, and a field rule
  applies to a transaction when the field name matches (``description``, ``amount_pence``).
* Deny wins over everything. A deny rule without fields excludes whole records of its kinds;
  a denied field is withheld even when a generalise rule names it too.
* Fail closed. A field a rule cannot evaluate (no generaliser for the level, an item without
  a category, an unparseable timestamp, an unknown item field) is withheld. A source a rule
  cannot evaluate (no timestamp under an age rule, a rule naming an unknown field) is
  excluded. Nothing is passed through because a rule could not be applied.
* Field paths are top-level names or ``items.<key>``, which applies to every item. ``items``
  alone covers all item detail. A rule naming part of something a full record carries whole
  (``signed_body.items``, ``issuer_signature.sig``, ``subject_ref.x``) cannot be evaluated,
  so it excludes the source like an unknown field.
* One record at a time the filter cannot see other exclusions, so the caller passes
  ``excluded_ids`` (records left out of the same package). Their ids are withheld from
  ``related_record_ids``, and a signed body naming one never goes out as a full record.
* Age limits keep a source dated exactly ``max_age_days`` before ``now`` and exclude anything
  strictly older (records use ``occurred_at``, transactions ``booked_at``).
* ``full_record_ok`` needs an issuer-verified, included record with nothing withheld or
  generalised, a signed body holding only allowlisted fields and opaque meta ids, and no field
  rule naming anything the full record carries (its body keys or the signature).

``parse_rule_text`` maps a handful of plain phrasings onto one typed rule with regexes and
returns ``unresolved`` for anything else. The LLM parser arrives later with the same
signature.
"""

import re
from collections.abc import Collection
from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, NamedTuple

from pydantic import BaseModel, TypeAdapter, ValidationError

from alibi.core.canon import make_id
from alibi.core.schemas import (
    AgeRule,
    BankTxn,
    DenyRule,
    EvidenceRecord,
    EvidenceRef,
    GeneraliseRule,
    ItemDetail,
    Policy,
    PolicyRule,
    RecipientRule,
    RecordKind,
    SourceTrust,
    parse_utc,
    utc_now,
)

__all__ = [
    "FULL_RECORD_META_FIELDS",
    "REASON_BAD_TIMESTAMP",
    "REASON_CATEGORY_WITHHELD",
    "REASON_DENIED",
    "REASON_EXCLUDED_REF",
    "REASON_MALFORMED",
    "REASON_NOT_SHAREABLE",
    "REASON_NO_CATEGORY",
    "REASON_REDACTED",
    "RECORD_FIELDS",
    "TIMESTAMP_FIELDS",
    "TXN_FIELDS",
    "CompiledPolicy",
    "FilterResult",
    "PolicyError",
    "RuleParse",
    "compile_policy",
    "parse_rule_text",
]

# --------------------------------------------------------------------------- field sets

RECORD_FIELDS: frozenset[str] = frozenset({
    "record_id", "revision", "kind", "issuer_id", "occurred_at", "effective_at",
    "amount_pence", "currency", "payment_ref", "settlement_ref", "status",
    "items.description", "items.category", "items.quantity", "items.unit_amount_pence",
    "related_record_ids", "corrects_revision", "correction_reason", "source_trust",
})
TXN_FIELDS: frozenset[str] = frozenset({
    "txn_id", "revision", "amount_pence", "currency", "booked_at", "description", "refs",
    "source_trust",
})
# Opaque ids that may sit inside a full issuer-signed record but never in an extract. The
# recipient already holds the subject id from its own request.
FULL_RECORD_META_FIELDS: frozenset[str] = frozenset({"schema_version", "event_id", "subject_ref"})
TIMESTAMP_FIELDS: frozenset[str] = frozenset({"occurred_at", "effective_at", "booked_at"})

# Left out of extracts without a review entry: bookkeeping, trust machinery (the signed body
# and its signature only travel as a full record) and the opaque meta ids above. Any other
# field outside the allowlist (text, account_ref, provenance, future fields) is reported as
# withheld.
_SILENT_RECORD_KEYS = FULL_RECORD_META_FIELDS | {
    "connector", "received_at", "payload_sha256", "issuer_signature", "signed_body"}
_SILENT_TXN_KEYS = frozenset({"schema_version", "subject_ref"})

_ITEM_KEYS = frozenset(ItemDetail.model_fields)
_SOURCE_KEYS = frozenset(EvidenceRecord.model_fields) | frozenset(BankTxn.model_fields)
_SHAREABLE_TOP = frozenset(path.split(".")[0] for path in RECORD_FIELDS | TXN_FIELDS)
# Fields that never leave the passport in any form, so a rule may name parts of them. The
# signed body, its signature and the meta ids are not among them: a full record carries
# them whole.
_PRIVATE_TOP = _SOURCE_KEYS - _SHAREABLE_TOP - FULL_RECORD_META_FIELDS - {
    "signed_body", "issuer_signature"}

REASON_NOT_SHAREABLE = "not in the shareable field set"
REASON_DENIED = "denied by policy"
REASON_REDACTED = "redacted by policy"
REASON_NO_CATEGORY = "cannot generalise: the item has no category"
REASON_CATEGORY_WITHHELD = "cannot generalise: the item category is withheld"
REASON_BAD_TIMESTAMP = "cannot generalise: not a valid timestamp"
REASON_MALFORMED = "malformed value"
REASON_EXCLUDED_REF = "refers to a record that is not shared"

_RULE_TYPES = (DenyRule, GeneraliseRule, AgeRule, RecipientRule)
_RULE_ADAPTER: TypeAdapter = TypeAdapter(PolicyRule)


class PolicyError(ValueError):
    """A policy or rule is malformed, or a filter was called with invalid arguments."""


@dataclass
class FilterResult:
    """What one source may disclose to one recipient.

    ``withheld`` entries are ``(field, reason, rule_id)``; ``generalised`` entries are
    ``(field, level, rule_id)``. ``rule_id`` is None when the allowlist, not a rule, decided.
    """

    ref: EvidenceRef
    included: bool
    fields: dict[str, Any]
    full_record_ok: bool
    excluded_reason: str | None = None
    excluded_rule: str | None = None
    withheld: list[tuple[str, str, str | None]] = dc_field(default_factory=list)
    generalised: list[tuple[str, str, str | None]] = dc_field(default_factory=list)


@dataclass
class RuleParse:
    """Outcome of turning one plain-English rule into a typed rule."""

    status: Literal["resolved", "unresolved"]
    rule: PolicyRule | None
    interpretation: str          # plain-English restatement for the holder to confirm
    reason: str | None = None    # why the text was not resolved


# --------------------------------------------------------------------------- small helpers


def _short(text: str, limit: int = 60) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _add(entries: list, entry: tuple) -> None:
    if entry not in entries:
        entries.append(entry)


def _as_utc(value: Any) -> datetime | None:
    """A UTC datetime from an aware datetime or an ISO-8601 string with a zone, else None."""
    if isinstance(value, str):
        try:
            return parse_utc(value)
        except ValueError:
            return None
    if isinstance(value, datetime) and value.utcoffset() is not None:
        return value.astimezone(timezone.utc)
    return None


def _resolve_now(now: datetime | None) -> datetime:
    if now is None:
        return utc_now()
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise PolicyError("now must be a timezone-aware datetime")
    return now.astimezone(timezone.utc)


def _check_audience(audience: Any) -> str:
    if not isinstance(audience, str) or not audience:
        raise PolicyError("audience must be a non-empty recipient id")
    return audience


def _check_ids(ids: Any) -> frozenset[str]:
    if (isinstance(ids, (str, bytes)) or not isinstance(ids, Collection)
            or not all(isinstance(item, str) for item in ids)):
        raise PolicyError("excluded_ids must be a collection of record ids")
    return frozenset(ids)


def _check_kind(kind: Any) -> RecordKind | None:
    if kind is None:
        return None
    try:
        return RecordKind(kind)
    except ValueError as exc:
        raise PolicyError(f"unknown record kind {_short(str(kind))!r}") from exc


def _validation_summary(exc: ValidationError) -> str:
    first = exc.errors()[0]
    where = ".".join(str(part) for part in first.get("loc", ())) or "rule"
    return f"{where}: {first.get('msg', 'invalid value')}"


def _coerce_rule(raw: Any) -> PolicyRule:
    """Validate a rule model or dict as one of the four typed rules."""
    if isinstance(raw, BaseModel):
        if not isinstance(raw, _RULE_TYPES):
            raise PolicyError(f"not a policy rule: {type(raw).__name__}")
        raw = raw.model_dump()
    if not isinstance(raw, dict):
        raise PolicyError(f"not a policy rule: {type(raw).__name__}")
    try:
        return _RULE_ADAPTER.validate_python(raw)
    except ValidationError as exc:
        raise PolicyError(f"invalid policy rule ({_validation_summary(exc)})") from exc


def _normalise_rules(rules: Any) -> list[PolicyRule]:
    """Validate every rule; drop exact repeats; refuse two different rules with one id."""
    if not isinstance(rules, (list, tuple)):
        raise PolicyError("rules must be a list")
    out: list[PolicyRule] = []
    seen: dict[str, dict] = {}
    for raw in rules:
        rule = _coerce_rule(raw)
        meaning = rule.model_dump(mode="json", exclude={"source_text"})
        if rule.rule_id in seen:
            if seen[rule.rule_id] != meaning:
                raise PolicyError(f"two different rules share the id {rule.rule_id}")
            continue
        seen[rule.rule_id] = meaning
        out.append(rule)
    return out


def _in_scope(rule: PolicyRule, kind: RecordKind | None) -> bool:
    """Unscoped rules reach everything; kind-scoped rules only records of those kinds."""
    return not rule.kinds or (kind is not None and kind in rule.kinds)


def _resolve_field(name: str) -> tuple[str | None, str | None]:
    """The path a rule field covers, and a problem note. Path None means unknown field."""
    top, _, sub = name.partition(".")
    if top == "items":
        if not sub or sub in _ITEM_KEYS:
            return name, None
        return "items", f"cannot evaluate unknown item field {_short(name)}; item detail withheld"
    if top in _SOURCE_KEYS and (not sub or top in _PRIVATE_TOP):
        return name, None  # a sub-path of a field that never leaves the passport
    return None, f"cannot evaluate: a rule names the unknown field {_short(name)}"


def _covers(target: str, path: str) -> bool:
    return path == target or path.startswith(target + ".")


# --------------------------------------------------------------------------- field decisions


class _Decision(NamedTuple):
    """keep, withhold (how = reason) or generalise (how = level), and the deciding rule."""

    action: Literal["keep", "withhold", "generalise"]
    how: str | None = None
    rule_id: str | None = None


_KEEP = _Decision("keep")
_LEVEL_RANK = {"category": 1, "day": 1, "month": 2}


def _generaliser(path: str, level: str, rule_id: str, problem: str | None) -> _Decision:
    if problem:
        return _Decision("withhold", problem, rule_id)
    if level == "redact":
        return _Decision("withhold", REASON_REDACTED, rule_id)
    if level == "category" and path == "items.description":
        return _Decision("generalise", "category", rule_id)
    if level == "category" and path == "items.category":
        return _KEEP  # already a category
    if level in ("day", "month") and path in TIMESTAMP_FIELDS:
        return _Decision("generalise", level, rule_id)
    return _Decision("withhold", f"cannot generalise this field to {level}", rule_id)


def _decide(path: str, allowlist: frozenset[str], deny: list[DenyRule],
            gen: list[GeneraliseRule]) -> _Decision:
    """Deny first (deny wins), then the allowlist, then the most restrictive generaliser."""
    for rule in deny:
        for name in rule.fields:
            target, problem = _resolve_field(name)
            if target is not None and _covers(target, path):
                return _Decision("withhold", problem or REASON_DENIED, rule.rule_id)
    if path not in allowlist:
        return _Decision("withhold", REASON_NOT_SHAREABLE)
    outcomes: list[_Decision] = []
    for rule in gen:
        for name in rule.fields:  # every covering name counts, so field order cannot matter
            target, problem = _resolve_field(name)
            if target is not None and _covers(target, path):
                outcomes.append(_generaliser(path, rule.level, rule.rule_id, problem))
    for outcome in outcomes:
        if outcome.action == "withhold":
            return outcome
    changes = [o for o in outcomes if o.action == "generalise"]
    return max(changes, key=lambda o: _LEVEL_RANK[o.how]) if changes else _KEEP


def _coarsen_time(value: Any, level: str | None) -> str | None:
    moment = _as_utc(value)
    if moment is None or level not in ("day", "month"):
        return None
    if level == "day":
        return f"{moment.year:04d}-{moment.month:02d}-{moment.day:02d}"
    return f"{moment.year:04d}-{moment.month:02d}"


def _filter_items(value: Any, allowlist, deny, gen, withheld: list, generalised: list) -> list:
    if not isinstance(value, list):
        _add(withheld, ("items", REASON_MALFORMED, None))
        return []
    category = _decide("items.category", allowlist, deny, gen)
    out = []
    for item in value:
        if not isinstance(item, dict):
            _add(withheld, ("items", REASON_MALFORMED, None))
            continue
        kept = {}
        for sub, subvalue in item.items():
            if subvalue is None:
                continue
            path = f"items.{sub}"
            decision = _decide(path, allowlist, deny, gen)
            if decision.action == "keep":
                kept[sub] = subvalue
            elif decision.action == "generalise" and decision.how == "category":
                # The name goes; the item's own category, disclosed unchanged, stands in for it.
                if item.get("category") is None:
                    _add(withheld, (path, REASON_NO_CATEGORY, decision.rule_id))
                elif category.action != "keep":
                    _add(withheld, (path, REASON_CATEGORY_WITHHELD, decision.rule_id))
                else:
                    _add(generalised, (path, "category", decision.rule_id))
            elif decision.action == "generalise":
                _add(withheld, (path, f"cannot generalise this field to {decision.how}",
                                decision.rule_id))
            else:
                _add(withheld, (path, decision.how, decision.rule_id))
        if kept:
            out.append(kept)
    return out


def _filter_data(data: dict[str, Any], *, allowlist: frozenset[str], silent: frozenset[str],
                 deny: list[DenyRule], gen: list[GeneraliseRule]):
    """Apply the allowlist and field rules to JSON-ready source data."""
    fields: dict[str, Any] = {}
    withheld: list[tuple[str, str, str | None]] = []
    generalised: list[tuple[str, str, str | None]] = []
    for key, value in data.items():
        if value is None or key in silent:
            continue
        if key == "items":
            items = _filter_items(value, allowlist, deny, gen, withheld, generalised)
            if items:
                fields["items"] = items
            continue
        decision = _decide(key, allowlist, deny, gen)
        if decision.action == "keep":
            fields[key] = value
        elif decision.action == "generalise":
            coarse = _coarsen_time(value, decision.how)
            if coarse is None:
                _add(withheld, (key, REASON_BAD_TIMESTAMP, decision.rule_id))
            else:
                fields[key] = coarse
                _add(generalised, (key, decision.how, decision.rule_id))
        else:
            _add(withheld, (key, decision.how, decision.rule_id))
    return fields, withheld, generalised


def _withhold_excluded_refs(fields: dict[str, Any], withheld: list,
                            excluded: frozenset[str]) -> None:
    """Drop ids of excluded records from ``related_record_ids``: they would reveal the gap."""
    related = fields.get("related_record_ids")
    if not excluded or not isinstance(related, list):
        return
    kept = [record_id for record_id in related if record_id not in excluded]
    if len(kept) == len(related):
        return
    _add(withheld, ("related_record_ids", REASON_EXCLUDED_REF, None))
    if kept:
        fields["related_record_ids"] = kept
    else:
        del fields["related_record_ids"]


def _names_any(value: Any, ids: frozenset[str]) -> bool:
    """True when one of ``ids`` appears as a whole string anywhere inside ``value``."""
    if isinstance(value, str):
        return value in ids
    if isinstance(value, dict):
        return any(_names_any(item, ids) for item in value.values())
    if isinstance(value, list):
        return any(_names_any(item, ids) for item in value)
    return False


def _field_rules(rules: list[PolicyRule]) -> tuple[list[DenyRule], list[GeneraliseRule]]:
    deny = [r for r in rules if isinstance(r, DenyRule) and r.fields]
    gen = [r for r in rules if isinstance(r, GeneraliseRule)]
    return deny, gen


def _exclusion(rules: list[PolicyRule], *, label: str, audience: str, moment: Any,
               now: datetime) -> tuple[str | None, str | None]:
    """Why the whole source is excluded, as (reason, rule_id), or (None, None)."""
    for rule in rules:
        if isinstance(rule, DenyRule) and not rule.fields:
            return f"{label} are denied by the policy", rule.rule_id
    for rule in rules:
        if isinstance(rule, RecipientRule) and audience not in rule.allow:
            return f"recipient {_short(audience)} is not allowed by the policy", rule.rule_id
    for rule in rules:
        if isinstance(rule, AgeRule):
            when = _as_utc(moment)
            if when is None:
                return "no valid timestamp to check the age limit against", rule.rule_id
            if when < now - timedelta(days=rule.max_age_days):
                return f"older than the {rule.max_age_days}-day limit", rule.rule_id
    for rule in rules:
        if isinstance(rule, (DenyRule, GeneraliseRule)):
            for name in rule.fields:
                target, problem = _resolve_field(name)
                if target is None:
                    return problem, rule.rule_id
    return None, None


def _flat_keys(body: dict[str, Any]) -> set[str] | None:
    """Keys of a signed body with items flattened to ``items.<key>``; None if malformed."""
    keys: set[str] = set()
    for key, value in body.items():
        if value is None:
            continue
        if key != "items":
            keys.add(key)
            continue
        if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
            return None
        keys.update(f"items.{sub}" for item in value for sub, v in item.items() if v is not None)
    return keys


def _rule_names(deny: list[DenyRule], gen: list[GeneraliseRule], path: str) -> bool:
    """True when any field rule covers ``path``."""
    for rule in (*deny, *gen):
        for name in rule.fields:
            target = _resolve_field(name)[0]
            if target is not None and _covers(target, path):
                return True
    return False


def _full_record_ok(record: EvidenceRecord, result: FilterResult, deny: list[DenyRule],
                    gen: list[GeneraliseRule], excluded: frozenset[str]) -> bool:
    """True when the exact signed body, with its issuer signature, may go out unchanged."""
    if not result.included or result.withheld or result.generalised:
        return False
    body = record.signed_body
    if (record.source_trust != SourceTrust.ISSUER_VERIFIED or record.issuer_signature is None
            or not isinstance(body, dict)):
        return False
    keys = _flat_keys(body)
    if keys is None or not keys <= RECORD_FIELDS | FULL_RECORD_META_FIELDS:
        return False
    # The signed body cannot be edited, so naming an excluded record rules it out.
    if excluded and _names_any(body, excluded):
        return False
    # A full record discloses every key of the signed body (meta ids included) and the
    # signature itself, so no field rule may name any of them, even one an extract ignores.
    disclosed = keys | {"issuer_signature", "signed_body"}
    return not any(_rule_names(deny, gen, path) for path in disclosed)


# --------------------------------------------------------------------------- compiled policy


@dataclass
class CompiledPolicy:
    """Validated rules ready to filter sources for a recipient. Build with ``compile_policy``."""

    policy_version: str
    rules: list[PolicyRule]

    def __post_init__(self) -> None:
        if not isinstance(self.policy_version, str) or not self.policy_version.strip():
            raise PolicyError("policy_version must be a non-empty string")
        self.rules = _normalise_rules(self.rules)

    def _scoped(self, kind: RecordKind | None) -> list[PolicyRule]:
        return [rule for rule in self.rules if _in_scope(rule, kind)]

    def recipient_allowed(self, audience: str,
                          kind: RecordKind | None = None) -> tuple[bool, str | None]:
        """``(allowed, blocking rule_id)``. With ``kind`` None only unscoped rules count."""
        audience = _check_audience(audience)
        for rule in self._scoped(_check_kind(kind)):
            if isinstance(rule, RecipientRule) and audience not in rule.allow:
                return False, rule.rule_id
        return True, None

    def filter_record(self, record: EvidenceRecord, *, audience: str,
                      now: datetime | None = None,
                      excluded_ids: Collection[str] = ()) -> FilterResult:
        """Filter one merchant record for ``audience`` at time ``now``.

        ``excluded_ids`` names records left out of the same package (by this policy or by
        the holder). References to them are withheld so a cross-reference cannot reveal the
        gap; pass them after a first filtering pass.
        """
        if not isinstance(record, EvidenceRecord):
            raise PolicyError("filter_record expects an EvidenceRecord")
        audience, now = _check_audience(audience), _resolve_now(now)
        excluded = _check_ids(excluded_ids)
        rules = self._scoped(record.kind)
        reason, rule_id = _exclusion(rules, label=f"{RecordKind(record.kind).value} records",
                                     audience=audience, moment=record.occurred_at, now=now)
        if reason is not None:
            return FilterResult(record.ref(), False, {}, False, reason, rule_id)
        deny, gen = _field_rules(rules)
        fields, withheld, generalised = _filter_data(
            record.to_json_dict(), allowlist=RECORD_FIELDS, silent=_SILENT_RECORD_KEYS,
            deny=deny, gen=gen)
        _withhold_excluded_refs(fields, withheld, excluded)
        result = FilterResult(record.ref(), True, fields, False, None, None, withheld, generalised)
        result.full_record_ok = _full_record_ok(record, result, deny, gen, excluded)
        return result

    def filter_txn(self, txn: BankTxn, *, audience: str,
                   now: datetime | None = None) -> FilterResult:
        """Filter one bank transaction. Only unscoped rules apply; never a full record."""
        if not isinstance(txn, BankTxn):
            raise PolicyError("filter_txn expects a BankTxn")
        audience, now = _check_audience(audience), _resolve_now(now)
        rules = self._scoped(None)
        reason, rule_id = _exclusion(rules, label="bank transactions", audience=audience,
                                     moment=txn.booked_at, now=now)
        if reason is not None:
            return FilterResult(txn.ref(), False, {}, False, reason, rule_id)
        deny, gen = _field_rules(rules)
        fields, withheld, generalised = _filter_data(
            txn.to_json_dict(), allowlist=TXN_FIELDS, silent=_SILENT_TXN_KEYS, deny=deny, gen=gen)
        return FilterResult(txn.ref(), True, fields, False, None, None, withheld, generalised)


def compile_policy(policy: Policy | list[PolicyRule] | None, *,
                   policy_version: str | None = None) -> CompiledPolicy:
    """Compile a stored policy, a bare rule list or None (allowlist only).

    The version defaults to ``policy.version_tag()``; for a bare list (or None) it is a
    digest of the rules, so it changes whenever they do. Unconfirmed policies are enforced
    too: every rule only restricts, so applying one is never less safe than ignoring it.
    """
    if policy is None:
        rules: list = []
        default_version = None
    elif isinstance(policy, Policy):
        rules, default_version = list(policy.rules), policy.version_tag()
    elif isinstance(policy, (list, tuple)):
        rules, default_version = list(policy), None
    else:
        raise PolicyError("compile_policy expects a Policy, a list of rules or None")
    rules = _normalise_rules(rules)
    if policy_version is None:
        policy_version = default_version or make_id("policy", [r.to_json_dict() for r in rules])
    return CompiledPolicy(policy_version=policy_version, rules=rules)


# --------------------------------------------------------------------------- rule text parser

_MAX_RULE_TEXT = 4000
_APOSTROPHES = str.maketrans({"’": "'", "‘": "'", "ʼ": "'", "`": "'"})

_NUMBER_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}
_UNIT_DAYS = {"day": 1, "week": 7, "month": 30, "year": 365}

_NUM = r"(?P<n>[0-9]{1,5}|" + "|".join(_NUMBER_WORDS) + r")"
_UNIT = r"(?P<unit>day|week|month|year)s?"
_NEG = r"(?:don'?t|do not|never)"
_VERB = r"(?:share|show|disclose|include|reveal|send|give out|pass on)"
_DET = r"(?:(?:my|the|any|all) )?"
_ANYONE = r"(?: with anyone| to anyone| with any recipient| with any bank| at all)?"
_OLDER = rf"(?:older than|from more than|dated more than) {_NUM} {_UNIT}(?: old| ago)?"
_NOUN = r"(?:(?:it|this|my data|my evidence|evidence|data|records|anything) )?"
_BANK = r"bank[ _-]?[ab]"
_BANKS = rf"(?P<banks>{_BANK}(?:(?:,| and| or|, and|, or| &) {_BANK})*)"
_ITEM_NAMES = r"(?:item|product) (?:names?|descriptions?)"
_DATES = r"(?:dates|times|timestamps|dates and times)"

_RECIPIENT_RES = [re.compile(p) for p in (
    rf"only (?:share |send |disclose |show )?{_NOUN}(?:with |to )?{_BANKS}",
    rf"(?:share|send|disclose|show) {_NOUN}(?:only|just) (?:with|to) {_BANKS}",
    rf"{_BANKS} only",
    rf"{_NEG} {_VERB} {_NOUN}(?:with|to) anyone (?:but|except|other than|apart from) {_BANKS}",
)]
_AGE_RES = [re.compile(p) for p in (
    rf"(?:(?:share|send|disclose|include|show) )?nothing {_OLDER}",
    rf"{_NEG} {_VERB} anything {_OLDER}{_ANYONE}",
    rf"only (?:share |send |include |show )?{_NOUN}(?:from )?(?:the )?(?:last|past) {_NUM} {_UNIT}",
)]
_ANYTHING_OLDER_RE = re.compile(rf"anything {_OLDER}")
_GEN_ITEM_RES = [re.compile(p) for p in (
    rf"(?:show|share|use|give) (?:(?:only|just) )?(?:the )?(?:item |product )?categor(?:y|ies) "
    rf"(?:instead of|rather than|in place of|not) {_DET}{_ITEM_NAMES}",
    rf"(?:replace|swap) {_DET}{_ITEM_NAMES} (?:with|for) (?:their |the |item )?categor(?:y|ies)",
)]
_GEN_DATE_RES = [re.compile(p) for p in (
    rf"(?:only show|show only|round|generalise|generalize|reduce|limit|coarsen) "
    rf"(?:the |my |all )?{_DATES} (?:to|by) (?:the )?(?P<level>day|month)",
    rf"(?:show|share) (?:the |my |all )?{_DATES} (?:only )?(?:to|by) (?:the )?(?P<level>day|month)"
    rf"(?: only)?",
)]
_DENY_RE = re.compile(rf"(?:{_NEG} {_VERB}|hide|withhold|exclude|remove) {_DET}(?P<obj>.+?){_ANYONE}")

_FIELD_PHRASES = [
    (re.compile(_ITEM_NAMES), "items.description", "Item names"),
    (re.compile(r"(?:item|product) (?:prices?|amounts?)|unit prices?"), "items.unit_amount_pence",
     "Item prices"),
    (re.compile(r"(?:item |product )?quantities"), "items.quantity", "Item quantities"),
    (re.compile(r"(?:item |product )?categories"), "items.category", "Item categories"),
    (re.compile(r"(?:item|product) details?|line items|items"), "items",
     "Item details (names, categories, quantities and prices)"),
]
_KIND_WORDS = {
    "refund": RecordKind.REFUND, "refunds": RecordKind.REFUND,
    "fee": RecordKind.FEE, "fees": RecordKind.FEE,
    "order": RecordKind.ORDER, "orders": RecordKind.ORDER,
    "sale": RecordKind.ORDER, "sales": RecordKind.ORDER,
    "payout": RecordKind.PAYOUT, "payouts": RecordKind.PAYOUT,
    "settlement": RecordKind.SETTLEMENT, "settlements": RecordKind.SETTLEMENT,
    "receipt": RecordKind.RECEIPT, "receipts": RecordKind.RECEIPT,
    "message": RecordKind.MESSAGE, "messages": RecordKind.MESSAGE,
}
_KIND_ALT = "|".join(sorted(_KIND_WORDS, key=len, reverse=True))
_KINDS_RE = re.compile(rf"(?:{_KIND_ALT})(?:(?:,| and| or|, and|, or) (?:{_KIND_ALT}))*(?: records?)?")
_KIND_WORD_RE = re.compile(rf"\b(?:{_KIND_ALT})\b")

# A text that opens by restricting is not told it reads as widening, unless the opening
# restricts nothing ("hide nothing") or lifts the rules themselves ("remove all limits").
_STARTS_RESTRICTIVE_RE = re.compile(
    rf"(?:{_NEG}|(?:hide|withhold|exclude|remove)(?! (?:nothing|none|(?:all |any |the |my )?"
    r"(?:rules?|restrictions?|limits?|polic(?:y|ies)))\b)"
    r"|keep\b.*\b(?:private|hidden|secret|confidential)"
    rf"|(?:{_VERB} )?nothing)\b")
_WIDENING_RE = re.compile(
    r"\b(?:ignore|override|overrule|disregard|bypass|forget|everything|everyone|everybody|"
    r"anyone|anybody|all (?:records|data|evidence|transactions)|no (?:limits?|restrictions?|rules?)|"
    r"unrestricted|unlimited)\b")
_OTHER_BANK_RE = re.compile(r"\bbank[ _-]?(?![ab]\b)[a-z0-9]+")

_Parsed = tuple[dict[str, Any], str]


def _normalise_text(text: str) -> str:
    t = re.sub(r"\s+", " ", text.translate(_APOSTROPHES).lower()).strip()
    t = t.strip(" .!?;:,\"'")
    t = re.sub(r"^please,? ", "", t)
    t = re.sub(r",? please$", "", t)
    return t.strip(" .!?;:,\"'")


def _join(words: list[str]) -> str:
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1]


def _age(match: re.Match) -> _Parsed | str:
    raw, unit = match.group("n"), match.group("unit")
    count = int(raw) if raw.isdigit() else _NUMBER_WORDS[raw]
    per = _UNIT_DAYS[unit]
    days = count * per
    if days < 1:
        return "An age limit must be at least 1 day."
    if days > 3650:
        return "An age limit can be at most 3650 days (about 10 years)."
    text = f"Nothing dated more than {days} days before the package is built will be shared."
    if per != 1 and count == 1:
        text += f" This counts 1 {unit} as {days} days."
    elif per != 1:
        text += f" This counts {count} {unit}s as {days} days ({per} days per {unit})."
    return {"type": "age", "max_age_days": days}, text


def _recipient(match: re.Match) -> _Parsed:
    letters = list(dict.fromkeys(re.findall(r"bank[ _-]?([ab])", match.group("banks"))))
    names = _join([f"Bank {letter.upper()}" for letter in letters])
    return ({"type": "recipient", "allow": [f"bank_{letter}" for letter in letters]},
            f"Only {names} may receive your evidence; any other recipient gets nothing.")


def _dates(level: str) -> _Parsed:
    data = {"type": "generalise", "fields": ["occurred_at", "effective_at", "booked_at"],
            "level": level}
    if level == "day":
        return data, "Dates will be shared to the day only (YYYY-MM-DD), without the time of day."
    return data, "Dates will be shared to the month only (YYYY-MM), without the day or time."


def _deny(obj: str) -> _Parsed | str | None:
    match = _ANYTHING_OLDER_RE.fullmatch(obj)
    if match:
        return _age(match)
    for regex, path, label in _FIELD_PHRASES:
        if regex.fullmatch(obj):
            return ({"type": "deny", "fields": [path]},
                    f"{label} will not be shared with any recipient.")
    if _KINDS_RE.fullmatch(obj):
        kinds = list(dict.fromkeys(_KIND_WORDS[word] for word in _KIND_WORD_RE.findall(obj)))
        names = _join([kind.value for kind in kinds])
        return ({"type": "deny", "kinds": [kind.value for kind in kinds]},
                f"{names[0].upper()}{names[1:]} records will not be shared with any recipient.")
    return None


def _match(text: str) -> _Parsed | str | None:
    """A typed rule and its interpretation; a reason string if matched but invalid; else None."""
    for regex in _RECIPIENT_RES:
        match = regex.fullmatch(text)
        if match:
            return _recipient(match)
    for regex in _AGE_RES:
        match = regex.fullmatch(text)
        if match:
            return _age(match)
    if any(regex.fullmatch(text) for regex in _GEN_ITEM_RES):
        return ({"type": "generalise", "fields": ["items.description"], "level": "category"},
                "Item names will be replaced by their categories; an item without a category "
                "will have its name withheld.")
    for regex in _GEN_DATE_RES:
        match = regex.fullmatch(text)
        if match:
            return _dates(match.group("level"))
    match = _DENY_RE.fullmatch(text)
    return _deny(match.group("obj")) if match else None


def _unresolved(reason: str) -> RuleParse:
    return RuleParse("unresolved", None, "No rule was created from this text.", reason)


def _unresolved_reason(text: str) -> str:
    if _WIDENING_RE.search(text) and not _STARTS_RESTRICTIVE_RE.match(text):
        return ("Policy rules can only restrict sharing. This reads as a request to widen "
                "sharing or override rules, so no rule was created.")
    if _OTHER_BANK_RE.search(text):
        return "Alibi knows two recipients, Bank A and Bank B."
    return ("This is not a supported rule yet. Try \"don't share item names\", "
            "\"nothing older than 90 days\" or \"only share with Bank A\".")


def parse_rule_text(text: str, *, rule_id: str | None = None) -> RuleParse:
    """Turn one plain-English rule into a typed rule, or say why it is unresolved.

    Only whole-sentence matches resolve, so a supported phrase wrapped in other instructions
    stays unresolved. ``rule_id`` defaults to a deterministic id from the normalised text and
    the parsed rule. The holder confirms ``interpretation`` before the rule is used.
    """
    if not isinstance(text, str) or not text.strip():
        return _unresolved("The rule is empty.")
    source = text.strip()
    if len(source) > _MAX_RULE_TEXT:
        return _unresolved(f"The rule is longer than {_MAX_RULE_TEXT} characters.")
    normalised = _normalise_text(source)
    if not normalised:
        return _unresolved("The rule is empty.")
    parsed = _match(normalised)
    if parsed is None:
        return _unresolved(_unresolved_reason(normalised))
    if isinstance(parsed, str):
        return _unresolved(parsed)
    data, interpretation = parsed
    rid = rule_id if rule_id is not None else make_id("rule", normalised, data)
    try:
        rule = _RULE_ADAPTER.validate_python({**data, "rule_id": rid, "source_text": source})
    except ValidationError as exc:
        raise PolicyError(f"could not build the rule ({_validation_summary(exc)})") from exc
    return RuleParse("resolved", rule, interpretation, None)
