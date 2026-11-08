"""Rules-only investigation: the deterministic baseline that answers a Request for Context.

``run_investigation(request, store, gateway)`` gives one claim per question, from explicit
fields only:

* Questions are classified first by the pinned question id (``q.source_of_funds``,
  ``q.breakdown``, ``q.sales_refunds_in_period``, see ``PINNED_QUESTIONS``), then by keyword
  (``classify_question``). With ``txn_ids``, a question that names a requested transaction, or
  pairs a source word (source, origin, explain, where, why, ...) with a word for the money
  (credit, payment, deposit, received, ...), is a source-of-funds question; one that asks how
  the amount was calculated or reconciles is a breakdown question; credit cards, loans and the
  like are not. With a period, questions about sales, refunds or fees are period totals.
  Several categories give a net total (signed terms) only when the question asks for one
  ("net", "after refunds", "less fees"); a sales word that is only the object ("refunds on
  orders") is ignored, and a question naming several totals without a net cue gets one
  documented total per kind in one claim (``documented_totals``). Anything else is unmapped.
* Source of funds, per transaction: match the credit to a payout by a reference it carries
  (a payout's payment reference, or a settlement reference that a settlement already in the
  passport ties to one), else by equal amount and currency within the window before the
  booking. When nothing in the passport matches and the credit carries references, the
  settlement lookup is asked for them (``MAX_REFERENCE_LOOKUPS`` at most); a payout record it
  returns for one of them is the candidate. Only a unique compatible candidate links, and a
  payout after the booking never matches. For a linked payout the retrieval is fixed: the
  settlement lookup for its payment reference, every settlement for that reference already in
  the passport, and one listing of its component records. Settlement evidence is a settlement
  record, or a payout record whose status is decisive (settled, failed, or pending when
  observed at or after the booking), from the payout's own issuer. An explicit conflict (a
  failed payout or settlement, or a settlement still pending at the booking or effective only
  after it) contradicts the claim whatever else is missing. Amounts are reconciled in code
  (``alibi.core.reconcile``); the status then comes from the settlement's timing (effective at
  or before the booking and at most five days before it) and amount and from the references
  the credit carries. Matching amounts alone never support a claim, self-supplied payouts or
  settlements never do, one payout never supports two credits (in this run or in any earlier
  active link of the subject), and merchant messages or free text are never settlement
  evidence.
* A breakdown question follows the source-of-funds verdict for the same transaction: when the
  credit's source is supported, the breakdown is the reconciliation of the payout's components
  (sales less refunds and fees) with ``breakdown_minor`` facts; otherwise it is unresolved with
  the same gap.
* Period totals sum the documented records in code. A record counts when its status says it
  went through, is left out when its status says it did not, and any other status leaves
  the total unresolved.
* Every claim carries a gap code in ``Claim.gap`` when it is not supported (one of the nine
  pinned codes, ``GAP_CODES``, or an engine code from ``ENGINE_GAP_CODES``) and the human
  sentence in ``Claim.gap_detail``; ``Claim.facts`` carries the typed facts the scorer reads.

Every read goes through the gateway, so it is bounded, scoped and traced. A refused or failed
read becomes a typed unresolved reason instead of a crash. Links, claims and the trace are
saved to the store. No network, no LLM.
"""

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any

from pydantic import TypeAdapter, ValidationError

from alibi.core import reconcile
from alibi.core.canon import make_id
from alibi.core.reconcile import (
    FAILED_STATUSES,
    PENDING_STATUSES,
    SETTLED_STATUSES,
    SIGN_BY_KIND,
    ReconcileError,
)
from alibi.core.answered import answered_calculation, answered_refs, examined_refs
from alibi.core.schemas import (
    BankTxn,
    Calculation,
    Claim,
    ClaimStatus,
    EvidenceRecord,
    EvidenceRef,
    InvestigationResult,
    Link,
    LinkState,
    Question,
    RecordKind,
    Ref,
    RequestForContext,
    SourceTrust,
    TraceEvent,
    format_utc,
    utc_now,
)
from alibi.core.store import Store, StoreError

try:
    from alibi.gateway import BudgetExceeded, GatewayError, NotFoundError, ScopeError, Unavailable
except ImportError:  # alibi/gateway.py not built yet: the same names and hierarchy
    class GatewayError(Exception):
        """A gateway call failed. The message is safe to show."""

    class ScopeError(GatewayError):
        """The call asked for another subject's data."""

    class BudgetExceeded(GatewayError):
        """The run's call budget is used up."""

    class Unavailable(GatewayError):
        """A connector is not available."""

    class NotFoundError(GatewayError):
        """The requested object does not exist."""

__all__ = [
    "BREAKDOWN", "COMPONENT_KINDS", "COUNTED_STATUSES", "DEFAULT_WINDOW", "DOCUMENTED_TOTALS",
    "ENGINE_GAP_CODES", "EXCLUDED_STATUSES", "GAP_CODES", "MAX_REFERENCE_LOOKUPS", "MODE",
    "PINNED_QUESTIONS", "UNMAPPED_GAP", "BudgetExceeded", "Classification", "GatewayError",
    "NotFoundError", "ScopeError", "Unavailable", "classify", "classify_question",
    "next_run_id", "reference_tokens", "run_investigation",
]

MODE = "rules"
ACTOR = "rules"
DEFAULT_WINDOW = timedelta(days=5)
UNMAPPED_GAP = "The rules engine could not map this question to evidence it can check."
MAX_REFERENCE_LOOKUPS = 2   # settlement lookups tried for the credit's own references

SOURCE_OF_FUNDS = "source_of_funds"
BREAKDOWN = "breakdown"
SALES_TOTAL = "sales_total"
REFUNDS_TOTAL = "refunds_total"
FEES_TOTAL = "fees_total"
NET_TOTAL = reconcile.METHOD_NET_TOTAL  # nets kinds; the other totals sum one kind as +amount
DOCUMENTED_TOTALS = reconcile.METHOD_DOCUMENTED_TOTALS  # one documented total per kind
SEVERAL_TOTALS = "several_totals"       # classify_question's name for the same question shape
UNMAPPED = "unmapped"
METHOD_REFERENCE = "reference"
METHOD_AMOUNT_DATE = "amount_date"
COMPONENT_KINDS = (RecordKind.ORDER, RecordKind.REFUND, RecordKind.FEE)
RELEASED_STATUS = "released_to_balance"

# The pinned question ids and what the rules engine checks for each.
Q_SOURCE_OF_FUNDS = "q.source_of_funds"
Q_BREAKDOWN = "q.breakdown"
Q_SALES_REFUNDS = "q.sales_refunds_in_period"
PINNED_QUESTIONS = {Q_SOURCE_OF_FUNDS: SOURCE_OF_FUNDS, Q_BREAKDOWN: BREAKDOWN,
                    Q_SALES_REFUNDS: DOCUMENTED_TOTALS}
_PINNED_KINDS = {Q_SALES_REFUNDS: (RecordKind.ORDER, RecordKind.REFUND)}

# The nine pinned gap codes (agents/data/README.md, Gold rules).
GAP_SETTLEMENT_NOT_FOUND = "settlement_not_found"
GAP_BALANCE_RELEASE = "balance_release_not_settlement"
GAP_SETTLEMENT_AFTER = "settlement_after_credit"
GAP_PENDING_AT_BOOKING = "settlement_pending_at_booking"
GAP_SETTLEMENT_FAILED = "settlement_failed"
GAP_AMBIGUOUS = "ambiguous_candidates"
GAP_AMOUNT_MISMATCH = "amount_mismatch"
GAP_MISSING_CITED = "missing_cited_record"
GAP_NO_RECORDS = "no_merchant_records"
GAP_CODES = (GAP_SETTLEMENT_NOT_FOUND, GAP_BALANCE_RELEASE, GAP_SETTLEMENT_AFTER,
             GAP_PENDING_AT_BOOKING, GAP_SETTLEMENT_FAILED, GAP_AMBIGUOUS, GAP_AMOUNT_MISMATCH,
             GAP_MISSING_CITED, GAP_NO_RECORDS)
# Engine codes for situations the nine do not name.
GAP_QUESTION_NOT_MAPPED = "question_not_mapped"    # no rules check fits the question
GAP_EVIDENCE_UNAVAILABLE = "evidence_unavailable"  # a gateway read was refused or failed
GAP_NOT_A_CREDIT = "not_a_credit"                  # the transaction is a debit
GAP_PAYOUT_ALREADY_LINKED = "payout_already_linked"  # the payout funds another credit
ENGINE_GAP_CODES = (GAP_QUESTION_NOT_MAPPED, GAP_EVIDENCE_UNAVAILABLE, GAP_NOT_A_CREDIT,
                    GAP_PAYOUT_ALREADY_LINKED)

# Period totals. A record counts when it has no status or one saying it went through, and is
# left out when its status says it did not happen. Any other status leaves the total
# unresolved: the rules engine does not guess whether such a record went through.
COUNTED_STATUSES = {
    RecordKind.ORDER: frozenset({"completed", "complete", "paid", "fulfilled", "delivered",
                                 "shipped", "dispatched", "settled", "succeeded", "closed",
                                 "refunded", "partially_refunded"}),
    RecordKind.REFUND: frozenset({"completed", "complete", "paid", "settled", "succeeded",
                                  "processed", "issued", "refunded", "approved"}),
    RecordKind.FEE: frozenset({"completed", "complete", "paid", "settled", "succeeded",
                               "processed", "charged", "deducted", "collected", "applied"}),
}
EXCLUDED_STATUSES = FAILED_STATUSES | frozenset({"canceled", "voided", "void", "declined",
                                                 "rejected", "pending", "unpaid", "draft"})

# Source of funds: a requested txn_id named in the question, or a cue word together with a
# word for the money. Credit cards, loans and the like are other questions.
_CUE_WORDS = re.compile(
    r"\b(?:sources?|origins?|originat\w*|explain\w*|where|why|who|(?:comes?|came|coming)\s+from)"
    r"\b|\bwhat\b(?=[^.?!]*\bfor\b)", re.IGNORECASE)
