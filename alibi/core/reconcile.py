"""Deterministic reconciliation: cited amounts are summed in code, and settlement timing is
checked from explicit fields.

* Every amount comes from a cited merchant record or bank transaction, pinned to its
  revision. Merchant amounts are non-negative magnitudes whose direction comes from ``kind``
  (``SIGN_BY_KIND``: orders add; refunds, fees and purchase receipts subtract). Bank amounts carry
  their own sign: a credit is a ``+amount`` term of kind ``bank_credit``.
* A calculation never mixes currencies (Alibi does not convert) and never counts the same
  record_id or txn_id twice, even at different revisions.
* Settlement status uses the exact vocabulary below. A settlement that took effect after the
  bank credit was booked cannot support that credit, and neither can one that took effect more
  than ``SETTLEMENT_WINDOW`` (five days) before it: the pinned rule is ``settled_at`` at most
  five days before ``booked_at`` and never after it. Equal times count as on time, and exactly
  five days counts. A settlement must also match the credit's currency and amount, and the
  payout's payment reference.
* Settlement evidence is a ``settlement`` record, or a ``payout`` record that carries a
  ``status`` (the team's data model folds the settlement state into the payout:
  ``status`` settled | pending | failed, with ``effective_at`` when settled). Such a payout
  is settlement evidence for its own payment reference.

Results are ``schemas.Calculation`` objects, whose validator re-checks the arithmetic.
Nothing here calls a network or an LLM.
"""

from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta
from types import MappingProxyType

from alibi.core.schemas import (
    BankTxn,
    CalcTerm,
    Calculation,
    EvidenceRecord,
    EvidenceRef,
    RecordKind,
    TimingCheck,
)

__all__ = [
    "BANK_CREDIT",
    "BANK_DEBIT",
    "FAILED_STATUSES",
    "METHOD_DOCUMENTED_TOTALS",
    "METHOD_NET_PAYOUT",
    "METHOD_NET_TOTAL",
    "NET_METHODS",
    "PENDING_STATUSES",
    "SETTLED_STATUSES",
    "SETTLEMENT_EVIDENCE_KINDS",
    "SETTLEMENT_WINDOW",
    "SIGN_BY_KIND",
    "DoubleCountError",
    "MissingAmountError",
    "MixedCurrencyError",
    "ReconcileError",
    "allowed_signs",
    "check_settlement_timing",
    "compare_to_target",
    "credit_supported",
    "is_settlement_evidence",
    "reconcile_credit",
    "settlement_effective_time",
    "sum_terms",
    "term_for",
]

# Status vocabulary, matched exactly. Any other status, including "released_to_balance"
# (money moved to a platform balance, not to the bank), is not a settlement.
SETTLED_STATUSES = frozenset({"settled", "paid", "completed", "succeeded"})
PENDING_STATUSES = frozenset({"pending", "processing", "in_transit", "initiated", "scheduled"})
FAILED_STATUSES = frozenset({"failed", "reversed", "returned", "cancelled"})

# Direction of a merchant amount by record kind. Read-only because it is a default argument.
SIGN_BY_KIND: Mapping[str, int] = MappingProxyType({
    RecordKind.ORDER: 1,
    RecordKind.RECEIPT: -1,  # a receipt documents a purchase: money out
    RecordKind.REFUND: -1,
    RecordKind.FEE: -1,
})

# What reconcile_credit sums: sales less refunds and fees. A receipt records money spent.
_COMPONENT_KINDS = frozenset({RecordKind.ORDER, RecordKind.REFUND, RecordKind.FEE})

# canon (RFC 8785, I-JSON) refuses integers beyond this, so no result may carry one.
_MAX_SAFE_PENCE = 2**53 - 1

BANK_CREDIT = "bank_credit"       # CalcTerm.kind of a bank credit: +amount
BANK_DEBIT = "bank_debit"         # CalcTerm.kind of a bank debit: -|amount|
METHOD_NET_PAYOUT = "net_payout"  # Calculation.method used by reconcile_credit
METHOD_NET_TOTAL = "net_total"    # Calculation.method of a period total net of refunds and fees
# Calculation.method of several documented totals answered together (sales and refunds over a
# period): every record is one signed term by kind, so the total is sales less refunds and
# fees, and the per-kind totals are read from the terms.
METHOD_DOCUMENTED_TOTALS = "documented_totals"

