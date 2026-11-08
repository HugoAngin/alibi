"""The pinned settlement-candidate rule (``shared/CONTRACTS.md``, Questions and matching).

A settlement candidate for a bank credit is one settled payout, or one whole settlement group
(payouts sharing a ``settlement_ref``), whose total equals the credit, whose settlement time is
at most ``WINDOW`` (5 days) before ``booked_at`` and never after it, and whose payout reference
or settlement reference equals a reference the credit carries when the credit carries one. A
``message`` record (a release to the platform balance) is never a candidate.

Two record shapes carry the settlement state, and both are evaluated here:

* S1's fixtures keep a payout and its settlement as separate records: the payout
  (``kind: payout``, ``payment_ref``) is pushed, the settlement (``kind: settlement``, the same
  ``payment_ref``, ``status`` and ``effective_at``) is usually reachable only by lookup.
* The team's cases fold the state into the payout itself (``status`` settled | pending |
  failed, ``effective_at`` when settled; ``alibi.core.reconcile.is_settlement_evidence``). Such
  a payout is settlement evidence for its own payment reference when its status is decisive
  (``own_status_decisive``): settled, failed, pending when observed at or after the booking, or
  any status once a settlement lookup for its reference returned the payout itself (the
  merchant's answer about settlement is then that record). A pending status observed before the
  booking on a payout nobody has looked up is just the payout's initial state (S1's
  ``initiated``), not evidence either way. A separate settlement record for the reference always
  takes precedence over the payout's own status, and a settlement group (several payouts
  sharing a ``settlement_ref``) is settled only by a separate settlement record.

So the rule is evaluated in two tiers:

* ``settled``: payouts whose settlement is known and settled in time (the full rule);
* ``provisional``: payouts that fit on amount, currency, reference and date (occurred at most
  5 days before the booking, never after) but whose settlement is not yet retrieved. They are
  what the Detective and Challenger should look up.

The deterministic join: one settled candidate links the credit; several settled candidates
(or, with none settled, several provisional ones) leave it ``ambiguous``, which is the only
case the Matcher is called for; nothing leaves it ``unmatched``. Explicit conflicts (a failed
or pending-at-booking settlement, a settlement after the booking, a settlement for another
amount) are reported as ``rejected`` with ``conflict=True``; settlement evidence that merely
fails to show bank settlement (pending before the booking, settled too early, no settlement
status) is ``rejected`` without conflict. Either way ``Rejection.code`` names the team gap code,
so the runner and the one-pass baseline report the verdict from fields, not prose.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Container, Iterable

from alibi.core.investigate_rules import reference_tokens
from alibi.core.reconcile import (
    FAILED_STATUSES,
    PENDING_STATUSES,
    SETTLED_STATUSES,
    is_settlement_evidence,
    settlement_effective_time,
)
from alibi.core.schemas import BankTxn, EvidenceRecord, LinkState, RecordKind, format_utc

WINDOW = timedelta(days=5)

# Team gap codes a rejection can carry (shared/CONTRACTS.md, Questions and matching).
GAP_SETTLEMENT_NOT_FOUND = "settlement_not_found"
GAP_SETTLEMENT_AFTER = "settlement_after_credit"
GAP_PENDING_AT_BOOKING = "settlement_pending_at_booking"
GAP_SETTLEMENT_FAILED = "settlement_failed"
GAP_AMOUNT_MISMATCH = "amount_mismatch"

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

    @property
    def settles_itself(self) -> bool:
        """The settlement evidence is the payout's own status (the team's record shape)."""
        return self.settlement is not None and any(
            self.settlement.record_id == p.record_id for p in self.payouts)

    def summary(self) -> str:
        ids = ", ".join(f"{p.record_id}@{p.revision}" for p in self.payouts)
        if self.settled_at and self.settles_itself:
            when = f"settled {self.settled_at} by its own status"
        elif self.settled_at:
            when = f"settled {self.settled_at}"
        else:
            when = "settlement not retrieved"
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
    code: str | None = None  # team gap code when the settlement evidence decided the rejection

    @property
    def settles_itself(self) -> bool:
        return self.settlement is not None and any(
            self.settlement.record_id == p.record_id for p in self.payouts)


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

    @property
    def coded(self) -> list[Rejection]:
        """Non-conflict rejections decided by settlement evidence (they carry a gap code)."""
        return [r for r in self.rejected if r.code and not r.conflict]

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


def own_status_decisive(payout: EvidenceRecord, booked: datetime,
                        looked_up: Container[str] = frozenset()) -> bool:
    """Does the payout's own status settle the question of its bank settlement?

    True for a payout carrying a settled or failed status, a pending status observed at or
    after ``booked``, or any status when ``looked_up`` holds its payment reference (upper-cased:
    a settlement lookup for the reference returned this payout, so its status is the merchant's
    current answer). A payout with no status, or a pending status observed before the booking
    that nobody has looked up, is not decisive: it is the payout's initial state.
    """
    if payout.kind != RecordKind.PAYOUT or not is_settlement_evidence(payout):
        return False
    if payout.payment_ref and payout.payment_ref.upper() in looked_up:
        return True
    if payout.status in SETTLED_STATUSES or payout.status in FAILED_STATUSES:
        return True
    return payout.status in PENDING_STATUSES and payout.occurred_at >= booked


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


def _settlement_for(payout: EvidenceRecord, settlements: Iterable[EvidenceRecord], *,
                    booked: datetime, looked_up: Container[str], alone: bool
                    ) -> EvidenceRecord | None:
    """The settlement evidence for the payout's reference: among separate settlement records a
    failed one wins, else the highest revision of the most recently observed; with none, the
    payout itself when it stands alone and its own status is decisive."""
    pool = [s for s in settlements if s.kind == RecordKind.SETTLEMENT and payout.payment_ref
            and s.payment_ref == payout.payment_ref and s.subject_ref == payout.subject_ref]
    if pool:
        failed = [s for s in pool if s.status in FAILED_STATUSES]
        if failed:
            return max(failed, key=lambda s: (s.occurred_at, s.revision))
        return max(pool, key=lambda s: (s.occurred_at, s.revision))
    if alone and own_status_decisive(payout, booked, looked_up):
        return payout
    return None


def settlement_candidates(txn: BankTxn, records: Iterable[EvidenceRecord], *,
                          window: timedelta = WINDOW,
                          looked_up: Iterable[str] = ()) -> CandidateResult:
    """Apply the pinned rule to a credit and the retrieved records (payouts and settlements).

    ``looked_up`` lists the payment references for which a settlement lookup returned a
    record (``Material.settlement_lookup_refs``); a payout returned for its own reference is
    then settlement evidence for itself whatever its status says. Self-supplied payouts and
    settlements are never candidates (a holder import cannot establish the source of funds);
    they are listed under ``rejected``.
    """
    result = CandidateResult()
    if not txn.is_credit:
        return result
    records = [r for r in records if r.subject_ref == txn.subject_ref]
    payouts = [r for r in records if r.kind == RecordKind.PAYOUT]
    settlements = [r for r in records if r.kind == RecordKind.SETTLEMENT]
    known_refs = frozenset(str(ref).upper() for ref in looked_up if ref)
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
        found = [_settlement_for(p, settlements, booked=booked, looked_up=known_refs,
                                 alone=len(members) == 1) for p in members]
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
            result.rejected.append(Rejection(members, None, "payout status is failed", conflict=True,
                                             code=GAP_SETTLEMENT_FAILED))
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
            itself = any(settlement.record_id == p.record_id for p in members)
            what = (f"{'payout' if itself else 'settlement'} "
                    f"{settlement.record_id}@{settlement.revision}")
            status = settlement.status
            if status in FAILED_STATUSES:
                result.rejected.append(Rejection(members, settlement, f"{what} has status {status}",
                                                 conflict=True, code=GAP_SETTLEMENT_FAILED))
                continue
            effective = settlement_effective_time(settlement)
            if effective is None:
                observed = format_utc(settlement.occurred_at)
                if status in PENDING_STATUSES and settlement.occurred_at >= booked:
                    result.rejected.append(Rejection(members, settlement,
                                                     f"{what} was still {status} at {observed}, at or after the "
                                                     f"booking at {format_utc(booked)}", conflict=True,
                                                     code=GAP_PENDING_AT_BOOKING))
                elif status in PENDING_STATUSES:
                    result.rejected.append(Rejection(members, settlement,
                                                     f"{what} was {status} when observed at {observed}, before the "
                                                     f"booking at {format_utc(booked)}, and no later status shows it "
                                                     "settled; a pending status does not show the funds reached "
                                                     "the bank", code=GAP_PENDING_AT_BOOKING))
                else:
                    result.rejected.append(Rejection(members, settlement,
                                                     f"{what} has status {status or 'none'}, which does not show "
                                                     "the funds reached the bank", code=GAP_SETTLEMENT_NOT_FOUND))
                continue
            if effective > booked:
                result.rejected.append(Rejection(members, settlement,
                                                 f"{what} took effect at {format_utc(effective)}, after the booking "
                                                 f"at {format_utc(booked)}; a later settlement cannot explain an "
                                                 "earlier credit", conflict=True, code=GAP_SETTLEMENT_AFTER))
                continue
            if booked - effective > window:
                result.rejected.append(Rejection(members, settlement,
                                                 f"{what} took effect at {format_utc(effective)}, more than "
                                                 f"{window.days} days before the booking", code=GAP_SETTLEMENT_NOT_FOUND))
                continue
            if not itself and (settlement.amount_pence, settlement.currency) != (txn.amount_pence, txn.currency):
                result.rejected.append(Rejection(members, settlement,
                                                 f"{what} is for {settlement.amount_pence} {settlement.currency}, "
                                                 "not the credit's amount", conflict=True, code=GAP_AMOUNT_MISMATCH))
                continue
            result.settled.append(Candidate(members, settlement, total, currency, format_utc(effective),
                                            matched, "settled", group_ref))
            continue
        # No settlement evidence: provisional on the payout's own dates.
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
