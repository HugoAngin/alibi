"""Deny-first package preview and exact holder approval, an eval reference for Alibi sharing.

    from agents.eval.sharing_privacy import (
        Policy, TrustedState, build_preview, HolderAuthenticator, ApprovalStore)

    preview = build_preview(state, "rfc-0001-01", proposed_claims, now=now)
    preview.body            # public body: exactly PUBLIC_BODY_FIELDS, allowlisted content only
    preview.private_review  # holder-side only: included, excluded, generalised, claim changes
    record = store.approve(state, preview, token=tok, recipient="bank_a",
                           confirmed_digest=preview.body_digest, now=now)
    store.validate(record.approval_id, body_to_send, state, now=later)   # raises ApprovalError

Rules (docs/SPEC.md "Consent and signing", "Threat controls"):
- Nothing reaches the body unless an allowlist names it. Unlisted claim keys, evidence-ref
  metadata and record fields are dropped. Policy can only narrow the allowlist.
- Deny wins. Unresolved policy rules hold the preview. A generalisation the code cannot
  perform withholds the field instead.
- Private exclusions, reasons and counts live in `private_review`, never in the body.
- A claim that cites withheld evidence is downgraded to `unresolved` with a gap that names
  neither the category nor the merchant, and its narrative and calculation are dropped.
- Narrative that mentions any withheld identifier, value, amount or denied term is replaced.
  A final scan of every string in the body fails closed if anything slips through.
- Approval is stored server side against the SHA-256 of the RFC 8785 (JCS) body, recipient,
  request ID, policy version and content fingerprint, relevant source revisions, the
  authenticated holder and an expiry. A preview filtered under another policy cannot be
  approved. Approval and validation re-derive every attachment from trusted current state and
  require exact values, not just allowed field names.
- Denied structural metadata keeps the wire keys: `issuer_id` is exported as null, unless the
  record is `issuer_verified` (an unnamed verified issuer cannot be checked), in which case the
  record is withheld. A denied ref, revision, kind or trust withholds the record.

Synthetic data only. This module signs nothing and sends nothing.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable, Mapping

import rfc8785

from agents.eval.settlement import match_credit

PACKAGE_SCHEMA_VERSION = "1"
PUBLIC_BODY_FIELDS = ("schema_version", "package_id", "request_id", "audience", "case_id",
                      "scope", "issued_at", "expires_at", "claims", "attachments")
SCOPE_FIELDS = ("txn_ids", "period", "question_ids")
CLAIM_FIELDS = ("question_id", "status", "rationale", "evidence_refs", "calculation", "gap")
ATTACHMENT_FIELDS = ("ref", "revision", "kind", "issuer_id", "trust", "presentation", "fields")
CLAIM_STATUSES = ("supported", "contradicted", "unresolved")
TRUST_LEVELS = ("issuer_verified", "bank_observed", "self_supplied")
# The only record fields that may ever be disclosed. Everything else is unclassified: withheld.
DISCLOSABLE_FIELDS = ("occurred_at", "booked_at", "amount_minor", "currency", "status",
                      "destination")
SUPPORTED_GENERALISATIONS = {("occurred_at", "day"), ("booked_at", "day")}
# Attachment metadata taken from the record. A denied redactable one is exported as null; a
# denied required one cannot be hidden without breaking binding or trust display, so the
# whole record is withheld. Wire keys never change.
REQUIRED_METADATA = ("record_id", "txn_id", "ref", "revision", "kind", "trust")
BANK_KIND = "bank_txn"
MAX_NARRATIVE = 500
DEFAULT_APPROVAL_TTL = timedelta(minutes=15)
DEFAULT_PACKAGE_TTL = timedelta(days=7)

WITHHELD_GAP = "Some supporting evidence is withheld at the account holder's request."
WITHHELD_RATIONALE = "Part of the evidence for this answer is withheld, so it is not shown."
REPLACED_RATIONALE = "Explanation removed by disclosure policy; see the cited evidence."
REPLACED_GAP = "Evidence needed for this answer is not available in this package."
UNANSWERED_RATIONALE = "No shareable answer was prepared for this question."
UNANSWERED_GAP = "No shareable evidence answers this question."
CALC_GAP = "The calculation could not be verified from the shared evidence."
_CODE_TEXTS = {t.casefold() for t in (WITHHELD_GAP, WITHHELD_RATIONALE, REPLACED_RATIONALE,
                                      REPLACED_GAP, UNANSWERED_RATIONALE, UNANSWERED_GAP,
                                      CALC_GAP)}

# Field values that are enumerations, not identifying content, so they never join the lexicon.
_ENUM_FIELDS = {"kind", "currency", "trust", "schema_version", "status", "destination"}
_WORD = re.compile(r"[a-z]{4,}")
# Generic words that never identify a merchant or category on their own.
_STOPWORDS = {"from", "with", "that", "this", "into", "over", "your", "their", "than", "then",
              "have", "been", "were", "what", "when", "where", "which", "about", "after",
              "before", "order", "payment", "transfer", "account", "bank", "card", "credit",
              "debit", "ltd", "limited"}
_ID_PATTERN = re.compile(r"(?<![0-9a-z])(?:rec|txn|evt|acct|rfc|pkg)-[0-9a-z][0-9a-z_-]*")


class SharingError(Exception):
    """Base error. `code` is a stable reason string for logs and tests."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