_MONEY_WORDS = re.compile(
    r"\b(?:credits?|payments?|deposits?|transfers?|funds?|money|transactions?|inflows?|"
    r"receiv\w*|arriv\w*|paid|credited|deposited|transferred)\b", re.IGNORECASE)
_NOT_SOURCE = re.compile(
    r"\b(?:credit\s+(?:cards?|scores?|history|limits?|ratings?)|loans?|lenders?|mortgages?|"
    r"overdrafts?|borrow\w*)\b", re.IGNORECASE)
# Cues that only ask for an explanation. With a breakdown word and no other source cue ("explain
# how this credit was calculated", "why does the payment not reconcile") the question asks how
# the amount is made up, not where it came from. "Arrive(d) at" is not one of these breakdown
# words here: "why did the money arrive at your account" asks for a source.
_EXPLAIN_ONLY = re.compile(r"\b(?:explain\w*|why)\b", re.IGNORECASE)
_BREAKDOWN_ONLY = re.compile(
    r"\b(?:calculat\w*|reconcil\w*|break\s*down|breakdown|made\s+up|composed|"
    r"work(?:ed|ing)?\s+out|adds?\s+up|itemi[sz]\w*)\b", re.IGNORECASE)
# Breakdown: how the amount was calculated, reconciles, breaks down or is made up.
_BREAKDOWN_CUE = re.compile(
    r"\b(?:calculat\w*|reconcil\w*|break\s*down|breakdown|made\s+up|composed|arrived?\s+at|"
    r"work(?:ed|ing)?\s+out|adds?\s+up|itemi[sz]\w*)\b", re.IGNORECASE)
_KIND_WORDS = (
    (RecordKind.ORDER, re.compile(
        r"\b(?:sales?|sold|orders?|revenues?|income|earn\w*|turnover|takings)\b", re.IGNORECASE)),
    (RecordKind.REFUND, re.compile(r"\brefund\w*|\b(?<!tax )returns\b", re.IGNORECASE)),
    (RecordKind.FEE, re.compile(r"\b(?:fees?|commissions?)\b", re.IGNORECASE)),
)
# A net total needs a net cue: "net", or "after / less / minus ... refunds / fees".
_NET_CUE = re.compile(
    r"\bnet\b|\b(?:after|less|minus|excluding|deduct\w*|subtract\w*)\W+(?:\w+\W+){0,3}?"
    r"(?:refund\w*|returns|fees?|commissions?)\b", re.IGNORECASE)
# A sales word that is only the object of the question ("refunds on orders", "fees on your
# sales", "in order to").
_OBJECT_SALES = re.compile(
    r"\bin\s+order\s+to\b|\b(?:on|for|from|against|per)\s+"
    r"(?:(?:your|the|these|those|its|their|my|our|all|each|any|her|his)\s+)?"
    r"(?:sales?|orders?|revenues?|income|turnover|takings)\b", re.IGNORECASE)
_TOTALS = {RecordKind.ORDER: SALES_TOTAL, RecordKind.REFUND: REFUNDS_TOTAL,
           RecordKind.FEE: FEES_TOTAL}
_LABELS = {RecordKind.ORDER: "sales", RecordKind.REFUND: "refunds", RecordKind.FEE: "fees"}
_FACT_KEYS = {RecordKind.ORDER: ("sales_minor", "sale_count"),
              RecordKind.REFUND: ("refunds_minor", "refund_count"),
              RecordKind.FEE: ("fees_minor", "fee_count")}
_REF_TOKEN = re.compile(r"[A-Z0-9][A-Z0-9-]{3,}")
_REF_PREFIX = re.compile(r"[A-Z]+")
_REF = TypeAdapter(Ref)
_ORIGIN = "the credit's origin remains unresolved"


# --------------------------------------------------------------------------- public helpers


@dataclass(frozen=True)
class Classification:
    """What the rules engine can check for one question."""

    category: str                       # source_of_funds | breakdown | sales_total | ... | unmapped
    kinds: tuple[RecordKind, ...] = ()  # record kinds a period total sums
    keywords: tuple[str, ...] = ()      # the words (or the question id) that decided it


def classify_question(text: str, request: RequestForContext) -> Classification:
    """Classify a question by keyword (case-insensitive).

    With ``txn_ids``, a question that names a requested txn_id, or pairs a cue (source,
    origin, explain, where, why, who, come from, what ... for) with a word for the money
    (credit, payment, deposit, transfer, funds, received, arrived, paid, ...), is
    ``source_of_funds``, unless it is about credit cards, loans, mortgages or borrowing, or
    it asks how the amount was calculated or reconciles with no source cue other than
    "explain" or "why" (``classify`` makes that a ``breakdown``).
    Otherwise, with a period: sales (orders, revenue, income, earnings, turnover, takings),
    refunds (returns) or fees (commission) give that total. Several categories give
    ``net_total`` only with a net cue ("net", "after refunds", "less fees"); without one, a
    sales word that is only the object ("refunds on orders") is ignored, and a question still
    naming several totals is ``several_totals``. Anything else is ``unmapped``.
    """
    breakdown_only = bool(_BREAKDOWN_ONLY.search(text)) \
        and not _CUE_WORDS.search(_EXPLAIN_ONLY.sub(" ", text))
    if request.txn_ids and not breakdown_only:
        named = tuple(sorted({txn_id.lower() for txn_id in request.txn_ids
                              if _names(text, txn_id)}))
        if named:
            return Classification(SOURCE_OF_FUNDS, (), named)
        if not _NOT_SOURCE.search(text):
            cues, money = _words(_CUE_WORDS, text), _words(_MONEY_WORDS, text)
            if cues and money:
                return Classification(SOURCE_OF_FUNDS, (), tuple(sorted({*cues, *money})))
    if request.period is not None:
        kinds, words = _kinds_in(text)
        if len(kinds) == 1:
            return Classification(_TOTALS[kinds[0]], kinds, words)
        if kinds:
            if RecordKind.ORDER in kinds and _NET_CUE.search(text):
                return Classification(NET_TOTAL, kinds, words)
            asked, asked_words = _kinds_in(_OBJECT_SALES.sub(" ", text))
            if len(asked) == 1:
                return Classification(_TOTALS[asked[0]], asked, asked_words)
            return Classification(SEVERAL_TOTALS, kinds, words)
    return Classification(UNMAPPED)


def classify(question: Question, request: RequestForContext) -> Classification:
    """What the rules engine checks for ``question``: the pinned question id first
    (``PINNED_QUESTIONS``, when the request has the scope it needs: ``txn_ids`` for the
    source and breakdown questions, a ``period`` for the period question), then
    ``classify_question`` by keyword, where a question with ``txn_ids`` that asks how the
    amount was calculated or reconciles is a ``breakdown`` and a period question naming
    several totals without a net cue is ``documented_totals``."""
    pinned = PINNED_QUESTIONS.get(question.question_id)
    if pinned in (SOURCE_OF_FUNDS, BREAKDOWN) and request.txn_ids:
        return Classification(pinned, (), (question.question_id,))
    if pinned == DOCUMENTED_TOTALS and request.period is not None:
        return Classification(pinned, _PINNED_KINDS[question.question_id], (question.question_id,))
    found = classify_question(question.text, request)
    if found.category == UNMAPPED and request.txn_ids and not _NOT_SOURCE.search(question.text):
        cues = _words(_BREAKDOWN_CUE, question.text)
        if cues:
            return Classification(BREAKDOWN, (), cues)
    if found.category == SEVERAL_TOTALS:
        return Classification(DOCUMENTED_TOTALS, found.kinds, found.keywords)
    return found


def reference_tokens(txn: BankTxn) -> frozenset[str]:
    """The references a bank transaction carries, upper-cased: its ``refs`` plus the tokens
    of its upper-cased description that look like references (``[A-Z0-9][A-Z0-9-]{3,}`` with
    at least one digit, so words such as TRANSFER or PAYOUT never count)."""
    words = {token for token in _REF_TOKEN.findall(txn.description.upper())
             if any(char.isdigit() for char in token)}
    return frozenset({ref.upper() for ref in txn.refs} | words)


def next_run_id(store: Store, request_id: str, *, audience: str) -> str:
    """The id of the next rules run for the request ``(audience, request_id)``:
    ``make_id("run", audience, request_id, "rules", n)``, where ``n`` counts that request's
    rules runs already recorded in the store (by their claims or trace). Another bank using
    the same request_id gets unrelated run ids. Build the gateway with this id so its calls
    share the run's trace."""
    used = {claim.run_id for claim in
            store.list_claims(request_id=request_id, audience=audience, state=None)}
    n = 0
    while True:
        run_id = make_id("run", audience, request_id, MODE, n)
        if run_id not in used and not store.list_traces(run_id):
            return run_id
        n += 1


def run_investigation(request: RequestForContext, store: Store, gateway: Any, mode: str = "rules",
                      *, now: datetime | None = None,
                      window: timedelta = DEFAULT_WINDOW) -> InvestigationResult:
    """Answer every question in ``request`` with one rules-based claim and save the results.

    Reads go through ``gateway`` (``alibi.gateway.Gateway`` or anything with its interface).
    Links, claims, the gateway's trace and the rules trace are saved to ``store``. The run id
    is ``next_run_id(store, request.request_id, audience=request.audience)``, so reruns are
    distinct but deterministic.
    ``mode`` other than ``"rules"`` raises ``NotImplementedError`` before anything happens. A
    gateway scoped to another subject, or recording under another run id (build a new one per
    run with ``run_id=next_run_id(...)``), raises ``ValueError`` before any call. ``window``
    is both the payout search window around the booking and the settlement window (a
    settlement supports the credit only when effective at most ``window`` before it).
    """
    if mode != MODE:
        raise NotImplementedError(f"mode {mode!r} is not implemented here; the rules "
                                  "investigation only runs mode 'rules'")
    if not isinstance(request, RequestForContext):
        request = RequestForContext.model_validate(request)
    scope = getattr(gateway, "subject_ref", None)
    if scope is not None and scope != request.subject_ref:
        raise ValueError("the gateway is scoped to a different subject than the request")
    if window <= timedelta(0):
        raise ValueError("window must be a positive duration")
    run_id = next_run_id(store, request.request_id, audience=request.audience)
    gateway_run = getattr(gateway, "run_id", None)
    if gateway_run is not None and gateway_run != run_id:
        raise ValueError("the gateway records its calls under another run id; build a new "
                         "gateway for each run with run_id=next_run_id(store, request_id, "
                         "audience=...)")
    run = _Run(request, store, gateway, run_id=run_id, now=now or utc_now(), window=window)
    claims = [run.answer(question) for question in request.questions]
    return run.finish(claims)


