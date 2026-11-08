"""Typed claim facts, computed by code from what a claim already cites.

The rules engine states ``Claim.facts`` as it decides each claim. The Gemini modes (agents
and one-pass) decide status, evidence and calculation with the same code checks, but they
did not state the typed facts the scorer reads. ``derive_facts`` computes those facts the
way the rules engine does, from the claim's own status, evidence and calculation, so every
mode is scored on the same typed facts. It never changes a status, a reference or an amount,
and it states nothing for a claim that is not ``supported``.

    from alibi.core.facts import derive_facts
    facts = derive_facts(store, rfc, question, outcome.status, outcome.evidence,
                         outcome.calculation)

Facts per question category (``investigate_rules.classify``):

* source of funds: ``payout_references`` (the linked payout's reference) and ``settled_at``
  (when its settlement was effective), one entry per credit;
* breakdown, for a single credit: ``breakdown_minor`` {sales, refunds, fees, net};
* period totals: ``<kind>_minor`` and ``<kind>_count`` per requested kind, and ``net_minor``
  for a net total.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from alibi.core import investigate_rules as rules
from alibi.core.answered import answered_calculation, answered_refs  # noqa: F401  (re-export)
from alibi.core.reconcile import is_settlement_evidence, settlement_effective_time
from alibi.core.schemas import (
    Calculation,
    ClaimStatus,
    EvidenceRecord,
    EvidenceRef,
    Question,
    RecordKind,
    RequestForContext,
    format_utc,
)
from alibi.core.store import NotFound

__all__ = ["answered_calculation", "answered_refs", "derive_facts"]

_PERIOD = frozenset({rules.SALES_TOTAL, rules.REFUNDS_TOTAL, rules.FEES_TOTAL, rules.NET_TOTAL,
                     rules.DOCUMENTED_TOTALS, rules.SEVERAL_TOTALS})
_BREAKDOWN_KINDS = {"sales": RecordKind.ORDER, "refunds": RecordKind.REFUND, "fees": RecordKind.FEE}


def derive_facts(store: Any, request: RequestForContext, question: Question, status: Any,
                 evidence: Iterable[EvidenceRef], calculation: Calculation | None) -> dict[str, Any]:
    """The typed facts a ``supported`` claim asserts; ``{}`` for any other status, except a
    ``contradicted`` breakdown, which states the record-derived breakdown that contradicts the
    holder's figures."""
    try:
        claimed = ClaimStatus(status)
    except ValueError:
        return {}
    if claimed not in (ClaimStatus.SUPPORTED, ClaimStatus.CONTRADICTED):
        return {}
    if claimed == ClaimStatus.CONTRADICTED and calculation is None:
        return {}   # a contradiction with no reconciled records states nothing
    found = rules.classify(question, request)
    credits = len(request.txn_ids or [])
    if claimed == ClaimStatus.CONTRADICTED and found.category != rules.BREAKDOWN:
        return {}
    if found.category == rules.SOURCE_OF_FUNDS:
        return _source_facts(store, list(evidence), calculation, credits)
    if found.category == rules.BREAKDOWN:
        if credits != 1 or calculation is None:
            return {}
        return {"breakdown_minor": _breakdown(calculation)}
    if found.category in _PERIOD and calculation is not None:
        return _period_facts(found, calculation, store)
    return {}


def _records(store: Any, evidence: list[EvidenceRef]) -> list[EvidenceRecord]:
    """The cited records (not bank rows), in citation order, each once."""
    out, seen = [], set()
    for ref in evidence:
        if ref.ref_type != "record" or ref.key() in seen:
            continue
        seen.add(ref.key())
        try:
            out.append(store.get_source(ref.ref_id, ref.revision))
        except NotFound:
            continue
    return out


def _source_facts(store: Any, evidence: list[EvidenceRef], calc: Calculation | None,
                  credits: int) -> dict[str, Any]:
    records = _records(store, evidence)
    payouts = [r for r in records if r.kind == RecordKind.PAYOUT]
    if credits == 1 and calc is not None and calc.timing is not None:
        # One credit: the calculation's own timing check names the settlement it relied on.
        settlement_id = calc.timing.settlement_ref.ref_id
        payout = next((p for p in payouts if p.record_id == settlement_id), None) \
            or (payouts[0] if payouts else None)
        ref = payout.payment_ref if payout is not None else None
        effective = calc.timing.settlement_effective_at
        return {"payout_references": [ref] if ref else [],
                "settled_at": [format_utc(effective)] if effective is not None else []}
    # Several credits: one entry per linked payout and per settlement, in citation order.
    references = [p.payment_ref for p in payouts if p.payment_ref]
    settled = []
    for record in records:
        if is_settlement_evidence(record):
            effective = settlement_effective_time(record)
            if effective is not None:
                settled.append(format_utc(effective))
    return {"payout_references": references, "settled_at": settled}


def _breakdown(calc: Calculation) -> dict[str, int]:
    sums = {name: sum(t.amount_pence for t in calc.terms if t.kind == kind.value)
            for name, kind in _BREAKDOWN_KINDS.items()}
    return {**sums, "net": calc.total_pence}


def _period_facts(found: Any, calc: Calculation, store: Any = None) -> dict[str, Any]:
    facts: dict[str, Any] = {}
    for kind in found.kinds:
        keys = rules._FACT_KEYS.get(kind)
        if keys is None:
            continue
        rows = [t for t in calc.terms if t.kind == kind.value]
        facts[keys[0]] = sum(t.amount_pence for t in rows)
        facts[keys[1]] = len(rows)
    by_issuer = _issuer_shares(found, calc, store)
    if by_issuer is not None:
        facts["by_issuer"] = by_issuer
    if found.category == rules.NET_TOTAL:
        facts["net_minor"] = calc.total_pence
    return facts


def _issuer_shares(found: Any, calc: Calculation, store: Any) -> dict[str, dict[str, int]] | None:
    """Each issuer's share of a period total's counted terms when they come from more than one
    issuer, read from the stored records the terms cite; ``None`` for a single issuer or when
    any term's issuer cannot be read, so a partial split is never stated."""
    if store is None:
        return None
    issuer_of: dict[int, str] = {}
    for index, term in enumerate(calc.terms):
        try:
            record = store.get_source(term.ref.ref_id, term.ref.revision)
        except Exception:   # noqa: BLE001 - an unreadable term means no split, not a crash
            return None
        issuer = getattr(record, "issuer_id", None)
        if not isinstance(issuer, str) or not issuer:
            return None
        issuer_of[index] = issuer
    issuers = sorted(set(issuer_of.values()))
    if len(issuers) < 2:
        return None
    shares: dict[str, dict[str, int]] = {}
    for issuer in issuers:
        share: dict[str, int] = {}
        for kind in found.kinds:
            keys = rules._FACT_KEYS.get(kind)
            if keys is None:
                continue
            rows = [t for i, t in enumerate(calc.terms)
                    if t.kind == kind.value and issuer_of[i] == issuer]
            share[keys[0]] = sum(t.amount_pence for t in rows)
            share[keys[1]] = len(rows)
        shares[issuer] = share
    return shares