class PolicyHold(SharingError):
    """The policy cannot be applied yet: an unresolved rule or a recipient restriction."""


class DisclosureError(SharingError):
    """A filtered body still contains withheld material. Fail closed."""


class ApprovalError(SharingError):
    """Approval is missing, stale, mismatched or expired."""


@dataclass(frozen=True)
class Policy:
    """Typed holder policy. Every field narrows disclosure; none can widen the allowlist."""

    version: str
    deny_record_ids: frozenset[str] = frozenset()
    deny_kinds: frozenset[str] = frozenset()
    deny_issuers: frozenset[str] = frozenset()
    deny_fields: frozenset[str] = frozenset()
    deny_terms: tuple[str, ...] = ()            # any record containing one is withheld whole
    generalise: Mapping[str, str] = field(default_factory=dict)   # field -> "day"
    allowed_recipients: frozenset[str] | None = None
    unresolved_rules: tuple[str, ...] = ()      # plain-English rules awaiting clarification


@dataclass
class TrustedState:
    """Backend-owned current state. Approval validation reads only from here."""

    records: dict[str, dict]       # record_id -> active record (bank rows keyed by txn_id)
    requests: dict[str, dict]      # request_id -> authenticated RFC
    policy: Policy
    holders: dict[str, str]        # subject_ref -> authenticated holder id


@dataclass(frozen=True)
class Preview:
    body: dict
    body_digest: str
    private_review: dict
    source_revisions: tuple[tuple[str, int], ...]
    policy_version: str = ""          # policy the body was filtered under; approve rejects stale
    policy_fingerprint: str = ""      # content hash of that policy, so a reused label is caught


@dataclass(frozen=True)
class ApprovalRecord:
    approval_id: str
    holder_id: str
    body_digest: str
    recipient: str
    request_id: str
    policy_version: str
    source_revisions: tuple[tuple[str, int], ...]
    approved_at: str
    expires_at: str


# ---------------------------------------------------------------- canonical form and time


def canonical_bytes(obj) -> bytes:
    """RFC 8785 JSON Canonicalization Scheme bytes."""
    return rfc8785.dumps(obj)


def body_digest(body: dict) -> str:
    """Lowercase hex SHA-256 of the JCS body."""
    return hashlib.sha256(canonical_bytes(body)).hexdigest()


def policy_fingerprint(policy: Policy) -> str:
    """SHA-256 of the JCS form of every policy field, version included."""
    doc = {"version": policy.version,
           "deny_record_ids": sorted(policy.deny_record_ids),
           "deny_kinds": sorted(policy.deny_kinds), "deny_issuers": sorted(policy.deny_issuers),
           "deny_fields": sorted(policy.deny_fields), "deny_terms": list(policy.deny_terms),
           "generalise": {k: policy.generalise[k] for k in sorted(policy.generalise)},
           "allowed_recipients": None if policy.allowed_recipients is None
           else sorted(policy.allowed_recipients),
           "unresolved_rules": list(policy.unresolved_rules)}
    return hashlib.sha256(canonical_bytes(doc)).hexdigest()