# --------------------------------------------------------------------------- run state


@dataclass
class _Outcome:
    """The rules verdict for one transaction or one question."""

    status: ClaimStatus
    statement: str
    rationale: str
    reason: str                      # why this status, for the status_change trace event
    gap: str | None = None           # gap code (GAP_CODES or ENGINE_GAP_CODES)
    detail: str | None = None        # the human sentence behind the code
    evidence: list[EvidenceRef] = field(default_factory=list)
    calculation: Calculation | None = None
    facts: dict[str, Any] = field(default_factory=dict)
    components: list[EvidenceRecord] | None = None   # the linked payout's components, when read
    payout: EvidenceRecord | None = None
    settlement: EvidenceRecord | None = None


@dataclass
class _Match:
    """Result of matching one credit against the payout candidates."""

    tried: str                         # the matching step that decided: reference | amount_date
    state: LinkState
    method: str | None
    payout: EvidenceRecord | None
    candidates: list[EvidenceRecord]   # every plausible payout
    excluded: list[EvidenceRecord]     # payouts the decision ruled out explicitly
    signals: list[str]
    summary: str


class _Run:
    """One rules run: gateway reads, rules trace events and the per-transaction cache."""

    def __init__(self, request: RequestForContext, store: Store, gateway: Any, *, run_id: str,
                 now: datetime, window: timedelta):
        self.request, self.store, self.gateway = request, store, gateway
        self.run_id, self.now, self.window = run_id, now, window
        self.events: list[dict[str, Any]] = []
        self.unavailable: list[str] = []
        self.links: dict[str, Link] = {}
        self.txns: dict[str, _Outcome] = {}
        self.combined: dict[str, _Outcome] = {}   # after the one-payout-one-credit rule
        self.bank: dict[str, BankTxn] = {}
        self.fetched: dict[str, EvidenceRecord] = {}   # payment_ref -> lookup result, reused
        self.question_id: str | None = None
        log_step = getattr(gateway, "log_step", None)
        self.log_step = log_step if callable(log_step) else None

    # ----------------------------------------------------------------------- trace and reads

    def note(self, step: str, *, reason: str | None = None, refs: Iterable[EvidenceRef] = (),
             outcome: str = "ok", tool: str | None = None, args: dict | None = None,
             result: dict | None = None) -> None:
        """Record a rules trace event. A gateway with ``log_step`` puts it in its own
        sequence, between the tool calls; otherwise it is queued and numbered after the
        gateway's events in ``finish``."""
        event = {"step": step, "question_id": self.question_id, "tool": tool,
                 "args": args or {}, "result": result or {}, "refs": _unique(refs),
                 "outcome": outcome, "reason": _clip(reason, 300) if reason else None}
        if self.log_step is not None:
            self.log_step(step, actor=ACTOR, **{k: v for k, v in event.items() if k != "step"})
        else:
            self.events.append(event)

    def read(self, tool: str, label: str, call: Callable[[], Any]) -> tuple[Any, str | None]:
        """One gateway read. A gateway error becomes a typed reason, never a crash."""
        try:
            return call(), None
        except GatewayError as exc:
            kind = _error_kind(exc)
            typed = _clip(f"{kind}: {label}", 300)
            if typed not in self.unavailable:
                self.unavailable.append(typed)
            self.note("note", tool=tool, outcome=kind, reason=f"{label} failed ({kind})",
                      args={"call": label})
            return None, kind

    # ----------------------------------------------------------------------- questions

    def answer(self, question: Question) -> Claim:
        self.question_id = question.question_id
        found = classify(question, self.request)
        how = "question id" if question.question_id in PINNED_QUESTIONS \
            and found.keywords == (question.question_id,) else "keyword"
        self.note("note", reason=f"Question classified as {found.category} by {how}.",
                  result={"category": found.category,
                          "kinds": [kind.value for kind in found.kinds],
                          "keywords": list(found.keywords)})
        if found.category == SOURCE_OF_FUNDS:
            outcome = self.source_of_funds()
        elif found.category == BREAKDOWN:
            outcome = self.breakdown()
        elif found.category == UNMAPPED:
            outcome = _Outcome(
                ClaimStatus.UNRESOLVED,
                "The rules engine did not establish an answer to this question.",
                "The question does not match a check the rules engine runs: the source of a "
                "requested transaction, how its amount breaks down, or documented sales, "
                "refunds or fees over the request's period.",
                "Question not mapped to a rules check.", gap=GAP_QUESTION_NOT_MAPPED,
                detail=UNMAPPED_GAP)
        else:
            outcome = self.period_total(found)
        rationale = outcome.rationale
        if outcome.detail and outcome.detail not in rationale:
            rationale = f"{rationale} {outcome.detail}"
        claim = Claim(
            claim_id=make_id("clm", self.run_id, question.question_id),
            request_id=self.request.request_id, audience=self.request.audience,
            question_id=question.question_id,
            statement=_clip(outcome.statement), status=outcome.status,
            rationale=_clip(rationale), evidence_refs=answered_refs(outcome.status, outcome.evidence),
            examined_refs=examined_refs(outcome.status, outcome.evidence, outcome.calculation),
            calculation=answered_calculation(outcome.status, outcome.calculation),
            gap=_clip(outcome.gap) if outcome.gap else None,
            gap_detail=_clip(outcome.detail) if outcome.detail else None,
            facts=dict(outcome.facts),
            run_id=self.run_id, created_at=self.now)
        self.note("status_change", reason=outcome.reason, refs=[*claim.evidence_refs, *claim.examined_refs],
                  result={"claim_id": claim.claim_id, "status": claim.status.value,
                          "gap": claim.gap or "none"})
        return claim

    def source_of_funds(self) -> _Outcome:
        """One outcome per requested transaction, combined: the worst status wins."""
        outcomes = [(txn_id, self.credit_outcome(txn_id)) for txn_id in self.request.txn_ids or []]
        if len(outcomes) == 1:
            return outcomes[0][1]
        status = max((outcome.status for _, outcome in outcomes), key=_RANK.__getitem__)
        deciding = next(o for _, o in outcomes if o.status == status)
        details = [f"{txn_id}: {o.detail}" for txn_id, o in outcomes if o.detail]
        facts: dict[str, Any] = {}
        if status == ClaimStatus.SUPPORTED:
            facts = {"payout_references": [r for _, o in outcomes
                                           for r in o.facts.get("payout_references", [])],
                     "settled_at": [t for _, o in outcomes for t in o.facts.get("settled_at", [])]}
        return _Outcome(
            status, " ".join(o.statement for _, o in outcomes),
            " ".join(f"{txn_id}: {o.rationale}" for txn_id, o in outcomes),
            "; ".join(f"{txn_id} {o.status.value}" for txn_id, o in outcomes),
            gap=deciding.gap if status != ClaimStatus.SUPPORTED else None,
            detail=" ".join(details) or None,
            evidence=[ref for _, o in outcomes for ref in o.evidence], facts=facts)

    def breakdown(self) -> _Outcome:
        """How each requested credit's amount is made up: the reconciliation of the linked
        payout's components when the credit's source is supported, else unresolved with the
        source-of-funds gap. ``breakdown_minor`` facts are stated for a single credit."""
        outcomes = [(txn_id, self.breakdown_outcome(txn_id))
                    for txn_id in self.request.txn_ids or []]
        if len(outcomes) == 1:
            return outcomes[0][1]
        status = max((outcome.status for _, outcome in outcomes), key=_RANK.__getitem__)
        deciding = next(o for _, o in outcomes if o.status == status)
        details = [f"{txn_id}: {o.detail}" for txn_id, o in outcomes if o.detail]
        return _Outcome(
            status, " ".join(o.statement for _, o in outcomes),
            " ".join(f"{txn_id}: {o.rationale}" for txn_id, o in outcomes),
            "; ".join(f"{txn_id} {o.status.value}" for txn_id, o in outcomes),
            gap=deciding.gap if status != ClaimStatus.SUPPORTED else None,
            detail=" ".join(details) or None,
            evidence=[ref for _, o in outcomes for ref in o.evidence])

    def breakdown_outcome(self, txn_id: str) -> _Outcome:
        base = self.credit_outcome(txn_id)
        txn = self.bank.get(txn_id)
        if base.status == ClaimStatus.SUPPORTED and base.calculation is not None \
                and base.components is not None and base.payout is not None and txn is not None:
            calc, payout = base.calculation, base.payout
            sums = {kind: sum(r.amount_pence or 0 for r in base.components if r.kind == kind)
                    for kind in COMPONENT_KINDS}
            minor = {"sales": sums[RecordKind.ORDER], "refunds": sums[RecordKind.REFUND],
                     "fees": sums[RecordKind.FEE], "net": calc.total_pence}
            statement = (f"Credit {txn_id} of {_money(txn.amount_pence, txn.currency)} is made up "
                         f"of the records covered by payout {payout.record_id} (reference "
                         f"{payout.payment_ref or 'none'}): sales {minor['sales']} less refunds "
                         f"{minor['refunds']} and fees {minor['fees']} = {minor['net']} pence "
                         f"{calc.currency}, reconciled in code against the credit.")
            return _Outcome(ClaimStatus.SUPPORTED, statement, base.rationale,
                            "The breakdown reconciles to the supported credit.",
                            evidence=list(base.evidence), calculation=calc,
                            facts={"breakdown_minor": minor}, components=base.components,
                            payout=payout, settlement=base.settlement)
        amount = _money(txn.amount_pence, txn.currency) if txn is not None else None
        statement = (f"The breakdown of credit {txn_id}"
                     + (f" of {amount}" if amount else "")
                     + " is not established from the available records.")
        detail = ("The breakdown cannot be verified because the credit's source is not "
                  f"established: {base.detail or base.rationale}")
        return _Outcome(ClaimStatus.UNRESOLVED, statement, base.rationale,
                        f"Breakdown follows the source of funds: {base.reason}",
                        gap=base.gap or GAP_SETTLEMENT_NOT_FOUND, detail=detail,
                        evidence=list(base.evidence), calculation=base.calculation)

    def credit_outcome(self, txn_id: str) -> _Outcome:
        """The source-of-funds outcome for one credit, after the one-payout-one-credit rule.
        Every requested transaction is analysed first, so the rule sees all of this run's
        links whichever credit is asked about. The combined outcome is computed once per
        credit, so the rule's trace note is written once however many questions ask."""
        if txn_id not in self.combined:
            for other in self.request.txn_ids or []:
                self.txn_outcome(other)
            self.combined[txn_id] = self.one_payout_one_credit(txn_id, self.txn_outcome(txn_id))
        return self.combined[txn_id]

    def one_payout_one_credit(self, txn_id: str, outcome: _Outcome) -> _Outcome:
        """A supported outcome whose payout is also linked to another credit, in this run or
        by an earlier active link of the subject, becomes unresolved: one payout cannot fund
        two credits, and the evidence fits both.

        Credits of this request are named (the recipient asked about them); credits linked by
        an earlier request are only counted, because they may belong to another recipient and
        the claim, its gap detail and its rationale travel in a signed package."""
        link = self.links.get(txn_id)
        if outcome.status != ClaimStatus.SUPPORTED or link is None or link.record_ref is None:
            return outcome
        payout = link.record_ref.ref_id
        others = [other for other, seen in self.links.items() if other != txn_id
                  and seen.record_ref is not None and seen.record_ref.ref_id == payout]
        stored = self.competing_links(payout, exclude={txn_id, *self.links})
        if not others and not stored:
            return outcome
        where = []
        if others:
            where.append(f"credit{'s' if len(others) != 1 else ''} {', '.join(others)} in this "
                         "request")
        if stored:
            where.append("another credit of the same account holder in an earlier request"
                         if len(stored) == 1 else
                         f"{len(stored)} other credits of the same account holder in earlier "
                         "requests")
        detail = (f"Payout {payout} is also matched to {' and to '.join(where)}; one payout "
                  f"cannot fund more than one credit, so it does not establish the source of "
                  f"{txn_id}.")
        self.note("validation", reason=_clip(detail, 300), refs=[link.txn_ref, link.record_ref],
                  result={"record_id": payout, "other_txn_ids": others,
                          "earlier_links": len(stored)})
        return replace(outcome, status=ClaimStatus.UNRESOLVED,
                       statement=_unknown_source(txn_id, self.bank.get(txn_id)),
                       rationale=f"{outcome.rationale} {detail}",
                       reason="The linked payout is matched to more than one credit.",
                       gap=GAP_PAYOUT_ALREADY_LINKED, detail=detail, facts={})

    def competing_links(self, record_id: str, *, exclude: set[str]) -> list[str]:
        """Transactions other than ``exclude`` whose active link in the store names
        ``record_id`` (a record id is unique in the passport, so these are the subject's own
        credits, from any earlier request)."""
        try:
            links = self.store.list_links(state="active")
        except StoreError:
            return []
        return sorted({l.txn_ref.ref_id for l in links
                       if l.record_ref is not None and l.record_ref.ref_id == record_id
                       and l.txn_ref.ref_id not in exclude})

    # ----------------------------------------------------------------------- source of funds

    def txn_outcome(self, txn_id: str) -> _Outcome:
        if txn_id not in self.txns:
            self.txns[txn_id] = self.analyse(txn_id)
        return self.txns[txn_id]

    def analyse(self, txn_id: str) -> _Outcome:
        txn, err = self.read("get_txn", f"get_txn {txn_id}", lambda: self.gateway.get_txn(txn_id))
        if txn is None:
            gap = f"Bank transaction {txn_id} could not be read ({err})."
            return _Outcome(ClaimStatus.UNRESOLVED, _unknown_source(txn_id), gap, gap,
                            gap=GAP_EVIDENCE_UNAVAILABLE, detail=gap)
        if txn.subject_ref != self.request.subject_ref:
            gap = f"Bank transaction {txn_id} belongs to another subject, so it was not used."
            return _Outcome(ClaimStatus.UNRESOLVED, _unknown_source(txn_id), gap, gap,
                            gap=GAP_EVIDENCE_UNAVAILABLE, detail=gap)
        self.bank[txn_id] = txn
        if not txn.is_credit:
            gap = (f"Transaction {txn_id} is a debit (money out); the rules engine explains the "
                   "source of credits only.")
            return _Outcome(ClaimStatus.UNRESOLVED, _unknown_source(txn_id, txn), gap, gap,
                            gap=GAP_NOT_A_CREDIT, detail=gap, evidence=[txn.ref()])
        found, err = self.read("list_candidates", f"list_candidates {txn_id}",
                               lambda: self.gateway.list_candidates(txn_id, window=self.window))
        if found is None:
            gap = f"Payout candidates for credit {txn_id} could not be listed ({err})."
            return _Outcome(ClaimStatus.UNRESOLVED, _unknown_source(txn_id, txn), gap, gap,
                            gap=GAP_EVIDENCE_UNAVAILABLE, detail=gap, evidence=[txn.ref()])
        found = list(found)
        match = _match(txn, found, self.window, self.settlement_refs(txn, found))
        if match.payout is None and match.state == LinkState.UNMATCHED and not match.excluded:
            fetched = self.fetch_by_credit_reference(txn, found)
            if fetched is not None:
                found.append(fetched)
                match = _match(txn, found, self.window)
        link = Link(
            link_id=make_id("lnk", self.run_id, txn.txn_id), txn_ref=txn.ref(),
            link_state=match.state, record_ref=match.payout.ref() if match.payout else None,
            candidates=[p.ref() for p in match.candidates], method=match.method,
            signals=[_clip(s, 300) for s in match.signals[:12]], run_id=self.run_id,
            created_at=self.now)
        self.links[txn.txn_id] = link
        self.note("validation", tool="list_candidates", reason=_clip(match.summary, 300),
                  refs=[txn.ref(), *link.candidates, *(p.ref() for p in match.excluded)],
                  args={"txn_id": txn.txn_id, "window": _window_text(self.window)},
                  result={"link_id": link.link_id, "link_state": match.state.value,
                          "method": match.method or "none",
                          "record_id": match.payout.record_id if match.payout else "none",
                          "candidates": [p.record_id for p in match.candidates]})
        if match.payout is None:
            return self.unlinked(txn, match)
        return self.linked(txn, match)

    def fetch_by_credit_reference(self, txn: BankTxn, found: list[EvidenceRecord]
                                  ) -> EvidenceRecord | None:
        """When no payout in the passport matches, ask the settlement lookup for the references
        the credit itself carries (explicit ``refs`` first, then description tokens), at most
        ``MAX_REFERENCE_LOOKUPS`` of them. A payout record returned for one of them, for this
        subject and carrying that reference, is the candidate; it is cached so the settlement
        step reuses it instead of asking again. The gateway decides whether each reference is
        this subject's to ask about."""
        known = {p.payment_ref.upper() for p in found if p.payment_ref}
        tokens = [ref.upper() for ref in txn.refs]
        tokens += sorted(reference_tokens(txn) - set(tokens))
        tried = 0
        for token in tokens:
            if tried >= MAX_REFERENCE_LOOKUPS:
                break
            if token in known or not _is_ref(token):
                continue
            tried += 1
            record, err = self.read("lookup_settlement", f"lookup_settlement {token}",
                                    lambda token=token: self.gateway.lookup_settlement(token))
            if record is None:
                if err is None:
                    self.note("note", tool="lookup_settlement",
                              reason=f"The lookup for {token}, a reference on the credit, "
                                     "returned no record.", args={"payment_ref": token})
                continue
            record = self.current(record)
            if record.subject_ref != self.request.subject_ref or not record.payment_ref:
                continue
            self.fetched[record.payment_ref] = record
            if record.kind == RecordKind.PAYOUT and record.payment_ref.upper() == token:
                self.note("validation", tool="lookup_settlement", refs=[txn.ref(), record.ref()],
                          reason=(f"The lookup for {token}, a reference on the credit, returned "
                                  f"payout {record.ref().key()}, which is taken as a candidate."),
                          args={"payment_ref": token},
                          result={"record_id": record.record_id, "revision": record.revision,
                                  "kind": record.kind.value, "status": record.status or "none"})
                return record
        return None

    def unlinked(self, txn: BankTxn, match: _Match) -> _Outcome:
        if match.state == LinkState.AMBIGUOUS:
            how = "by reference" if match.method == METHOD_REFERENCE else "by amount and date"
            gap = (f"{len(match.candidates)} payout records fit this credit equally {how}; the "
                   "rules engine cannot choose between them.")
            evidence = [txn.ref(), *(p.ref() for p in match.candidates)]
            reason = f"Link ambiguous: {len(match.candidates)} candidates."
            code = GAP_AMBIGUOUS
        else:
            if match.tried == METHOD_REFERENCE:
                gap = ("The payout named by the credit's reference cannot explain it (it occurred "
                       "after the booking or is in another currency), and no other payout "
                       "carries a reference on the credit.")
                code = (GAP_SETTLEMENT_AFTER if any(p.occurred_at > txn.booked_at
                                                    for p in match.excluded)
                        else GAP_AMOUNT_MISMATCH)
            else:
                gap = (f"No payout record matches this credit by reference, or by amount and "
                       f"currency within {_window_text(self.window)} before its booking.")
                if match.excluded:
                    gap += (f" {len(match.excluded)} payout record"
                            f"{'s' if len(match.excluded) != 1 else ''} with the same amount "
                            "occurred after the booking and cannot explain it.")
                code = GAP_SETTLEMENT_NOT_FOUND
            evidence = [txn.ref(), *(p.ref() for p in match.excluded)]
            reason = "No payout matched the credit."
        return _Outcome(ClaimStatus.UNRESOLVED, _unknown_source(txn.txn_id, txn), match.summary,
                        reason, gap=code, detail=gap, evidence=evidence)

    def linked(self, txn: BankTxn, match: _Match) -> _Outcome:
        payout = match.payout
        assert payout is not None
        ref = payout.payment_ref
        statement = (f"Credit {txn.txn_id} of {_money(txn.amount_pence, txn.currency)} is payout "
                     f"{payout.record_id} (reference {ref or 'none'})")
        parts = [match.summary]
        settlement, path, notes = self.settlement_for(payout, txn)
        parts += notes
        self.note("note", tool="lookup_settlement",
                  reason=f"Settlement path: {path}." + (f" {notes[-1]}" if notes else ""),
                  refs=[payout.ref(), *([settlement.ref()] if settlement else [])],
                  result={"path": path, "payment_ref": ref or "none",
                          "settlement": settlement.ref().key() if settlement else "none"})
        evidence = [txn.ref(), payout.ref(), *([settlement.ref()] if settlement else [])]
        own = _self_supplied(payout, settlement)
        if own:
            parts.append(f"Trust: {_listing(own)} {'is' if len(own) == 1 else 'are'} "
                         "self-supplied (imported by the account holder without an issuer "
                         "signature).")
        # An explicit status or timing conflict needs no component records, so a missing
        # component or an amount that cannot be reconciled never hides it.
        conflict = _conflict(txn, payout, settlement)
        components, problem_code, problem = self.components(payout)
        calc = None
        if components is not None:
            evidence += [record.ref() for record in components]
            try:
                calc = reconcile.reconcile_credit(txn, components=components, payout=payout,
                                                  settlement=settlement, window=self.window)
            except ReconcileError as exc:
                problem_code, problem = GAP_AMOUNT_MISMATCH, \
                    f"The amounts could not be reconciled: {exc}"
        if calc is not None:
            parts.append(_calc_text(calc, components, payout, settlement))
            self.note("calculation", reason=_clip(parts[-1], 300), refs=calc.cited_refs,
                      result=_calc_summary(calc))
        if settlement is not None:
            parts.append(_timing_text(settlement, txn, self.window))
        if problem:
            parts.append(problem)
        base = dict(evidence=evidence, calculation=calc, components=components, payout=payout,
                    settlement=settlement)
        if conflict is not None:
            reason, code, gap = conflict
            return _Outcome(ClaimStatus.CONTRADICTED, statement + ".", " ".join(parts), reason,
                            gap=code, detail=" ".join(filter(None, [gap, problem])), **base)
        gaps = [_no_settlement_gap(payout, notes)] if settlement is None else []
        if problem:
            return _Outcome(ClaimStatus.UNRESOLVED, statement + ".", " ".join(parts), problem,
                            gap=problem_code, detail=" ".join(gaps + [problem]), **base)
        if settlement is None:
            code = GAP_BALANCE_RELEASE if payout.status == RELEASED_STATUS \
                else GAP_SETTLEMENT_NOT_FOUND
            return _Outcome(ClaimStatus.UNRESOLVED, statement + ".", " ".join(parts),
                            "No settlement record for the linked payout.", gap=code,
                            detail=" ".join(gaps), **base)
        assert calc is not None
        status, reason, code, gap, refs_text = _verdict(txn, payout, settlement, calc, self.window)
        if refs_text:
            parts.append(refs_text)
        if status == ClaimStatus.SUPPORTED and own:
            status = ClaimStatus.UNRESOLVED
            reason = "Self-supplied evidence is not issuer evidence."
            named = _listing(own)
            code = GAP_SETTLEMENT_NOT_FOUND
            gap = (f"{named[:1].upper()}{named[1:]} {'is' if len(own) == 1 else 'are'} "
                   "self-supplied (imported by the account holder without an issuer signature), "
                   "so the source of funds is not established from issuer evidence.")
        facts: dict[str, Any] = {}
        if status == ClaimStatus.SUPPORTED:
            effective = calc.timing.settlement_effective_at if calc.timing else None
            settled = (f"settled (status {settlement.status}) before the credit was booked"
                       if settlement.record_id == payout.record_id
                       else f"settled as {settlement.record_id} before the credit was booked")
            statement += f", {settled}"
            facts = {"payout_references": [ref] if ref else [],
                     "settled_at": [format_utc(effective)] if effective is not None else []}
        return _Outcome(status, statement + ".", " ".join(parts), reason, gap=code, detail=gap,
                        facts=facts, **base)

    def settlement_for(self, payout: EvidenceRecord, txn: BankTxn
                       ) -> tuple[EvidenceRecord | None, str, list[str]]:
        """Fixed retrieval: the settlement lookup for the payout's payment reference (reusing a
        result already fetched for the credit's reference), then every active settlement with
        that reference already in the passport, plus the payout itself when its own status is
        decisive, so a record the passport holds is never hidden. Issuer-verified records come
        first (a self-supplied record never overrides one); among them a failed status wins,
        else the most recently observed record. Returns (settlement or None, path "lookup" |
        "passport" | "none", notes)."""
        ref = payout.payment_ref
        if ref is None:
            return None, "none", [f"Payout {payout.record_id} carries no payment reference, so "
                                  "no settlement record can be retrieved for it."]
        notes: list[str] = []
        cached = self.fetched.get(ref)
        if cached is not None:
            found, err = cached, None
            notes.append(f"The lookup for {ref} made for the credit's reference is reused.")
        else:
            found, err = self.read("lookup_settlement", f"lookup_settlement {ref}",
                                   lambda: self.gateway.lookup_settlement(ref))
        looked_up = None
        if err is not None:
            notes.append(f"The settlement lookup for {ref} failed ({err}).")
        elif found is None:
            notes.append(f"The settlement lookup for {ref} returned no record.")
        elif not self.settles(found, payout, txn):
            notes.append(f"The settlement lookup for {ref} returned {found.ref().key()}, which is "
                         f"not settlement evidence for {ref} from {payout.issuer_id}, so it is "
                         "not used.")
        else:
            looked_up = self.current(found)
            if not self.settles(looked_up, payout, txn):
                notes.append(f"{found.ref().key()} is superseded by a revision that no longer "
                             f"settles {ref}.")
                looked_up = None
        records, err = self.read("list_records", "list_records settlement",
                                 lambda: self.gateway.list_records(kinds=[RecordKind.SETTLEMENT]))
        if records is None:
            notes.append(f"Settlement records in the passport could not be listed ({err}).")
        own_status = [payout] if self.settles(payout, payout, txn) else []
        pool: dict[str, EvidenceRecord] = {}
        for record in [*([looked_up] if looked_up else []),
                       *(r for r in records or [] if self.settles(r, payout, txn)), *own_status]:
            held = pool.get(record.record_id)
            if held is None or record.revision > held.revision:
                pool[record.record_id] = record
        if not pool:
            notes.append(f"No settlement record for {ref} is in the passport.")
            return None, "none", notes
        best, others, set_aside = _choose_settlement(list(pool.values()))
        if looked_up is not None and best.record_id == looked_up.record_id:
            path = "lookup"
            notes.append(f"Settlement {best.ref().key()} retrieved by follow-up lookup for {ref}.")
        elif best.record_id == payout.record_id:
            path = "passport"
            notes.append(f"Payout {best.ref().key()} carries its own settlement status "
                         f"({best.status}) and is used as settlement evidence.")
        else:
            path = "passport"
            notes.append(f"Settlement {best.ref().key()} was already in the passport and is used.")
        if others:
            why = ("a failed status outweighs the others" if best.status in FAILED_STATUSES
                   else "the most recently observed one is used")
            notes.append(f"Other settlement records for {ref}: "
                         + _listing([f"{r.ref().key()} (status {r.status or 'none'})"
                                     for r in others]) + f"; {why}.")
        if set_aside:
            notes.append(f"Self-supplied settlement record{'s' if len(set_aside) != 1 else ''} "
                         f"{_ids(set_aside)} set aside: issuer-verified settlement evidence for "
                         f"{ref} exists.")
        return best, path, notes

    def settles(self, record: EvidenceRecord, payout: EvidenceRecord, txn: BankTxn) -> bool:
        """Is ``record`` settlement evidence for ``payout``? It must be this subject's, carry
        the payout's payment reference and come from the payout's issuer (another issuer's
        signed record never shows this payout settled; a self-supplied record carries no issuer
        attestation, so it is considered and then set aside as self-supplied). A settlement
        record always qualifies; a payout
        record qualifies when its own status is decisive: settled, failed, or pending when
        observed at or after the credit's booking (a pending status observed earlier is just
        the payout's initial state, not evidence either way)."""
        if record.subject_ref != self.request.subject_ref or record.payment_ref is None \
                or record.payment_ref != payout.payment_ref:
            return False
        if record.source_trust != SourceTrust.SELF_SUPPLIED and record.issuer_id != payout.issuer_id:
            return False
        if record.kind == RecordKind.SETTLEMENT:
            return True
        if record.kind != RecordKind.PAYOUT or record.status is None:
            return False
        return (record.status in SETTLED_STATUSES or record.status in FAILED_STATUSES
                or (record.status in PENDING_STATUSES and record.occurred_at >= txn.booked_at))

    def current(self, record: EvidenceRecord) -> EvidenceRecord:
        """The active revision of a record the gateway returned: a lookup can hand back a
        revision that a later correction already superseded in the passport."""
        try:
            active = self.store.get_source(record.record_id)
        except StoreError:
            return record
        if active.revision <= record.revision or active.subject_ref != record.subject_ref:
            return record
        self.note("validation", reason=(f"{record.ref().key()} is superseded: revision "
                                        f"{active.revision} is active and is used instead."),
                  refs=[record.ref(), active.ref()],
                  result={"returned": record.ref().key(), "active": active.ref().key()})
        return active

    def components(self, payout: EvidenceRecord
                   ) -> tuple[list[EvidenceRecord] | None, str | None, str]:
        """The payout's component records (its ``related_record_ids``), picked from one
        listing of the subject's active orders, refunds and fees, so a bundled payout costs
        one call however many components it has. Returns (records or None, gap code, problem)."""
        wanted = list(dict.fromkeys(payout.related_record_ids))
        if not wanted:
            return [], None, ""
        records, err = self.read(
            "list_records", f"list_records components of {payout.record_id}",
            lambda: self.gateway.list_records(kinds=list(COMPONENT_KINDS)))
        if records is None:
            return None, GAP_EVIDENCE_UNAVAILABLE, (
                f"The component records of payout {payout.record_id} could not be listed ({err}).")
        held = {r.record_id: r for r in records
                if r.kind in COMPONENT_KINDS and r.subject_ref == self.request.subject_ref}
        missing = [record_id for record_id in wanted if record_id not in held]
        if missing:
            return None, GAP_MISSING_CITED, (
                f"Component record{'s' if len(missing) != 1 else ''} "
                f"{_listing(missing)} of payout {payout.record_id} "
                f"{'is' if len(missing) == 1 else 'are'} not among the passport's "
                "active order, refund and fee records.")
        return [held[record_id] for record_id in wanted], None, ""

    def settlement_refs(self, txn: BankTxn, found: Iterable[EvidenceRecord]
                        ) -> list[EvidenceRecord]:
        """Ordinary reference parsing through the passport: when no payout's payment
        reference is on the credit but the credit carries other references, the
        issuer-verified settlements already in the passport whose settlement reference it
        carries (each names its payout's payment reference)."""
        tokens = reference_tokens(txn)
        if not tokens or any(r.kind == RecordKind.PAYOUT and r.subject_ref == txn.subject_ref
                             and r.payment_ref and r.payment_ref.upper() in tokens
                             for r in found):
            return []
        records, _ = self.read("list_records", "list_records settlement",
                               lambda: self.gateway.list_records(kinds=[RecordKind.SETTLEMENT]))
        return [r for r in records or []
                if r.kind == RecordKind.SETTLEMENT and r.subject_ref == txn.subject_ref
                and r.source_trust == SourceTrust.ISSUER_VERIFIED and r.payment_ref
                and r.settlement_ref and r.settlement_ref.upper() in tokens]

    # ----------------------------------------------------------------------- period totals

    def period_total(self, found: Classification) -> _Outcome:
        """One claim for a period question: a single kind's total, a net total (signed terms)
        when the question asks for one, or one documented total per kind
        (``documented_totals``), each stated with its record count and carried in ``facts``."""
        period = self.request.period
        assert period is not None
        span = f"[{format_utc(period.from_)}, {format_utc(period.to)})"
        label = _join([_LABELS[kind] for kind in found.kinds])
        unknown = f"Documented {label} over {span} are not established from the available records."
        records, err = self.read(
            "list_records", f"list_records {'/'.join(k.value for k in found.kinds)} {span}",
            lambda: self.gateway.list_records(kinds=list(found.kinds), period=period))
        if records is None:
            gap = f"The {label} records for this period could not be retrieved ({err})."
            return _Outcome(ClaimStatus.UNRESOLVED, unknown, gap, gap,
                            gap=GAP_EVIDENCE_UNAVAILABLE, detail=gap)
        records = sorted((r for r in records if r.kind in found.kinds
                          and r.subject_ref == self.request.subject_ref
                          and period.contains(r.occurred_at)),
                         key=lambda r: (r.occurred_at, r.record_id, r.revision))
        counted, skipped, unclear = [], [], []
        for r in records:
            if r.status is None or r.status in COUNTED_STATUSES[r.kind]:
                counted.append(r)
            elif r.status in EXCLUDED_STATUSES:
                skipped.append(r)
            else:
                unclear.append(r)
        skipped_text = ""
        if skipped:
            skipped_text = ("Not counted: "
                            + _listing([f"{r.record_id} (status {r.status})" for r in skipped])
                            + ".")
        if unclear:
            gap = (f"The status of {_listing([f'{r.record_id} ({r.status})' for r in unclear])} "
                   f"does not show whether {'it' if len(unclear) == 1 else 'they'} went "
                   f"through, so the documented {label} total over {span} is not established.")
            return _Outcome(ClaimStatus.UNRESOLVED, unknown,
                            " ".join(filter(None, [gap, skipped_text])),
                            f"{len(unclear)} record{'s' if len(unclear) != 1 else ''} with an "
                            "unrecognised status.", gap=GAP_EVIDENCE_UNAVAILABLE, detail=gap,
                            evidence=[r.ref() for r in unclear])
        if not counted:
            gap = " ".join(filter(None, [f"No documented {label} records are in the passport "
                                         f"for {span}.", skipped_text]))
            return _Outcome(ClaimStatus.UNRESOLVED, unknown, gap, f"No {label} records found.",
                            gap=GAP_NO_RECORDS, detail=gap)
        net = found.category == NET_TOTAL
        several = found.category == DOCUMENTED_TOTALS or (len(found.kinds) > 1 and not net)
        method = DOCUMENTED_TOTALS if several else found.category
        signs = SIGN_BY_KIND if (net or several) else {found.kinds[0]: 1}
        try:
            calc = reconcile.sum_terms(counted, method=method, signs=signs)
        except ReconcileError as exc:
            gap = f"The {label} records could not be summed: {exc}"
            return _Outcome(ClaimStatus.UNRESOLVED, unknown, gap, gap,
                            gap=GAP_AMOUNT_MISMATCH, detail=gap,
                            evidence=[r.ref() for r in counted])
        total = _money(calc.total_pence, calc.currency)
        groups = [(kind, [r for r in counted if r.kind == kind]) for kind in found.kinds]
        subtotals = [f"{_LABELS[kind]} {sum(r.amount_pence for r in rows)} from {len(rows)} "
                     f"record{'s' if len(rows) != 1 else ''} ({_amounts(rows)})" if rows
                     else f"{_LABELS[kind]} 0 (no counted records)"
                     for kind, rows in groups]
        facts: dict[str, Any] = {}
        for kind, rows in groups:
            amount_key, count_key = _FACT_KEYS[kind]
            facts[amount_key] = sum(r.amount_pence for r in rows)
            facts[count_key] = len(rows)
        if net:
            formula = " ".join(f"{'+' if signs[kind] > 0 else '-'} {_LABELS[kind]} "
                               f"{sum(r.amount_pence for r in rows)}" for kind, rows in groups)
            statement = f"Documented {label} over {span} net to {total} ({formula.lstrip('+ ')})."
            facts["net_minor"] = calc.total_pence
        elif several:
            stated = [f"{_LABELS[kind]} total {_money(sum(r.amount_pence for r in rows), calc.currency)} "
                      f"across {len(rows)} record{'s' if len(rows) != 1 else ''}"
                      for kind, rows in groups]
            formula = " ".join(f"{'+' if signs[kind] > 0 else '-'} {_LABELS[kind]}"
                               for kind, rows in groups).lstrip("+ ")
            statement = (f"Documented {_join(stated)} over {span}; each total is stated "
                         f"separately, and the signed sum of the same records ({formula}) is "
                         f"{total}.")
        else:
            statement = (f"Documented {label} over {span} total {total} across {len(counted)} "
                         f"record{'s' if len(counted) != 1 else ''}.")
        rationale = [f"Summed in code from the passport records dated in {span}: "
                     + "; ".join(subtotals) + f". Total {total}."]
        own = [r for r in counted if r.source_trust == SourceTrust.SELF_SUPPLIED]
        if own:
            rationale.append(f"Includes {len(own)} self-supplied record"
                             f"{'s' if len(own) != 1 else ''} ({_ids(own)}) imported by the "
                             "account holder without an issuer signature.")
        if skipped_text:
            rationale.append(skipped_text)
        self.note("calculation", reason=_clip(f"{method}: {total}", 300),
                  refs=calc.cited_refs, result=_calc_summary(calc))
        return _Outcome(ClaimStatus.SUPPORTED, statement, " ".join(rationale),
                        f"{len(counted)} documented records sum to {total}.",
                        evidence=list(calc.cited_refs), calculation=calc, facts=facts)

    # ----------------------------------------------------------------------- persistence

    def finish(self, claims: list[Claim]) -> InvestigationResult:
        """Save links, claims and both traces in one transaction, then return the stored
        versions (a result citing a superseded revision is stored already invalidated)."""
        gateway_events = [TraceEvent.model_validate(event)
                          for event in getattr(self.gateway, "trace", [])]
        base = max((event.seq for event in gateway_events), default=-1) + 1
        rules_events = [
            TraceEvent(trace_id=make_id("trc", self.run_id, ACTOR, index), run_id=self.run_id,
                       seq=base + index, at=self.now, actor=ACTOR,
                       request_id=self.request.request_id, **event)
            for index, event in enumerate(self.events)]
        with self.store.transaction():
            for link in self.links.values():
                self.store.save_link(link)
            for claim in claims:
                self.store.save_claim(claim)
            flush = getattr(self.gateway, "flush_trace", None)
            if callable(flush):
                flush(self.store)
            else:
                self.store.append_traces(gateway_events)
            self.store.append_traces(rules_events)
        stored = {c.claim_id: c for c in
                  self.store.list_claims(request_id=self.request.request_id,
                                         audience=self.request.audience, state=None)}
        links = []
        for link in self.links.values():
            saved = {l.link_id: l for l in self.store.list_links(txn_id=link.txn_ref.ref_id,
                                                                  state=None)}
            links.append(saved.get(link.link_id, link))
        return InvestigationResult(
            run_id=self.run_id, request_id=self.request.request_id, mode=MODE, links=links,
            claims=[stored.get(claim.claim_id, claim) for claim in claims],
            trace=[*gateway_events, *rules_events],
            calls_used=int(getattr(self.gateway, "calls_used", 0)),
            unavailable=list(self.unavailable))


