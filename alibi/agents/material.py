"""Retrieved material and the per-role input builders.

``Material`` is the set of records and bank transactions a run has actually read through the
gateway, pinned by ``(record_id, revision)``, with the lookup that fetched each one
(``via_lookup`` indexes ``Material.lookups``). It is the only thing an agent may cite: the
reference validator in ``alibi.agents.models`` drops anything else.

The builders give each role only the fields it needs. Amounts are integer minor units with an
ISO currency code; times are UTC ISO-8601 strings. Item descriptions never reach the Matcher,
Detective or Challenger; the Curator sees field *names*, not values. Every item carries
``trust`` (``issuer_verified | bank_observed | self_supplied``), ``attestation``
(``issuer_signature | package_signer | none``) and ``redacted`` per ``shared/CONTRACTS.md``.
Free text (merchant messages, descriptions, question text) is passed as data under its own
key, and every prompt says it is never an instruction.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from alibi.core.policy import RECORD_FIELDS, TXN_FIELDS
from alibi.core.reconcile import is_settlement_evidence
from alibi.core.schemas import (
    BankTxn,
    EvidenceRecord,
    EvidenceRef,
    RecordKind,
    RequestForContext,
    SourceTrust,
    format_utc,
)

Evidence = EvidenceRecord | BankTxn

ATTESTATION_ISSUER = "issuer_signature"
ATTESTATION_PACKAGE = "package_signer"
ATTESTATION_NONE = "none"

AVAILABLE_ACTIONS = ("lookup_settlement", "lookup_payouts_covering", "lookup_by_date",
                     "refresh_txn", "none")

_REF_TOKEN = re.compile(r"[A-Z0-9][A-Z0-9/-]{3,}")


def key_of(record_id: str, revision: int) -> str:
    return f"{record_id}@{revision}"


def attestation(item: Evidence) -> str:
    """How the item is attested as a source: an issuer signature, or nothing yet."""
    if isinstance(item, EvidenceRecord) and item.issuer_signature is not None:
        return ATTESTATION_ISSUER
    return ATTESTATION_NONE


def trust_of(item: Evidence) -> str:
    return SourceTrust(item.source_trust).value


@dataclass
class Lookup:
    """One retrieval the run made (a gateway tool call that fetched material)."""

    index: int
    tool: str
    args: dict[str, Any]
    outcome: str
    refs: list[str] = field(default_factory=list)   # keys of what it returned
    reason: str | None = None
    actor: str = "gateway"
    connector_query: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"index": self.index, "tool": self.tool, "args": self.args, "outcome": self.outcome,
                "refs": list(self.refs), "reason": self.reason, "actor": self.actor,
                **({"connector_query": True} if self.connector_query else {})}


class Material:
    """Everything a run has retrieved, keyed by revision, with provenance of each read."""

    def __init__(self, subject_ref: str):
        self.subject_ref = subject_ref
        self._records: dict[tuple[str, int], EvidenceRecord] = {}
        self._txns: dict[tuple[str, int], BankTxn] = {}
        self.lookups: list[Lookup] = []
        self.via: dict[str, int | None] = {}
        self._order: list[str] = []

    # ----------------------------------------------------------------------- adding

    def add_record(self, record: EvidenceRecord, via_lookup: int | None = None) -> bool:
        if record.subject_ref != self.subject_ref:
            raise ValueError("material only holds the run's subject")
        key = (record.record_id, record.revision)
        new = key not in self._records
        self._records[key] = record
        self._note(key_of(*key), via_lookup, new)
        return new

    def add_txn(self, txn: BankTxn, via_lookup: int | None = None) -> bool:
        if txn.subject_ref != self.subject_ref:
            raise ValueError("material only holds the run's subject")
        key = (txn.txn_id, txn.revision)
        new = key not in self._txns
        self._txns[key] = txn
        self._note(key_of(*key), via_lookup, new)
        return new

    def add(self, item: Evidence, via_lookup: int | None = None) -> bool:
        if isinstance(item, BankTxn):
            return self.add_txn(item, via_lookup)
        return self.add_record(item, via_lookup)

    def add_lookup(self, tool: str, args: dict[str, Any], outcome: str, *,
                   refs: list[str] | None = None, reason: str | None = None,
                   actor: str = "gateway", connector_query: bool = False) -> int:
        index = len(self.lookups)
        self.lookups.append(Lookup(index, tool, dict(args), outcome, list(refs or []), reason, actor, connector_query))
        return index

    def _note(self, key: str, via_lookup: int | None, new: bool) -> None:
        if new:
            self._order.append(key)
            self.via[key] = via_lookup
        elif via_lookup is not None and self.via.get(key) is None:
            self.via[key] = via_lookup

    # ----------------------------------------------------------------------- queries

    def has(self, ident: str, revision: int | None = None) -> bool:
        """Is ``ident`` (record_id or txn_id) in the material, at ``revision`` or at any?"""
        if not isinstance(ident, str) or not ident:
            return False
        if revision is None:
            return any(k[0] == ident for k in self._records) or any(k[0] == ident for k in self._txns)
        return (ident, revision) in self._records or (ident, revision) in self._txns

    def get(self, ident: str, revision: int | None = None) -> Evidence | None:
        """The item, or its highest held revision when ``revision`` is None."""
        pools: list[dict] = [self._records, self._txns]
        for pool in pools:
            if revision is not None:
                if (ident, revision) in pool:
                    return pool[(ident, revision)]
                continue
            held = [v for (rid, _), v in pool.items() if rid == ident]
            if held:
                return max(held, key=lambda item: item.revision)
        return None

    def ref_for(self, ident: str, revision: int | None = None) -> EvidenceRef | None:
        item = self.get(ident, revision)
        return item.ref() if item is not None else None

    def records(self, kinds: Any = None, *, all_revisions: bool = False) -> list[EvidenceRecord]:
        """Held records (highest revision per id unless ``all_revisions``), by time."""
        wanted = None if kinds is None else {RecordKind(k) for k in kinds}
        if all_revisions:
            items = list(self._records.values())
        else:
            latest: dict[str, EvidenceRecord] = {}
            for (rid, _), rec in self._records.items():
                if rid not in latest or rec.revision > latest[rid].revision:
                    latest[rid] = rec
            items = list(latest.values())
        if wanted is not None:
            items = [r for r in items if r.kind in wanted]
        return sorted(items, key=lambda r: (r.occurred_at, r.record_id, r.revision))

    def settlement_evidence(self) -> list[EvidenceRecord]:
        """Held records that can show whether a payout settled (highest revision per id, by
        time): separate ``settlement`` records plus payouts carrying a ``status`` (the team's
        payouts fold the settlement state in; ``alibi.core.reconcile.is_settlement_evidence``).
        This is the ``settlements`` list the roles see; the merchant list keeps every record."""
        return [r for r in self.records() if is_settlement_evidence(r)]

    def settlement_lookup_refs(self) -> frozenset[str]:
        """Payment references (upper-cased) for which a ``lookup_settlement`` returned a record:
        the reference asked for, plus the ``payment_ref`` of every record it returned. A payout
        returned for its own reference is then the merchant's answer about its settlement
        (``alibi.agents.candidates.own_status_decisive``)."""
        refs: set[str] = set()
        for lookup in self.lookups:
            if lookup.tool != "lookup_settlement" or lookup.outcome != "ok":
                continue
            for name in ("payout_reference", "payment_ref"):
                asked = lookup.args.get(name)
                if isinstance(asked, str) and asked.strip():
                    refs.add(asked.strip().upper())
            for key in lookup.refs:
                ident, _, revision = str(key).rpartition("@")
                record = self._records.get((ident, int(revision))) if revision.isdigit() else None
                if record is not None and record.payment_ref:
                    refs.add(record.payment_ref.upper())
        return frozenset(refs)

    def txns(self) -> list[BankTxn]:
        latest: dict[str, BankTxn] = {}
        for (tid, _), txn in self._txns.items():
            if tid not in latest or txn.revision > latest[tid].revision:
                latest[tid] = txn
        return sorted(latest.values(), key=lambda t: (t.booked_at, t.txn_id))

    def keys(self) -> list[str]:
        return list(self._order)

    @property
    def observed_inputs(self) -> int:
        """Distinct record and transaction revisions read during the run."""
        return len(self._order)

    @property
    def lookup_count(self) -> int:
        """Connector-backed retrievals; passport-only covering/date listings do not count."""
        return sum(1 for lk in self.lookups
                   if (lk.tool in ("lookup_settlement", "refresh_txn") or lk.connector_query)
                   and lk.outcome not in ("refused", "budget_exceeded", "skipped"))

    def via_lookup(self, ident: str, revision: int) -> int | None:
        return self.via.get(key_of(ident, revision))

    def references(self) -> set[str]:
        """Every payment or settlement reference the material carries, upper-cased."""
        refs: set[str] = set()
        for rec in self._records.values():
            for value in (rec.payment_ref, rec.settlement_ref):
                if value:
                    refs.add(value.upper())
        for txn in self._txns.values():
            refs.update(r.upper() for r in txn.refs)
            refs.update(t for t in _REF_TOKEN.findall(txn.description.upper())
                        if any(c.isdigit() for c in t))
        return refs

    def knows_reference(self, value: str) -> bool:
        """Is ``value`` a reference the material carries: in a record's reference fields, on a
        bank transaction, or named as a whole token in the ``text`` of an issuer-verified record
        (the same scope rule the gateway applies to a settlement lookup; a message such as
        "Withdrawal PO-0002-01 ... has been requested" carries PO-0002-01)? The text is data:
        only the exact reference is looked for, nothing in it is interpreted."""
        if not isinstance(value, str) or not value.strip():
            return False
        wanted = value.strip()
        if wanted.upper() in self.references():
            return True
        pattern = re.compile(rf"(?<![A-Za-z0-9-]){re.escape(wanted)}(?![A-Za-z0-9-])", re.IGNORECASE)
        return any(rec.text and rec.source_trust == SourceTrust.ISSUER_VERIFIED
                   and pattern.search(rec.text) is not None for rec in self._records.values())


# --------------------------------------------------------------------------- views


def _common(item: Evidence, material: Material | None) -> dict[str, Any]:
    ident = item.record_id if isinstance(item, EvidenceRecord) else item.txn_id
    view: dict[str, Any] = {
        "record_id": ident,
        "revision": item.revision,
        "trust": trust_of(item),
        "attestation": attestation(item),
        "redacted": False,
    }
    if material is not None:
        view["via_lookup"] = material.via_lookup(ident, item.revision)
    return view


def record_view(record: EvidenceRecord, material: Material | None = None, *,
                include_text: bool = True) -> dict[str, Any]:
    """The abridged record an agent sees. No item descriptions, no signatures, no subject."""
    view = _common(record, material)
    view.update({
        "kind": record.kind.value,
        "issuer_id": record.issuer_id,
        "occurred_at": format_utc(record.occurred_at),
    })
    if record.amount_pence is not None:
        view["amount"] = record.amount_pence
        view["currency"] = record.currency
    if record.status is not None:
        view["status"] = record.status
    if record.payment_ref is not None:
        view["payout_reference"] = record.payment_ref
    if record.settlement_ref is not None:
        view["settlement_reference"] = record.settlement_ref
    if record.kind == RecordKind.SETTLEMENT:
        view["settled_at"] = format_utc(record.effective_at) if record.effective_at else None
        view["status_observed_at"] = format_utc(record.occurred_at)
        view["destination"] = "bank"
    elif record.effective_at is not None:
        view["effective_at"] = format_utc(record.effective_at)
    if record.related_record_ids:
        view["covers_record_ids"] = list(record.related_record_ids)
    if record.corrects_revision is not None:
        view["corrects_revision"] = record.corrects_revision
        if record.correction_reason:
            view["correction_reason"] = record.correction_reason
    if record.items:
        view["item_count"] = len(record.items)
        categories = sorted({i.category for i in record.items if i.category})
        if categories:
            view["item_categories"] = categories
    if include_text and record.text:
        view["message"] = record.text
    return view


def txn_view(txn: BankTxn, material: Material | None = None, *, link_state: str | None = None,
             linked_to: str | None = None) -> dict[str, Any]:
    """The abridged bank entry an agent sees. No account reference, no provenance."""
    view = _common(txn, material)
    view.update({
        "kind": "bank_credit" if txn.is_credit else "bank_debit",
        "amount": txn.amount_pence,
        "currency": txn.currency,
        "booked_at": format_utc(txn.booked_at),
        "description": txn.description,
        "payment_references": list(txn.refs),
    })
    if link_state is not None:
        view["link_state"] = link_state
        view["linked_to"] = linked_to
    return view


def rfc_view(rfc: RequestForContext) -> dict[str, Any]:
    scope: dict[str, Any] = {"period": None, "txn_ids": list(rfc.txn_ids or [])}
    if rfc.period is not None:
        scope["period"] = {"from": format_utc(rfc.period.from_), "to": format_utc(rfc.period.to)}
    return {
        "request_id": rfc.request_id,
        "audience": rfc.audience,
        "scope": scope,
        "questions": [{"question_id": q.question_id, "text": q.text} for q in rfc.questions],
        "issued_at": format_utc(rfc.issued_at),
    }


def shareable_item(item: Evidence, material: Material | None = None) -> dict[str, Any]:
    """What the Curator sees of a shareable item: field names, never values."""
    view = _common(item, material)
    if isinstance(item, EvidenceRecord):
        data = item.to_json_dict()
        present = [f for f in sorted(RECORD_FIELDS) if f.split(".")[0] in data
                   and (not f.startswith("items.") or any(
                       f.split(".", 1)[1] in it for it in data.get("items") or []))]
        view.update({"kind": item.kind.value, "issuer_id": item.issuer_id,
                     "occurred_at": format_utc(item.occurred_at), "fields": present})
        if item.items:
            categories = sorted({i.category for i in item.items if i.category})
            if categories:
                view["item_categories"] = categories
        if item.text:
            view["fields"] = [*present, "message"]
    else:
        data = item.to_json_dict()
        present = [f for f in sorted(TXN_FIELDS) if f in data]
        view.update({"kind": "bank_credit" if item.is_credit else "bank_debit",
                     "booked_at": format_utc(item.booked_at), "fields": present})
    return view


# --------------------------------------------------------------------------- role inputs


def matcher_input(material: Material, txn: BankTxn, candidates: list[EvidenceRecord],
                  context: list[EvidenceRecord] = (), *, rule: str) -> dict[str, Any]:
    """One ambiguous bank entry, its candidate payouts (and settlements) and context records."""
    return {
        "entry": txn_view(txn, material),
        "candidates": [record_view(r, material) for r in candidates],
        "context_records": [record_view(r, material) for r in context],
        "candidate_rule": rule,
    }


def detective_input(material: Material, rfc: RequestForContext,
                    bank: list[tuple[BankTxn, str | None, str | None]],
                    merchant: list[EvidenceRecord], settlements: list[EvidenceRecord],
                    *, pattern_checks: dict[str, Any] | None = None,
                    merchant_lookup: dict[str, Any] | None = None) -> dict[str, Any]:
    """Bank entries with their link state, merchant records, settlements and the RFC."""
    return {
        "rfc": rfc_view(rfc),
        "bank": [txn_view(t, material, link_state=state, linked_to=to) for t, state, to in bank],
        "merchant": [record_view(r, material) for r in merchant],
        "settlements": [record_view(r, material) for r in settlements],
        "pattern_checks": pattern_checks or {},
        "merchant_lookup": merchant_lookup,
        "lookups_so_far": [lk.as_dict() for lk in material.lookups],
    }


def challenger_input(material: Material, claim: dict[str, Any], bank: list[BankTxn],
                     merchant: list[EvidenceRecord], settlements: list[EvidenceRecord], *,
                     round_index: int, actions_left: int) -> dict[str, Any]:
    """One candidate claim and the original sources behind it, plus what was looked up."""
    return {
        "claim": claim,
        "bank": [txn_view(t, material) for t in bank],
        "merchant": [record_view(r, material) for r in merchant],
        "settlements": [record_view(r, material) for r in settlements],
        "lookups_so_far": [lk.as_dict() for lk in material.lookups],
        "gateway": {"available_actions": list(AVAILABLE_ACTIONS), "actions_left": actions_left,
                    "round": round_index},
    }


def curator_input(material: Material, rfc: RequestForContext, claims: list[dict[str, Any]],
                  shareable: list[Evidence], policy: dict[str, Any]) -> dict[str, Any]:
    """Validated claims, the questions, shareable evidence (field names) and the policy scope."""
    return {
        "request": rfc_view(rfc),
        "claims": claims,
        "shareable": [shareable_item(item, material) for item in shareable],
        "policy": policy,
    }