def parse_time(value: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError(f"expected UTC ISO 8601 with Z, got {value!r}")
    return datetime.fromisoformat(value[:-1] + "+00:00")


def format_time(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        raise ValueError("now must be timezone-aware UTC")
    return moment.astimezone(timezone.utc)


# ---------------------------------------------------------------- record classification


def _strings(value) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _strings(v)


def _record_time(record: dict) -> str | None:
    return record.get("occurred_at") or record.get("booked_at")


def _withhold_reason(record: dict | None, ref: str, rfc: dict, policy: Policy,
                     removals: frozenset[str]) -> str | None:
    """Why a cited record cannot be attached, or None if it may be. Deny-first order."""
    if record is None:
        return "unknown_record"
    if ref in removals:
        return "removed_by_holder"
    if (not isinstance(record.get("revision"), int) or record.get("trust") not in TRUST_LEVELS
            or not isinstance(record.get("kind"), str) or "subject_ref" not in record):
        return "unclassifiable_record"
    if record["subject_ref"] != rfc["subject_ref"]:
        return "foreign_subject"
    if ref in policy.deny_record_ids:
        return "policy_deny_record"
    if record["kind"] in policy.deny_kinds:
        return "policy_deny_kind"
    if record.get("issuer_id") in policy.deny_issuers:
        return "policy_deny_issuer"
    if any(_field_denied(n, policy) for n in REQUIRED_METADATA):
        return "policy_deny_structural"
    if (_field_denied("issuer_id", policy) and record["trust"] == "issuer_verified"
            and record.get("issuer_id") is not None):
        return "policy_deny_structural"   # verified trust with its issuer hidden is uncheckable
    folded = [s.casefold() for s in _strings(record)]
    if any(t.casefold() in s for t in policy.deny_terms for s in folded):
        return "policy_deny_term"
    period = rfc.get("period")
    if period:
        moment = _record_time(record)
        if moment is None or not (period["from"] <= moment < period["to"]):
            return "outside_period"
    elif record["kind"] == BANK_KIND and ref not in (rfc.get("txn_ids") or []):
        return "outside_scope"
    return None


def _field_denied(name: str, policy: Policy) -> bool:
    """Denied outright, or a generalisation the code cannot perform (so withheld)."""
    how = policy.generalise.get(name)
    return name in policy.deny_fields or (
        how is not None and (name, how) not in SUPPORTED_GENERALISATIONS)


def _disclose_fields(record: dict, policy: Policy) -> tuple[dict, list, list]:
    """Allowlisted, policy-narrowed fields. Returns (fields, generalised, withheld_names)."""
    fields, generalised, withheld = {}, [], []
    for name, value in record.items():
        if name not in DISCLOSABLE_FIELDS or name in policy.deny_fields:
            withheld.append(name)
            continue
        how = policy.generalise.get(name)
        if how is None:
            fields[name] = value
        elif (name, how) in SUPPORTED_GENERALISATIONS and isinstance(value, str):
            fields[name] = value[:10]
            generalised.append({"field": name, "to": how})
        else:
            withheld.append(name)   # restriction cannot be evaluated: withhold
    return fields, generalised, withheld


def _attachment(ref: str, record: dict, policy: Policy) -> tuple[dict, list, list]:
    """Public attachment for an admitted record. Returns (attachment, generalised, hidden)."""
    fields, generalised, withheld = _disclose_fields(record, policy)
    issuer = None if _field_denied("issuer_id", policy) else record.get("issuer_id")
    hidden = [n for n in withheld if n not in REQUIRED_METADATA
              and not (n == "issuer_id" and issuer is not None)]
    full = not generalised and not hidden
    return ({"ref": ref, "revision": record["revision"], "kind": record["kind"],
             "issuer_id": issuer, "trust": record["trust"],
             "presentation": "full_record" if full else "redacted_extract",
             "fields": fields}, generalised, hidden)


# ---------------------------------------------------------------- leak lexicon


def _amount_tokens(n: int) -> set[str]:
    pounds, pence = divmod(abs(n), 100)
    tokens = {str(abs(n)), f"{pounds}.{pence:02d}", f"{pounds:,}.{pence:02d}"}
    if pence == 0:
        tokens |= {str(pounds), f"{pounds:,}"}
    return {t for t in tokens if len(t) >= 3}


def _value_tokens(name: str, value) -> set[str]:
    if name in _ENUM_FIELDS:
        return set()
    if isinstance(value, bool):
        return set()
    if isinstance(value, int):
        return _amount_tokens(value) if name.endswith("_minor") else set()
    out: set[str] = set()
    for text in _strings(value):
        folded = text.casefold()
        if len(folded) >= 3:
            out.add(folded)
        if any(c.isspace() for c in folded):    # phrases also leak word by word
            out |= {w for w in _WORD.findall(folded) if w not in _STOPWORDS}
    return out


def _body_tokens(body: dict) -> set[str]:
    """Every value the structured part of the body discloses, as lexicon tokens."""
    out: set[str] = set()

    def walk(value, name=""):
        if isinstance(value, Mapping):
            for k, v in value.items():
                if k not in ("rationale", "gap"):
                    walk(v, k)
        elif isinstance(value, list):
            for v in value:
                walk(v, name)
        elif isinstance(value, str):
            out.add(value.casefold())
            out.update(_WORD.findall(value.casefold()))
        elif isinstance(value, int) and not isinstance(value, bool) and name.endswith("_minor"):
            out.update(_amount_tokens(value))

    walk(body)
    return out


def _lexicon(state: TrustedState, attached: dict[str, dict], extra_ids: Iterable[str],
             disclosed: set[str]) -> set[str]:
    """Withheld tokens: every non-attached record whole, withheld fields of attached ones."""
    withheld: set[str] = {i.casefold() for i in extra_ids if len(i) >= 3}
    for ref, record in state.records.items():
        att = attached.get(ref)
        if att is None:
            withheld.add(ref.casefold())
        coarse = {g["field"] for g in (att or {}).get("_generalised", [])}
        for name, value in record.items():
            if att is not None and (name in ("record_id", "txn_id")
                                    or (name == "issuer_id" and att["issuer_id"] is not None)
                                    or (name in att["fields"] and name not in coarse)):
                continue        # disclosed as is
            withheld |= _value_tokens(name, value)
    withheld -= disclosed
    withheld |= {t.casefold() for t in state.policy.deny_terms if t}
    return withheld


def _compile(lexicon: set[str]):
    if not lexicon:
        return None
    alts = "|".join(re.escape(t) for t in sorted(lexicon, key=len, reverse=True))
    return re.compile(rf"(?<![0-9a-z])(?:{alts})(?![0-9a-z])")


def _narrative_leaks(text: str, pattern, disclosed: set[str]) -> bool:
    folded = text.casefold()
    if pattern is not None and pattern.search(folded):
        return True
    return any(m.group(0) not in disclosed for m in _ID_PATTERN.finditer(folded))


# ---------------------------------------------------------------- preview


def _case_id(state: TrustedState, rfc: dict) -> str:
    if rfc.get("case_id"):
        return str(rfc["case_id"])
    seen, current = set(), rfc
    while current.get("parent_request_id") and current["parent_request_id"] not in seen:
        seen.add(current["request_id"])
        parent = state.requests.get(current["parent_request_id"])
        if parent is None:
            break
        current = parent
    return current["request_id"]


def scope_of(rfc: dict) -> dict:
    period = rfc.get("period")
    txn_ids = rfc.get("txn_ids", [])
    if not isinstance(txn_ids, list) or bool(txn_ids) == (period is not None):
        raise ApprovalError("invalid_scope", "request needs exactly one of period or txn_ids")
    if not all(isinstance(txn_id, str) and txn_id for txn_id in txn_ids) or len(set(txn_ids)) != len(txn_ids):
        raise ApprovalError("invalid_scope", "transaction IDs must be non-empty and distinct")
    if period is not None:
        try:
            if (not isinstance(period, dict) or set(period) != {"from", "to"}
                    or parse_time(period["from"]) >= parse_time(period["to"])):
                raise ValueError
        except (TypeError, ValueError):
            raise ApprovalError("invalid_scope", "request period must be a half-open UTC window") from None
    return {"txn_ids": sorted(txn_ids), "period": dict(period) if period is not None else None,
            "question_ids": list(rfc["question_ids"])}


def _load_request(state: TrustedState, request_id: str, now: datetime) -> dict:
    rfc = state.requests.get(request_id)
    if rfc is None:
        raise ApprovalError("unknown_request", request_id)
    if now >= parse_time(rfc["expires_at"]):
        raise ApprovalError("request_expired", request_id)
    return rfc


def _cited(claim: dict) -> list[str]:
    refs = [r.get("ref") for r in claim.get("evidence_refs") or [] if isinstance(r, Mapping)]
    calc = claim.get("calculation")
    if isinstance(calc, Mapping):
        refs += [t.get("ref") for t in calc.get("terms") or [] if isinstance(t, Mapping)]
    return [r for r in refs if isinstance(r, str)]


def _supported_source(state: TrustedState, rfc: dict, refs: Mapping) -> bool:
    """A cited credit needs the uniquely matching, already-settled payout."""
    credits = {ref for ref in refs if state.records[ref].get("kind") == BANK_KIND
               and state.records[ref].get("trust") == "bank_observed"}
    if not credits or (rfc.get("txn_ids") and credits != set(rfc["txn_ids"])):
        return False
    payouts = {
        ref: {**record, "source_kind": "import" if record["trust"] == "self_supplied" else "merchant"}
        for ref, record in state.records.items()
        if record.get("kind") == "payout" and record.get("subject_ref") == rfc["subject_ref"]
        and record.get("trust") in {"issuer_verified", "self_supplied"}
    }
    for ref in credits:
        try:
            credit = state.records[ref]
            if type(credit["amount_minor"]) is not int or credit["amount_minor"] <= 0:
                return False
            link = match_credit(credit, payouts)
        except (KeyError, TypeError, ValueError):
            return False
        if link["state"] != "linked" or not set(link["payout_record_ids"]) <= refs.keys():
            return False
    return True


def _check_calculation(calc, kept: dict[str, dict], attachments: dict[str, dict]) -> dict | None:
    """Recompute in code. Returns the sanitised calculation or None if it cannot stand."""
    if not isinstance(calc, Mapping) or not isinstance(calc.get("terms"), list):
        return None
    currency, terms, total = calc.get("currency"), [], 0
    for term in calc["terms"]:
        if not isinstance(term, Mapping):
            return None
        ref, amount = term.get("ref"), term.get("amount_minor")
        if ref not in kept or isinstance(amount, bool) or not isinstance(amount, int):
            return None
        disclosed = attachments[ref]["fields"]
        if (disclosed.get("amount_minor") is None or abs(disclosed["amount_minor"]) != abs(amount)
                or disclosed.get("currency") != currency):
            return None
        terms.append({"ref": ref, "amount_minor": amount})
        total += amount
    if not terms or calc.get("total_minor") != total:
        return None
    return {"terms": terms, "total_minor": total, "currency": currency}


def build_preview(state: TrustedState, request_id: str, proposed_claims: list, *,
                  now: datetime, holder_removals: Iterable[str] = (),
                  package_ttl: timedelta = DEFAULT_PACKAGE_TTL) -> Preview:
    """Filter Curator proposals into a public body plus a private holder review."""
    now = _utc(now)
    policy = state.policy
    if policy.unresolved_rules:
        raise PolicyHold("unresolved_rules", "; ".join(policy.unresolved_rules))
    rfc = _load_request(state, request_id, now)
    scope = scope_of(rfc)
    if policy.allowed_recipients is not None and rfc["audience"] not in policy.allowed_recipients:
        raise PolicyHold("recipient_not_allowed", rfc["audience"])
    if rfc["subject_ref"] not in state.holders:
        raise PolicyHold("no_holder", rfc["subject_ref"])
    removals = frozenset(holder_removals)
    questions = list(rfc["question_ids"])

    excluded: dict[str, dict] = {}
    attachments: dict[str, dict] = {}
    generalised, withheld_fields, claim_changes = [], [], []
    relevant: set[str] = {t for t in rfc.get("txn_ids") or [] if t in state.records}

    def admit(ref: str) -> bool:
        if ref in attachments:
            return True
        if ref in excluded:
            return False
        record = state.records.get(ref)
        reason = _withhold_reason(record, ref, rfc, policy, removals)
        if record is not None:
            relevant.add(ref)
        if reason:
            excluded[ref] = {"ref": ref, "revision": (record or {}).get("revision"),
                             "reason": reason}
            return False
        att, gen, held = _attachment(ref, record, policy)
        attachments[ref] = att
        generalised.extend({"ref": ref, **g} for g in gen)
        withheld_fields.extend({"ref": ref, "field": n, "reason": "policy"
                                if n in policy.deny_fields or n in policy.generalise
                                else "not_allowlisted"} for n in held)
        return True

    # Pass 1: structure. Decide claims, refs, statuses and calculations.
    drafts: dict[str, dict] = {}
    for claim in proposed_claims:
        if not isinstance(claim, Mapping):
            claim_changes.append({"question_id": None, "change": "dropped", "reason": "not_object"})
            continue
        qid = claim.get("question_id")
        if qid not in questions:
            claim_changes.append({"question_id": qid, "change": "dropped",
                                  "reason": "out_of_scope_question"})
            continue
        if qid in drafts:
            claim_changes.append({"question_id": qid, "change": "dropped",
                                  "reason": "duplicate_question"})
            continue
        status = claim.get("status")
        if status not in CLAIM_STATUSES:
            claim_changes.append({"question_id": qid, "change": "dropped",
                                  "reason": "invalid_status"})
            continue
        for key in claim:
            if key not in CLAIM_FIELDS:
                claim_changes.append({"question_id": qid, "change": "field_dropped",
                                      "reason": "not_allowlisted", "field": key})
        kept, lost = {}, False
        for ref_obj in claim.get("evidence_refs") or []:
            ref = ref_obj.get("ref") if isinstance(ref_obj, Mapping) else None
            if not isinstance(ref, str):
                lost = True
                continue
            if not admit(ref):
                lost = True
                continue
            if ref_obj.get("revision") != state.records[ref]["revision"]:
                claim_changes.append({"question_id": qid, "change": "ref_dropped",
                                      "reason": "stale_revision", "ref": ref})
                lost = True
                continue
            kept[ref] = state.records[ref]
        for ref in _cited(claim):          # calculation terms count as citations
            if ref not in kept and not admit(ref):
                lost = True
        draft = {"question_id": qid, "status": status, "rationale": claim.get("rationale"),
                 "evidence_refs": [{"ref": r, "revision": kept[r]["revision"]} for r in kept],
                 "calculation": None, "gap": claim.get("gap")}
        if lost:
            if status != "unresolved":
                claim_changes.append({"question_id": qid, "change": f"{status}->unresolved",
                                      "reason": "cites_withheld_evidence"})
            draft.update(status="unresolved", rationale=WITHHELD_RATIONALE, gap=WITHHELD_GAP)
        elif claim.get("calculation") is not None:
            calc = _check_calculation(claim["calculation"], kept, attachments)
            if calc is None:
                claim_changes.append({"question_id": qid, "change": "calculation_dropped",
                                      "reason": "not_verifiable"})
                if status == "supported":
                    draft.update(status="unresolved", gap=CALC_GAP)
            draft["calculation"] = calc
        if draft["status"] in ("supported", "contradicted") and not kept:
            claim_changes.append({"question_id": qid, "change": f"{draft['status']}->unresolved",
                                  "reason": "no_shareable_evidence"})
            draft.update(status="unresolved", gap=UNANSWERED_GAP)
        elif (qid == "q.source_of_funds" and draft["status"] == "supported"
              and not _supported_source(state, rfc, kept)):
            claim_changes.append({"question_id": qid, "change": "supported->unresolved",
                                  "reason": "settlement_not_supported"})
            draft.update(status="unresolved", rationale=UNANSWERED_RATIONALE, gap=UNANSWERED_GAP,
                         evidence_refs=[], calculation=None)
        drafts[qid] = draft

    # Attach only evidence a surviving claim still cites: minimum disclosure.
    used = {r["ref"] for d in drafts.values() for r in d["evidence_refs"]}
    for ref in list(attachments):
        if ref not in used:
            excluded[ref] = {"ref": ref, "revision": attachments.pop(ref)["revision"],
                             "reason": "not_cited"}
    generalised = [g for g in generalised if g["ref"] in attachments]
    withheld_fields = [w for w in withheld_fields if w["ref"] in attachments]
    for g in generalised:
        attachments[g["ref"]].setdefault("_generalised", []).append(g)

    claims = []
    for qid in questions:
        claims.append(drafts.get(qid) or {
            "question_id": qid, "status": "unresolved", "rationale": UNANSWERED_RATIONALE,
            "evidence_refs": [], "calculation": None, "gap": UNANSWERED_GAP})

    issued = format_time(now)
    expires = format_time(min(now + package_ttl, parse_time(rfc["expires_at"])))
    public_attachments = [{k: v for k, v in attachments[r].items() if k != "_generalised"}
                          for r in sorted(attachments)]
    body = {"schema_version": PACKAGE_SCHEMA_VERSION, "package_id": "", "request_id": request_id,
            "audience": rfc["audience"], "case_id": _case_id(state, rfc), "scope": scope,
            "issued_at": issued, "expires_at": expires, "claims": claims,
            "attachments": public_attachments}

    # Pass 2: narrative. Replace any free text that touches withheld material.
    disclosed = _body_tokens(body)
    pattern = _compile(_lexicon(state, attachments, list(excluded) + list(removals), disclosed))
    for claim in claims:
        for key, fallback in (("rationale", REPLACED_RATIONALE), ("gap", REPLACED_GAP)):
            text = claim[key]
            if text is None and key == "gap":
                continue
            if (not isinstance(text, str) or len(text) > MAX_NARRATIVE
                    or _narrative_leaks(text, pattern, disclosed)):
                claim[key] = fallback
                claim_changes.append({"question_id": claim["question_id"],
                                      "change": f"{key}_replaced", "reason": "withheld_material"})

    stem = {k: v for k, v in body.items() if k != "package_id"}
    body["package_id"] = "pkg-" + hashlib.sha256(canonical_bytes(stem)).hexdigest()[:20]
    validate_public_body(body)
    _final_scan(body, pattern, disclosed | {body["package_id"].casefold()}, policy)

    revisions = tuple(sorted((r, state.records[r]["revision"]) for r in relevant))
    review = {"request_id": request_id, "audience": rfc["audience"],
              "included": [{"ref": a["ref"], "revision": a["revision"], "reason": "cited_by_claim"}
                           for a in public_attachments],
              "excluded": sorted(excluded.values(), key=lambda e: e["ref"]),
              "generalised": generalised, "withheld_fields": withheld_fields,
              "claim_changes": claim_changes,
              "counts": {"included": len(public_attachments), "excluded": len(excluded),
                         "generalised": len(generalised)}}
    return Preview(body=body, body_digest=body_digest(body), private_review=review,
                   source_revisions=revisions, policy_version=policy.version,
                   policy_fingerprint=policy_fingerprint(policy))


def validate_public_body(body: dict) -> None:
    """Exact public shape. Raises DisclosureError on any unlisted key or field."""
    def need(cond: bool, what: str):
        if not cond:
            raise DisclosureError("schema", what)

    need(isinstance(body, dict) and set(body) == set(PUBLIC_BODY_FIELDS), "body keys")
    need(isinstance(body["scope"], dict) and set(body["scope"]) == set(SCOPE_FIELDS), "scope")
    for claim in body["claims"]:
        need(isinstance(claim, dict) and set(claim) == set(CLAIM_FIELDS), "claim keys")
        need(claim["status"] in CLAIM_STATUSES, "claim status")
        need(all(set(r) == {"ref", "revision"} for r in claim["evidence_refs"]), "evidence_refs")
        calc = claim["calculation"]
        need(calc is None or (set(calc) == {"terms", "total_minor", "currency"} and all(
            set(t) == {"ref", "amount_minor"} for t in calc["terms"])), "calculation")
    for att in body["attachments"]:
        need(isinstance(att, dict) and set(att) == set(ATTACHMENT_FIELDS), "attachment keys")
        need(att["trust"] in TRUST_LEVELS, "attachment trust")
        need(set(att["fields"]) <= set(DISCLOSABLE_FIELDS), "attachment fields")
    canonical_bytes(body)   # rejects values JCS cannot represent


def _final_scan(body: dict, pattern, disclosed: set[str], policy: Policy) -> None:
    deny = [t.casefold() for t in policy.deny_terms if t]
    for text in _strings(body):
        folded = text.casefold()
        if any(t in folded for t in deny):
            raise DisclosureError("denied_term_in_body")
        if folded in disclosed or folded in _CODE_TEXTS:
            continue
        if pattern is not None and pattern.search(folded):
            raise DisclosureError("withheld_material_in_body")
        if any(m.group(0) not in disclosed for m in _ID_PATTERN.finditer(folded)):
            raise DisclosureError("undisclosed_identifier_in_body")


# ---------------------------------------------------------------- holder authentication


class HolderAuthenticator:
    """HMAC session tokens for the demo holder login. The key never leaves the backend."""

    def __init__(self, key: bytes):
        if len(key) < 16:
            raise ValueError("holder session key must be at least 16 bytes")
        self._key = key

    def _mac(self, payload: bytes) -> str:
        return hmac.new(self._key, b"alibi.holder.v1|" + payload, hashlib.sha256).hexdigest()

    def issue(self, holder_id: str, *, now: datetime, ttl: timedelta = timedelta(hours=1)) -> str:
        payload = json.dumps({"sub": holder_id, "exp": format_time(_utc(now) + ttl)},
                             sort_keys=True, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(payload).decode() + "." + self._mac(payload)

    def verify(self, token: str, *, now: datetime) -> str:
        try:
            encoded, mac = token.split(".", 1)
            payload = base64.urlsafe_b64decode(encoded.encode())
        except (ValueError, AttributeError) as exc:
            raise ApprovalError("holder_unauthenticated", "malformed token") from exc
        if not hmac.compare_digest(mac, self._mac(payload)):
            raise ApprovalError("holder_unauthenticated", "bad signature")
        try:
            claims = json.loads(payload)
            holder, expiry = claims["sub"], parse_time(claims["exp"])
        except (ValueError, KeyError, TypeError) as exc:
            raise ApprovalError("holder_unauthenticated", "malformed token") from exc
        if not isinstance(holder, str) or _utc(now) >= expiry:
            raise ApprovalError("holder_unauthenticated", "session expired")
        return holder


# ---------------------------------------------------------------- approval


def _require(cond: bool, code: str, detail: str = "") -> None:
    if not cond:
        raise ApprovalError(code, detail)


def _current_revisions(state: TrustedState, revisions) -> None:
    for ref, revision in revisions:
        record = state.records.get(ref)
        _require(record is not None and record.get("revision") == revision, "source_changed", ref)


def _check_against_state(body: dict, state: TrustedState, rfc: dict,
                         bound: dict[str, int]) -> None:
    """The body must be exactly what current trusted state and policy would let through."""
    policy = state.policy
    _require(not policy.unresolved_rules, "policy_changed", "unresolved rules")
    _require(policy.allowed_recipients is None or rfc["audience"] in policy.allowed_recipients,
             "policy_changed", "recipient not allowed")
    _require(body["scope"] == scope_of(rfc), "scope_mismatch")
    _require(body["case_id"] == _case_id(state, rfc), "scope_mismatch", "case_id")
    _require([c["question_id"] for c in body["claims"]] == list(rfc["question_ids"]),
             "scope_mismatch", "questions")
    attached: dict[str, dict] = {}
    for att in body["attachments"]:
        ref = att["ref"]
        _require(ref not in attached and bound.get(ref) == att["revision"], "source_changed", ref)
        current = state.records.get(ref)
        _require(current is not None and current.get("revision") == att["revision"],
                 "source_changed", ref)
        reason = _withhold_reason(current, ref, rfc, policy, frozenset())
        _require(reason is None, "scope_mismatch", f"{ref}: {reason}")
        expected, generalised, _ = _attachment(ref, current, policy)
        _require(att == expected, "disclosure_mismatch", ref)       # exact values, not names
        attached[ref] = {**expected, "_generalised": generalised}
    for claim in body["claims"]:
        refs = {r["ref"]: r["revision"] for r in claim["evidence_refs"]}
        _require(all(attached.get(r, {}).get("revision") == v for r, v in refs.items()),
                 "disclosure_mismatch", "evidence_refs")
        if claim["question_id"] == "q.source_of_funds" and claim["status"] == "supported":
            _require(_supported_source(state, rfc, refs), "disclosure_mismatch", "settlement_not_supported")
        if claim["calculation"] is not None:
            kept = {r: state.records[r] for r in refs}
            _require(_check_calculation(claim["calculation"], kept, attached)
                     == claim["calculation"], "disclosure_mismatch", "calculation")
    disclosed = _body_tokens(body) | {body["package_id"].casefold()}
    try:
        _final_scan(body, _compile(_lexicon(state, attached, (), disclosed)), disclosed, policy)
    except DisclosureError as exc:
        raise ApprovalError("disclosure_mismatch", exc.code) from exc


class ApprovalStore:
    """Server-side approvals. Callers hold an approval ID, never a reusable credential."""

    def __init__(self, authenticator: HolderAuthenticator):
        self._auth = authenticator
        self._records: dict[str, ApprovalRecord] = {}
        self._policy_fps: dict[str, str] = {}
        self._lock = threading.Lock()

    def approve(self, state: TrustedState, preview: Preview, *, token: str, recipient: str,
                confirmed_digest: str, now: datetime,
                ttl: timedelta = DEFAULT_APPROVAL_TTL) -> ApprovalRecord:
        """Record the holder's decision on this exact preview for this named recipient."""
        now = _utc(now)
        holder = self._auth.verify(token, now=now)
        body = preview.body
        with self._lock:
            validate_public_body(body)
            digest = body_digest(body)
            _require(hmac.compare_digest(preview.body_digest, digest)
                     and hmac.compare_digest(confirmed_digest, digest), "digest_mismatch")
            fingerprint = policy_fingerprint(state.policy)
            _require(preview.policy_version == state.policy.version
                     and preview.policy_fingerprint == fingerprint, "policy_changed",
                     "preview was filtered under another policy")
            rfc = _load_request(state, body["request_id"], now)
            _require(state.holders.get(rfc["subject_ref"]) == holder, "holder_mismatch")
            _require(recipient == body["audience"] == rfc["audience"], "recipient_mismatch")
            _require(now < parse_time(body["expires_at"]), "package_expired")
            revisions = dict(preview.source_revisions)
            for att in body["attachments"]:
                revisions.setdefault(att["ref"], att["revision"])
                _require(revisions[att["ref"]] == att["revision"], "source_changed", att["ref"])
            revisions = tuple(sorted(revisions.items()))
            _current_revisions(state, revisions)
            _check_against_state(body, state, rfc, dict(revisions))
            expires = min(now + ttl, parse_time(body["expires_at"]), parse_time(rfc["expires_at"]))
            record = ApprovalRecord(
                approval_id="apr-" + secrets.token_hex(12), holder_id=holder, body_digest=digest,
                recipient=recipient, request_id=body["request_id"],
                policy_version=state.policy.version, source_revisions=revisions,
                approved_at=format_time(now), expires_at=format_time(expires))
            self._records[record.approval_id] = record
            self._policy_fps[record.approval_id] = fingerprint
            return record

    def validate(self, approval_id: str, body: dict, state: TrustedState, *,
                 now: datetime) -> ApprovalRecord:
        """Revalidate at dispatch or export against trusted current state. Raises ApprovalError."""
        now = _utc(now)
        with self._lock:
            record = self._records.get(approval_id)
            _require(record is not None, "unknown_approval")
            try:
                validate_public_body(body)
            except DisclosureError as exc:
                raise ApprovalError("body_changed", exc.code) from exc
            _require(hmac.compare_digest(body_digest(body), record.body_digest), "body_changed")
            _require(body["request_id"] == record.request_id, "request_mismatch")
            rfc = _load_request(state, record.request_id, now)
            _require(record.recipient == body["audience"] == rfc["audience"],
                     "recipient_mismatch")
            _require(state.policy.version == record.policy_version
                     and policy_fingerprint(state.policy) == self._policy_fps[approval_id],
                     "policy_changed")
            _require(state.holders.get(rfc["subject_ref"]) == record.holder_id, "holder_mismatch")
            _current_revisions(state, record.source_revisions)
            _check_against_state(body, state, rfc, dict(record.source_revisions))
            _require(now < parse_time(record.expires_at), "approval_expired")
            _require(now < parse_time(body["expires_at"]), "package_expired")
            return record
