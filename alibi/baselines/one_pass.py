"""One-pass Gemini baseline: one model call proposes the whole draft over a fixed retrieval.

This is baseline (2) of the SPEC comparison. It gets the same starting input, candidate pool
and deterministic validation and policy checks as full Alibi, but no adaptive retrieval and no
challenger:

1. **Fixed retrieval** (``FIXED_RETRIEVAL``, predeclared): read the RFC's bank entries and
   every record in the passport, then look up the settlement of any payout reference that a
   ``message`` record (the team's ``balance_release``) names in its text. Nothing else is
   retrieved. On the Maya fixtures the message names no reference, so the baseline performs
   zero lookups and never sees the settlement; that is its honest behaviour.
2. **One call** to Gemini (``alibi/agents/prompts/one_pass.md``) with the RFC, bank entries,
   merchant records, settlements, shareable items, policy scope and the candidate rule. The
   answer is parsed as ``OnePassOutput`` and every reference is validated against the
   retrieved ``Material``; unknown refs and non-shareable disclosures are dropped and traced.
3. **Code decides.** Statuses are recomputed from explicit fields with
   ``alibi.agents.candidates.settlement_candidates`` and ``alibi.core.reconcile``: supported
   only when one settled candidate in time, carrying a reference the credit names, reconciles;
   contradicted on an explicit conflict; else unresolved with a team gap code. Period totals
   are summed in code. The model's status is recorded as ``proposed_status`` in the trace.
   A model statement is kept only when its status equals the code status, it names no id or
   amount outside the material and repeats no message text (the whole message, any sentence
   of three or more words, or any four-word run); otherwise a code statement is used. Message
   records never reach bank-facing fields. A model claim's refs join ``Claim.evidence_refs``
   only when the item is in the shareable set the policy allows; the rest are traced as
   refused.
4. **Policy gate**: ``alibi.core.package.build_body`` runs over the claims with the subject's
   latest policy. ``unsafe_proposals`` counts model disclosures the gate did not attach, model
   claim refs outside the shareable set, plus model claims proposed ``supported`` whose code
   verdict is not supported. ``model_calls`` follows ``alibi.agents.roles.call_cost``: a
   cached or replayed answer counts no call, an answer that failed the schema counts one, a
   transport failure or budget refusal counts none. A replay client is labelled in the trace
   like the agents runner does.
5. Links (method ``rule``), claims and the trace are saved like the rules engine does; the
   result has ``mode="single_call"``.

Gap codes used: ``settlement_not_found``, ``balance_release_not_settlement``,
``settlement_after_credit``, ``settlement_pending_at_booking``, ``settlement_failed``,
``ambiguous_candidates``, ``amount_mismatch``, ``missing_cited_record``,
``no_merchant_records`` (the nine team codes) plus ``reference_not_carried`` (a settled
candidate the credit does not name) and ``model_unavailable`` (no proposal was obtained).
Nothing here is called from tests with a real network: pass ``alibi.llm.fake.FakeClient``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from pydantic import ValidationError

from alibi.agents.candidates import RULE_TEXT, Candidate, CandidateResult, Rejection, settlement_candidates
from alibi.agents.material import Material, record_view, rfc_view, shareable_item, txn_view
from alibi.agents.models import OnePassClaim, OnePassOutput, parse_output, undeclared_fields, validate_refs
from alibi.agents.roles import call_cost
from alibi.core import investigate_rules as core_rules
from alibi.core import reconcile
from alibi.core.canon import make_id
from alibi.core.investigate_rules import COMPONENT_KINDS, COUNTED_STATUSES, EXCLUDED_STATUSES, classify_question
from alibi.core.package import PackageError, build_body
from alibi.core.policy import CompiledPolicy, compile_policy
from alibi.core.reconcile import FAILED_STATUSES, PENDING_STATUSES, SIGN_BY_KIND, ReconcileError
from alibi.core.schemas import (
    AgeRule,
    BankTxn,
    Calculation,
    Claim,
    ClaimStatus,
    DenyRule,
    EvidenceRecord,
    EvidenceRef,
    GeneraliseRule,
    InvestigationResult,
    Link,
    LinkState,
    Question,
    RecipientRule,
    RecordKind,
    RequestForContext,
    SourceTrust,
    TraceEvent,
    format_utc,
    utc_now,
)
from alibi.core.signing import DEFAULT_KEY_DIR, DemoKeystore
from alibi.core.store import Store
from alibi.gateway import DEFAULT_BUDGET, Gateway, GatewayError
from alibi.llm import config
from alibi.llm.client import BudgetExceeded as LLMBudgetExceeded
from alibi.llm.client import CallBudget, LLMError, SchemaValidationError
from alibi.llm.client import Unavailable as LLMUnavailable
from alibi.llm.prompts import load_prompt

__all__ = ["ACTOR", "FIXED_RETRIEVAL", "LINK_METHOD", "MODE", "ROLE", "OnePassRun", "next_run_id",
           "run_investigation", "run_one_pass"]

MODE = "single_call"
ACTOR = "one_pass"
ROLE = "one_pass"
LINK_METHOD = "rule"
FIXED_RETRIEVAL = "look up any payout reference named in a balance_release message"

GAP_SETTLEMENT_NOT_FOUND = "settlement_not_found"
GAP_BALANCE_RELEASE = "balance_release_not_settlement"
GAP_SETTLEMENT_AFTER = "settlement_after_credit"
GAP_SETTLEMENT_PENDING = "settlement_pending_at_booking"
GAP_SETTLEMENT_FAILED = "settlement_failed"
GAP_AMBIGUOUS = "ambiguous_candidates"
GAP_AMOUNT_MISMATCH = "amount_mismatch"
GAP_MISSING_RECORD = "missing_cited_record"
GAP_NO_RECORDS = "no_merchant_records"
GAP_REFERENCE = "reference_not_carried"
GAP_MODEL = "model_unavailable"

_LABELS = {RecordKind.ORDER: "sales", RecordKind.REFUND: "refunds", RecordKind.FEE: "fees"}
_RANK = {ClaimStatus.SUPPORTED: 0, ClaimStatus.UNRESOLVED: 1, ClaimStatus.CONTRADICTED: 2}
_REF_TOKEN = re.compile(r"[A-Z0-9][A-Z0-9_./:-]*")
# An identifier in free text: letters, then an underscore or hyphen, and at least one digit.
_ID_TOKEN = re.compile(r"(?<![\w-])[A-Za-z][A-Za-z0-9]*[_-][A-Za-z0-9_-]*\d[A-Za-z0-9_-]*(?![\w-])")
_ISO_TIME = re.compile(r"\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?Z?)?")
_DIGITS = re.compile(r"\d{3,}")
_WORD = re.compile(r"[a-z0-9]+")
_MIN_SENTENCE_WORDS = 3
_NGRAM = 4


@dataclass
class OnePassRun:
    """One baseline run: the stored result, the pre-policy proposal and the SPEC counts."""

    result: InvestigationResult
    proposal: dict[str, Any]
    unsafe_proposals: int
    lookups: int
    observed_inputs: int
    model_calls: int
    tokens: int


@dataclass
class _Outcome:
    """The code verdict for one transaction or one question."""

    status: ClaimStatus
    statement: str
    rationale: str
    reason: str
    gap: str | None = None
    evidence: list[EvidenceRef] = field(default_factory=list)
    calculation: Calculation | None = None


# --------------------------------------------------------------------------- public entry points


def next_run_id(store: Store, request_id: str, *, audience: str | None = None) -> str:
    """``make_id("run", audience, request_id, "single_call", n)`` (no bank: ``make_id("run",
    request_id, "single_call", n)``) for the first ``n`` no stored claim or trace of this request
    uses; request ids are per bank."""
    used = {claim.run_id for claim in store.list_claims(request_id=request_id, state=None)}
    n = 0
    while True:
        run_id = make_id("run", audience, request_id, MODE, n) if audience else make_id("run", request_id, MODE, n)
        if run_id not in used and not store.list_traces(run_id):
            return run_id
        n += 1


def run_investigation(store: Store, request: str | RequestForContext, *, client: Any,
                      gateway: Any | None = None, settlement_lookup: Callable | None = None,
                      txn_refresh: Callable | None = None, budget: int = DEFAULT_BUDGET,
                      registry: Any | None = None, now: datetime | None = None,
                      model: str | None = None) -> InvestigationResult:
    """Run the one-pass baseline for a stored request and return the saved result
    (``mode="single_call"``). Same arguments as ``alibi.api.run_investigation`` plus
    ``client`` (``GeminiClient`` or ``FakeClient``) and an optional ``model`` id."""
    return run_one_pass(store, request, client=client, gateway=gateway,
                        settlement_lookup=settlement_lookup, txn_refresh=txn_refresh,
                        budget=budget, registry=registry, now=now, model=model).result


def run_one_pass(store: Store, request: str | RequestForContext, *, client: Any,
                 gateway: Any | None = None, settlement_lookup: Callable | None = None,
                 txn_refresh: Callable | None = None, budget: int = DEFAULT_BUDGET,
                 registry: Any | None = None, now: datetime | None = None,
                 model: str | None = None) -> OnePassRun:
    """Run the baseline and return the result with its proposal and counts.

    Without ``gateway`` one is built for the request's subject under the next single_call run
    id, with the connector callables, budget and registry given. The model is called at most
    once (a ``CallBudget`` of 1). Never raises for a model or connector failure: those become
    unresolved claims and typed trace events.
    """
    rfc = request if isinstance(request, RequestForContext) else store.get_request(request)
    at = _clock(now)
    if gateway is None:
        if registry is None and settlement_lookup is not None:
            registry = DemoKeystore(DEFAULT_KEY_DIR).ensure().registry()
        gateway = Gateway(store, subject_ref=rfc.subject_ref,
                          run_id=next_run_id(store, rfc.request_id, audience=rfc.audience), registry=registry,
                          budget=budget, settlement_lookup=settlement_lookup,
                          txn_refresh=txn_refresh, now=now, request_id=rfc.request_id)
    scope = getattr(gateway, "subject_ref", None)
    if scope is not None and scope != rfc.subject_ref:
        raise ValueError("the gateway is scoped to a different subject than the request")
    run_id = getattr(gateway, "run_id", None) or next_run_id(store, rfc.request_id, audience=rfc.audience)
    run = _Run(rfc, store, gateway, client, run_id=run_id, now=at,
               model=model or config.FLASH)
    return run.execute()


# --------------------------------------------------------------------------- the run


class _Run:
    def __init__(self, rfc: RequestForContext, store: Store, gateway: Any, client: Any, *,
                 run_id: str, now: datetime, model: str):
        self.rfc, self.store, self.gateway, self.client = rfc, store, gateway, client
        self.run_id, self.now, self.model = run_id, now, model
        self.material = Material(rfc.subject_ref)
        self.events: list[dict[str, Any]] = []
        self.unavailable: list[str] = []
        self.links: dict[str, Link] = {}
        self.question_id: str | None = None
        self.compiled: CompiledPolicy = compile_policy(store.latest_policy(rfc.subject_ref))
        self.shareable: list[EvidenceRecord | BankTxn] = []
        self.shareable_keys: set[tuple[str, int]] = set()
        self.records_err: str | None = None               # the all-kinds list_records read failed
        self.unsafe_refs: list[str] = []                  # model claim refs outside the shareable set
        self.output: OnePassOutput | None = None          # validated proposal
        self.raw_output: OnePassOutput | None = None      # parsed, before reference validation
        self.proposal: dict[str, Any] = {"claims": [], "disclosures": [], "rationale": ""}
        self.model_failure: str | None = None
        self.model_calls = 0
        self.tokens = 0
        log_step = getattr(gateway, "log_step", None)
        self.log_step = log_step if callable(log_step) else None

    # ----------------------------------------------------------------------- orchestration

    def execute(self) -> OnePassRun:
        if getattr(self.client, "replay", False):
            self.note("note", reason="REPLAY: served from recording",
                      result={"replay": True, "label": getattr(self.client, "label", None)})
        self.retrieve()
        self.propose()
        claims = [self.answer(question) for question in self.rfc.questions]
        self.question_id = None
        unsafe = self.gate(claims)
        self.note("note", reason="One-pass baseline counts.", result={
            "lookups": self.material.lookup_count,
            "observed_inputs": self.material.observed_inputs,
            "model_calls": self.model_calls, "tokens": self.tokens,
            "unsafe_proposals": unsafe, "fixed_retrieval": FIXED_RETRIEVAL,
            "proposed_statuses": self.proposed_statuses(),
            "model": self.model})
        result = self.finish(claims)
        return OnePassRun(result=result, proposal=self.proposal, unsafe_proposals=unsafe,
                          lookups=self.material.lookup_count,
                          observed_inputs=self.material.observed_inputs,
                          model_calls=self.model_calls, tokens=self.tokens)

    # ----------------------------------------------------------------------- trace and reads

    def note(self, step: str, *, reason: str | None = None, refs: Iterable[EvidenceRef] = (),
             outcome: str = "ok", tool: str | None = None, args: dict | None = None,
             result: dict | None = None) -> None:
        event = {"step": step, "question_id": self.question_id, "tool": tool,
                 "args": args or {}, "result": result or {}, "refs": _unique(refs),
                 "outcome": outcome, "reason": _clip(reason, 300) if reason else None}
        if self.log_step is not None:
            self.log_step(step, actor=ACTOR, **{k: v for k, v in event.items() if k != "step"})
        else:
            self.events.append(event)

    def read(self, tool: str, label: str, call: Callable[[], Any]) -> tuple[Any, str | None]:
        """One gateway read; a gateway error becomes a typed reason, never a crash."""
        try:
            return call(), None
        except GatewayError as exc:
            kind = exc.outcome if exc.outcome in _OUTCOMES else "error"
            typed = _clip(f"{kind}: {label}", 300)
            if typed not in self.unavailable:
                self.unavailable.append(typed)
            self.note("note", tool=tool, outcome=kind, reason=f"{label} failed ({kind})",
                      args={"call": label})
            return None, kind

    # ----------------------------------------------------------------------- step 1 and 2

    def retrieve(self) -> None:
        """The fixed retrieval: requested txns, every record, then the predeclared lookups."""
        for txn_id in self.rfc.txn_ids or []:
            txn, _ = self.read("get_txn", f"get_txn {txn_id}", lambda t=txn_id: self.gateway.get_txn(t))
            if txn is not None and txn.subject_ref == self.rfc.subject_ref:
                self.material.add_txn(txn)
        records, err = self.read("list_records", "list_records all kinds",
                                 lambda: self.gateway.list_records(kinds=None))
        if records is None:
            self.records_err = err or "error"
        for record in records or []:
            if record.subject_ref == self.rfc.subject_ref:
                self.material.add_record(record)
        self.note("note", reason=f"Fixed retrieval strategy: {FIXED_RETRIEVAL}.",
                  result={"fixed_retrieval": FIXED_RETRIEVAL,
                          "txns": len(self.material.txns()),
                          "records": len(self.material.records()),
                          "records_error": self.records_err})
        self.fixed_lookups()

    def fixed_lookups(self) -> None:
        payouts = self.material.records(kinds=[RecordKind.PAYOUT])
        by_norm = {_norm(p.payment_ref): p.payment_ref for p in payouts if p.payment_ref}
        named: dict[str, list[EvidenceRecord]] = {}
        messages = self.material.records(kinds=[RecordKind.MESSAGE])
        for message in messages:
            for token in _tokens(message.text or ""):
                if token in by_norm:
                    named.setdefault(by_norm[token], []).append(message)
        if not named:
            self.note("note", tool="lookup_settlement",
                      reason=(f"Fixed retrieval: none of {len(messages)} balance_release message(s) "
                              "names a payout reference; no settlement lookup performed."),
                      refs=[m.ref() for m in messages],
                      result={"messages": len(messages), "lookups": 0})
            return
        for ref in sorted(named):
            found, err = self.read("lookup_settlement", f"lookup_settlement {ref}",
                                   lambda r=ref: self.gateway.lookup_settlement(r))
            if err is not None:
                outcome, reason = err, f"the settlement lookup for {ref} failed ({err})"
            elif found is None:
                outcome, reason = "not_found", f"the merchant knows no settlement for {ref}"
            else:
                outcome, reason = "ok", None
            keys = [f"{found.record_id}@{found.revision}"] if found is not None else []
            index = self.material.add_lookup("lookup_settlement", {"payment_ref": ref}, outcome,
                                             refs=keys, reason=reason, actor=ACTOR)
            if found is not None:
                self.material.add_record(found, via_lookup=index)
            sources = ", ".join(f"{m.record_id}@{m.revision}" for m in named[ref])
            self.note("note", tool="lookup_settlement", outcome=outcome,
                      reason=(f"Fixed retrieval: message {sources} names {ref}; settlement lookup "
                              f"{outcome}" + (f" ({reason})" if reason else "") + "."),
                      refs=[*(m.ref() for m in named[ref]), *([found.ref()] if found else [])],
                      args={"payment_ref": ref},
                      result={"payment_ref": ref, "outcome": outcome, "via_lookup": index,
                              "returned": keys})

    # ----------------------------------------------------------------------- step 3

    def propose(self) -> None:
        """The one model call, parsed and reference-validated. Failures leave no proposal."""
        self.shareable = self.shareable_items()
        self.shareable_keys = {(_ident(item), item.revision) for item in self.shareable}
        shareable_keys = self.shareable_keys
        user = json.dumps(self.model_input(), sort_keys=True, ensure_ascii=False)
        prompt = load_prompt(ROLE)
        try:
            result = self.client.generate(self.model, prompt.system, user, prompt.schema,
                                          CallBudget(1, name="one_pass"))
        except Exception as exc:  # noqa: BLE001 - typed below; the payload is never logged
            outcome = _llm_outcome(exc)
            # A schema failure is an answered, billed call; an outage or budget refusal is not.
            self.model_calls = 1 if isinstance(exc, SchemaValidationError) else 0
            self.tokens = int(getattr(getattr(exc, "usage", None), "total_tokens", 0) or 0)
            self.model_failure = f"{outcome}: {type(exc).__name__}"
            self.note("note", tool="generate", outcome=outcome,
                      reason=f"The model call failed ({type(exc).__name__}); no proposal.",
                      args={"model": self.model},
                      result={"model_calls": self.model_calls, "tokens": self.tokens})
            return
        self.model_calls, self.tokens = call_cost(result)
        data = result.data
        self.note("note", tool="generate", reason="The model returned a proposal.",
                  args={"model": self.model},
                  result={"model_calls": self.model_calls, "tokens": self.tokens,
                          "cached": bool(getattr(result, "cached", False)),
                          "prompt_sha256": getattr(result, "prompt_sha256", None)})
        if not isinstance(data, dict):
            self.model_failure = "error: the answer was not a JSON object"
            self.note("validation", outcome="refused", reason=self.model_failure)
            return
        extra = undeclared_fields(ROLE, data)
        if extra:
            self.note("validation", outcome="refused",
                      reason=f"Undeclared fields dropped from the answer: {', '.join(extra)}.",
                      result={"undeclared": extra})
        try:
            parsed = parse_output(ROLE, data)
        except ValidationError as exc:
            self.model_failure = f"error: the answer did not match the schema ({len(exc.errors())} problems)"
            self.note("validation", outcome="refused", reason=self.model_failure)
            return
        assert isinstance(parsed, OnePassOutput)
        self.raw_output = parsed
        cleaned, failures = validate_refs(parsed, self.material, role=ROLE, shareable=shareable_keys)
        for failure in failures:
            self.note("validation", outcome="refused",
                      reason=f"{failure.path}: {failure.record_id}@{failure.revision} {failure.reason}",
                      result=failure.as_dict())
        assert isinstance(cleaned, OnePassOutput)
        self.output = cleaned
        self.proposal = cleaned.model_dump()
        self.note("note", reason="Pre-policy proposal recorded.",
                  result={"claims": len(cleaned.claims), "disclosures": len(cleaned.disclosures),
                          "dropped_refs": len(failures),
                          "proposed_statuses": self.proposed_statuses()})

    def shareable_items(self) -> list[EvidenceRecord | BankTxn]:
        """Items the policy lets this audience see: never a message record."""
        items: list[EvidenceRecord | BankTxn] = []
        for record in self.material.records():
            if record.kind == RecordKind.MESSAGE:
                continue
            if self.compiled.filter_record(record, audience=self.rfc.audience, now=self.now).included:
                items.append(record)
        for txn in self.material.txns():
            if self.compiled.filter_txn(txn, audience=self.rfc.audience, now=self.now).included:
                items.append(txn)
        return items

    def model_input(self) -> dict[str, Any]:
        material = self.material
        records = material.records()
        return {
            "rfc": rfc_view(self.rfc),
            "bank": [txn_view(t, material) for t in material.txns()],
            "merchant": [record_view(r, material) for r in records if r.kind != RecordKind.SETTLEMENT],
            "settlements": [record_view(r, material) for r in records if r.kind == RecordKind.SETTLEMENT],
            "shareable": [shareable_item(item, material) for item in self.shareable],
            "policy": _policy_summary(self.compiled),
            "candidate_rule": RULE_TEXT,
            "fixed_retrieval": FIXED_RETRIEVAL,
        }

    def proposed(self, question_id: str) -> OnePassClaim | None:
        if self.output is None:
            return None
        return next((c for c in self.output.claims if c.question_id == question_id), None)

    def proposed_statuses(self) -> dict[str, str]:
        if self.raw_output is None:
            return {}
        return {c.question_id: c.status for c in self.raw_output.claims
                if c.question_id in {q.question_id for q in self.rfc.questions}}

    # ----------------------------------------------------------------------- step 4: verdicts

    def answer(self, question: Question) -> Claim:
        self.question_id = question.question_id
        found = classify_question(question.text, self.rfc)
        self.note("note", reason=f"Question classified as {found.category} by keyword.",
                  result={"category": found.category, "kinds": [k.value for k in found.kinds]})
        if found.category == core_rules.SOURCE_OF_FUNDS:
            outcome = self.source_of_funds()
        elif found.category in (core_rules.UNMAPPED, core_rules.SEVERAL_TOTALS):
            gap = (core_rules.UNMAPPED_GAP if found.category == core_rules.UNMAPPED else
                   "The question names several totals without asking for a net figure.")
            outcome = _Outcome(ClaimStatus.UNRESOLVED,
                               "No answer to this question is established from the available records.",
                               gap, "Question not mapped to a code check.", gap=gap)
        else:
            outcome = self.period_total(found)
        if self.output is None:
            why = self.model_failure or "no proposal"
            gap = f"{GAP_MODEL}: the one-pass model produced no proposal ({why})."
            outcome = _Outcome(ClaimStatus.UNRESOLVED, outcome.statement,
                               f"{outcome.rationale} {gap}", "No model proposal.",
                               gap=" ".join(filter(None, [outcome.gap, gap])),
                               evidence=outcome.evidence, calculation=outcome.calculation)
        proposed = self.proposed(question.question_id)
        statement = outcome.statement
        evidence = list(outcome.evidence)
        if proposed is not None:
            if proposed.status == outcome.status.value and self.statement_ok(proposed.statement, outcome.calculation):
                statement = proposed.statement
            else:
                self.note("validation", reason=("The model's statement was replaced by a code statement "
                                                 "(status or content check)."),
                          result={"proposed_status": proposed.status, "code_status": outcome.status.value})
            # Model refs join the evidence only when the policy lets this audience see the item;
            # the code outcome already cites everything the verdict used.
            code_keys = {ref.key() for ref in evidence}
            for ref in proposed.refs:
                item = self.material.get(ref.record_id, ref.revision)
                if item is None or item.ref().key() in code_keys:
                    continue
                key = (_ident(item), item.revision)
                if isinstance(item, EvidenceRecord) and item.kind == RecordKind.MESSAGE:
                    reason = f"{item.record_id}@{item.revision} is a message record; it stays holder-side."
                elif key not in self.shareable_keys:
                    reason = (f"{key[0]}@{key[1]} is not in the shareable evidence under the holder's "
                              "policy; not attached to the claim.")
                else:
                    evidence.append(item.ref())
                    continue
                self.unsafe_refs.append(f"{key[0]}@{key[1]}")
                self.note("validation", outcome="refused", refs=[item.ref()], reason=reason,
                          result={"path": "claims.refs", "record_id": key[0], "revision": key[1]})
        claim = Claim(
            claim_id=make_id("clm", self.run_id, question.question_id),
            request_id=self.rfc.request_id, audience=self.rfc.audience, question_id=question.question_id,
            statement=_clip(statement), status=outcome.status, rationale=_clip(outcome.rationale),
            evidence_refs=_unique(evidence), calculation=outcome.calculation,
            gap=_clip(outcome.gap) if outcome.gap else None, run_id=self.run_id,
            created_at=self.now)
        self.note("status_change", reason=outcome.reason, refs=claim.evidence_refs,
                  result={"claim_id": claim.claim_id, "status": claim.status.value,
                          "proposed_status": proposed.status if proposed else None})
        return claim

    def source_of_funds(self) -> _Outcome:
        outcomes = [(txn_id, self.txn_outcome(txn_id)) for txn_id in self.rfc.txn_ids or []]
        outcomes = [(txn_id, self.one_payout_one_credit(txn_id, o)) for txn_id, o in outcomes]
        if len(outcomes) == 1:
            return outcomes[0][1]
        if not outcomes:
            gap = f"{GAP_MISSING_RECORD}: the request names no transaction."
            return _Outcome(ClaimStatus.UNRESOLVED, "No transaction was named.", gap, gap, gap=gap)
        status = max((o.status for _, o in outcomes), key=_RANK.__getitem__)
        gaps = [f"{txn_id}: {o.gap}" for txn_id, o in outcomes if o.gap]
        return _Outcome(status, " ".join(o.statement for _, o in outcomes),
                        " ".join(f"{txn_id}: {o.rationale}" for txn_id, o in outcomes),
                        "; ".join(f"{txn_id} {o.status.value}" for txn_id, o in outcomes),
                        gap=" ".join(gaps) or None,
                        evidence=[ref for _, o in outcomes for ref in o.evidence])

    def one_payout_one_credit(self, txn_id: str, outcome: _Outcome) -> _Outcome:
        link = self.links.get(txn_id)
        if outcome.status != ClaimStatus.SUPPORTED or link is None or link.record_ref is None:
            return outcome
        payout = link.record_ref.ref_id
        others = [other for other, seen in self.links.items() if other != txn_id
                  and seen.record_ref is not None and seen.record_ref.ref_id == payout]
        if not others:
            return outcome
        gap = (f"{GAP_AMBIGUOUS}: payout {payout} is also matched to {', '.join(others)}; one payout "
               f"cannot fund more than one credit.")
        return _Outcome(ClaimStatus.UNRESOLVED, _unknown_source(txn_id), f"{outcome.rationale} {gap}",
                        "The linked payout is matched to more than one credit.", gap=gap,
                        evidence=outcome.evidence, calculation=outcome.calculation)

    def txn_outcome(self, txn_id: str) -> _Outcome:
        txn = self.material.get(txn_id)
        if not isinstance(txn, BankTxn):
            gap = f"{GAP_MISSING_RECORD}: bank transaction {txn_id} could not be read."
            return _Outcome(ClaimStatus.UNRESOLVED, _unknown_source(txn_id), gap, gap, gap=gap)
        if not txn.is_credit:
            gap = f"{GAP_NO_RECORDS}: transaction {txn_id} is a debit; only credits are explained."
            return _Outcome(ClaimStatus.UNRESOLVED, _unknown_source(txn_id, txn), gap, gap, gap=gap,
                            evidence=[txn.ref()])
        records = self.material.records()
        found = settlement_candidates(txn, records)
        pool_refs = [p.ref() for c in found.pool for p in c.payouts]
        chosen = found.chosen
        link = Link(link_id=make_id("lnk", self.run_id, txn.txn_id), txn_ref=txn.ref(),
                    link_state=found.state,
                    record_ref=chosen.payouts[0].ref() if chosen and len(chosen.payouts) == 1 else None,
                    candidates=pool_refs, method=LINK_METHOD,
                    signals=[_clip(found.summary(), 300)], run_id=self.run_id, created_at=self.now)
        if link.link_state == LinkState.LINKED and link.record_ref is None:
            link = link.model_copy(update={"link_state": LinkState.AMBIGUOUS}) if len(pool_refs) > 1 \
                else link.model_copy(update={"link_state": LinkState.UNMATCHED})
        self.links[txn.txn_id] = link
        self.note("validation", tool="settlement_candidates", reason=found.summary(),
                  refs=[txn.ref(), *pool_refs, *(p.ref() for r in found.rejected for p in r.payouts)],
                  args={"txn_id": txn.txn_id, "rule": RULE_TEXT[:120]},
                  result={"link_id": link.link_id, "link_state": link.link_state.value,
                          "record_id": link.record_ref.ref_id if link.record_ref else "none",
                          "candidates": [r.ref_id for r in pool_refs],
                          "conflicts": len(found.conflicts)})
        if chosen is not None:
            return self.linked(txn, chosen, found)
        if len(found.pool) > 1:
            listed = ", ".join(c.summary() for c in found.pool)
            gap = (f"{GAP_AMBIGUOUS}: {len(found.pool)} payout candidates fit this credit equally "
                   f"({listed}); code cannot choose between them.")
            return _Outcome(ClaimStatus.UNRESOLVED, _unknown_source(txn.txn_id, txn), found.summary(),
                            "Link ambiguous.", gap=gap, evidence=[txn.ref(), *pool_refs])
        if found.pool:
            candidate = found.pool[0]
            ref = candidate.payouts[0].payment_ref or candidate.payouts[0].record_id
            gap = (f"{GAP_SETTLEMENT_NOT_FOUND}: no settlement record for payout {ref} was retrieved; "
                   "a payout record does not show a bank transfer.")
            if any(r.kind == RecordKind.MESSAGE for r in records):
                gap += (f" {GAP_BALANCE_RELEASE}: a 'released to balance' message is a release to the "
                        "platform balance, not a settlement.")
            return _Outcome(ClaimStatus.UNRESOLVED, _unknown_source(txn.txn_id, txn), found.summary(),
                            "No settlement record for the candidate payout.", gap=gap,
                            evidence=[txn.ref(), *pool_refs])
        if found.conflicts:
            return self.contradicted(txn, found.conflicts[0], found)
        if not any(r.kind == RecordKind.PAYOUT for r in records) and self.records_err:
            gap = (f"The merchant records could not be retrieved ({self.records_err}), so no payout could be "
                   "checked against this credit.")
            return _Outcome(ClaimStatus.UNRESOLVED, _unknown_source(txn.txn_id, txn), gap,
                            f"Merchant records unavailable ({self.records_err}).", gap=gap, evidence=[txn.ref()])
        if not any(r.kind == RecordKind.PAYOUT for r in records):
            gap = f"{GAP_NO_RECORDS}: the passport holds no payout record for this subject."
            return _Outcome(ClaimStatus.UNRESOLVED, _unknown_source(txn.txn_id, txn), found.summary() or gap,
                            "No payout records.", gap=gap, evidence=[txn.ref()])
        reasons = "; ".join(f"{', '.join(p.record_id for p in r.payouts)}: {r.reason}" for r in found.rejected)
        gap = f"{GAP_SETTLEMENT_NOT_FOUND}: no payout fits this credit under the candidate rule ({reasons})."
        return _Outcome(ClaimStatus.UNRESOLVED, _unknown_source(txn.txn_id, txn), found.summary(),
                        "No payout matched the credit.", gap=gap,
                        evidence=[txn.ref(), *(p.ref() for r in found.rejected for p in r.payouts)])

    def components_of(self, payouts: list[EvidenceRecord]) -> tuple[list[EvidenceRecord] | None, str]:
        wanted = list(dict.fromkeys(rid for p in payouts for rid in p.related_record_ids))
        if not wanted:
            return [], ""
        held = {r.record_id: r for r in self.material.records() if r.kind in COMPONENT_KINDS}
        missing = [rid for rid in wanted if rid not in held]
        if missing:
            return None, (f"{GAP_MISSING_RECORD}: component record(s) {', '.join(missing)} of the "
                          "payout are not among the retrieved order, refund and fee records.")
        return [held[rid] for rid in wanted], ""

    def linked(self, txn: BankTxn, chosen: Candidate, found: CandidateResult) -> _Outcome:
        payouts, settlement = chosen.payouts, chosen.settlement
        assert settlement is not None
        payout = payouts[0] if len(payouts) == 1 else None
        ref = chosen.group_ref or (payout.payment_ref if payout else None) or payouts[0].record_id
        ids = ", ".join(p.record_id for p in payouts)
        statement = (f"Credit {txn.txn_id} of {_money(txn.amount_pence, txn.currency)} is payout {ids} "
                     f"(reference {ref})")
        evidence = [txn.ref(), *(p.ref() for p in payouts), settlement.ref()]
        parts = [found.summary()]
        components, problem = self.components_of(payouts)
        calc = None
        if components is not None:
            evidence += [c.ref() for c in components]
            try:
                calc = reconcile.reconcile_credit(txn, components=components, payout=payout,
                                                  settlement=settlement)
            except ReconcileError as exc:
                problem = f"{GAP_AMOUNT_MISMATCH}: the amounts could not be reconciled: {exc}"
        if calc is not None:
            parts.append(_calc_text(calc, components or []))
            self.note("calculation", reason=_clip(parts[-1], 300), refs=calc.cited_refs,
                      result=_calc_summary(calc))
        parts.append(f"Timing: {reconcile.check_settlement_timing(settlement, txn).reason}")
        own = [r for r in (*payouts, settlement) if r.source_trust == SourceTrust.SELF_SUPPLIED]
        if problem:
            parts.append(problem)
            return _Outcome(ClaimStatus.UNRESOLVED, statement + ".", " ".join(parts), problem,
                            gap=problem, evidence=evidence, calculation=calc)
        assert calc is not None
        if calc.balanced is not True:
            gap = (f"{GAP_AMOUNT_MISMATCH}: the components total {calc.total_pence} against "
                   f"{calc.target_ref.key() if calc.target_ref else 'the target'} at {calc.target_pence} "
                   f"(difference {calc.difference_pence}).")
            return _Outcome(ClaimStatus.UNRESOLVED, statement + ".", " ".join(parts) + " " + gap,
                            "The amounts do not reconcile.", gap=gap, evidence=evidence, calculation=calc)
        if chosen.reference_matched is not True:
            gap = (f"{GAP_REFERENCE}: the credit carries no reference to payout {ref} or its settlement; "
                   "matching amounts and dates alone do not establish the source of funds.")
            return _Outcome(ClaimStatus.UNRESOLVED, statement + ".", " ".join(parts) + " " + gap,
                            "Only amounts and dates tie the credit to the payout.", gap=gap,
                            evidence=evidence, calculation=calc)
        if own:
            gap = (f"{GAP_SETTLEMENT_NOT_FOUND}: {', '.join(r.record_id for r in own)} is self-supplied, so "
                   "the source of funds is not established from issuer evidence.")
            return _Outcome(ClaimStatus.UNRESOLVED, statement + ".", " ".join(parts) + " " + gap,
                            "Self-supplied evidence is not issuer evidence.", gap=gap,
                            evidence=evidence, calculation=calc)
        if not reconcile.credit_supported(calc):
            effective = reconcile.settlement_effective_time(settlement)
            code = (GAP_SETTLEMENT_PENDING if settlement.status in PENDING_STATUSES else
                    GAP_SETTLEMENT_AFTER if effective is not None and effective > txn.booked_at else
                    GAP_SETTLEMENT_NOT_FOUND)
            gap = f"{code}: {reconcile.check_settlement_timing(settlement, txn).reason}"
            return _Outcome(ClaimStatus.UNRESOLVED, statement + ".", " ".join(parts) + " " + gap,
                            "The reconciliation does not support the credit.", gap=gap,
                            evidence=evidence, calculation=calc)
        statement += f", settled as {settlement.record_id} before the credit was booked."
        return _Outcome(ClaimStatus.SUPPORTED, statement, " ".join(parts),
                        (f"Settlement {settlement.record_id}@{settlement.revision} is {settlement.status} "
                         f"by the booking, the amounts reconcile and the credit carries {ref}."),
                        evidence=evidence, calculation=calc)

    def contradicted(self, txn: BankTxn, rejection: Rejection, found: CandidateResult) -> _Outcome:
        payouts, settlement = rejection.payouts, rejection.settlement
        ids = ", ".join(p.record_id for p in payouts)
        ref = next((p.payment_ref for p in payouts if p.payment_ref), payouts[0].record_id)
        code = _conflict_code(rejection, txn)
        evidence = [txn.ref(), *(p.ref() for p in payouts), *([settlement.ref()] if settlement else [])]
        calc = None
        components, _ = self.components_of(payouts)
        if components:
            try:
                calc = reconcile.reconcile_credit(txn, components=components,
                                                  payout=payouts[0] if len(payouts) == 1 else None,
                                                  settlement=None)
                evidence += [c.ref() for c in components]
                self.note("calculation", reason=_clip(_calc_text(calc, components), 300),
                          refs=calc.cited_refs, result=_calc_summary(calc))
            except ReconcileError:
                calc = None
        statement = (f"Credit {txn.txn_id} of {_money(txn.amount_pence, txn.currency)} is not explained by "
                     f"payout {ids} (reference {ref}): {rejection.reason}.")
        gap = f"{code}: {rejection.reason}; the credit's origin remains unresolved."
        return _Outcome(ClaimStatus.CONTRADICTED, statement, found.summary(), _clip(rejection.reason, 300),
                        gap=gap, evidence=evidence, calculation=calc)

    def period_total(self, found: core_rules.Classification) -> _Outcome:
        period = self.rfc.period
        assert period is not None
        span = f"[{format_utc(period.from_)}, {format_utc(period.to)})"
        label = " and ".join(_LABELS[k] for k in found.kinds)
        unknown = f"Documented {label} over {span} are not established from the available records."
        records = sorted((r for r in self.material.records() if r.kind in found.kinds
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
        if unclear:
            gap = (f"{GAP_MISSING_RECORD}: the status of "
                   f"{', '.join(f'{r.record_id} ({r.status})' for r in unclear)} does not show whether "
                   f"it went through, so the documented {label} total over {span} is not established.")
            return _Outcome(ClaimStatus.UNRESOLVED, unknown, gap, "Unrecognised record status.", gap=gap,
                            evidence=[r.ref() for r in unclear])
        if not counted:
            gap = f"{GAP_NO_RECORDS}: no documented {label} records are in the passport for {span}."
            return _Outcome(ClaimStatus.UNRESOLVED, unknown, gap, f"No {label} records found.", gap=gap)
        net = found.category == core_rules.NET_TOTAL
        signs = SIGN_BY_KIND if net else {found.kinds[0]: 1}
        try:
            calc = reconcile.sum_terms(counted, method=found.category, signs=signs)
        except ReconcileError as exc:
            gap = f"{GAP_AMOUNT_MISMATCH}: the {label} records could not be summed: {exc}"
            return _Outcome(ClaimStatus.UNRESOLVED, unknown, gap, gap, gap=gap,
                            evidence=[r.ref() for r in counted])
        total = _money(calc.total_pence, calc.currency)
        statement = (f"Documented {label} over {span} {'net to' if net else 'total'} {total} across "
                     f"{len(counted)} record{'s' if len(counted) != 1 else ''}.")
        rationale = ("Summed in code from the retrieved records dated in " + span + ": "
                     + "; ".join(f"{r.record_id} {r.amount_pence}" for r in counted) + f". Total {total}.")
        if skipped:
            rationale += " Not counted: " + ", ".join(f"{r.record_id} (status {r.status})" for r in skipped) + "."
        self.note("calculation", reason=_clip(f"{found.category}: {total}", 300), refs=calc.cited_refs,
                  result=_calc_summary(calc))
        return _Outcome(ClaimStatus.SUPPORTED, statement, rationale,
                        f"{len(counted)} documented records sum to {total}.",
                        evidence=list(calc.cited_refs), calculation=calc)

    # ----------------------------------------------------------------------- statement check

    def statement_ok(self, text: str, calc: Calculation | None) -> bool:
        """A model statement is usable when it is non-empty, names no identifier outside the
        material, repeats no message text (the whole message, a sentence of three or more
        words, or any four-word run of it) and quotes no unknown amount."""
        if not isinstance(text, str) or not text.strip() or len(text) > 4000:
            return False
        words = _WORD.findall(text.lower())
        joined = " ".join(words)
        grams = _ngrams(words)
        for message in self.material.records(kinds=[RecordKind.MESSAGE], all_revisions=True):
            body = message.text or ""
            whole = " ".join(_WORD.findall(body.lower()))
            if whole and whole in joined:
                return False
            if any(fragment in joined for fragment in _fragments(body)):
                return False
            if grams & _ngrams(_WORD.findall(body.lower())):
                return False
        known_ids = self.known_identifiers()
        for token in _ID_TOKEN.findall(text):
            if token.lower() not in known_ids:
                return False
        stripped = _ID_TOKEN.sub(" ", _ISO_TIME.sub(" ", text))
        allowed = self.known_numbers(calc)
        return all(run in allowed for run in _DIGITS.findall(stripped))

    def known_identifiers(self) -> set[str]:
        ids = {self.rfc.request_id.lower(), self.rfc.audience.lower(), self.rfc.issuer.lower()}
        ids.update(q.question_id.lower() for q in self.rfc.questions)
        for record in self.material.records(all_revisions=True):
            ids.update({record.record_id.lower(), record.issuer_id.lower()})
            ids.update(r.lower() for r in (record.payment_ref, record.settlement_ref) if r)
        for txn in self.material.txns():
            ids.add(txn.txn_id.lower())
        ids.update(r.lower() for r in self.material.references())
        return ids

    def known_numbers(self, calc: Calculation | None) -> set[str]:
        numbers: set[str] = set()
        for record in self.material.records(all_revisions=True):
            if record.amount_pence is not None:
                numbers.add(str(record.amount_pence))
            numbers.add(str(record.occurred_at.year))
            for item in record.items or []:
                if item.unit_amount_pence is not None:
                    numbers.add(str(item.unit_amount_pence))
        for txn in self.material.txns():
            numbers.update({str(abs(txn.amount_pence)), str(txn.booked_at.year)})
        if calc is not None:
            numbers.add(str(abs(calc.total_pence)))
            for value in (calc.target_pence, calc.difference_pence):
                if value is not None:
                    numbers.add(str(abs(value)))
        return numbers

    # ----------------------------------------------------------------------- step 4: policy gate

    def gate(self, claims: list[Claim]) -> int:
        """Run the policy filter over the claims and count the proposals it did not carry."""
        refs = _unique([ref for claim in claims for ref in
                        [*claim.evidence_refs, *(claim.calculation.cited_refs if claim.calculation else [])]])
        evidence = [item for item in (self.material.get(r.ref_id, r.revision) for r in refs) if item is not None]
        attached: set[tuple[str, int]] = set()
        gate_result: dict[str, Any] = {}
        try:
            body, review = build_body(self.rfc, claims, evidence, self.compiled, now=self.now)
            attached = {(a.ref.ref_id, a.ref.revision) for a in body.attachments}
            gate_result = {"attached": len(body.attachments), "excluded": len(review.excluded),
                           "withheld_fields": len(review.withheld_fields),
                           "claim_changes": len(review.claim_changes)}
            outcome, reason = "ok", "Policy gate applied to the claims."
        except PackageError as exc:
            outcome, reason = "error", f"Policy gate failed: {exc}"
        disclosures = list(self.raw_output.disclosures) if self.raw_output is not None else []
        unsafe_disclosures = [d for d in disclosures if (d.record_id, d.revision) not in attached]
        final = {c.question_id: c.status for c in claims}
        unsafe_claims = [qid for qid, status in self.proposed_statuses().items()
                         if status == ClaimStatus.SUPPORTED.value and final.get(qid) != ClaimStatus.SUPPORTED]
        attached_refs = [self.material.ref_for(i, r) for i, r in sorted(attached)]
        self.note("validation", tool="build_body", outcome=outcome, reason=reason,
                  refs=[ref for ref in attached_refs if ref is not None],
                  result={**gate_result, "proposed_disclosures": len(disclosures),
                          "unsafe_disclosures": [f"{d.record_id}@{d.revision}" for d in unsafe_disclosures],
                          "unsafe_refs": list(self.unsafe_refs),
                          "unsafe_supported": unsafe_claims})
        return len(unsafe_disclosures) + len(self.unsafe_refs) + len(unsafe_claims)

    # ----------------------------------------------------------------------- step 5

    def finish(self, claims: list[Claim]) -> InvestigationResult:
        gateway_events = [TraceEvent.model_validate(e) if not isinstance(e, TraceEvent) else e
                          for e in getattr(self.gateway, "trace", [])]
        base = max((e.seq for e in gateway_events), default=-1) + 1
        own_events = [TraceEvent(trace_id=make_id("trc", self.run_id, ACTOR, i), run_id=self.run_id,
                                 seq=base + i, at=self.now, actor=ACTOR, request_id=self.rfc.request_id,
                                 **event) for i, event in enumerate(self.events)]
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
            if own_events:
                self.store.append_traces(own_events)
        stored = {c.claim_id: c for c in self.store.list_claims(request_id=self.rfc.request_id, state=None)}
        links = []
        for link in self.links.values():
            saved = {l.link_id: l for l in self.store.list_links(txn_id=link.txn_ref.ref_id, state=None)}
            links.append(saved.get(link.link_id, link))
        return InvestigationResult(
            run_id=self.run_id, request_id=self.rfc.request_id, mode=MODE, links=links,
            claims=[stored.get(c.claim_id, c) for c in claims],
            trace=[*gateway_events, *own_events],
            calls_used=int(getattr(self.gateway, "calls_used", 0)),
            unavailable=list(self.unavailable))


# --------------------------------------------------------------------------- helpers

_OUTCOMES = frozenset({"refused", "unavailable", "error", "budget_exceeded", "not_found"})


def _clock(now: datetime | None) -> datetime:
    if now is None:
        return utc_now()
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise ValueError("now must be a timezone-aware datetime")
    return now.astimezone(timezone.utc)


def _llm_outcome(exc: BaseException) -> str:
    if isinstance(exc, LLMUnavailable):
        return "unavailable"
    if isinstance(exc, LLMBudgetExceeded):
        return "budget_exceeded"
    if isinstance(exc, (SchemaValidationError, LLMError)):
        return "error"
    return "error"


def _ident(item: EvidenceRecord | BankTxn) -> str:
    return item.record_id if isinstance(item, EvidenceRecord) else item.txn_id


def _norm(ref: str) -> str:
    return re.sub(r"\s+", "", ref.upper())


def _tokens(text: str) -> set[str]:
    """Reference-looking tokens of a message, upper-cased, trailing punctuation removed."""
    return {t.rstrip(".:,;/") for t in _REF_TOKEN.findall(text.upper())} - {""}


def _fragments(text: str) -> list[str]:
    """Each sentence of a message with at least ``_MIN_SENTENCE_WORDS`` words, as lower-cased
    words joined by single spaces (the form ``statement_ok`` compares against)."""
    pieces = [" ".join(_WORD.findall(p.lower())) for p in re.split(r"[.!?;\n]+", text)]
    return [p for p in pieces if len(p.split()) >= _MIN_SENTENCE_WORDS]


def _ngrams(words: list[str], n: int = _NGRAM) -> set[str]:
    """The ``n``-word runs of ``words``."""
    return {" ".join(words[i:i + n]) for i in range(len(words) - n + 1)}


def _conflict_code(rejection: Rejection, txn: BankTxn) -> str:
    settlement = rejection.settlement
    if settlement is not None:
        if settlement.status in FAILED_STATUSES:
            return GAP_SETTLEMENT_FAILED
        if settlement.status in PENDING_STATUSES:
            return GAP_SETTLEMENT_PENDING
        effective = reconcile.settlement_effective_time(settlement)
        if effective is not None and effective > txn.booked_at:
            return GAP_SETTLEMENT_AFTER
        if (settlement.amount_pence, settlement.currency) != (txn.amount_pence, txn.currency):
            return GAP_AMOUNT_MISMATCH
    if any(p.status in FAILED_STATUSES for p in rejection.payouts):
        return GAP_SETTLEMENT_FAILED
    return GAP_AMOUNT_MISMATCH


def _policy_summary(compiled: CompiledPolicy) -> dict[str, Any]:
    """The compiled rules as kinds and fields only: no rule text."""
    rules = []
    for rule in compiled.rules:
        row: dict[str, Any] = {"type": rule.type, "kinds": [k.value for k in rule.kinds]}
        if isinstance(rule, (DenyRule, GeneraliseRule)):
            row["fields"] = list(rule.fields)
        if isinstance(rule, GeneraliseRule):
            row["level"] = rule.level
        if isinstance(rule, AgeRule):
            row["max_age_days"] = rule.max_age_days
        if isinstance(rule, RecipientRule):
            row["allow"] = list(rule.allow)
        rules.append(row)
    return {"policy_version": compiled.policy_version, "rules": rules}


def _calc_text(calc: Calculation, components: list[EvidenceRecord]) -> str:
    pieces = []
    for kind, sign in ((RecordKind.ORDER, ""), (RecordKind.REFUND, "- "), (RecordKind.FEE, "- ")):
        rows = [r for r in components if r.kind == kind]
        pieces.append(f"{sign}{_LABELS[kind]} {sum(r.amount_pence or 0 for r in rows)} "
                      f"({', '.join(r.record_id for r in rows) or 'none'})")
    target = calc.target_ref.key() if calc.target_ref else "no target"
    return (f"Calculation in code: {' '.join(pieces)} = {_money(calc.total_pence, calc.currency)}, against "
            f"{target} at {calc.target_pence}: difference {calc.difference_pence}, "
            f"{'balanced' if calc.balanced else 'not balanced'}.")


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
    return (f"The source of {kind} {txn_id} of {_money(abs(txn.amount_pence), txn.currency)} is not "
            "established from the available records.")


def _money(pence: int | None, currency: str | None) -> str:
    return f"{pence} pence {currency}"


def _unique(refs: Iterable[EvidenceRef]) -> list[EvidenceRef]:
    seen: dict[str, EvidenceRef] = {}
    for ref in refs:
        seen.setdefault(ref.key(), ref)
    return list(seen.values())


def _clip(text: str, limit: int = 4000) -> str:
    text = text if text else "-"
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."