_RANK = {ClaimStatus.SUPPORTED: 0, ClaimStatus.UNRESOLVED: 1, ClaimStatus.CONTRADICTED: 2}


# --------------------------------------------------------------------------- matching


def _match(txn: BankTxn, found: Iterable[EvidenceRecord], window: timedelta,
           settlements: Iterable[EvidenceRecord] = ()) -> _Match:
    """Match a credit to payouts: by a reference it carries, else by amount and date.

    A reference is a payout's payment reference, or (when the credit carries none) the
    settlement reference of one of ``settlements``, which names its payout's payment
    reference and comes from the payout's issuer. Only a unique compatible candidate links:
    the same currency, and occurred at or before the booking (a payout after the booking never
    matches). When the credit's reference names payouts, amounts never override it. Amount and
    date need the same amount and currency within ``window`` before the booking.
    """
    payouts = sorted((r for r in found if r.kind == RecordKind.PAYOUT
                      and r.subject_ref == txn.subject_ref),
                     key=lambda r: (r.occurred_at, r.record_id, r.revision))
    tokens = reference_tokens(txn)
    booked = format_utc(txn.booked_at)
    amount = _money(txn.amount_pence, txn.currency)
    carried = {p.record_id: f"{p.payment_ref}, the payment reference of"
               for p in payouts if p.payment_ref and p.payment_ref.upper() in tokens}
    if not carried:
        for s in settlements:
            for p in payouts:
                if p.payment_ref and p.payment_ref == s.payment_ref \
                        and p.issuer_id == s.issuer_id:
                    carried.setdefault(p.record_id, (
                        f"{s.settlement_ref}, the settlement reference of {s.ref().key()} for "
                        f"{p.payment_ref}, the payment reference of"))
    named = [p for p in payouts if p.record_id in carried]
    if named:
        fits = [p for p in named if _compatible(p, txn)]
        excluded = [p for p in named if not _compatible(p, txn)]
        signals = [f"The credit carries {carried[p.record_id]} {p.ref().key()}." for p in named]
        signals += [f"{p.ref().key()} cannot explain the credit: {_incompatible(p, txn)}."
                    for p in excluded]
        if len(fits) == 1:
            p = fits[0]
            summary = (f"Matched by reference: the credit carries {carried[p.record_id]} payout "
                       f"{p.ref().key()} ({_money(p.amount_pence, p.currency)}, occurred "
                       f"{format_utc(p.occurred_at)}).")
            return _Match(METHOD_REFERENCE, LinkState.LINKED, METHOD_REFERENCE, p, fits, excluded,
                          signals, summary)
        if fits:
            summary = (f"Ambiguous by reference: the credit's references name "
                       f"{len(fits)} payouts ({_ids(fits, keys=True)}).")
            return _Match(METHOD_REFERENCE, LinkState.AMBIGUOUS, METHOD_REFERENCE, None, fits,
                          excluded, signals, summary)
        summary = ("Not matched: the credit's reference names "
                   + "; ".join(f"{p.ref().key()}, which {_incompatible(p, txn)}" for p in excluded)
                   + ". Amounts are not used to override the credit's own reference.")
        return _Match(METHOD_REFERENCE, LinkState.UNMATCHED, None, None, [], excluded, signals,
                      summary)
    fits = [p for p in payouts if p.amount_pence == txn.amount_pence and p.currency == txn.currency
            and p.occurred_at <= txn.booked_at and txn.booked_at - p.occurred_at <= window]
    late = [p for p in payouts if p.amount_pence == txn.amount_pence
            and p.currency == txn.currency and p.occurred_at > txn.booked_at]
    signals = [f"{len(payouts)} payout record{'s' if len(payouts) != 1 else ''} within "
               f"{_window_text(window)} of the booking; none carries a reference on the credit."]
    signals += [f"{p.ref().key()} has the credit's amount but occurred after the booking "
                f"({format_utc(p.occurred_at)}), so it cannot explain it." for p in late]
    if len(fits) == 1:
        p = fits[0]
        summary = (f"Matched by amount and date: payout {p.ref().key()} (reference "
                   f"{p.payment_ref or 'none'}, occurred {format_utc(p.occurred_at)}) is the only "
                   f"payout of {amount} within {_window_text(window)} before the booking at "
                   f"{booked}; the credit carries no payout reference.")
        return _Match(METHOD_AMOUNT_DATE, LinkState.LINKED, METHOD_AMOUNT_DATE, p, fits, late,
                      signals, summary)
    if fits:
        listed = "; ".join(f"{p.ref().key()} (reference {p.payment_ref or 'none'}, occurred "
                           f"{format_utc(p.occurred_at)})" for p in fits[:12])
        summary = (f"Ambiguous by amount and date: {len(fits)} payouts of {amount} fall within "
                   f"{_window_text(window)} before the booking at {booked} ({listed}), and the "
                   "credit carries no payout reference to choose between them.")
        return _Match(METHOD_AMOUNT_DATE, LinkState.AMBIGUOUS, METHOD_AMOUNT_DATE, None, fits,
                      late, signals, summary)
    summary = (f"Not matched: no payout of {amount} occurred within {_window_text(window)} "
               f"before the booking at {booked}, and the credit carries no payout reference.")
    if late:
        summary += (f" {_ids(late, keys=True)} has the same amount but occurred after the "
                    "booking, so it cannot explain the credit.")
    return _Match(METHOD_AMOUNT_DATE, LinkState.UNMATCHED, None, None, [], late, signals, summary)


