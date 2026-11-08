"""Agents investigation: Gemini roles propose, code retrieves, validates and decides.

``run_investigation(store, request, client=...)`` has the shape of
``alibi.core.investigate_rules.run_investigation`` and ``alibi.api.run_investigation`` and
returns an ``InvestigationResult`` with ``mode="agents"``. Per request:

1. The requested bank transactions and the subject's merchant records are read once through
   the scoped, budgeted ``Gateway`` into a ``Material`` (every revision pinned, every later
   retrieval tagged with the lookup that fetched it).
2. Each question is classified: by its team question id, else by keyword
   (``classify_question``). Period totals (sales, refunds, fees, net) are summed in code
   exactly like the rules baseline; the Detective sees them but never decides them.
3. Source-of-funds questions, per credit:
   a. the pinned candidate rule (``settlement_candidates``) joins the credit to payouts;
      only an ``ambiguous`` join calls the Matcher, whose ``linked`` is accepted only for a
      record in the candidate pool (and only if the optional second opinion agrees). The
      settlement evidence is a separate ``settlement`` record (S1's fixtures) or the payout's
      own ``status`` (the team's cases fold the settlement state into the payout; see
      ``alibi.agents.candidates``), and the ``settlements`` list the roles see holds both;
   b. the Detective proposes a claim per question (and alerts, saved when their refs validate);
   c. the Challenger runs up to ``max_rounds`` rounds; its one validated ``next_action`` is
      executed through ``alibi.agents.actions`` within ``action_budget`` and fed back under
      ``claim.lookup_results``; candidates are recomputed on the grown material;
   d. the verdict is computed IN CODE from the final candidates and ``alibi.core.reconcile``:
      a settled candidate whose reference the credit carries, whose components reconcile and
      whose evidence is issuer evidence is ``supported``; an explicit conflict (settlement
      failed, pending at or after booking, settled after booking, amount mismatch) is
      ``contradicted``; settlement evidence that merely fails to show bank settlement (a payout
      still pending before the booking, a settlement too early or without a settlement
      status) is ``unresolved`` with the rejection's gap code; everything else is
      ``unresolved`` with a team gap code at the start of ``Claim.gap``. The Challenger may only downgrade a status: to ``contradicted`` only when
      every conflicting ref it cites is a payout or settlement with an explicit-field conflict
      (``CandidateResult.conflicts``, or a failed, late, pending-at-booking or mismatched
      settlement), else a ``supported`` verdict goes to ``unresolved``; a proposal produced
      before the last retrieval changed the material is stale and not applied; the Detective's
      status is a proposal;
   e. the Curator proposes per-question disclosures over the shareable evidence (what the
      final claims cite, minus ``message`` records); the final ``evidence_refs`` are the
      code-validated refs the Curator kept plus everything the calculation cites. Exclusions
      and their notes go only into a holder-side trace note.
4. Every step is a ``TraceEvent`` on the gateway's trace; links, claims, alerts and the trace
   are saved in one store transaction. ``run_stats(result)`` reads the runner's closing note
   (``lookups``, ``observed_inputs``, ``model_calls``, ``tokens``, ``action_budget_used``,
   ``rounds``, ``unsafe_proposals``). ``unsafe_proposals`` uses the one-pass definition:
   Detective and Challenger proposals of ``supported`` whose final code status is not
   supported, plus Curator disclosures dropped as not shareable. ``model_calls`` counts calls
   the model answered (``roles.call_cost``): cached and replayed answers and transport
   failures count none, an answer that failed the schema counts one.

``Claim.statement``, ``rationale``, ``gap`` and ``evidence_refs`` are bank-facing and are
written by code from ids, amounts, statuses and times only: no merchant message text, no model
prose, no holder notes; a ``message`` record never enters ``evidence_refs``. ``Alert.title``,
``explanation`` and ``Link.signals`` are likewise built in code from kinds, ids, amounts and
statuses. Model prose (alert summaries, matcher signals), validation failures and curator
exclusions live in the trace, which stays with the holder. A role failure (budget, outage, bad
schema) degrades the affected claim to ``unresolved`` with a typed reason; nothing here raises
for it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

from alibi.agents import roles
from alibi.agents.actions import ActionBudget, execute_action
from alibi.agents.candidates import RULE_TEXT, Candidate, CandidateResult, Rejection, settlement_candidates
from alibi.agents.material import Material, challenger_input, curator_input, detective_input, matcher_input
from alibi.agents.models import ChallengerOutput, DetectiveClaim, DetectiveOutput
from alibi.agents.roles import RoleError, RoleResult
from alibi.core import reconcile
from alibi.core.canon import make_id
from alibi.core.facts import answered_calculation, answered_refs, derive_facts
from alibi.core.answered import examined_refs
from alibi.core.investigate_rules import (
    COMPONENT_KINDS,
    COUNTED_STATUSES,
    EXCLUDED_STATUSES,
    FEES_TOTAL,
    NET_TOTAL,
    REFUNDS_TOTAL,
    SALES_TOTAL,
    SEVERAL_TOTALS,
    SOURCE_OF_FUNDS,
    UNMAPPED,
    Classification,
    classify_question,
)
from alibi.core.policy import compile_policy
from alibi.core.reconcile import FAILED_STATUSES, PENDING_STATUSES, SIGN_BY_KIND, ReconcileError
from alibi.core.schemas import (
    Alert,
    AlertKind,
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
    RequestForContext,
    SourceTrust,
    TraceEvent,
    format_utc,
    utc_now,
)
from alibi.core.store import Store
from alibi.gateway import Gateway, GatewayError

__all__ = ["ACTOR", "MODE", "GAP_CODES", "next_run_id", "run_investigation", "run_stats"]

MODE = "agents"
ACTOR = "runner"

# Team gap codes (shared/CONTRACTS.md, Questions and matching; agents/data/README.md).
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

# Team question ids decide the question kind before any keyword does.
KNOWN_QUESTIONS = {"q.source_of_funds": SOURCE_OF_FUNDS, "q.breakdown": SOURCE_OF_FUNDS,
                   "q.sales_refunds_in_period": NET_TOTAL}
PERIOD_CATEGORIES = (SALES_TOTAL, REFUNDS_TOTAL, FEES_TOTAL, NET_TOTAL)
_TOTAL_KINDS = {SALES_TOTAL: (RecordKind.ORDER,), REFUNDS_TOTAL: (RecordKind.REFUND,),
                FEES_TOTAL: (RecordKind.FEE,), NET_TOTAL: (RecordKind.ORDER, RecordKind.REFUND)}
_LABELS = {RecordKind.ORDER: "sales", RecordKind.REFUND: "refunds", RecordKind.FEE: "fees"}
_RANK = {ClaimStatus.SUPPORTED: 0, ClaimStatus.UNRESOLVED: 1, ClaimStatus.CONTRADICTED: 2}
_TRACE_OUTCOMES = frozenset({"ok", "refused", "unavailable", "error", "budget_exceeded", "not_found"})
GENERIC_CURATOR_GAP = "Some supporting evidence is withheld at the account holder's request."
UNMAPPED_GAP = "The investigation could not map this question to evidence it can check."


# --------------------------------------------------------------------------- public


def next_run_id(store: Store, request_id: str, *, audience: str | None = None) -> str:
    """``make_id("run", audience, request_id, "agents", n)`` (``make_id("run", request_id, "agents",
    n)`` without a bank) for the first ``n`` no stored claim or trace of this request uses
    (mirrors ``investigate_rules.next_run_id`` for the agents mode; request ids are per bank)."""
    used = {claim.run_id for claim in store.list_claims(request_id=request_id, state=None)}
    n = 0
    while True:
        run_id = make_id("run", audience, request_id, MODE, n) if audience else make_id("run", request_id, MODE, n)
        if run_id not in used and not store.list_traces(run_id):
            return run_id
        n += 1


def run_investigation(store: Store, request: str | RequestForContext, *, client: Any,
                      gateway: Any | None = None, settlement_lookup: Callable | None = None,
                      txn_refresh: Callable | None = None, budget: int = 12,
                      registry: Any | None = None, now: datetime | None = None,
                      action_budget: int = 3, max_rounds: int = 3,
                      second_opinion: roles.SecondOpinion | None = None,
                      call_budget: Any | None = None) -> InvestigationResult:
    """Answer every question of ``request`` (a request_id or ``RequestForContext``) with the
    agents pipeline (module doc) and save links, claims, alerts and the trace.

    Without ``gateway`` one is built like ``alibi.api.run_investigation`` does: for the
    request's subject, the next agents run id, the connector callables, ``budget`` and
    ``registry`` (the demo keystore's registry when a settlement lookup is given without one).
    ``client`` is any object with ``GeminiClient.generate``'s signature; ``call_budget`` is an
    optional ``CallBudget`` passed to every role call. Nothing here writes audit events.
    """
    rfc = store.get_request(request) if isinstance(request, str) else request
    if not isinstance(rfc, RequestForContext):
        rfc = RequestForContext.model_validate(rfc)
    at = _clock(now)
    if gateway is None:
        if registry is None and settlement_lookup is not None:
            from alibi.api import default_keystore  # lazy: alibi.api may dispatch to this module
            registry = default_keystore().registry()
        run_id = next_run_id(store, rfc.request_id, audience=rfc.audience)
        gateway = Gateway(store, subject_ref=rfc.subject_ref, run_id=run_id, registry=registry,
                          budget=budget, settlement_lookup=settlement_lookup,
                          txn_refresh=txn_refresh, now=now, request_id=rfc.request_id)
    else:
        scope = getattr(gateway, "subject_ref", None)
        if scope is not None and scope != rfc.subject_ref:
            raise ValueError("the gateway is scoped to a different subject than the request")
        run_id = getattr(gateway, "run_id", None) or next_run_id(store, rfc.request_id, audience=rfc.audience)
    run = _Run(rfc, store, gateway, client, run_id=run_id, now=at, action_budget=action_budget,
               max_rounds=max_rounds, second_opinion=second_opinion, call_budget=call_budget)
    return run.run()


def run_stats(result: InvestigationResult) -> dict[str, Any]:
    """The runner's closing note: ``{lookups, observed_inputs, model_calls, tokens,
    action_budget_used, rounds, unsafe_proposals}`` (zeros when the trace has none)."""
    for event in reversed(result.trace):
        if event.actor == ACTOR and event.step == "note" and "model_calls" in event.result:
            return {"unsafe_proposals": 0, **event.result}
    return {"lookups": 0, "observed_inputs": 0, "model_calls": 0, "tokens": 0,
            "action_budget_used": 0, "rounds": 0, "unsafe_proposals": 0}


# --------------------------------------------------------------------------- run state


@dataclass
class _Outcome:
    """The code verdict for one transaction or one question (bank-facing text only)."""

    status: ClaimStatus
    statement: str
    rationale: str
    reason: str                          # for the status_change trace event
    gap: str | None = None
    evidence: list[EvidenceRef] = field(default_factory=list)
    calculation: Calculation | None = None


@dataclass
class _Join:
    """What the deterministic join and the Matcher decided for one credit."""

    initial: CandidateResult
    choice: str | None = None            # payout record_id the Matcher linked, if accepted
    signals: list[str] = field(default_factory=list)
    note: str = ""


class _Run:
    def __init__(self, rfc: RequestForContext, store: Store, gateway: Any, client: Any, *,
                 run_id: str, now: datetime, action_budget: int, max_rounds: int,
                 second_opinion: roles.SecondOpinion | None, call_budget: Any | None):
        self.rfc, self.store, self.gateway, self.client = rfc, store, gateway, client
        self.run_id, self.now = run_id, now
        self.max_rounds = max(int(max_rounds), 0)
        self.second_opinion, self.call_budget = second_opinion, call_budget
        self.material = Material(rfc.subject_ref)
        self.budget = ActionBudget(action_budget)
        self.bank: dict[str, BankTxn] = {}
        self.txn_errors: dict[str, str] = {}
        self.joins: dict[str, _Join] = {}
        self.links: dict[str, Link] = {}
        self.alerts: list[Alert] = []
        self.outcomes: dict[str, _Outcome] = {}
        self.unavailable: list[str] = []
        self.events: list[dict[str, Any]] = []
        self.model_calls = self.tokens = self.rounds = 0
        self.records_err: str | None = None              # the all-kinds list_records read failed
        self.supported_proposals: list[str] = []         # question_id per 'supported' proposal event
        self.unsafe_disclosures = 0                      # curator disclosures outside the shareable set
        self.question_id: str | None = None
        self.detective: RoleResult | None = None
        log_step = getattr(gateway, "log_step", None)
        self.log_step = log_step if callable(log_step) else None

    # ----------------------------------------------------------------------- trace, reads, roles

    def note(self, step: str, *, actor: str = ACTOR, reason: str | None = None,
             refs: Iterable[EvidenceRef] = (), outcome: str = "ok", tool: str | None = None,
             args: dict | None = None, result: dict | None = None) -> None:
        event = {"step": step, "actor": actor, "tool": tool, "args": args or {},
                 "result": result or {}, "refs": _unique(refs),
                 "outcome": outcome if outcome in _TRACE_OUTCOMES else "ok",
                 "reason": _clip(reason, 300) if reason else None, "question_id": self.question_id}
        if self.log_step is not None:
            self.log_step(**event)
        else:
            self.events.append(event)

    def read(self, tool: str, label: str, call: Callable[[], Any]) -> tuple[Any, str | None]:
        """One gateway read; a gateway error becomes a typed reason, never a crash."""
        try:
            return call(), None
        except GatewayError as exc:
            kind = getattr(exc, "outcome", "error")
            kind = kind if kind in _TRACE_OUTCOMES else "error"
            typed = _clip(f"{kind}: {label}", 300)
            if typed not in self.unavailable:
                self.unavailable.append(typed)
            self.note("note", tool=tool, outcome=kind, reason=f"{label} failed ({kind})",
                      args={"call": label})
            return None, kind

    def call(self, role: str, fn: Callable[..., RoleResult], input_dict: dict[str, Any],
             **extra: Any) -> RoleResult | None:
        """Run one role; log what validation dropped; a RoleError becomes a typed note."""
        try:
            rr = fn(self.client, self.material, input_dict, budget=self.call_budget,
                    second_opinion=self.second_opinion, **extra)
        except RoleError as exc:
            self.model_calls += int(getattr(exc, "model_calls", 0) or 0)
            self.tokens += int(getattr(exc, "tokens", 0) or 0)
            typed = _clip(f"{exc.outcome}: {role} ({exc.kind})", 300)
            if typed not in self.unavailable:
                self.unavailable.append(typed)
            self.note("note", actor=role, outcome=exc.outcome,
                      reason=f"{role} gave no usable answer: {exc.kind}: {exc.message}",
                      result={"role": role, "kind": exc.kind, "model_calls": int(getattr(exc, "model_calls", 0) or 0)})
            return None
        self.model_calls += rr.model_calls
        self.tokens += rr.tokens
        for failure in rr.failures:
            if role == "curator" and failure.reason == "not in the shareable evidence":
                self.unsafe_disclosures += 1
            self.note("validation", actor=role, outcome="refused", result=failure.as_dict(),
                      reason=f"{failure.path}: {failure.reason}")
        if rr.undeclared:
            self.note("note", actor=role, result={"undeclared_fields": rr.undeclared},
                      reason="undeclared output fields were ignored")
        if rr.distrusted:
            self.note("note", actor=role, result={"second_opinion": "disagree"},
                      reason=f"second opinion disagrees: {rr.opinion_note or 'no note'}")
        return rr

    # ----------------------------------------------------------------------- pipeline

    def run(self) -> InvestigationResult:
        if getattr(self.client, "replay", False):
            self.note("note", reason="REPLAY: served from recording",
                      result={"replay": True, "label": getattr(self.client, "label", None)})
        self.load_material()
        kinds = {q.question_id: self.classify(q) for q in self.rfc.questions}
        self.question_id = None
        for txn in list(self.bank.values()):
            self.join(txn)
        self.detect()
        proposals = self.proposals()
        claims: list[tuple[Question, _Outcome]] = []
        for question in self.rfc.questions:
            self.question_id = question.question_id
            found = kinds[question.question_id]
            proposal = proposals.get(question.question_id)
            if found.category == SOURCE_OF_FUNDS:
                outcome = self.source_of_funds(question, proposal)
            elif found.category in PERIOD_CATEGORIES and self.rfc.period is not None:
                outcome = self.period_total(found)
            else:
                outcome = self.unmapped(found)
            if proposal is not None:
                self.note("status_change", reason=f"Code verdict {outcome.status.value}: {outcome.reason}"
                          if proposal.status != outcome.status.value else outcome.reason,
                          refs=outcome.evidence,
                          result={"from": proposal.status, "to": outcome.status.value, "decided_by": "code"})
            claims.append((question, outcome))
        self.question_id = None
        curated = self.curate(claims)
        return self.finish(curated)

    def load_material(self) -> None:
        for txn_id in self.rfc.txn_ids or []:
            txn, err = self.read("get_txn", f"get_txn {txn_id}", lambda: self.gateway.get_txn(txn_id))
            if txn is None:
                self.txn_errors[txn_id] = err or "error"
                continue
            if txn.subject_ref != self.rfc.subject_ref:
                self.txn_errors[txn_id] = "refused"
                continue
            self.bank[txn_id] = txn
            self.material.add_txn(txn)
        records, err = self.read("list_records", "list_records all kinds",
                                 lambda: self.gateway.list_records(kinds=None))
        if records is None:
            self.records_err = err or "error"
        for record in records or []:
            if record.subject_ref == self.rfc.subject_ref:
                self.material.add_record(record)
        self.note("note", reason=f"Material loaded: {len(self.bank)} bank transaction(s), "
                                 f"{len(records or [])} merchant record(s)."
                                 + (f" The merchant records could not be read ({self.records_err})."
                                    if self.records_err else ""),
                  result={"observed_inputs": self.material.observed_inputs,
                          "txn_ids": sorted(self.bank), "unreadable_txn_ids": sorted(self.txn_errors),
                          "records_error": self.records_err})

    def classify(self, question: Question) -> Classification:
        self.question_id = question.question_id
        found = classify_question(question.text, self.rfc)
        known = KNOWN_QUESTIONS.get(question.question_id)
        how = "keyword"
        if known is not None and (found.category in (UNMAPPED, SEVERAL_TOTALS)
                                  or (known == SOURCE_OF_FUNDS) != (found.category == SOURCE_OF_FUNDS)):
            found = Classification(known, _TOTAL_KINDS.get(known, ()), (question.question_id,))
            how = "question id"
        self.note("note", reason=f"Question classified as {found.category} by {how}.",
                  result={"category": found.category, "kinds": [k.value for k in found.kinds],
                          "keywords": list(found.keywords)})
        return found

    # ----------------------------------------------------------------------- joins

    def candidates(self, txn: BankTxn) -> CandidateResult:
        """The pinned rule over the current material; a payout that a settlement lookup
        returned for its own reference counts as settlement evidence for itself."""
        return settlement_candidates(txn, self.material.records(),
                                     looked_up=self.material.settlement_lookup_refs())

    def role_records(self) -> tuple[list[EvidenceRecord], list[EvidenceRecord]]:
        """``(merchant, settlements)`` for the Detective and Challenger: every record but the
        separate settlement records, and the settlement evidence (separate settlement records
        plus payouts carrying a status), so a payout with a status appears in both."""
        merchant = [r for r in self.material.records() if r.kind != RecordKind.SETTLEMENT]
        return merchant, self.material.settlement_evidence()

    def join(self, txn: BankTxn) -> None:
        """Deterministic candidate rule, then the Matcher only when it is ambiguous."""
        result = self.candidates(txn)
        join = _Join(result)
        self.joins[txn.txn_id] = join
        self.note("validation", tool="settlement_candidates", reason=result.summary(),
                  refs=[txn.ref(), *_pool_refs(result)],
                  args={"txn_id": txn.txn_id, "rule": "5-day settlement candidate rule"},
                  result={"link_state": result.state.value,
                          "settled": [c.record_ids for c in result.settled],
                          "provisional": [c.record_ids for c in result.provisional],
                          "conflicts": [[p.record_id for p in r.payouts] for r in result.conflicts]})
        if result.state != LinkState.AMBIGUOUS or not txn.is_credit:
            return
        pool_payouts = [p for c in result.pool for p in c.payouts]
        pool_ids = {p.record_id for p in pool_payouts}
        pool_refs = {p.payment_ref for p in pool_payouts if p.payment_ref}
        settlements = [s for s in self.material.records(kinds=[RecordKind.SETTLEMENT])
                       if s.payment_ref in pool_refs]
        context = self.material.records(kinds=[*COMPONENT_KINDS, RecordKind.MESSAGE])
        rr = self.call("matcher", roles.run_matcher,
                       matcher_input(self.material, txn, [*pool_payouts, *settlements], context,
                                     rule=RULE_TEXT))
        if rr is None:
            join.note = "Matcher unavailable; the join stays ambiguous."
            return
        out = rr.output
        # Model prose: traced for the holder, never stored in Link.signals.
        join.signals = [_clip(s.strip(), 300) for c in out.candidates for s in c.signals
                        if isinstance(s, str) and s.strip()][:12]
        decision = out.decision
        if decision == "linked" and out.chosen_record_id not in pool_ids:
            decision, join.note = "ambiguous", (f"Matcher chose {out.chosen_record_id}, which is not "
                                                "in the candidate pool; kept ambiguous.")
            self.note("validation", actor="matcher", outcome="refused",
                      result={"role": "matcher", "path": "chosen_record_id",
                              "record_id": out.chosen_record_id, "revision": out.chosen_revision,
                              "reason": "not in the candidate pool"},
                      reason="chosen_record_id is not in the candidate pool")
        elif decision == "linked" and rr.distrusted:
            decision, join.note = "ambiguous", "Matcher linked but the second opinion disagreed; kept ambiguous."
        elif decision == "linked":
            join.choice = out.chosen_record_id
            join.note = f"Matcher linked {out.chosen_record_id} from the candidate pool."
        else:
            join.note = f"Matcher decision {decision}; the join stays ambiguous."
        self.note("status_change", actor="matcher", reason=join.note,
                  refs=[txn.ref(), *(p.ref() for p in pool_payouts)],
                  result={"link_state": "linked" if join.choice else "ambiguous",
                          "matcher_decision": out.decision, "chosen": join.choice or "none",
                          "matcher_signals": list(join.signals)})

    # ----------------------------------------------------------------------- detective

    def detect(self) -> None:
        bank = []
        for txn in self.bank.values():
            join = self.joins.get(txn.txn_id)
            state, to = (None, None)
            if join is not None:
                state, to = _link_view(join.initial, join.choice)
            bank.append((txn, state, to))
        merchant, settlements = self.role_records()
        rr = self.call("detective", roles.run_detective,
                       detective_input(self.material, self.rfc, bank, merchant, settlements))
        self.detective = rr
        if rr is None:
            return
        out: DetectiveOutput = rr.output  # type: ignore[assignment]
        for index, alert in enumerate(out.alerts):
            refs = [ref for ref in (self.material.ref_for(a.record_id, a.revision) for a in alert.refs)
                    if ref is not None]
            if not refs:
                self.note("validation", actor="detective", outcome="refused",
                          result={"role": "detective", "path": f"alerts[{index}]", "record_id": "",
                                  "revision": None, "reason": "alert has no validated refs"},
                          reason="alert dropped: no validated refs")
                continue
            # The holder-facing text is built in code from the kind and the validated items; the
            # Detective's own summary and suggestion are prose and stay in the trace.
            items = [item for item in (self.material.get(r.ref_id, r.revision) for r in refs) if item is not None]
            title, explanation = _alert_text(alert.kind, items)
            saved = Alert(alert_id=make_id("alr", self.run_id, index, alert.kind), subject_ref=self.rfc.subject_ref,
                          kind=AlertKind(alert.kind), severity="info", title=title, explanation=explanation,
                          refs=refs, suggested_evidence=[], created_at=self.now)
            self.alerts.append(saved)
            self.note("note", actor="detective", refs=refs,
                      reason=f"Alert {alert.kind} saved with code text; the Detective's summary is kept here.",
                      result={"alert_id": saved.alert_id, "kind": alert.kind,
                              "model_summary": _clip(alert.summary, 300),
                              "model_suggested_evidence": _clip(alert.suggested_evidence, 300)})
        self.note("note", actor="detective", reason=f"Detective proposed {len(out.claims)} claim(s) and "
                                                    f"{len(self.alerts)} alert(s) with validated refs.",
                  result={"claims": [c.question_id for c in out.claims],
                          "alerts": [a.alert_id for a in self.alerts],
                          "distrusted": rr.distrusted})

    def proposals(self) -> dict[str, DetectiveClaim]:
        found: dict[str, DetectiveClaim] = {}
        if self.detective is None:
            return found
        for claim in self.detective.output.claims:  # type: ignore[attr-defined]
            if claim.question_id in found or claim.question_id not in {q.question_id for q in self.rfc.questions}:
                continue
            found[claim.question_id] = claim
            self.question_id = claim.question_id
            if claim.status == ClaimStatus.SUPPORTED.value:
                self.supported_proposals.append(claim.question_id)
            refs = [ref for ref in (self.material.ref_for(r.record_id, r.revision) for r in claim.refs)
                    if ref is not None]
            self.note("status_change", actor="detective", refs=refs,
                      reason=f"Detective proposed {claim.status} (claim {claim.claim_id})",
                      result={"from": None, "to": claim.status, "claim_id": claim.claim_id,
                              "refs": [f"{r.record_id}@{r.revision}" for r in claim.refs],
                              "next_checks": [c.type for c in claim.next_checks],
                              "proposal": True})
        self.question_id = None
        return found

    # ----------------------------------------------------------------------- source of funds

    def source_of_funds(self, question: Question, proposal: DetectiveClaim | None) -> _Outcome:
        outcomes = [(txn_id, self.txn_outcome(txn_id, question, proposal))
                    for txn_id in self.rfc.txn_ids or []]
        if not outcomes:
            gap = "The request names no transaction whose source could be investigated."
            return _Outcome(ClaimStatus.UNRESOLVED, "The source of funds is not established.", gap,
                            "No transaction ids in the request.", gap=gap)
        if len(outcomes) == 1:
            return outcomes[0][1]
        status = max((o.status for _, o in outcomes), key=_RANK.__getitem__)
        gaps = [f"{txn_id}: {o.gap}" for txn_id, o in outcomes if o.gap]
        return _Outcome(status, " ".join(o.statement for _, o in outcomes),
                        " ".join(f"{txn_id}: {o.rationale}" for txn_id, o in outcomes),
                        "; ".join(f"{txn_id} {o.status.value}" for txn_id, o in outcomes),
                        gap=" ".join(gaps) or None,
                        evidence=[ref for _, o in outcomes for ref in o.evidence],
                        calculation=next((o.calculation for _, o in outcomes if o.calculation), None))

    def txn_outcome(self, txn_id: str, question: Question, proposal: DetectiveClaim | None) -> _Outcome:
        if txn_id in self.outcomes:
            return self.outcomes[txn_id]
        txn = self.bank.get(txn_id)
        if txn is None:
            err = self.txn_errors.get(txn_id, "error")
            gap = f"Bank transaction {txn_id} could not be read ({err})."
            outcome = _Outcome(ClaimStatus.UNRESOLVED, _unknown_source(txn_id), gap, gap, gap=gap)
        elif not txn.is_credit:
            gap = f"Transaction {txn_id} is a debit (money out); only the source of credits is investigated."
            outcome = _Outcome(ClaimStatus.UNRESOLVED, _unknown_source(txn_id, txn), gap, gap, gap=gap,
                               evidence=[txn.ref()])
        else:
            outcome = self.investigate_credit(txn, question, proposal)
        self.outcomes[txn_id] = outcome
        return outcome

    def investigate_credit(self, txn: BankTxn, question: Question, proposal: DetectiveClaim | None) -> _Outcome:
        join = self.joins[txn.txn_id]
        claim = self.claim_dict(txn, question, proposal, join)
        result = self.candidates(txn)
        last: ChallengerOutput | None = None
        stale = False        # the last proposal predates a retrieval that changed the material
        for index in range(self.max_rounds):
            self.rounds += 1
            merchant, settlements = self.role_records()
            rr = self.call("challenger", roles.run_challenger,
                           challenger_input(self.material, claim, [txn], merchant, settlements,
                                            round_index=index, actions_left=self.budget.remaining))
            if rr is None:
                break
            out: ChallengerOutput = rr.output  # type: ignore[assignment]
            last, stale = out, False
            if out.proposed_status == ClaimStatus.SUPPORTED.value:
                self.supported_proposals.append(question.question_id)
            self.note("status_change", actor="challenger",
                      reason=f"Challenger round {index + 1} proposed {out.proposed_status} "
                             f"(settlement established: {out.bank_settlement_established})",
                      refs=[ref for ref in (self.material.ref_for(r.record_id, r.revision)
                                            for r in [*out.confirming_refs, *out.conflicting_refs])
                            if ref is not None],
                      result={"from": claim["status"], "to": out.proposed_status, "round": index + 1,
                              "confirming": [f"{r.record_id}@{r.revision}" for r in out.confirming_refs],
                              "conflicting": [f"{r.record_id}@{r.revision}" for r in out.conflicting_refs],
                              "next_action": out.next_action.type, "proposal": True})
            claim["status"] = out.proposed_status
            if out.next_action.type == "none":
                break
            res = execute_action(self.gateway, self.material, out.next_action, self.budget)
            new_refs = [*(r.ref() for r in res.records), *(t.ref() for t in res.txns)]
            self.note("note", actor="challenger", tool=res.tool, args=res.args,
                      outcome=res.outcome if res.outcome != "skipped" else "ok",
                      reason=f"{res.tool}: {res.outcome}" + (f" ({res.reason})" if res.reason else ""),
                      refs=new_refs,
                      result={"outcome": res.outcome, "via_lookup": res.lookup_index,
                              "new_keys": list(res.new_keys),
                              "material_keys": [f"{r.record_id}@{r.revision}" for r in res.records]
                              + [f"{t.txn_id}@{t.revision}" for t in res.txns],
                              "action_budget_used": self.budget.used})
            claim.setdefault("lookup_results", []).append(res.feedback())
            if res.outcome == "budget_exceeded":
                break
            if res.outcome == "ok" and res.new_keys:
                stale = True
            result = self.candidates(txn)
            claim["link_state"], claim["linked_to"] = _link_view(result, join.choice)
        outcome = self.verdict(txn, result, join)
        if last is not None and stale:
            self.note("note", actor="challenger",
                      reason=(f"Challenger proposal {last.proposed_status} predates the final material (the last "
                              "retrieval added records it never saw); the code verdict stands."),
                      result={"proposed": last.proposed_status, "kept": outcome.status.value, "stale": True})
        elif last is not None:
            outcome = self.challenge(outcome, last, txn, result)
        if self.detective is not None and self.detective.distrusted and outcome.status == ClaimStatus.SUPPORTED:
            gap = ("The second opinion did not agree with the Detective's reading of the records, so "
                   "the claim is left unresolved for the holder to review.")
            outcome = replace(outcome, status=ClaimStatus.UNRESOLVED, statement=_unknown_source(txn.txn_id, txn),
                              reason="Second opinion disagreed with the Detective.", gap=gap)
            self.note("status_change", reason=outcome.reason, refs=outcome.evidence,
                      result={"from": "supported", "to": "unresolved", "decided_by": "second_opinion"})
        self.record_link(txn, result, join)
        return outcome

    def claim_dict(self, txn: BankTxn, question: Question, proposal: DetectiveClaim | None,
                   join: _Join) -> dict[str, Any]:
        state, to = _link_view(join.initial, join.choice)
        if proposal is None:
            return {"claim_id": make_id("c", self.run_id, question.question_id), "question_id": question.question_id,
                    "statement": f"The source of credit {txn.txn_id} is not yet established.",
                    "status": "unresolved", "refs": [{"record_id": txn.txn_id, "revision": txn.revision,
                                                      "role": "context"}],
                    "gaps": ["The Detective returned no claim for this question."], "next_checks": [],
                    "link_state": state, "linked_to": to, "lookup_results": []}
        return {"claim_id": proposal.claim_id, "question_id": proposal.question_id,
                "statement": proposal.statement, "status": proposal.status,
                "refs": [r.model_dump() for r in proposal.refs], "gaps": list(proposal.gaps),
                "next_checks": [c.model_dump(exclude_none=True) for c in proposal.next_checks],
                "link_state": state, "linked_to": to, "lookup_results": []}

    # ----------------------------------------------------------------------- verdict (code)

    def verdict(self, txn: BankTxn, result: CandidateResult, join: _Join) -> _Outcome:
        chosen = result.chosen
        if chosen is None and result.state == LinkState.AMBIGUOUS and join.choice:
            chosen = next((c for c in result.pool if join.choice in c.record_ids), None)
        if chosen is not None:
            if chosen.tier == "settled":
                return self.linked(txn, chosen, result)
            return self.provisional(txn, chosen, result)
        if result.conflicts and not result.pool:
            return self.contradicted(txn, result.conflicts[0], result)
        if result.state == LinkState.AMBIGUOUS:
            pool = [p for c in result.pool for p in c.payouts]
            gap = (f"{GAP_AMBIGUOUS}: {len(result.pool)} payout candidates ({_ids(pool)}) fit this credit "
                   "equally on amount and date, and nothing retrieved chooses between them.")
            return _Outcome(ClaimStatus.UNRESOLVED, _unknown_source(txn.txn_id, txn), result.summary(),
                            f"Link ambiguous: {len(result.pool)} candidates.", gap=gap,
                            evidence=[txn.ref(), *(p.ref() for p in pool)])
        if len(result.provisional) == 1:
            return self.provisional(txn, result.provisional[0], result)
        if result.coded:
            return self.undecided(txn, result.coded[0], result)
        payouts = self.material.records(kinds=[RecordKind.PAYOUT])
        if not payouts and self.records_err:
            gap = (f"The merchant records could not be retrieved ({self.records_err}), so no payout could be "
                   "checked against this credit.")
            return _Outcome(ClaimStatus.UNRESOLVED, _unknown_source(txn.txn_id, txn), gap,
                            f"Merchant records unavailable ({self.records_err}).", gap=gap, evidence=[txn.ref()])
        if not payouts:
            gap = f"{GAP_NO_RECORDS}: the passport holds no payout record that could explain this credit."
        elif any("differs from the credit" in r.reason for r in result.rejected):
            gap = (f"{GAP_AMOUNT_MISMATCH}: no payout matches the credit's amount and currency; "
                   + "; ".join(r.reason for r in result.rejected if "differs" in r.reason)[:600])
        else:
            gap = (f"{GAP_SETTLEMENT_NOT_FOUND}: no payout within 5 days before the booking explains this "
                   "credit, and no settlement record was retrieved for it.")
        return _Outcome(ClaimStatus.UNRESOLVED, _unknown_source(txn.txn_id, txn), result.summary(),
                        "No candidate payout.", gap=gap,
                        evidence=[txn.ref(), *(p.ref() for r in result.rejected for p in r.payouts)])

    def linked(self, txn: BankTxn, cand: Candidate, result: CandidateResult) -> _Outcome:
        payouts, settlement = cand.payouts, cand.settlement
        assert settlement is not None
        itself = cand.settles_itself      # the payout's own status is the settlement evidence
        what = _what(settlement)
        payout = payouts[0] if len(payouts) == 1 else None
        ref = cand.group_ref or (payout.payment_ref if payout else None) or "none"
        statement = (f"Credit {txn.txn_id} of {_money(txn.amount_pence, txn.currency)} is payout "
                     f"{_ids(payouts)} (reference {ref})")
        unknown = _unknown_source(txn.txn_id, txn)
        evidence = _unique([txn.ref(), *(p.ref() for p in payouts), settlement.ref()])
        parts = [result.summary()]
        components, problem = self.components(payouts)
        calc = None
        if components is not None:
            evidence += [c.ref() for c in components]
            try:
                calc = reconcile.reconcile_credit(txn, components=components, payout=payout,
                                                  settlement=settlement)
            except ReconcileError as exc:
                problem = f"{GAP_AMOUNT_MISMATCH}: the amounts could not be reconciled: {exc}"
        if calc is not None:
            parts.append(_calc_text(calc, components or [], payouts, settlement))
            self.note("calculation", reason=_clip(parts[-1], 300), refs=calc.cited_refs,
                      result=_calc_summary(calc))
        parts.append(_timing_text(settlement, txn))
        if problem:
            parts.append(problem)
            return _Outcome(ClaimStatus.UNRESOLVED, unknown, " ".join(parts), problem, gap=problem,
                            evidence=evidence, calculation=calc)
        own = [f"{r.kind.value} {r.record_id}"
               for r in [*payouts, *([] if itself else [settlement]), *(components or [])]
               if r.source_trust == SourceTrust.SELF_SUPPLIED]
        if own:
            gap = (f"{', '.join(own)} {'is' if len(own) == 1 else 'are'} self-supplied (imported by the "
                   "account holder without an issuer signature), so the source of funds is not "
                   "established from issuer evidence.")
            return _Outcome(ClaimStatus.UNRESOLVED, unknown, " ".join(parts + [gap]),
                            "Self-supplied evidence is not issuer evidence.", gap=gap,
                            evidence=evidence, calculation=calc)
        if cand.reference_matched is None:
            gap = (f"The credit carries no reference to payout {ref} or its settlement; matching amounts "
                   "and dates alone do not establish the source of funds.")
            return _Outcome(ClaimStatus.UNRESOLVED, unknown, " ".join(parts + [gap]),
                            "Only amounts and dates tie the credit to the payout.", gap=gap,
                            evidence=evidence, calculation=calc)
        assert calc is not None
        if calc.balanced is not True:
            gap = (f"{GAP_AMOUNT_MISMATCH}: the payout's components total {calc.total_pence} pence against "
                   f"{calc.target_ref.key() if calc.target_ref else 'the credit'} at {calc.target_pence} "
                   f"pence (difference {calc.difference_pence}).")
            return _Outcome(ClaimStatus.UNRESOLVED, unknown, " ".join(parts + [gap]),
                            "The amounts do not reconcile.", gap=gap, evidence=evidence, calculation=calc)
        if not reconcile.credit_supported(calc):
            gap = (f"{GAP_SETTLEMENT_NOT_FOUND}: {what} does not show the funds reached the bank by the "
                   "booking.")
            return _Outcome(ClaimStatus.UNRESOLVED, unknown, " ".join(parts + [gap]),
                            "The reconciliation does not support the credit.", gap=gap,
                            evidence=evidence, calculation=calc)
        effective = reconcile.settlement_effective_time(settlement)
        if itself:
            statement += (f", whose own status {settlement.status} took effect at "
                          f"{format_utc(effective)}, before the credit was booked.")
            parts.append(f"References: the credit carries {ref}, matching the payout.")
            reason = (f"Payout {settlement.ref().key()} carries status {settlement.status}, effective by the "
                      f"booking; the amounts reconcile and the credit carries {ref}.")
        else:
            statement += f", settled as {settlement.record_id} before the credit was booked."
            parts.append(f"References: the credit carries {ref}, matching the payout and settlement.")
            reason = (f"Settlement {settlement.ref().key()} is {settlement.status} by the booking, the "
                      f"amounts reconcile and the credit carries {ref}.")
        return _Outcome(ClaimStatus.SUPPORTED, statement, " ".join(parts), reason,
                        evidence=evidence, calculation=calc)

    def provisional(self, txn: BankTxn, cand: Candidate, result: CandidateResult) -> _Outcome:
        payouts = cand.payouts
        ref = cand.group_ref or (payouts[0].payment_ref if payouts else None) or payouts[0].record_id
        released = [r for r in self.material.records(kinds=[RecordKind.MESSAGE])
                    if r.status == "released_to_balance"
                    or (r.text and "balance" in r.text.lower() and "released" in r.text.lower())]
        code = GAP_BALANCE_RELEASE if released else GAP_SETTLEMENT_NOT_FOUND
        payout = payouts[0] if len(payouts) == 1 else None
        # Honest about what the payout record itself says: a status that is not decisive
        # (``alibi.agents.candidates.own_status_decisive``) is named, never called absent.
        if payout is None:
            why = "the payout records of the group do not show bank settlement"
        elif payout.status is None:
            why = "a payout record with no status does not show bank settlement"
        elif payout.status in PENDING_STATUSES:
            why = (f"the payout's own status {payout.status}, observed {format_utc(payout.occurred_at)} before "
                   "the booking, is its initial state and does not show bank settlement")
        else:
            why = f"the payout's own status {payout.status} is not a settlement status and does not show bank settlement"
        gap = (f"{code}: no settlement record for payout {ref} shows a transfer to the bank account; {why}"
               + ("; a release to the platform balance is not a settlement." if released else "."))
        misses = [lk for lk in self.material.lookups if lk.tool == "lookup_settlement"
                  and lk.outcome == "not_found" and str(lk.args.get("payout_reference", "")).upper() == str(ref).upper()]
        if misses:
            gap += f" The settlement lookup for {ref} returned no record."
        return _Outcome(ClaimStatus.UNRESOLVED, _unknown_source(txn.txn_id, txn),
                        " ".join([result.summary(), gap]), "No settlement record for the candidate payout.",
                        gap=gap, evidence=[txn.ref(), *(p.ref() for p in payouts)])

    def undecided(self, txn: BankTxn, rejection: Rejection, result: CandidateResult) -> _Outcome:
        """Settlement evidence was retrieved but shows no bank settlement and no explicit
        conflict (a payout still pending before the booking, a settlement too early or with no
        settlement status): unresolved, with the rejection's gap code."""
        settlement = rejection.settlement
        code = rejection.code or GAP_SETTLEMENT_NOT_FOUND
        evidence = _unique([txn.ref(), *(p.ref() for p in rejection.payouts),
                            *([settlement.ref()] if settlement is not None else [])])
        gap = f"{code}: {rejection.reason}; the source of funds is not established."
        parts = [result.summary()]
        if settlement is not None:
            parts.append(_timing_text(settlement, txn))
        return _Outcome(ClaimStatus.UNRESOLVED, _unknown_source(txn.txn_id, txn), " ".join(parts),
                        f"Settlement evidence does not show bank settlement: {rejection.reason}",
                        gap=gap, evidence=evidence)

    def contradicted(self, txn: BankTxn, rejection: Rejection, result: CandidateResult) -> _Outcome:
        code = _conflict_code(rejection, txn)
        settlement = rejection.settlement
        evidence = _unique([txn.ref(), *(p.ref() for p in rejection.payouts),
                            *([settlement.ref()] if settlement is not None else [])])
        gap = f"{code}: {rejection.reason}; the credit's origin remains unresolved."
        parts = [result.summary()]
        if settlement is not None:
            parts.append(_timing_text(settlement, txn))
        return _Outcome(ClaimStatus.CONTRADICTED, _unknown_source(txn.txn_id, txn), " ".join(parts),
                        f"Explicit conflict: {rejection.reason}", gap=gap, evidence=evidence)

    def components(self, payouts: list[EvidenceRecord]) -> tuple[list[EvidenceRecord] | None, str]:
        """The payouts' component records (``related_record_ids``) from the material, listing
        the subject's orders, refunds and fees once more only when one is missing."""
        wanted = list(dict.fromkeys(rid for p in payouts for rid in p.related_record_ids))
        if not wanted:
            return [], ""
        held = {r.record_id: r for r in self.material.records(kinds=COMPONENT_KINDS)}
        if any(rid not in held for rid in wanted):
            records, _ = self.read("list_records", f"list_records components of {_ids(payouts)}",
                                   lambda: self.gateway.list_records(kinds=list(COMPONENT_KINDS)))
            for record in records or []:
                if record.subject_ref == self.rfc.subject_ref:
                    self.material.add_record(record)
            held = {r.record_id: r for r in self.material.records(kinds=COMPONENT_KINDS)}
        missing = [rid for rid in wanted if rid not in held]
        if missing:
            return None, (f"{GAP_MISSING_CITED}: component record{'s' if len(missing) != 1 else ''} "
                          f"{', '.join(missing[:12])} of payout {_ids(payouts)} "
                          f"{'is' if len(missing) == 1 else 'are'} not among the passport's active order, "
                          "refund and fee records.")
        return [held[rid] for rid in wanted], ""

    def challenge(self, outcome: _Outcome, out: ChallengerOutput, txn: BankTxn,
                  result: CandidateResult) -> _Outcome:
        """Apply the Challenger's last proposal: a downgrade only, never an upgrade.

        ``contradicted`` is accepted only when every conflicting ref the Challenger cites is a
        payout or settlement with an explicit-field conflict against this credit (``result``
        is the final candidate rule run). Otherwise the proposal counts as ``unresolved``: a
        ``supported`` verdict is left unresolved for the holder, any other verdict stands.
        A message record is never added to the bank-facing evidence.
        """
        proposed = out.proposed_status
        if proposed == "contradicted" and outcome.status != ClaimStatus.CONTRADICTED:
            refs = self.shareable_refs(out.conflicting_refs, "challenger", "conflicting_refs")
            field_conflicts = [ref for ref in refs if _field_conflict(self.material.get(ref.ref_id, ref.revision),
                                                                      txn, result)]
            if refs and len(field_conflicts) == len(refs):
                gap = (f"challenger_contradicted: {', '.join(r.key() for r in refs)} conflict"
                       f"{'s' if len(refs) == 1 else ''} with the claim that this payout funded the credit; "
                       "the credit's origin remains unresolved.")
                revised = replace(outcome, status=ClaimStatus.CONTRADICTED,
                                  statement=_unknown_source(txn.txn_id, txn),
                                  rationale=f"{outcome.rationale} {gap}",
                                  reason="Challenger proposed contradicted; every conflicting ref has an "
                                         "explicit-field conflict.",
                                  gap=gap, evidence=_unique([*outcome.evidence, *refs]))
                self.note("status_change", reason=revised.reason, refs=revised.evidence,
                          result={"from": outcome.status.value, "to": revised.status.value,
                                  "decided_by": "challenger"})
                return revised
            self.note("note", actor="challenger",
                      reason="Challenger proposed contradicted without an explicit-field conflict; "
                             + ("the supported verdict is left unresolved." if outcome.status == ClaimStatus.SUPPORTED
                                else "code verdict stands."),
                      result={"proposed": proposed, "kept": outcome.status.value,
                              "conflicting": [r.key() for r in refs],
                              "field_conflicts": [r.key() for r in field_conflicts]})
            proposed = "unresolved"
        if proposed == "unresolved" and outcome.status == ClaimStatus.SUPPORTED:
            gap = ("challenger_unresolved: the Challenger did not confirm bank settlement from the "
                   "retrieved records, so the claim is left unresolved for the holder to review.")
            revised = replace(outcome, status=ClaimStatus.UNRESOLVED, statement=_unknown_source(txn.txn_id, txn),
                              rationale=f"{outcome.rationale} {gap}", reason="Challenger proposed unresolved.",
                              gap=gap)
        else:
            if proposed == "supported" and outcome.status != ClaimStatus.SUPPORTED:
                self.note("note", reason=f"Challenger proposed supported; code verdict {outcome.status.value} "
                                         "stands (a proposal never upgrades a status).",
                          result={"proposed": proposed, "kept": outcome.status.value})
            return outcome
        self.note("status_change", reason=revised.reason, refs=revised.evidence,
                  result={"from": outcome.status.value, "to": revised.status.value, "decided_by": "challenger"})
        return revised

    def shareable_refs(self, cited: Iterable[Any], actor: str, path: str) -> list[EvidenceRef]:
        """The cited refs that are in the material and are not ``message`` records; a message
        ref is traced as refused and never reaches ``Claim.evidence_refs``."""
        kept: list[EvidenceRef] = []
        for cite in cited:
            record_id = getattr(cite, "record_id", None) or getattr(cite, "ref_id", None)
            revision = getattr(cite, "revision", None)
            ref = self.material.ref_for(record_id, revision) if record_id else None
            if ref is None:
                continue
            item = self.material.get(ref.ref_id, ref.revision)
            if isinstance(item, EvidenceRecord) and item.kind == RecordKind.MESSAGE:
                self.note("validation", actor=actor, outcome="refused", refs=[ref],
                          result={"role": actor, "path": path, "record_id": ref.ref_id, "revision": ref.revision,
                                  "reason": "message records stay holder-side"},
                          reason=f"{path}: {ref.key()} is a message record; it stays holder-side")
                continue
            kept.append(ref)
        return _unique(kept)

    def record_link(self, txn: BankTxn, result: CandidateResult, join: _Join) -> None:
        pool = [p for c in result.pool for p in c.payouts]
        state, chosen, method = result.state, result.chosen, "rule"
        # Link.signals holds code-generated text only (the Matcher's signals are in the trace).
        signals = [_clip(s, 300) for s in [result.summary()] if s and s.strip()][:12]
        if chosen is None and state == LinkState.AMBIGUOUS and join.choice:
            chosen = next((c for c in result.pool if join.choice in c.record_ids), None)
            if chosen is not None:
                state, method = LinkState.LINKED, "agent"
                signals = [_clip(s, 300) for s in [join.note, *signals] if s and s.strip()][:12]
        if state == LinkState.AMBIGUOUS and len(pool) < 2:
            state = LinkState.UNMATCHED
        link = Link(link_id=make_id("lnk", self.run_id, txn.txn_id), txn_ref=txn.ref(), link_state=state,
                    record_ref=chosen.payouts[0].ref() if (chosen is not None and state == LinkState.LINKED) else None,
                    candidates=[p.ref() for p in pool], method=method if state == LinkState.LINKED else None,
                    signals=signals, run_id=self.run_id, created_at=self.now)
        self.links[txn.txn_id] = link
        self.note("validation", tool="link", reason=_clip(f"Link {state.value} ({method}).", 300),
                  refs=[txn.ref(), *link.candidates],
                  result={"link_id": link.link_id, "link_state": state.value, "method": link.method or "none",
                          "record_id": link.record_ref.ref_id if link.record_ref else "none"})

    # ----------------------------------------------------------------------- period totals (code)

    def period_total(self, found: Classification) -> _Outcome:
        period = self.rfc.period
        assert period is not None
        span = f"[{format_utc(period.from_)}, {format_utc(period.to)})"
        label = _join([_LABELS[k] for k in found.kinds])
        unknown = f"Documented {label} over {span} are not established from the available records."
        records, err = self.read("list_records", f"list_records {'/'.join(k.value for k in found.kinds)} {span}",
                                 lambda: self.gateway.list_records(kinds=list(found.kinds), period=period))
        if records is None:
            gap = f"The {label} records for this period could not be retrieved ({err})."
            return _Outcome(ClaimStatus.UNRESOLVED, unknown, gap, gap, gap=gap)
        for record in records:
            if record.subject_ref == self.rfc.subject_ref:
                self.material.add_record(record)
        records = sorted((r for r in records if r.kind in found.kinds and r.subject_ref == self.rfc.subject_ref
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
        skipped_text = ("Not counted: " + ", ".join(f"{r.record_id} (status {r.status})" for r in skipped[:12]) + "."
                        if skipped else "")
        if unclear:
            gap = (f"The status of {', '.join(f'{r.record_id} ({r.status})' for r in unclear[:12])} does not show "
                   f"whether {'it' if len(unclear) == 1 else 'they'} went through, so the documented {label} "
                   f"total over {span} is not established.")
            return _Outcome(ClaimStatus.UNRESOLVED, unknown, " ".join(filter(None, [gap, skipped_text])),
                            f"{len(unclear)} record(s) with an unrecognised status.", gap=gap,
                            evidence=[r.ref() for r in unclear])
        if not counted:
            gap = " ".join(filter(None, [f"{GAP_NO_RECORDS}: no documented {label} records are in the passport "
                                         f"for {span}.", skipped_text]))
            return _Outcome(ClaimStatus.UNRESOLVED, unknown, gap, f"No {label} records found.", gap=gap)
        net = found.category == NET_TOTAL
        signs = SIGN_BY_KIND if net else {found.kinds[0]: 1}
        try:
            calc = reconcile.sum_terms(counted, method=found.category, signs=signs)
        except ReconcileError as exc:
            gap = f"{GAP_AMOUNT_MISMATCH}: the {label} records could not be summed: {exc}"
            return _Outcome(ClaimStatus.UNRESOLVED, unknown, gap, gap, gap=gap, evidence=[r.ref() for r in counted])
        total = _money(calc.total_pence, calc.currency)
        groups = [(kind, [r for r in counted if r.kind == kind]) for kind in found.kinds]
        subtotals = [f"{_LABELS[kind]} {sum(r.amount_pence or 0 for r in rows)} from {len(rows)} record"
                     f"{'s' if len(rows) != 1 else ''} ({', '.join(f'{r.record_id} {r.amount_pence}' for r in rows[:12])})"
                     if rows else f"{_LABELS[kind]} 0 (no counted records)" for kind, rows in groups]
        if net:
            formula = " ".join(f"{'+' if signs[kind] > 0 else '-'} {_LABELS[kind]} {sum(r.amount_pence or 0 for r in rows)}"
                               for kind, rows in groups)
            statement = f"Documented {label} over {span} net to {total} ({formula.lstrip('+ ')})."
        else:
            statement = (f"Documented {label} over {span} total {total} across {len(counted)} record"
                         f"{'s' if len(counted) != 1 else ''}.")
        rationale = [f"Summed in code from the passport records dated in {span}: " + "; ".join(subtotals)
                     + f". Total {total}."]
        own = [r for r in counted if r.source_trust == SourceTrust.SELF_SUPPLIED]
        if own:
            rationale.append(f"Includes {len(own)} self-supplied record{'s' if len(own) != 1 else ''} "
                             f"({_ids(own)}) imported by the account holder without an issuer signature.")
        if skipped_text:
            rationale.append(skipped_text)
        self.note("calculation", reason=_clip(f"{found.category}: {total}", 300), refs=calc.cited_refs,
                  result=_calc_summary(calc))
        return _Outcome(ClaimStatus.SUPPORTED, statement, " ".join(rationale),
                        f"{len(counted)} documented records sum to {total}.",
                        evidence=list(calc.cited_refs), calculation=calc)

    def unmapped(self, found: Classification) -> _Outcome:
        if found.category == SEVERAL_TOTALS:
            gap = (f"The question names {_join([_LABELS[k] for k in found.kinds])} without asking for a net "
                   "figure; one documented total, or a net total when asked for, can be given.")
            reason = "Question names several totals."
        else:
            gap, reason = UNMAPPED_GAP, "Question not mapped to a check."
        return _Outcome(ClaimStatus.UNRESOLVED, "The investigation did not establish an answer to this question.",
                        gap, reason, gap=gap)

    # ----------------------------------------------------------------------- curator

    def curate(self, claims: list[tuple[Question, _Outcome]]) -> list[tuple[Question, _Outcome]]:
        # Whatever the Curator does (or fails to do), a message record never stays in the evidence.
        stripped = []
        for question, outcome in claims:
            self.question_id = question.question_id
            stripped.append((question, replace(outcome, evidence=self.shareable_refs(outcome.evidence, ACTOR,
                                                                                      "evidence_refs"))))
        self.question_id = None
        claims = stripped
        ids = {q.question_id: make_id("clm", self.run_id, q.question_id) for q, _ in claims}
        shareable: dict[tuple[str, int], Any] = {}
        for _, outcome in claims:
            for ref in [*outcome.evidence, *(outcome.calculation.cited_refs if outcome.calculation else [])]:
                item = self.material.get(ref.ref_id, ref.revision)
                if item is None or (isinstance(item, EvidenceRecord) and item.kind == RecordKind.MESSAGE):
                    continue
                shareable[(ref.ref_id, ref.revision)] = item
        if not shareable:
            return claims
        claim_dicts = [{"claim_id": ids[q.question_id], "question_id": q.question_id, "status": o.status.value,
                        "statement": o.statement, "gap": o.gap,
                        "refs": [f"{r.ref_id}@{r.revision}" for r in o.evidence]} for q, o in claims]
        rr = self.call("curator", roles.run_curator,
                       curator_input(self.material, self.rfc, claim_dicts, list(shareable.values()),
                                     self.policy_summary()),
                       shareable=set(shareable))
        if rr is None:
            return claims
        out = rr.output
        if out.exclusions:   # private: holder-side trace only
            self.note("note", actor="curator", reason=f"Curator excluded {len(out.exclusions)} item(s) (private).",
                      refs=[ref for ref in (self.material.ref_for(e.record_id, e.revision) for e in out.exclusions)
                            if ref is not None],
                      result={"exclusions": [{"record_id": e.record_id, "revision": e.revision,
                                              "reason": e.reason, "note": e.note} for e in out.exclusions]})
        gapped = {g.question_id for g in out.gaps}
        curated = []
        for question, outcome in claims:
            self.question_id = question.question_id
            disclosed = {(d.record_id, d.revision) for d in out.disclosures if d.question_id == question.question_id}
            kept = [ref for ref in outcome.evidence if (ref.ref_id, ref.revision) in disclosed]
            cited = outcome.calculation.cited_refs if outcome.calculation else []
            final = _unique([*kept, *cited])
            dropped = [ref for ref in outcome.evidence if ref.key() not in {r.key() for r in final}]
            if not final and outcome.status != ClaimStatus.UNRESOLVED and outcome.evidence:
                final, dropped = list(outcome.evidence), []
                self.note("note", actor="curator", reason="Curator kept no evidence for a decided claim; code "
                                                          "keeps its validated refs.")
            gap = outcome.gap
            # The generic withholding gap goes out only when something was in fact withheld; a
            # Curator "gap" with nothing dropped is holder-side commentary and stays in the trace.
            if question.question_id in gapped and dropped:
                gap = f"{gap} {GENERIC_CURATOR_GAP}" if gap else GENERIC_CURATOR_GAP
            self.note("note", actor="curator", refs=final,
                      reason=f"Curator kept {len(final)} of {len(outcome.evidence)} cited item(s) for the claim.",
                      result={"kept": [r.key() for r in final], "dropped": [r.key() for r in dropped],
                              "curator_gap": question.question_id in gapped})
            curated.append((question, replace(outcome, evidence=final, gap=gap)))
        self.question_id = None
        return curated

    def policy_summary(self) -> dict[str, Any]:
        """Rule kinds and fields of the subject's active policy (never rule texts)."""
        try:
            compiled = compile_policy(self.store.latest_policy(self.rfc.subject_ref))
        except Exception as exc:  # noqa: BLE001 - a bad policy is reported, not fatal
            self.note("note", actor="curator", outcome="error", reason=f"policy unavailable ({type(exc).__name__})")
            return {"version": None, "rules": []}
        rules = []
        for rule in compiled.rules:
            item: dict[str, Any] = {"type": rule.type, "kinds": [k.value for k in rule.kinds]}
            for name in ("fields", "level", "max_age_days", "allow"):
                value = getattr(rule, name, None)
                if value is not None:
                    item[name] = list(value) if isinstance(value, (list, tuple)) else value
            rules.append(item)
        return {"version": compiled.policy_version, "rules": rules}

    # ----------------------------------------------------------------------- persistence

    def finish(self, claims: list[tuple[Question, _Outcome]]) -> InvestigationResult:
        built = []
        for question, outcome in claims:
            self.question_id = question.question_id
            claim = Claim(claim_id=make_id("clm", self.run_id, question.question_id), request_id=self.rfc.request_id,
                          audience=self.rfc.audience,
                          question_id=question.question_id, statement=_clip(outcome.statement, 4000),
                          status=outcome.status, rationale=_clip(outcome.rationale, 4000),
                          evidence_refs=answered_refs(outcome.status, outcome.evidence),
                          examined_refs=examined_refs(outcome.status, outcome.evidence, outcome.calculation),
                          calculation=answered_calculation(outcome.status, outcome.calculation),
                          gap=_clip(outcome.gap, 4000) if outcome.gap else None, run_id=self.run_id,
                          facts=derive_facts(self.store, self.rfc, question, outcome.status,
                                             outcome.evidence, outcome.calculation),
                          created_at=self.now)
            self.note("status_change", reason=outcome.reason, refs=[*claim.evidence_refs, *claim.examined_refs],
                      result={"claim_id": claim.claim_id, "status": claim.status.value, "final": True})
            built.append(claim)
        self.question_id = None
        final = {c.question_id: c.status for c in built}
        unsafe_supported = [qid for qid in self.supported_proposals if final.get(qid) != ClaimStatus.SUPPORTED]
        self.note("note", reason="Run complete.",
                  result={"lookups": self.material.lookup_count, "observed_inputs": self.material.observed_inputs,
                          "model_calls": self.model_calls, "tokens": self.tokens,
                          "action_budget_used": self.budget.used, "rounds": self.rounds,
                          "unsafe_proposals": len(unsafe_supported) + self.unsafe_disclosures,
                          "unsafe_supported": unsafe_supported, "unsafe_disclosures": self.unsafe_disclosures})
        gateway_events = [TraceEvent.model_validate(e) for e in getattr(self.gateway, "trace", [])]
        base = max((e.seq for e in gateway_events), default=-1) + 1
        own_events = [TraceEvent(trace_id=make_id("trc", self.run_id, ACTOR, i), run_id=self.run_id, seq=base + i,
                                 at=self.now, actor=event.pop("actor"), request_id=self.rfc.request_id, **event)
                      for i, event in enumerate(self.events)]
        with self.store.transaction():
            for link in self.links.values():
                self.store.save_link(link)
            for claim in built:
                self.store.save_claim(claim)
            for alert in self.alerts:
                self.store.save_alert(alert)
            flush = getattr(self.gateway, "flush_trace", None)
            if callable(flush):
                flush(self.store)
            else:
                self.store.append_traces(gateway_events)
            if own_events:
                self.store.append_traces(own_events)
        stored = {c.claim_id: c for c in self.store.list_claims(request_id=self.rfc.request_id, state=None)}
        links = []
        for link in self.links.values():
            saved = {l.link_id: l for l in self.store.list_links(txn_id=link.txn_ref.ref_id, state=None)}
            links.append(saved.get(link.link_id, link))
        return InvestigationResult(run_id=self.run_id, request_id=self.rfc.request_id, mode=MODE, links=links,
                                   claims=[stored.get(c.claim_id, c) for c in built],
                                   trace=[*gateway_events, *own_events],
                                   calls_used=int(getattr(self.gateway, "calls_used", 0)),
                                   unavailable=list(self.unavailable))


# --------------------------------------------------------------------------- helpers


def _clock(now: datetime | None) -> datetime:
    if now is None:
        return utc_now()
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise ValueError("now must be a timezone-aware datetime")
    return now


def _link_view(result: CandidateResult, choice: str | None) -> tuple[str, str | None]:
    """``(link_state, linked_to)`` for the role inputs, honouring an accepted Matcher choice."""
    if result.chosen is not None:
        return LinkState.LINKED.value, result.chosen.payouts[0].record_id
    if result.state == LinkState.AMBIGUOUS and choice and any(choice in c.record_ids for c in result.pool):
        return LinkState.LINKED.value, choice
    return result.state.value, None


def _pool_refs(result: CandidateResult) -> list[EvidenceRef]:
    refs = [p.ref() for c in result.pool for p in c.payouts]
    refs += [c.settlement.ref() for c in result.settled if c.settlement is not None]
    refs += [p.ref() for r in result.rejected for p in r.payouts]
    return refs


def _field_conflict(item: Any, txn: BankTxn, result: CandidateResult) -> bool:
    """True when ``item`` is a payout or settlement with an explicit-field conflict against the
    credit: one of a ``result.conflicts`` rejection's records, a failed payout, or a settlement
    that failed, was pending at or after the booking, took effect after it, or is for another
    amount or currency. Prose, orders, the credit itself and messages never qualify."""
    if not isinstance(item, EvidenceRecord):
        return False
    key = (item.record_id, item.revision)
    for rejection in result.conflicts:
        if any((p.record_id, p.revision) == key for p in rejection.payouts):
            return True
        if rejection.settlement is not None and (rejection.settlement.record_id,
                                                 rejection.settlement.revision) == key:
            return True
    if item.kind == RecordKind.PAYOUT:
        # A payout carrying a status is settlement evidence for itself: failed, still pending
        # when observed at or after the booking, or effective only after it conflicts; a
        # payout with no status, or pending before the booking, conflicts with nothing.
        if item.status in FAILED_STATUSES:
            return True
        if not reconcile.is_settlement_evidence(item):
            return False
        if item.status in PENDING_STATUSES and item.occurred_at >= txn.booked_at:
            return True
        effective = reconcile.settlement_effective_time(item)
        return effective is not None and effective > txn.booked_at
    if item.kind != RecordKind.SETTLEMENT:
        return False
    if item.status in FAILED_STATUSES:
        return True
    if item.status in PENDING_STATUSES and item.occurred_at >= txn.booked_at:
        return True
    effective = reconcile.settlement_effective_time(item)
    if effective is not None and effective > txn.booked_at:
        return True
    return (item.amount_pence, item.currency) != (txn.amount_pence, txn.currency)


_ALERT_LABELS = {"unexplained_credit": "Unexplained credit", "unusual_inflow": "Unusual inflow",
                 "unusual_outflow": "Unusual outflow", "amount_discrepancy": "Amount discrepancy",
                 "settlement_discrepancy": "Settlement discrepancy", "merchant_unverified": "Merchant unverified"}
_ALERT_WHY = {
    "unexplained_credit": "No settled payout in the passport explains this credit.",
    "unusual_inflow": "This inflow does not match the pattern of the passport's payouts.",
    "unusual_outflow": "This outflow does not match the pattern of the passport's records.",
    "amount_discrepancy": "The amounts of the cited records do not agree.",
    "settlement_discrepancy": "The cited settlement does not agree with the credit it should explain.",
    "merchant_unverified": "The cited records come from a merchant the registry does not verify.",
}


def _item_text(item: Any) -> str:
    """One record or bank entry as ids, kind, status, amount and time: no free text."""
    if isinstance(item, BankTxn):
        return (f"{'credit' if item.is_credit else 'debit'} {item.txn_id}@{item.revision} of "
                f"{_money(abs(item.amount_pence), item.currency)} booked {format_utc(item.booked_at)}")
    text = f"{item.kind.value} {item.record_id}@{item.revision}"
    details = [f"status {item.status}" if item.status else "",
               _money(item.amount_pence, item.currency) if item.amount_pence is not None else "",
               f"at {format_utc(item.occurred_at)}"]
    return text + " (" + ", ".join(d for d in details if d) + ")"


def _alert_text(kind: str, items: list[Any]) -> tuple[str, str]:
    """``(title, explanation)`` for a holder alert, written in code from the kind and the
    validated items only (the Detective's prose stays in the trace)."""
    label = _ALERT_LABELS.get(kind, kind.replace("_", " ").capitalize())
    ids = ", ".join(f"{getattr(i, 'txn_id', None) or getattr(i, 'record_id', '')}@{i.revision}" for i in items[:6])
    title = _clip(f"{label}: {ids}" if ids else label, 300)
    listed = "; ".join(_item_text(i) for i in items[:12])
    explanation = f"{label}. Records: {listed}. {_ALERT_WHY.get(kind, '')}".strip()
    return title, _clip(explanation, 4000)


def _conflict_code(rejection: Rejection, txn: BankTxn) -> str:
    """The team gap code for an explicit conflict, from the records' fields (the candidate
    rule's own code when it set one)."""
    if rejection.code:
        return rejection.code
    settlement = rejection.settlement
    if settlement is None:
        return GAP_SETTLEMENT_FAILED if any(p.status in FAILED_STATUSES for p in rejection.payouts) else GAP_AMOUNT_MISMATCH
    if settlement.status in FAILED_STATUSES:
        return GAP_SETTLEMENT_FAILED
    if settlement.status in PENDING_STATUSES:
        return GAP_PENDING_AT_BOOKING
    effective = reconcile.settlement_effective_time(settlement)
    if effective is not None and effective > txn.booked_at:
        return GAP_SETTLEMENT_AFTER
    return GAP_AMOUNT_MISMATCH


def _calc_text(calc: Calculation, components: list[EvidenceRecord], payouts: list[EvidenceRecord],
               settlement: EvidenceRecord | None) -> str:
    pieces = []
    for kind, sign in ((RecordKind.ORDER, ""), (RecordKind.REFUND, "- "), (RecordKind.FEE, "- ")):
        rows = [r for r in components if r.kind == kind]
        pieces.append(f"{sign}{_LABELS[kind]} {sum(r.amount_pence or 0 for r in rows)} ({_ids(rows) or 'none'})")
    text = (f"Calculation in code: {' '.join(pieces)} = {_money(calc.total_pence, calc.currency)}, against "
            f"{calc.target_ref.key() if calc.target_ref else 'no target'} at {calc.target_pence}: difference "
            f"{calc.difference_pence}, {'balanced' if calc.balanced else 'not balanced'}.")
    amounts = " Payout " + ", ".join(f"{p.record_id} is {p.amount_pence}" for p in payouts)
    if settlement is not None and not any(settlement.record_id == p.record_id for p in payouts):
        amounts += f" and settlement {settlement.record_id} is {settlement.amount_pence}"
    return text + amounts + "."


def _what(settlement: EvidenceRecord) -> str:
    """``payout <key>`` when the settlement evidence is a payout's own status, else
    ``settlement <key>``: the trace never calls a payout a settlement record."""
    label = "payout" if settlement.kind == RecordKind.PAYOUT else "settlement"
    return f"{label} {settlement.ref().key()}"


def _timing_text(settlement: EvidenceRecord, txn: BankTxn) -> str:
    text = (f"Timing: {_what(settlement)} status {settlement.status or 'none'}, observed "
            f"{format_utc(settlement.occurred_at)}")
    if settlement.effective_at is not None:
        text += f", effective {format_utc(settlement.effective_at)}"
    text += f"; credit booked {format_utc(txn.booked_at)}."
    try:
        return f"{text} {reconcile.check_settlement_timing(settlement, txn).reason}"
    except ReconcileError:
        return text


def _calc_summary(calc: Calculation) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "method": calc.method, "currency": calc.currency, "total": calc.total_pence,
        "terms": [{"label": t.kind, "sign": t.sign, "amount": t.amount_pence, "source_ids": [t.ref.key()]}
                  for t in calc.terms]}
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
    return (f"The source of {kind} {txn_id} of {_money(abs(txn.amount_pence), txn.currency)} is not "
            "established from the available records.")


def _money(pence: int | None, currency: str | None) -> str:
    return f"{pence} pence {currency}"


def _ids(records: list[EvidenceRecord], limit: int = 12) -> str:
    ids = [r.record_id for r in records]
    more = f" and {len(ids) - limit} more" if len(ids) > limit else ""
    return ", ".join(ids[:limit]) + more


def _join(words: list[str]) -> str:
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1]


def _unique(refs: Iterable[EvidenceRef]) -> list[EvidenceRef]:
    seen: dict[str, EvidenceRef] = {}
    for ref in refs:
        seen.setdefault(ref.key(), ref)
    return list(seen.values())


def _clip(text: str | None, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."