# Methods that net several kinds with SIGN_BY_KIND. Every other method totals records of one
# kind, with one sign for every term (package verification enforces both).
NET_METHODS = frozenset({METHOD_NET_PAYOUT, METHOD_NET_TOTAL, METHOD_DOCUMENTED_TOTALS})

# The pinned settlement window: a settlement supports a credit only when it took effect at most
# this long before the booking (and never after it).
SETTLEMENT_WINDOW = timedelta(days=5)

# Record kinds that can carry settlement evidence (see ``is_settlement_evidence``).
SETTLEMENT_EVIDENCE_KINDS = frozenset({RecordKind.SETTLEMENT, RecordKind.PAYOUT})


def allowed_signs(method: str, kind: str) -> frozenset[int]:
    """Signs a merchant term of ``kind`` may carry in a calculation using ``method``.

    Net methods use SIGN_BY_KIND. Any other method totals one kind either as positive amounts
    (a refunds total of +18000) or in the kind's SIGN_BY_KIND direction (a receipt total of
    -4500 compared with a bank debit)."""
    fixed = SIGN_BY_KIND.get(kind)
    if method in NET_METHODS:
        return frozenset({fixed}) if fixed is not None else frozenset({1, -1})
    return frozenset({1, 1 if fixed is None else fixed})


Evidence = EvidenceRecord | BankTxn


class ReconcileError(ValueError):
    """The cited evidence cannot be reconciled as asked. The message is safe to show."""


class MixedCurrencyError(ReconcileError):
    """Amounts in different currencies were combined or compared. Alibi never converts."""


class DoubleCountError(ReconcileError):
    """The same record_id or txn_id was cited twice, at the same or different revisions."""


class MissingAmountError(ReconcileError):
    """A cited item has no amount and currency."""


# --------------------------------------------------------------------------- arithmetic


def term_for(item: Evidence, *, sign: int | None = None) -> CalcTerm:
    """One signed term citing ``item`` at its exact revision.

    A merchant record's sign defaults to ``SIGN_BY_KIND[kind]``. Pass ``sign`` for a kind
    with no default, such as a payout. A bank transaction keeps its own sign: a credit is
    ``+amount`` (kind ``bank_credit``), a debit ``-|amount|`` (kind ``bank_debit``), and a
    ``sign`` that disagrees with it is rejected.
    """
    _require_evidence(item)
    if sign is not None:
        sign = _check_sign(sign)
    if isinstance(item, BankTxn):
        direction = 1 if item.amount_pence > 0 else -1
        if sign is not None and sign != direction:
            raise ReconcileError(
                f"{item.ref().token()} carries its own sign from the bank; it cannot be flipped")
        return CalcTerm(ref=item.ref(), kind=BANK_CREDIT if direction == 1 else BANK_DEBIT,
                        sign=direction, amount_pence=abs(item.amount_pence),
                        currency=item.currency)
    amount, currency = _amount(item)
    if sign is None:
        sign = SIGN_BY_KIND.get(item.kind)
        if sign is None:
            raise _no_sign(item)
    return CalcTerm(ref=item.ref(), kind=item.kind.value, sign=sign, amount_pence=amount,
                    currency=currency)


def sum_terms(items: Iterable[Evidence], *, method: str,
              signs: Mapping[str, int] = SIGN_BY_KIND,
              currency: str | None = None) -> Calculation:
    """Sum the cited items in code into a ``Calculation`` with no target.

    Merchant records take their sign from ``signs`` by kind; bank transactions keep their
    own sign. ``currency``, when given, is the currency every item must be in. Raises
    ``MissingAmountError`` for an item with no amount, ``ReconcileError`` for a kind with no
    sign, ``DoubleCountError`` for a record_id or txn_id cited twice (even at different
    revisions) and ``MixedCurrencyError`` for a second currency. A total that canonical JSON
    cannot carry (beyond 2**53 - 1 either way) raises ``ReconcileError``. ``cited_refs``
    lists every term's ref.
    """
    terms: list[CalcTerm] = []
    seen: dict[str, int] = {}  # revision-free token -> revision already cited
    for item in items:
        _require_evidence(item)
        ref = item.ref()
        if ref.token() in seen:
            raise DoubleCountError(
                f"{ref.token()} is cited more than once (revisions {seen[ref.token()]} and "
                f"{ref.revision}); each source can count only once")
        seen[ref.token()] = ref.revision
        if isinstance(item, BankTxn):
            term = term_for(item)
        else:
            _amount(item)  # report a missing amount before a missing sign
            sign = signs.get(item.kind)
            if sign is None:
                raise _no_sign(item)
            term = term_for(item, sign=sign)
        if currency is None:
            currency = term.currency
        elif term.currency != currency:
            raise _currency_mismatch(ref, term.currency, currency)
        terms.append(term)
    if currency is None:
        raise ReconcileError("nothing to sum: give at least one item or a currency")
    total = _in_range(sum(term.signed_pence for term in terms), "the total")
    return Calculation(method=method, currency=currency, terms=terms, total_pence=total,
                       cited_refs=[term.ref for term in terms])