def _compatible(payout: EvidenceRecord, txn: BankTxn) -> bool:
    return payout.currency == txn.currency and payout.occurred_at <= txn.booked_at


def _incompatible(payout: EvidenceRecord, txn: BankTxn) -> str:
    if payout.currency != txn.currency:
        return f"is in {payout.currency}, not {txn.currency}"
    return (f"occurred at {format_utc(payout.occurred_at)}, after the booking at "
            f"{format_utc(txn.booked_at)}")


# --------------------------------------------------------------------------- verdict


def _conflict(txn: BankTxn, payout: EvidenceRecord,
              settlement: EvidenceRecord | None) -> tuple[str, str, str] | None:
    """An explicit field that conflicts with the claim that the payout funded the credit, as
    (short reason, gap code, gap sentence), else None: a failed settlement, a settlement still
    pending when observed at or after the booking, a settlement effective only after the
    booking (its ``effective_at``, else the time its settled status was observed), or a
    failed payout. Component records are not needed to see any of these."""
    ref = payout.payment_ref or payout.record_id
    booked = format_utc(txn.booked_at)
    if settlement is not None:
        stl = settlement.ref().key()
        status = settlement.status
        what = "Payout" if settlement.record_id == payout.record_id else "Settlement"
        if status in FAILED_STATUSES:
            return (f"{what} {stl} has status {status}.", GAP_SETTLEMENT_FAILED,
                    f"{what} {stl} for payout {ref} has status {status}, which conflicts with "
                    f"the claim that {ref} funded this credit; {_ORIGIN}.")
        if status in PENDING_STATUSES and settlement.occurred_at >= txn.booked_at:
            return (f"{what} {stl} was still {status} at or after the booking.",
                    GAP_PENDING_AT_BOOKING,
                    f"{what} {stl} for payout {ref} was still {status} when observed at "
                    f"{format_utc(settlement.occurred_at)}, at or after the credit was booked at "
                    f"{booked}, so {ref} had not settled when the credit arrived; {_ORIGIN}.")
        effective = reconcile.settlement_effective_time(settlement)
        if effective is not None and effective > txn.booked_at:
            how = ("took effect" if settlement.effective_at is not None
                   else f"was first observed as {status}, with no earlier effective time,")
            return (f"{what} {stl} took effect only after the booking.", GAP_SETTLEMENT_AFTER,
                    f"{what} {stl} for payout {ref} {how} at {format_utc(effective)}, after "
                    f"the credit was booked at {booked}; a later settlement cannot explain an "
                    f"earlier credit, so {_ORIGIN}.")
    if payout.status in FAILED_STATUSES:
        return (f"Payout {payout.record_id} has status {payout.status}.", GAP_SETTLEMENT_FAILED,
                f"Payout {payout.ref().key()} ({ref}) has status {payout.status}, which conflicts "
                f"with the claim that it funded this credit; {_ORIGIN}.")
    return None


