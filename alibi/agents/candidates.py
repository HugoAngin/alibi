"""The pinned settlement-candidate rule (``shared/CONTRACTS.md``, Questions and matching).

A settlement candidate for a bank credit is one settled payout, or one whole settlement group
(payouts sharing a ``settlement_ref``), whose total equals the credit, whose settlement time is
at most ``WINDOW`` (5 days) before ``booked_at`` and never after it, and whose payout reference
or settlement reference equals a reference the credit carries when the credit carries one. A
``message`` record (a release to the platform balance) is never a candidate.

In this build's record model a payout and its settlement are separate records: the payout
(``kind: payout``, ``payment_ref``) is pushed, the settlement (``kind: settlement``, the same
``payment_ref``, ``status`` and ``effective_at``) is usually reachable only by lookup. So the
rule is evaluated in two tiers:

* ``settled``: payouts whose settlement is known and settled in time (the full rule);
* ``provisional``: payouts that fit on amount, currency, reference and date (occurred at most
  5 days before the booking, never after) but whose settlement is not yet retrieved. They are
  what the Detective and Challenger should look up.

The deterministic join: one settled candidate links the credit; several settled candidates
(or, with none settled, several provisional ones) leave it ``ambiguous``, which is the only
case the Matcher is called for; nothing leaves it ``unmatched``. Explicit conflicts (a failed
or pending-at-booking settlement, a settlement after the booking) are reported as
``rejected`` with the reason so the runner can contradict a claim from fields, not prose.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Iterable

from alibi.core.investigate_rules import reference_tokens
from alibi.core.reconcile import FAILED_STATUSES, PENDING_STATUSES, settlement_effective_time
from alibi.core.schemas import BankTxn, EvidenceRecord, LinkState, RecordKind, format_utc

WINDOW = timedelta(days=5)

RULE_TEXT = (
    "A candidate is one settled payout, or one whole settlement group, whose total equals the "
    "credit, settled at most 5 days before the credit was booked and never after it, and whose "
    "payout reference or settlement reference equals the credit's payment reference when the "
    "credit carries one. A release to the platform balance is never a candidate. Abstain "
    "(ambiguous) when more than one candidate remains."
)


@dataclass
class Candidate:
    payouts: list[EvidenceRecord]
    settlement: EvidenceRecord | None
    total_pence: int
    currency: str
    settled_at: str | None          # UTC ISO-8601 when settled, else None
    reference_matched: bool | None  # None when the credit carries no reference
    tier: str                       # settled | provisional
    group_ref: str | None = None    # settlement_ref shared by a group, else None

    @property
    def record_ids(self) -> list[str]:
        return [p.record_id for p in self.payouts]

    def summary(self) -> str:
        ids = ", ".join(f"{p.record_id}@{p.revision}" for p in self.payouts)
        when = f"settled {self.settled_at}" if self.settled_at else "settlement not retrieved"
        ref = ("reference matches the credit" if self.reference_matched
               else "credit carries no reference" if self.reference_matched is None
               else "reference does not match")
        return f"{ids} ({self.total_pence} {self.currency}, {when}, {ref})"


@dataclass
class Rejection:
    payouts: list[EvidenceRecord]
    settlement: EvidenceRecord | None
    reason: str
    conflict: bool = False   # an explicit status or timing conflict, not just a non-fit


@dataclass
class CandidateResult:
    settled: list[Candidate] = field(default_factory=list)
    provisional: list[Candidate] = field(default_factory=list)
    rejected: list[Rejection] = field(default_factory=list)

    @property
    def state(self) -> LinkState:
        if len(self.settled) == 1:
            return LinkState.LINKED
        if len(self.settled) > 1:
            return LinkState.AMBIGUOUS
        if len(self.provisional) > 1:
            return LinkState.AMBIGUOUS
        return LinkState.UNMATCHED

    @property
    def chosen(self) -> Candidate | None:
        return self.settled[0] if len(self.settled) == 1 else None

    @property
    def pool(self) -> list[Candidate]:
        return self.settled or self.provisional

    @property
    def conflicts(self) -> list[Rejection]:
        return [r for r in self.rejected if r.conflict]

    def summary(self) -> str:
        parts = [f"link {self.state.value}: {len(self.settled)} settled candidate(s), "
                 f"{len(self.provisional)} provisional"]
        for c in self.settled:
            parts.append("settled " + c.summary())
        for c in self.provisional:
            parts.append("provisional " + c.summary())
        for r in self.rejected:
            parts.append("rejected " + ", ".join(p.record_id for p in r.payouts) + ": " + r.reason)
        return "; ".join(parts)


def _groups(payouts: list[EvidenceRecord]) -> list[tuple[str | None, list[EvidenceRecord]]]:
    """Payouts sharing a settlement_ref form one group; the rest stand alone."""
    by_ref: dict[str, list[EvidenceRecord]] = {}
    singles: list[tuple[str | None, list[EvidenceRecord]]] = []
    for p in payouts:
        if p.settlement_ref:
            by_ref.setdefault(p.settlement_ref, []).append(p)
        else:
            singles.append((None, [p]))
    groups = [(ref, members) for ref, members in by_ref.items()]
    return sorted(groups + singles, key=lambda g: (g[1][0].occurred_at, g[1][0].record_id))


def _settlement_for(payout: EvidenceRecord, settlements: Iterable[EvidenceRecord]
                    ) -> EvidenceRecord | None:
    """The settlement record for the payout's reference: a failed one wins, else the highest
    revision of the most recently observed."""
    pool = [s for s in settlements if s.kind == RecordKind.SETTLEMENT and payout.payment_ref
            and s.payment_ref == payout.payment_ref and s.subject_ref == payout.subject_ref]
    if not pool:
        return None
    failed = [s for s in pool if s.status in FAILED_STATUSES]
    if failed:
        return max(failed, key=lambda s: (s.occurred_at, s.revision))
    return max(pool, key=lambda s: (s.occurred_at, s.revision))


def settlement_candidates(txn: BankTxn, records: Iterable[EvidenceRecord], *,
                          window: timedelta = WINDOW) -> CandidateResult:
    """Apply the pinned rule to a credit and the retrieved records (payouts and settlements).

    Self-supplied payouts and settlements are never candidates (a holder import cannot
    establish the source of funds); they are listed under ``rejected``.
    """
    result = CandidateResult()
    if not txn.is_credit:
        return result
    records = [r for r in records if r.subject_ref == txn.subject_ref]
    payouts = [r for r in records if r.kind == RecordKind.PAYOUT]
    settlements = [r for r in records if r.kind == RecordKind.SETTLEMENT]
    tokens = reference_tokens(txn)
    booked = txn.booked_at

    for group_ref, members in _groups(payouts):
        own = [p for p in members if p.source_trust == "self_supplied"]
        if own:
            result.rejected.append(Rejection(members, None,
                                             "self-supplied payout records cannot establish the source "
                                             "of funds"))
            continue
        currencies = {p.currency for p in members}
        if None in currencies or len(currencies) != 1:
            result.rejected.append(Rejection(members, None, "payout amounts are missing or in mixed currencies"))
            continue
        currency = currencies.pop()
        if currency != txn.currency:
            result.rejected.append(Rejection(members, None, f"currency {currency} differs from the credit's {txn.currency}"))
            continue
        total = sum(p.amount_pence or 0 for p in members)
        if total != txn.amount_pence:
            result.rejected.append(Rejection(members, None,
                                             f"total {total} differs from the credit's {txn.amount_pence}"))
            continue
        # Reference: when the credit carries any reference token, one must match.
        refs = {p.payment_ref.upper() for p in members if p.payment_ref}
        if group_ref:
            refs.add(group_ref.upper())
        found = [_settlement_for(p, settlements) for p in members]
        for s in found:
            if s is not None and s.settlement_ref:
                refs.add(s.settlement_ref.upper())
        matched: bool | None = None
        if tokens:
            matched = bool(refs & tokens)
            if not matched:
                result.rejected.append(Rejection(members, None,
                                                 "the credit carries a payment reference that names none of "
                                                 "this payout's references"))
                continue
        if any(p.status in FAILED_STATUSES for p in members):
            result.rejected.append(Rejection(members, None, "payout status is failed", conflict=True))
            continue
        # Timing from explicit fields: a settled settlement's effective time, else the payout's
        # own settled status, else unknown (provisional).
        own_settled = [s for s in found if s is not None and s.source_trust == "self_supplied"]
        if own_settled:
            result.rejected.append(Rejection(members, own_settled[0],
                                             "self-supplied settlement records cannot establish bank settlement"))
            continue
        known = [s for s in found if s is not None]
        if known:
            settlement = max(known, key=lambda s: (s.status in FAILED_STATUSES, s.occurred_at, s.revision))
            if settlement.status in FAILED_STATUSES:
                result.rejected.append(Rejection(members, settlement,
                                                 f"settlement {settlement.record_id}@{settlement.revision} has status "
                                                 f"{settlement.status}", conflict=True))
                continue
            effective = settlement_effective_time(settlement)
            if effective is None:
                if settlement.status in PENDING_STATUSES and settlement.occurred_at >= booked:
                    result.rejected.append(Rejection(members, settlement,
                                                     f"settlement {settlement.record_id}@{settlement.revision} was still "
                                                     f"{settlement.status} at {format_utc(settlement.occurred_at)}, at or "
                                                     f"after the booking at {format_utc(booked)}", conflict=True))
                else:
                    result.rejected.append(Rejection(members, settlement,
                                                     f"settlement {settlement.record_id}@{settlement.revision} has status "
                                                     f"{settlement.status or 'none'}, which does not show the funds reached "
                                                     "the bank"))
                continue
            if effective > booked:
                result.rejected.append(Rejection(members, settlement,
                                                 f"settlement {settlement.record_id}@{settlement.revision} took effect at "
                                                 f"{format_utc(effective)}, after the booking at {format_utc(booked)}; a "
                                                 "later settlement cannot explain an earlier credit", conflict=True))
                continue
            if booked - effective > window:
                result.rejected.append(Rejection(members, settlement,
                                                 f"settlement {settlement.record_id}@{settlement.revision} took effect at "
                                                 f"{format_utc(effective)}, more than {window.days} days before the booking"))
                continue
            if (settlement.amount_pence, settlement.currency) != (txn.amount_pence, txn.currency):
                result.rejected.append(Rejection(members, settlement,
                                                 f"settlement {settlement.record_id}@{settlement.revision} is for "
                                                 f"{settlement.amount_pence} {settlement.currency}, not the credit's amount",
                                                 conflict=True))
                continue
            result.settled.append(Candidate(members, settlement, total, currency, format_utc(effective),
                                            matched, "settled", group_ref))
            continue
        # No settlement record: provisional on the payout's own dates.
        latest = max(p.occurred_at for p in members)
        if latest > booked:
            result.rejected.append(Rejection(members, None,
                                             f"payout occurred at {format_utc(latest)}, after the booking at "
                                             f"{format_utc(booked)}, so it cannot explain the credit"))
            continue
        if booked - min(p.occurred_at for p in members) > window:
            result.rejected.append(Rejection(members, None,
                                             f"payout occurred more than {window.days} days before the booking"))
            continue
        result.provisional.append(Candidate(members, None, total, currency, None, matched,
                                            "provisional", group_ref))
    return result