def compare_to_target(calc: Calculation, target: Evidence) -> Calculation:
    """Compare an untargeted calculation's total with one cited target amount.

    A bank transaction compares by its signed amount (a debit is negative); a merchant record
    by its amount. Sets ``target_ref``, ``target_pence``, ``difference_pence`` (total minus
    target) and ``balanced``, and adds the target to ``cited_refs``. Raises
    ``MixedCurrencyError`` on a currency mismatch, ``DoubleCountError`` when the target is
    already a term, and ``ReconcileError`` when the calculation already has a target or the
    difference is beyond what canonical JSON can carry.
    """
    _require_evidence(target)
    if calc.target_ref is not None:
        raise ReconcileError("this calculation already has a target; compare the untargeted sum")
    amount, currency = _amount(target)
    ref = target.ref()
    if currency != calc.currency:
        raise _currency_mismatch(ref, currency, calc.currency)
    if any(term.ref.token() == ref.token() for term in calc.terms):
        raise DoubleCountError(f"{ref.token()} is already a term, so it cannot also be the target")
    difference = _in_range(calc.total_pence - amount, "the difference from the target")
    return _rebuild(calc, target_ref=ref, target_pence=amount, difference_pence=difference,
                    balanced=difference == 0, cited_refs=_cite(calc.cited_refs, [ref]))


# --------------------------------------------------------------------------- settlement timing


def is_settlement_evidence(record: EvidenceRecord) -> bool:
    """Can this record show whether a payout settled? A ``settlement`` record always can; a
    ``payout`` record can when it carries a ``status`` (the team's payouts fold the settlement
    state in). Orders, fees, refunds, receipts and messages never can, nor can a payout with
    no status."""
    if record.kind == RecordKind.SETTLEMENT:
        return True
    return record.kind == RecordKind.PAYOUT and record.status is not None


def settlement_effective_time(settlement: EvidenceRecord) -> datetime | None:
    """When the settlement took effect according to its explicit fields, or None.

    Only settlement evidence (``is_settlement_evidence``) with a settled status has an
    effective time: ``effective_at``, or else ``occurred_at`` (when that status was observed).
    Pending, failed, unknown or missing statuses (``released_to_balance`` included) have
    none, and so does any record that is not settlement evidence, such as a completed order.
    """
    if not is_settlement_evidence(settlement) or settlement.status not in SETTLED_STATUSES:
        return None
    if settlement.effective_at is not None:
        return settlement.effective_at
    return settlement.occurred_at


def check_settlement_timing(settlement: EvidenceRecord, credit: BankTxn, *,
                            window: timedelta = SETTLEMENT_WINDOW) -> TimingCheck:
    """Was the settlement effective in time for the bank credit?

    ``ok`` only when ``settlement_effective_time`` is at or before ``credit.booked_at`` and at
    most ``window`` before it (exactly ``window`` counts). A settlement effective after the
    booking cannot support the credit, one effective earlier than the window cannot be the
    transfer that produced it, and a pending status never can: the reason says whether it was
    pending at or after booking (not settled at booking) or before booking (no evidence it
    settled in time).
    """
    if not isinstance(settlement, EvidenceRecord):
        raise ReconcileError("settlement evidence must be a merchant record")
    if not isinstance(credit, BankTxn) or not credit.is_credit:
        raise ReconcileError("settlement timing is checked against a bank credit")
    if not isinstance(window, timedelta) or window < timedelta(0):
        raise ReconcileError("the settlement window must be a non-negative duration")
    effective = settlement_effective_time(settlement)
    ok, reason = _timing_verdict(settlement, effective, credit.booked_at, window)
    return TimingCheck(settlement_ref=settlement.ref(), credit_ref=credit.ref(),
                       settlement_status=settlement.status, settlement_effective_at=effective,
                       status_observed_at=settlement.occurred_at, booked_at=credit.booked_at,
                       ok=ok, reason=reason)