def _verdict(txn: BankTxn, payout: EvidenceRecord, settlement: EvidenceRecord,
             calc: Calculation, window: timedelta
             ) -> tuple[ClaimStatus, str, str | None, str | None, str | None]:
    """Claim status for a linked credit with no explicit conflict (``_conflict``), from
    explicit fields only.

    Returns (status, short reason, gap code, gap sentence, references sentence for the
    rationale).
    """
    ref = payout.payment_ref or payout.record_id
    stl = settlement.ref().key()
    status = settlement.status
    timing = calc.timing
    if timing is None or not timing.ok:
        reason = timing.reason if timing is not None else "No settlement timing check."
        effective = timing.settlement_effective_at if timing is not None else None
        if status in PENDING_STATUSES:
            code = GAP_PENDING_AT_BOOKING
            gap = (f"Settlement {stl} for payout {ref} was {status} when last observed at "
                   f"{format_utc(settlement.occurred_at)}, before the booking at "
                   f"{format_utc(txn.booked_at)}; there is no evidence it settled in time.")
        elif effective is not None and effective <= txn.booked_at:
            code = GAP_SETTLEMENT_NOT_FOUND
            gap = (f"Settlement {stl} for payout {ref} took effect at {format_utc(effective)}, "
                   f"more than {_window_text(window)} before the booking at "
                   f"{format_utc(txn.booked_at)}; a settlement that early cannot be the transfer "
                   f"that produced this credit, so {_ORIGIN}.")
        else:
            code = GAP_SETTLEMENT_NOT_FOUND
            gap = (f"Settlement {stl} for payout {ref} has status {status or 'none'}, which does "
                   "not show that the funds reached the bank.")
        return ClaimStatus.UNRESOLVED, reason, code, gap, None
    if calc.balanced is not True:
        if (settlement.amount_pence, settlement.currency) != (txn.amount_pence, txn.currency):
            return (ClaimStatus.CONTRADICTED, f"Settlement {stl} amount differs from the credit.",
                    GAP_AMOUNT_MISMATCH,
                    f"Settlement {stl} for payout {ref} is for "
                    f"{_money(settlement.amount_pence, settlement.currency)}, but the credit is "
                    f"{_money(txn.amount_pence, txn.currency)}; {_ORIGIN}.", None)
        target = calc.target_ref.key() if calc.target_ref else "the credit"
        return (ClaimStatus.UNRESOLVED, "The amounts do not reconcile.", GAP_AMOUNT_MISMATCH,
                f"The amounts do not reconcile: the payout's components total {calc.total_pence} "
                f"pence against {target} at {calc.target_pence} pence (difference "
                f"{calc.difference_pence}).", None)
    tokens = reference_tokens(txn)
    conflicting = _conflicting_refs(tokens, payout, settlement)
    if conflicting:
        listed = ", ".join(conflicting)
        return (ClaimStatus.UNRESOLVED, "The credit carries a different settlement reference.",
                GAP_SETTLEMENT_NOT_FOUND,
                f"The credit carries {listed}, but settlement {stl} for payout {ref} has "
                f"settlement reference {settlement.settlement_ref}; the credit may be a different "
                "transfer, so the source of funds is not established.",
                f"References: the credit carries {listed}, not {settlement.settlement_ref}.")
    named = sorted({r for r in (payout.payment_ref, settlement.payment_ref,
                                settlement.settlement_ref) if r and r.upper() in tokens})
    if not named:
        return (ClaimStatus.UNRESOLVED, "Only amounts and dates tie the credit to the payout.",
                GAP_SETTLEMENT_NOT_FOUND,
                f"The credit carries no reference to payout {ref} or its settlement; matching "
                "amounts and dates alone do not establish the source of funds.",
                f"References: the credit carries none of {ref}, "
                f"{settlement.settlement_ref or 'no settlement reference'}.")
    refs_text = (f"References: the credit carries {', '.join(named)}, matching the payout and "
                 "settlement.")
    if reconcile.credit_supported(calc, window=window):
        return (ClaimStatus.SUPPORTED,
                f"Settlement {stl} is {status} by the booking, the amounts reconcile and the "
                f"credit carries {', '.join(named)}.", None, None, refs_text)
    return (ClaimStatus.UNRESOLVED, "The reconciliation does not support the credit.",
            GAP_SETTLEMENT_NOT_FOUND, "The reconciliation does not support the credit.",
            refs_text)


def _conflicting_refs(tokens: frozenset[str], payout: EvidenceRecord,
                      settlement: EvidenceRecord) -> list[str]:
    """References on the credit that look like another settlement's: the credit does not
    carry the settlement's own reference but carries one with the same prefix (``STL`` in
    ``STL-88001``) that neither the payout nor the settlement explains."""
    own = settlement.settlement_ref
    if not own or own.upper() in tokens:
        return []
    prefix = _ref_prefix(own)
    explained = {r.upper() for r in (payout.payment_ref, settlement.payment_ref) if r}
    return sorted(token for token in tokens
                  if prefix and _ref_prefix(token) == prefix and token not in explained)


def _ref_prefix(ref: str) -> str:
    found = _REF_PREFIX.match(ref.upper())
    return found.group(0) if found else ""


def _choose_settlement(pool: list[EvidenceRecord]
                       ) -> tuple[EvidenceRecord, list[EvidenceRecord], list[EvidenceRecord]]:
    """The settlement that decides, from every active one for a payment reference: issuer-
    verified records first (self-supplied ones only when there is no other); among those a
    failed status wins, else the most recently observed. Returns (chosen, the other records
    considered, self-supplied records set aside)."""
    key = lambda r: (r.occurred_at, r.record_id, r.revision)  # noqa: E731
    verified = [r for r in pool if r.source_trust == SourceTrust.ISSUER_VERIFIED]
    considered = sorted(verified or pool, key=key)
    failed = [r for r in considered if r.status in FAILED_STATUSES]
    best = max(failed or considered, key=key)
    kept = {r.record_id for r in considered}
    return (best, [r for r in considered if r.record_id != best.record_id],
            sorted((r for r in pool if r.record_id not in kept), key=key))