def _window_text(window: timedelta) -> str:
    if window % timedelta(days=1) == timedelta(0):
        days = window // timedelta(days=1)
        return f"{days} day{'s' if days != 1 else ''}"
    return f"{window.total_seconds() / 3600:g} hours"


def _timing_verdict(settlement: EvidenceRecord, effective: datetime | None,
                    booked: datetime, window: timedelta) -> tuple[bool, str]:
    status = settlement.status
    if not is_settlement_evidence(settlement):
        what = (f"a {settlement.kind.value} record with no status"
                if settlement.kind == RecordKind.PAYOUT else f"a {settlement.kind.value} record")
        return False, (f"This is {what}, not a settlement record, so it cannot show when the "
                       "funds reached the bank.")
    if effective is not None:  # a settled status
        stated = settlement.effective_at is not None
        if effective <= booked:
            when = ("effective" if stated
                    else "observed (no separate effective time given)")
            if booked - effective <= window:
                return True, (f"Status {status}, {when} at or before the credit's booking time: "
                              "settled in time to support the credit.")
            return False, (f"Status {status}, {when} more than {_window_text(window)} before the "
                           "credit's booking time: a settlement that early cannot be the transfer "
                           "that produced this credit.")
        when = ("effective only after the credit was booked" if stated
                else "observed only after the credit was booked, with no earlier effective time")
        return False, (f"Status {status}, but {when}: a later settlement cannot support an "
                       "earlier credit.")
    if status in PENDING_STATUSES:
        if settlement.occurred_at >= booked:
            return False, (f"Status {status} was observed at or after the credit's booking time "
                           "(pending at or after booking): it was not settled at booking.")
        return False, (f"Status {status} was observed before the credit's booking time (pending "
                       "before booking): there is no evidence it settled in time.")
    if status in FAILED_STATUSES:
        return False, (f"Status {status}: the transfer did not complete, so it cannot support "
                       "the credit.")
    if status is None:
        return False, "The settlement record has no status, so there is no evidence it settled."
    return False, (f"Status {status} is not a settlement status, so it does not show the funds "
                   "reached the bank.")


# --------------------------------------------------------------------------- credit reconciliation


def reconcile_credit(credit: BankTxn, *, components: Iterable[EvidenceRecord],
                     payout: EvidenceRecord | None = None,
                     settlement: EvidenceRecord | None = None,
                     window: timedelta = SETTLEMENT_WINDOW) -> Calculation:
    """Reconcile a bank credit: orders minus refunds minus fees, in code, against its amount.

    Components must be orders, refunds and fees. A given payout must be in the same currency
    (else ``MixedCurrencyError``) and its amount must equal the components' total, or the
    result is not balanced. The schema ties ``balanced`` to ``difference_pence == 0``, so
    when the components match the credit but the payout does not, the calculation targets
    the payout to show that difference. Settlement evidence (a settlement record, or a payout
    carrying a status, which may be the payout itself) is held to the same currency and
    amount rule and, when a payout is given, must share its payment reference (else
    ``ReconcileError``). The settlement attaches ``check_settlement_timing`` with ``window``;
    without one, ``timing`` stays None and ``credit_supported`` is False. ``cited_refs``
    lists the components, payout, settlement and credit.
    """
    if not isinstance(credit, BankTxn) or not credit.is_credit:
        raise ReconcileError("reconcile_credit needs a bank credit (a positive bank amount)")
    components = list(components)
    if not all(isinstance(item, EvidenceRecord) and item.kind in _COMPONENT_KINDS
               for item in components):
        raise ReconcileError("components must be merchant records (orders, refunds, fees)")
    if payout is not None and (not isinstance(payout, EvidenceRecord)
                               or payout.kind != RecordKind.PAYOUT):
        raise ReconcileError("payout must be a payout record")

    base = sum_terms(components, method=METHOD_NET_PAYOUT, currency=credit.currency)
    calc = compare_to_target(base, credit)
    cited = list(base.cited_refs)
    if payout is not None:
        calc = _match_total(calc, base, payout)
        cited.append(payout.ref())
    timing = None
    if settlement is not None:
        timing = check_settlement_timing(settlement, credit, window=window)
        if is_settlement_evidence(settlement):  # any other record already fails timing
            same = payout is not None and settlement.record_id == payout.record_id
            if payout is not None and not same and (payout.payment_ref is None
                                                    or settlement.payment_ref != payout.payment_ref):
                raise ReconcileError(
                    f"{settlement.ref().token()} and {payout.ref().token()} do not share a "
                    "payment reference, so the settlement cannot show that payout settled")
            calc = _match_total(calc, base, settlement)
        cited.append(settlement.ref())
    cited.append(credit.ref())
    return _rebuild(calc, timing=timing, cited_refs=_cite(cited))