def _self_supplied(*records: EvidenceRecord | None) -> list[str]:
    """``"<kind> <record_id>"`` for each given record the holder supplied without an issuer
    signature (a record given twice is named once)."""
    seen: dict[str, str] = {}
    for r in records:
        if r is not None and r.source_trust == SourceTrust.SELF_SUPPLIED:
            seen.setdefault(r.record_id, f"{r.kind.value} {r.record_id}")
    return list(seen.values())


# --------------------------------------------------------------------------- text helpers


def _is_ref(token: str) -> bool:
    try:
        _REF.validate_python(token)
    except ValidationError:
        return False
    return True


def _no_settlement_gap(payout: EvidenceRecord, notes: list[str]) -> str:
    ref = payout.payment_ref or payout.record_id
    gap = (f"No settlement record for payout {ref}; a payout record or a 'released to balance' "
           "message does not show a bank transfer.")
    failures = [note for note in notes
                if any(word in note for word in ("failed", "could not", "no payment reference"))]
    return " ".join([gap, *failures])


def _calc_text(calc: Calculation, components: list[EvidenceRecord], payout: EvidenceRecord,
               settlement: EvidenceRecord | None) -> str:
    pieces = []
    for kind, sign in ((RecordKind.ORDER, ""), (RecordKind.REFUND, "- "), (RecordKind.FEE, "- ")):
        rows = [r for r in components if r.kind == kind]
        pieces.append(f"{sign}{_LABELS[kind]} {sum(r.amount_pence for r in rows)} "
                      f"({_ids(rows) or 'none'})")
    text = (f"Calculation in code: {' '.join(pieces)} = {_money(calc.total_pence, calc.currency)}, "
            f"against {calc.target_ref.key() if calc.target_ref else 'no target'} at "
            f"{calc.target_pence}: difference {calc.difference_pence}, "
            f"{'balanced' if calc.balanced else 'not balanced'}.")
    amounts = f" Payout {payout.record_id} is {payout.amount_pence}"
    if settlement is not None and settlement.record_id != payout.record_id:
        amounts += f" and settlement {settlement.record_id} is {settlement.amount_pence}"
    return text + amounts + "."


def _timing_text(settlement: EvidenceRecord, txn: BankTxn, window: timedelta) -> str:
    what = "payout" if settlement.kind == RecordKind.PAYOUT else "settlement"
    text = (f"Timing: {what} {settlement.ref().key()} status {settlement.status or 'none'}, "
            f"observed {format_utc(settlement.occurred_at)}")
    if settlement.effective_at is not None:
        text += f", effective {format_utc(settlement.effective_at)}"
    text += f"; credit booked {format_utc(txn.booked_at)}."
    return f"{text} {reconcile.check_settlement_timing(settlement, txn, window=window).reason}"


def _calc_summary(calc: Calculation) -> dict[str, Any]:
    summary: dict[str, Any] = {"method": calc.method, "currency": calc.currency,
                               "total_pence": calc.total_pence, "terms": len(calc.terms)}
    if calc.target_ref is not None:
        summary.update(target=calc.target_ref.key(), target_pence=calc.target_pence,
                       difference_pence=calc.difference_pence, balanced=bool(calc.balanced))
    if calc.timing is not None:
        summary["timing_ok"] = calc.timing.ok
    return summary


def _unknown_source(txn_id: str, txn: BankTxn | None = None) -> str:
    if txn is None:
        return f"The source of transaction {txn_id} is not established from the available records."
    kind = "credit" if txn.is_credit else "debit"
    return (f"The source of {kind} {txn_id} of {_money(abs(txn.amount_pence), txn.currency)} is "
            "not established from the available records.")


def _money(pence: int | None, currency: str | None) -> str:
    return f"{pence} pence {currency}"


def _listing(texts: list[str], limit: int = 12) -> str:
    """Comma-separated, at most ``limit`` items, then "and N more"."""
    more = f" and {len(texts) - limit} more" if len(texts) > limit else ""
    return ", ".join(texts[:limit]) + more


def _ids(records: list[EvidenceRecord], *, keys: bool = False) -> str:
    return _listing([r.ref().key() if keys else r.record_id for r in records])


def _amounts(records: list[EvidenceRecord]) -> str:
    return _listing([f"{r.record_id} {r.amount_pence}" for r in records])


def _join(words: list[str]) -> str:
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1]


def _window_text(window: timedelta) -> str:
    if window % timedelta(days=1) == timedelta(0):
        days = window // timedelta(days=1)
        return f"{days} day{'s' if days != 1 else ''}"
    return f"{window.total_seconds() / 3600:g} hours"


def _words(pattern: re.Pattern, text: str) -> tuple[str, ...]:
    return tuple(sorted({match.group(0).lower() for match in pattern.finditer(text)}))


def _kinds_in(text: str) -> tuple[tuple[RecordKind, ...], tuple[str, ...]]:
    """The period-total kinds a text names, in ``_KIND_WORDS`` order, and the words found."""
    kinds: list[RecordKind] = []
    words: list[str] = []
    for kind, pattern in _KIND_WORDS:
        found = _words(pattern, text)
        if found:
            kinds.append(kind)
            words.extend(found)
    return tuple(kinds), tuple(words)


def _names(text: str, txn_id: str) -> bool:
    """Does the text name this txn_id as a whole word (case-insensitive)?"""
    return re.search(rf"(?<![\w-]){re.escape(txn_id)}(?![\w-])", text, re.IGNORECASE) is not None


_TRACE_OUTCOMES = frozenset({"refused", "unavailable", "error", "budget_exceeded", "not_found"})


def _error_kind(exc: GatewayError) -> str:
    """The typed reason (a TraceEvent outcome) for a gateway error: the error's own
    ``outcome`` when it declares one, else by class."""
    declared = getattr(exc, "outcome", None)
    if declared in _TRACE_OUTCOMES:
        return declared
    if isinstance(exc, BudgetExceeded):
        return "budget_exceeded"
    if isinstance(exc, Unavailable):
        return "unavailable"
    if isinstance(exc, ScopeError):
        return "refused"
    if isinstance(exc, NotFoundError):
        return "not_found"
    return "error"


def _unique(refs: Iterable[EvidenceRef]) -> list[EvidenceRef]:
    """Refs in order without exact repeats (same id and revision)."""
    seen: dict[str, EvidenceRef] = {}
    for ref in refs:
        seen.setdefault(ref.key(), ref)
    return list(seen.values())


def _clip(text: str, limit: int = 4000) -> str:
    """Fit a schema text limit (LongText 4000, ShortText 300)."""
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."