def credit_supported(calc: Calculation, *, window: timedelta = SETTLEMENT_WINDOW) -> bool:
    """True only when the calculation balances against a bank credit and the settlement
    timing attached for that same credit is ok: effective at or before the booking and at
    most ``window`` before it.

    The verdict is re-derived from the explicit fields rather than trusted from the
    ``balanced`` and ``ok`` flags, so a timing check whose own status or times say otherwise
    never counts. A calculation from an untrusted source should still be recomputed from its
    cited evidence with ``reconcile_credit``.
    """
    timing, target = calc.timing, calc.target_ref
    if timing is None or target is None or target.ref_type != "txn":
        return False
    effective = timing.settlement_effective_at
    return (calc.balanced is True and calc.difference_pence == 0
            and calc.target_pence is not None and calc.target_pence > 0
            and timing.credit_ref == target and timing.ok
            and timing.settlement_status in SETTLED_STATUSES
            and effective is not None and effective <= timing.booked_at
            and timing.booked_at - effective <= window)


# --------------------------------------------------------------------------- helpers


def _require_evidence(item: object) -> None:
    if not isinstance(item, (EvidenceRecord, BankTxn)):
        raise ReconcileError(
            f"cannot cite a {type(item).__name__}; expected an EvidenceRecord or BankTxn")


def _check_sign(sign: object) -> int:
    if type(sign) is not int or sign not in (1, -1):
        raise ReconcileError("a term's sign must be 1 or -1")
    return sign


def _amount(item: Evidence) -> tuple[int, str]:
    """The amount and currency as recorded: signed for a bank txn, a magnitude for a record."""
    if item.amount_pence is None or item.currency is None:
        raise MissingAmountError(f"{item.ref().token()} has no amount and currency to cite")
    return item.amount_pence, item.currency


def _in_range(pence: int, what: str) -> int:
    """``pence`` unchanged, or ``ReconcileError`` when canonical JSON could not carry it."""
    if abs(pence) > _MAX_SAFE_PENCE:
        raise ReconcileError(f"{what} is out of range: canonical JSON carries at most "
                             "2**53 - 1 pence, so the result could not be hashed or signed")
    return pence


def _match_total(calc: Calculation, base: Calculation, record: EvidenceRecord) -> Calculation:
    """Hold a payout or settlement to the components: the same currency (else
    ``MixedCurrencyError``) and the same amount. A different amount retargets a balanced
    calculation at ``record``, so the difference shows and ``balanced`` is False."""
    amount, currency = _amount(record)
    if currency != base.currency:
        raise _currency_mismatch(record.ref(), currency, base.currency)
    if calc.balanced and amount != base.total_pence:
        return compare_to_target(base, record)
    return calc


def _no_sign(record: EvidenceRecord) -> ReconcileError:
    return ReconcileError(f"{record.ref().token()}: {record.kind.value} records have no sign in "
                          "this calculation; pass one explicitly to count them")


def _currency_mismatch(ref: EvidenceRef, found: str, expected: str) -> MixedCurrencyError:
    return MixedCurrencyError(f"{ref.token()} is in {found} but the calculation is in "
                              f"{expected}; Alibi does not convert currencies")


def _cite(*groups: Iterable[EvidenceRef]) -> list[EvidenceRef]:
    """Concatenate refs in order, dropping exact repeats (same id and revision)."""
    out: list[EvidenceRef] = []
    seen: set[str] = set()
    for group in groups:
        for ref in group:
            if ref.key() not in seen:
                seen.add(ref.key())
                out.append(ref)
    return out


def _rebuild(calc: Calculation, **changes) -> Calculation:
    """A changed copy of ``calc``, validated again so the schema re-checks the arithmetic."""
    data = {name: getattr(calc, name) for name in Calculation.model_fields}
    data.update(changes)
    return Calculation.model_validate(data)
