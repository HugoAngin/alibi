"""Alibi public API: the functions teammates call, wired over the core modules.

Import it as ``from alibi.api import ...`` or, per RULES.md, ``from agents.lead.api import ...``
(the same names). ``alibi/CONTRACTS.md`` documents every function with JSON examples.

This module only wires. Each function loads what a step needs from the passport, calls the core
function that owns the logic (``alibi.core`` and ``alibi.gateway``), saves the result and writes
an audit event. Conventions:

* Money is integer pence with an ISO currency code. Times are timezone-aware UTC (``Z`` on the
  wire). Periods are half-open ``[from, to)``.
* The store comes first. Every function that depends on the clock takes ``now`` (default: the
  current UTC time; ``load_fixture`` defaults to the fixture clock ``FIXTURE_NOW``), so tests
  and replays are deterministic.
* Signing takes an optional ``keystore`` and ingest an optional ``registry``. The default is
  the demo keystore under ``data/alibi/keys`` (created on first use). ``verify_package`` only
  reads the pinned ``trust_registry.json`` there. Tests always pass their own.
* Errors are the core's typed exceptions, and their messages are safe to show.
* A request is identified by ``(audience, request_id)``: request ids are chosen by banks, so
  holder calls name the bank with ``audience`` (or pass the stored ``RequestForContext``). A
  bare request_id still resolves while exactly one bank has used it (the app, the demo cases
  and teammates' acceptance tests rely on that); once several banks have used it the call
  raises ``AmbiguousRequest`` instead of guessing, and the store itself resolves a bare id to
  the request received first, so a later bank never captures an earlier one's drafts.
"""

import copy
from collections.abc import Collection, Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, TypeAdapter, ValidationError

from alibi.core import approval as core_approval
from alibi.core import canon
from alibi.core import ingest as core_ingest
from alibi.core import investigate_rules as core_rules
from alibi.core import package as core_package
from alibi.core.approval import ApprovalError
from alibi.core.ingest import IngestError
from alibi.core.package import PackageError
from alibi.core.policy import (
    CompiledPolicy,
    PolicyError,
    RuleParse,
    compile_policy,
    parse_rule_text,
)
from alibi.core.schemas import (
    AgeRule,
    Alert,
    Approval,
    ApprovalStatus,
    AuditEvent,
    AuthResult,
    BankTxn,
    Claim,
    ClaimStatus,
    ContextPackage,
    Delivery,
    DenyRule,
    DerivedState,
    Draft,
    DraftStatus,
    EvidenceRecord,
    EvidenceRef,
    GeneraliseRule,
    Ident,
    InvestigationResult,
    Link,
    LinkState,
    PackageBody,
    Period,
    Policy,
    RecipientRule,
    RecordKind,
    RequestForContext,
    Review,
    SourceTrust,
    TraceEvent,
    VerificationResult,
    format_utc,
    parse_utc,
    utc_now,
)
from alibi.core.signing import (
    DEFAULT_KEY_DIR,
    REGISTRY_FILE,
    DemoKeystore,
    SignatureError,
    TrustRegistry,
    load_registry,
)
from alibi.core.store import (
    DEFAULT_DB_PATH,
    AmbiguousRequest,
    IdempotencyConflict,
    NamespaceError,
    NotFound,
    PutResult,
    RevisionConflict,
    Store,
    StoreError,
    dependency_refs,
)
from alibi.fixtures import maya
from alibi.gateway import (
    DEFAULT_BUDGET,
    BudgetExceeded,
    Gateway,
    GatewayError,
    NotFoundError,
    ScopeError,
    PayoutQuery,
    SettlementLookup,
    TxnRefresh,
    Unavailable,
)

__all__ = [
    # functions
    "approve", "build_review", "default_keystore", "import_self_supplied", "ingest_bank_txns",
    "ingest_merchant_event", "load_fixture", "open_store", "parse_rule_text", "receive_rfc",
    "record_policy", "run_investigation", "sign_and_package", "verify_package",
    # store, keys and gateway
    "DEFAULT_DB_PATH", "DEFAULT_KEY_DIR", "DemoKeystore", "Gateway", "PutResult", "RuleParse",
    "Store", "TrustRegistry",
    # schema models and enums
    "AgeRule", "Alert", "Approval", "ApprovalStatus", "AuditEvent", "BankTxn", "Claim",
    "ClaimStatus", "ContextPackage", "Delivery", "DenyRule", "Draft", "DraftStatus",
    "EvidenceRecord", "EvidenceRef", "GeneraliseRule", "InvestigationResult", "Link",
    "LinkState", "PackageBody", "Period", "Policy", "RecipientRule", "RecordKind",
    "RequestForContext", "Review", "SourceTrust", "TraceEvent", "VerificationResult",
    # errors
    "AmbiguousRequest", "ApprovalError", "BudgetExceeded", "GatewayError", "IdempotencyConflict",
    "IngestError",
    "NamespaceError", "NotFound", "NotFoundError", "PackageError", "PolicyError",
    "RevisionConflict", "RuleClarificationError", "ScopeError", "SignatureError",
    "StoreError", "Unavailable",
]

SYSTEM = "alibi"            # audit actor for backend steps (review, dispatch)
HOLDER = "holder"           # audit actor for a holder step when no valid holder id is known
FIXTURE_ACCOUNT_BANK = "bank_a"   # the fixture account acc_maya_bank_a is held at Bank A
# load_fixture's default clock: inside every fixture RFC's window (RFC A expires 2026-11-11T09:00Z).
FIXTURE_NOW = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)
_IDENT = TypeAdapter(Ident)


class RuleClarificationError(PolicyError):
    """Some plain-English rules could not be turned into typed rules, so nothing was saved.

    ``interpretations`` lists every rule as given, in order, as ``{"text", "status",
    "interpretation", "reason", "rule"}`` (``rule`` is the typed rule as JSON when resolved).
    Show it to the holder, ask them to rephrase the unresolved ones, then record again.
    """

    def __init__(self, interpretations: list[dict[str, Any]]):
        self.interpretations = interpretations
        unresolved = [item for item in interpretations if item["status"] != "resolved"]
        reasons = "; ".join(f"{_clip(item['text'], 80)!r}: {item['reason']}" for item in unresolved)
        super().__init__(f"{len(unresolved)} rule(s) need clarification: {reasons}")


# --------------------------------------------------------------------------- helpers


def _clock(now: datetime | None) -> datetime:
    if now is None:
        return utc_now()
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise ValueError("now must be a timezone-aware datetime")
    return now.astimezone(timezone.utc)


def _is_ident(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        _IDENT.validate_python(value)
    except ValidationError:
        return False
    return True


def _clip(text: Any, limit: int = 300) -> str:
    """One line, at most ``limit`` characters."""
    text = " ".join(str(text).split()) or "-"
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _audit(store: Store, *, action: str, actor: str, outcome: str, at: datetime,
           target_id: Any = None, detail: str | None = None) -> None:
    """Append one audit event. Its id is derived from every field, so an identical event is a
    no-op and two different events never share an id. Details hold ids and reasons only."""
    target = target_id if _is_ident(target_id) else None
    text = _clip(detail) if detail else None
    audit_id = canon.make_id("aud", action, actor, target, outcome, text, format_utc(at))
    store.append_audit(AuditEvent(audit_id=audit_id, at=at, actor=actor, action=action,
                                  target_id=target, outcome=outcome, detail=text))


def _keystore(keystore: DemoKeystore | None) -> DemoKeystore:
    return keystore if keystore is not None else default_keystore()


def _registry(registry: TrustRegistry | None) -> TrustRegistry:
    return registry if registry is not None else default_keystore().registry()


def _stored_request(store: Store, request: str | RequestForContext,
                    audience: str | None = None) -> RequestForContext:
    """The stored request ``(audience, request_id)``, or the one a model names, which must equal
    the stored one. A bare request_id resolves while one bank alone has used it; once several
    banks have, it raises ``AmbiguousRequest`` (banks choose request ids, so guessing could pick
    a request another bank sent under that id first)."""
    if isinstance(request, RequestForContext):
        if audience is not None and audience != request.audience:
            raise StoreError("audience does not match the request's audience")
        stored = store.get_request(request.request_id, audience=request.audience)
        if canon.digest(stored) != canon.digest(request):
            raise IdempotencyConflict(
                f"request {request.request_id!r} differs from the stored request")
        return stored
    if isinstance(request, str):
        return store.get_request(request, audience=audience, strict=True)
    raise StoreError("request must be a request_id or a RequestForContext")


def _current_policy(store: Store, subject_ref: str) -> tuple[Policy | None, CompiledPolicy]:
    """The subject's latest policy (or None) and its compiled form. With no policy the version
    is the compiled empty policy's digest, so adding a first policy changes it too."""
    policy = store.latest_policy(subject_ref)
    return policy, compile_policy(policy)


def _latest_claims(store: Store, rfc: RequestForContext) -> list[Claim]:
    """The claims of the request's investigation run recorded last, which must all be active.

    Only this bank's request counts (another bank may use the same request_id). "Last" is
    storage order (``list_claims`` returns rows as inserted, and every run inserts new claims),
    not ``created_at``: a rerun on an earlier clock is still the latest run.
    """
    request_id = rfc.request_id
    claims = store.list_claims(request_id=request_id, audience=rfc.audience, state=None)
    if not claims:
        raise PackageError(f"request {request_id} has no investigation results yet; "
                           "call run_investigation first")
    latest = claims[-1].run_id
    run = [claim for claim in claims if claim.run_id == latest]
    stale = [claim for claim in run if claim.state != DerivedState.ACTIVE]
    if stale:
        raise PackageError(f"the latest investigation of {request_id} is out of date: "
                           f"{len(stale)} claim(s) were invalidated by newer evidence; run the "
                           "investigation again")
    return run


def _load_ref(store: Store, ref: EvidenceRef) -> EvidenceRecord | BankTxn:
    if ref.ref_type == "record":
        return store.get_source(ref.ref_id, ref.revision)
    return store.get_bank_txn(ref.ref_id, ref.revision)


def _get(load, obj_id: Any, what: str):
    """``load(obj_id)``, with a missing object reported as an ApprovalError."""
    try:
        return load(obj_id)
    except NotFound:
        raise ApprovalError([f"no {what} {_clip(obj_id, 80)!r}"]) from None


def _default_policy_id(subject_ref: str) -> str:
    candidate = f"pol_{subject_ref}"
    return candidate if _is_ident(candidate) else canon.make_id("pol", subject_ref)


def _policy_refusal(exc: PolicyError) -> str:
    """Audit detail for a refused policy: counts for rule texts, never the texts themselves."""
    if isinstance(exc, RuleClarificationError):
        unresolved = sum(item["status"] != "resolved" for item in exc.interpretations)
        return (f"refused: {unresolved} of {len(exc.interpretations)} rule text(s) need "
                "clarification; nothing was saved")
    return f"refused: {exc}"


def _existing_approval(store: Store, draft: Draft, holder_id: Any) -> Approval | None:
    """The holder's approval of a draft that is already approved or packaged: the last pending
    or consumed approval naming this draft, holder, recipient and body digest."""
    if DraftStatus(draft.status) not in (DraftStatus.APPROVED, DraftStatus.PACKAGED):
        return None
    usable = (ApprovalStatus.PENDING, ApprovalStatus.CONSUMED)
    matches = [approval for approval in store.list_approvals(request_id=draft.request_id,
                                                             audience=draft.audience)
               if approval.draft_id == draft.draft_id and approval.holder_id == holder_id
               and approval.audience == draft.audience
               and approval.body_sha256 == draft.body_sha256
               and ApprovalStatus(approval.status) in usable]
    return matches[-1] if matches else None


def _typed_rules(rules: Any) -> list[Any]:
    """Typed rules from rule models, dicts or plain-English strings (parsed with
    ``parse_rule_text``). Any unresolved string raises RuleClarificationError."""
    if isinstance(rules, (str, dict, BaseModel)):
        rules = [rules]
    if not isinstance(rules, (list, tuple)):
        raise PolicyError("rules must be a list of rules or plain-English rule texts")
    typed, parses = [], []
    for item in rules:
        if not isinstance(item, str):
            typed.append(item)
            continue
        parsed = parse_rule_text(item)
        parses.append({"text": item, "status": parsed.status,
                       "interpretation": parsed.interpretation, "reason": parsed.reason,
                       "rule": parsed.rule.to_json_dict() if parsed.rule is not None else None})
        if parsed.rule is not None:
            typed.append(parsed.rule)
    if any(item["status"] != "resolved" for item in parses):
        raise RuleClarificationError(parses)
    return typed


def _invalidate_for_policy(store: Store, subject_ref: str, version_tag: str,
                           at: datetime) -> tuple[list[str], list[str]]:
    """Invalidate the subject's pending/approved drafts and pending approvals made under any
    other policy version. Returns (draft ids, approval ids)."""
    reason = f"the disclosure policy changed to {version_tag}"
    drafts, approvals = [], []
    for rfc in store.list_requests(subject_ref=subject_ref):
        # Scope by bank too: another bank's request may share this request_id.
        for draft in store.list_drafts(request_id=rfc.request_id, audience=rfc.audience):
            if draft.status in (DraftStatus.PENDING, DraftStatus.APPROVED) \
                    and draft.policy_version != version_tag:
                store.update_draft_status(draft.draft_id, DraftStatus.INVALIDATED, reason)
                drafts.append(draft.draft_id)
        for approval in store.list_approvals(request_id=rfc.request_id, audience=rfc.audience,
                                             status=ApprovalStatus.PENDING.value):
            if approval.policy_version != version_tag:
                store.update_approval(Approval.model_validate({
                    **approval.to_json_dict(), "status": ApprovalStatus.INVALIDATED.value,
                    "invalidated_reason": reason}))
                approvals.append(approval.approval_id)
    return drafts, approvals


# Consent-matrix refusals (agents/execution/out/consent_matrix.md) raised by this module come
# from ``alibi.core.approval.REFUSAL_TEMPLATES``: ``refusal_reason`` fills a template and
# ``code_for_reason`` recovers the code when an invalidated approval or draft is used later.
_refusal_reason = core_approval.refusal_reason
_code_for = core_approval.code_for_reason
_C2 = core_approval.REFUSAL_TEMPLATES["approval_digest_mismatch"]
_C6 = core_approval.REFUSAL_TEMPLATES["approval_digest_not_current"]


def _load_approval(store: Store, approval_id: Any) -> Approval:
    """The stored approval, or consent matrix C1 (``approval_missing``) when there is none. The
    reason names the request when ``approval_id`` is a request id that was never approved."""
    try:
        return store.get_approval(approval_id)
    except NotFound:
        pass
    problem = f"no approval {_clip(approval_id, 80)!r}"
    request_id: str = f"(unknown approval {_clip(approval_id, 80)})"
    if _is_ident(approval_id):
        try:
            request_id = store.get_request(approval_id).request_id
            problem = f"no holder approval recorded for request {request_id!r}"
        except NotFound:
            pass
    raise ApprovalError([problem], code="approval_missing", request_id=request_id)


def _source_change(draft: Draft, revisions: Mapping[str, int | None]) -> tuple[str, str] | None:
    """Consent matrix C8 for a draft whose cited source was corrected: ``(code, reason)``."""
    for ref in draft.source_revisions:
        active = revisions.get(ref.token())
        if active != ref.revision:
            record = ref.ref_id if ref.ref_type == "record" else ref.token()
            return ("source_revision_changed", _refusal_reason(
                "source_revision_changed", record_id=record, old=ref.revision,
                new=active if active is not None else "none"))
    return None


def _removed_by_holder(draft: Draft) -> list[str]:
    """The sources the holder took out of this draft (its review's ``removed`` entries)."""
    return sorted(entry.ref.token() for entry in draft.review.excluded
                  if entry.reason == core_package.REASON_REMOVED)


def _regenerated_body(store: Store, rfc: RequestForContext, draft: Draft,
                      compiled: CompiledPolicy) -> PackageBody | None:
    """The body the request's latest investigation run gives for this draft's review choices,
    built at the draft's own clock so an unchanged run reproduces the draft byte for byte.

    This binds an approval to the investigation behind its draft (consent matrix C3): a rerun
    that changed any answer, even only its rationale, changes the body. None when the latest
    run cannot be packaged (no claims, or claims invalidated by newer evidence)."""
    try:
        claims = _latest_claims(store, rfc)
        refs = _claim_dependencies(claims)
        if refs != core_approval.merge_refs(draft.source_revisions):
            return None  # the examined inputs changed, even if the recipient text did not
        evidence = [_load_ref(store, ref) for ref in refs]
        body, _ = core_package.build_body(rfc, claims, evidence, compiled, now=draft.created_at,
                                          removed=_removed_by_holder(draft))
    except (PackageError, NotFound):
        return None
    return body


def _claim_dependencies(claims: Iterable[Claim]) -> list[EvidenceRef]:
    """All private revision pins, including examined sources an unresolved answer withholds."""
    return core_approval.merge_refs(*(dependency_refs(claim) for claim in claims))


_VOLATILE_BODY_FIELDS = ("expires_at", "issued_at")


def _body_fingerprint(body: PackageBody) -> str:
    """Digest of a body without the fields derived from the clock, so re-rendering the same
    review a minute later is the same draft, while any change to content is a new one."""
    data = body.to_json_dict()
    for field in _VOLATILE_BODY_FIELDS:
        data.pop(field, None)
    return canon.digest(data)


def _live_drafts(store: Store, request_id: str, audience: str) -> list[Draft]:
    """One bank's drafts of a request that are still pending, approved or packaged, oldest
    first. Request ids are chosen by banks, so two banks can share one: always scope by the
    audience, or one bank's draft would supersede or block the other's."""
    live = (DraftStatus.PENDING, DraftStatus.APPROVED, DraftStatus.PACKAGED)
    drafts = [d for d in store.list_drafts(request_id=request_id, audience=audience)
              if DraftStatus(d.status) in live]
    # Store insertion order breaks equal-clock ties; a content hash is not chronology.
    return sorted(drafts, key=lambda d: d.created_at)


def _supersede(store: Store, request_id: str, audience: str, current: Draft, *,
               change: str) -> None:
    """A newly built body replaces every earlier live draft of the request (consent matrix C6)
    and invalidates every pending approval of an earlier body (C2), so an approval never signs
    a body the holder has since edited."""
    for old in _live_drafts(store, request_id, audience):
        if old.draft_id == current.draft_id or DraftStatus(old.status) == DraftStatus.PACKAGED:
            continue
        store.update_draft_status(old.draft_id, DraftStatus.INVALIDATED, _C6.format(
            approved12=old.body_sha256[:12], current12=current.body_sha256[:12]))
    for approval in store.list_approvals(request_id=request_id,
                                         status=ApprovalStatus.PENDING.value):
        if approval.audience != audience or (approval.body_sha256 == current.body_sha256
                and core_approval.merge_refs(approval.source_revisions)
                == core_approval.merge_refs(current.source_revisions)):
            continue
        store.update_approval(Approval.model_validate({
            **approval.to_json_dict(), "status": ApprovalStatus.INVALIDATED.value,
            "invalidated_reason": _C2.format(approved12=approval.body_sha256[:12],
                                             actual12=current.body_sha256[:12], change=change)}))


def _draft_problems(draft: Draft, approval: Approval) -> list[str]:
    problems = []
    status = DraftStatus(draft.status)
    if status != DraftStatus.APPROVED:
        why = f" ({draft.invalidated_reason})" if draft.invalidated_reason else ""
        problems.append(f"the draft is {status.value}{why}, not approved")
    if draft.request_id != approval.request_id:
        problems.append(f"the draft answers request {draft.request_id}, not the approved "
                        f"request {approval.request_id}")
    if draft.audience != approval.audience:
        problems.append(f"the draft is addressed to {draft.audience}, not the approved "
                        f"recipient {approval.audience}")
    return problems


def _stored_package(store: Store, approval: Approval) -> ContextPackage | None:
    """The package an approval was already consumed by, if it is stored."""
    if ApprovalStatus(approval.status) != ApprovalStatus.CONSUMED or not approval.package_id:
        return None
    try:
        return store.get_package(approval.package_id)
    except NotFound:
        return None


def _on_connection(txn: dict[str, Any], connection_id: str) -> dict[str, Any]:
    """A fixture txn as delivered by ``connection_id``."""
    if txn.get("provenance", {}).get("connection_id") == connection_id:
        return txn
    moved = copy.deepcopy(txn)
    moved["provenance"]["connection_id"] = connection_id
    return moved


# --------------------------------------------------------------------------- store and keys


def open_store(path: Path | str | None = None, *, master_key: bytes | str | None = None) -> Store:
    """Open (or create) the encrypted passport database, by default ``data/alibi/passport.db``.

    The master key is ``master_key``, else ``ALIBI_MASTER_KEY``, else ``master.key`` beside the
    database (created with mode 0600 on first use). Close the store, or use it as a context
    manager.
    """
    return Store(DEFAULT_DB_PATH if path is None else path, master_key=master_key)


def default_keystore(key_dir: Path | str | None = None) -> DemoKeystore:
    """The demo keystore (``data/alibi/keys`` by default), with any missing keys and
    ``trust_registry.json`` created."""
    return DemoKeystore(DEFAULT_KEY_DIR if key_dir is None else key_dir).ensure()


# --------------------------------------------------------------------------- ingest


def ingest_merchant_event(store: Store, payload: Any, *, registry: TrustRegistry | None = None,
                          connector: str = core_ingest.CONNECTOR_WEBHOOK,
                          subject_ref: str | None = None,
                          now: datetime | None = None) -> PutResult:
    """Verify and store one issuer-signed merchant event (``{"event", "signature"}``).

    Replaying identical bytes is a ``duplicate``; a higher revision is a ``corrected`` record
    that invalidates what depended on the old one. Raises ``IngestError``.
    """
    return core_ingest.ingest_merchant_event(store, _registry(registry), payload,
                                             connector=connector, subject_ref=subject_ref,
                                             now=_clock(now))


def ingest_bank_txns(store: Store, txns: Any, *, subject_ref: str, connection_id: str,
                     cursor: str | None = None, now: datetime | None = None) -> list[PutResult]:
    """Store one bank-feed batch (and its cursor) atomically. Raises ``IngestError``."""
    return core_ingest.ingest_bank_txns(store, txns, subject_ref=subject_ref,
                                        connection_id=connection_id, cursor=cursor,
                                        now=_clock(now))


def import_self_supplied(store: Store, payload: Any, *, subject_ref: str,
                         now: datetime | None = None) -> PutResult:
    """Import the holder's own receipt or export at ``self_supplied`` trust, under a ``self_``
    record id. Trust claims in the payload are dropped. Raises ``IngestError``."""
    return core_ingest.import_self_supplied(store, payload, subject_ref=subject_ref,
                                            now=_clock(now))


def receive_rfc(store: Store, payload: Any, *, authenticated_issuer: str,
                registry: TrustRegistry | None = None,
                account_refs: Collection[str] | None = None,
                now: datetime | None = None) -> RequestForContext:
    """Accept a bank's Request for Context (signed ``{"request", "signature"}`` or bare).

    The authenticated bank must be the request's issuer and audience. A request naming
    ``txn_ids`` also needs ``account_refs``, the bank's own accounts (fail closed). A
    follow-up (``parent_request_id``) must name this bank's earlier request for the same
    subject and stay inside its scope: same scope kind, a period inside the parent's,
    ``txn_ids`` within the parent's; otherwise the refusal reads
    ``scope_wider_than_parent: ...``. Request ids are per bank: the same bank resending an id
    with identical content is idempotent and with different content is refused, while another
    bank may use the same id without conflict (``store.get_request(id, issuer=...)`` tells
    them apart; the bare id resolves to the request received first). Raises ``IngestError``.
    """
    return core_ingest.receive_rfc(store, _registry(registry), payload,
                                   authenticated_issuer=authenticated_issuer,
                                   account_refs=account_refs, now=_clock(now))


# --------------------------------------------------------------------------- investigation


def run_investigation(store: Store, request: str | RequestForContext, *, mode: str = "rules",
                      audience: str | None = None,
                      gateway: Gateway | None = None,
                      settlement_lookup: SettlementLookup | None = None,
                      txn_refresh: TxnRefresh | None = None, budget: int = DEFAULT_BUDGET,
                      registry: TrustRegistry | None = None,
                      now: datetime | None = None, client: Any = None,
                      holder_claim: Any = None,
                      payout_query: PayoutQuery | None = None) -> InvestigationResult:
    """Investigate a stored request and save its links, claims and trace.

    ``holder_claim`` (rules and one-pass modes) is the holder's own untrusted account of a credit,
    ``{txn_id, stated_minor}``; it is checked against the records and never used as evidence.

    ``request`` is a request_id together with ``audience`` (the bank that sent it; without it a
    bare id must be one bank's alone, else ``AmbiguousRequest``), or the stored
    ``RequestForContext``, whose own audience is used (an ``audience`` that contradicts it raises ``StoreError``). Without ``gateway`` a
    ``Gateway`` is built for the
    request's subject and the next run id, with the connector callables, budget and registry
    given (the registry verifies settlement lookups).

    ``mode`` picks the engine: ``"rules"`` is the deterministic core engine
    (``alibi.core.investigate_rules``); ``"agents"`` is the Gemini roles pipeline
    (``alibi.agents.runner.run_investigation``); ``"one_pass"`` (alias ``"single_call"``) is the
    single-call baseline (``alibi.baselines.one_pass.run_investigation``). The model modes take
    ``client`` (anything with ``GeminiClient.generate``'s signature; default a ``GeminiClient``,
    which needs ``GEMINI_API_KEY`` on first use) and never raise for a model failure: the
    affected claims come back unresolved and the result's ``unavailable`` and trace say why.
    Any other mode name raises ``NotImplementedError``.
    """
    modes = {core_rules.MODE: "rules", "agents": "agents", "one_pass": "one_pass",
             "single_call": "one_pass"}
    engine = modes.get(mode)
    if engine is None:
        raise NotImplementedError(f"mode {mode!r} is not built; modes are 'rules', 'agents' and "
                                  "'one_pass' (alias 'single_call')")
    at = _clock(now)
    rfc = _stored_request(store, request, audience)
    if engine != "rules":
        if client is None:
            from alibi.llm.client import GeminiClient   # lazy: no key is needed to import
            client = GeminiClient()
        if engine == "agents":
            from alibi.agents import runner as agents_runner   # lazy: the runner imports this module
            result = agents_runner.run_investigation(
                store, rfc, client=client, gateway=gateway, settlement_lookup=settlement_lookup,
                txn_refresh=txn_refresh, budget=budget, registry=registry, now=now,
                payout_query=payout_query)
            actor = agents_runner.ACTOR
        else:
            from alibi.baselines import one_pass
            result = one_pass.run_investigation(
                store, rfc, client=client, gateway=gateway, settlement_lookup=settlement_lookup,
                txn_refresh=txn_refresh, budget=budget, registry=registry, now=now,
                holder_claim=holder_claim, payout_query=payout_query)
            actor = one_pass.ACTOR
        answers = ", ".join(f"{claim.question_id} {ClaimStatus(claim.status).value}"
                            for claim in result.claims)
        problems = f"; {len(result.unavailable)} model step(s) failed" if result.unavailable else ""
        _audit(store, action="run_investigation", actor=actor, outcome="ok", at=at,
               target_id=rfc.request_id,
               detail=f"run {result.run_id} ({result.mode}) for {rfc.audience}: {answers}; "
                      f"{result.calls_used} gateway calls{problems}")
        return result
    if gateway is None:
        if registry is None and settlement_lookup is not None:
            registry = default_keystore().registry()
        gateway = Gateway(store, subject_ref=rfc.subject_ref,
                          run_id=core_rules.next_run_id(store, rfc.request_id,
                                                        audience=rfc.audience),
                          registry=registry, budget=budget, settlement_lookup=settlement_lookup,
                          txn_refresh=txn_refresh, now=now, request_id=rfc.request_id,
                          payout_query=payout_query)
    result = core_rules.run_investigation(rfc, store, gateway, mode, now=now,
                                          holder_claim=holder_claim)
    answers = ", ".join(f"{claim.question_id} {ClaimStatus(claim.status).value}"
                        for claim in result.claims)
    _audit(store, action="run_investigation", actor=core_rules.ACTOR, outcome="ok", at=at,
           target_id=rfc.request_id,
           detail=f"run {result.run_id} for {rfc.audience}: {answers}; "
                  f"{result.calls_used} gateway calls")
    return result


# --------------------------------------------------------------------------- policy


def record_policy(store: Store, subject_ref: str, rules: Any, *, policy_id: str | None = None,
                  confirmed: bool = False, now: datetime | None = None) -> Policy:
    """Save the subject's disclosure rules as the next policy version.

    ``rules`` is a list of ``PolicyRule`` models or dicts, or plain-English texts (a single
    rule may be given bare). Each text goes through ``parse_rule_text``; if any is unresolved,
    nothing is saved and ``RuleClarificationError`` lists every interpretation. ``policy_id``
    defaults to the subject's current policy, else ``pol_<subject_ref>``. Saving invalidates the
    subject's pending/approved drafts and pending approvals made under another policy version.
    Recording rules identical to the policy in force (the subject's latest version, under the
    same ``policy_id``) returns it unchanged; naming an older ``policy_id`` saves a new version
    of it, which then takes effect. A ``PolicyError`` refusal is audited (counts, never texts).
    """
    at = _clock(now)
    try:
        compiled = compile_policy(_typed_rules(rules))
        with store.transaction():
            latest = store.latest_policy(subject_ref)
            pid = policy_id or (latest.policy_id if latest is not None else
                                _default_policy_id(subject_ref))
            meaning = [rule.to_json_dict() for rule in compiled.rules]
            if latest is not None and latest.policy_id == pid \
                    and latest.confirmed == confirmed \
                    and [rule.to_json_dict() for rule in latest.rules] == meaning:
                return latest           # already the policy in force
            try:
                current = store.get_policy(pid)
            except NotFound:
                current = None
            try:
                policy = Policy(policy_id=pid,
                                version=1 if current is None else current.version + 1,
                                subject_ref=subject_ref, rules=compiled.rules,
                                confirmed=confirmed, created_at=at)
            except ValidationError as exc:
                raise PolicyError(f"invalid policy: {exc.errors()[0]['msg']}") from None
            store.save_policy(policy)
            tag = policy.version_tag()
            drafts, approvals = _invalidate_for_policy(store, subject_ref, tag, at)
            _audit(store, action="record_policy", actor=HOLDER, outcome="ok", at=at,
                   target_id=pid,
                   detail=f"{tag}: {len(policy.rules)} rule(s), confirmed={confirmed}; "
                          f"invalidated {len(drafts)} draft(s) and {len(approvals)} approval(s)")
    except PolicyError as exc:
        _audit(store, action="record_policy", actor=HOLDER, outcome="refused", at=at,
               target_id=policy_id or subject_ref, detail=_policy_refusal(exc))
        raise
    return store.get_policy(policy.policy_id, policy.version)


# --------------------------------------------------------------------------- review, approval, dispatch


def build_review(store: Store, request_id: str, *, audience: str | None = None,
                 now: datetime | None = None, removed: Collection[str] = ()) -> Draft:
    """Build and save the draft the holder reviews for the request ``(audience, request_id)``.

    ``audience`` is the bank that sent the request (request ids are chosen by banks).
    Uses the active claims of the request's investigation run recorded last, every source and
    txn they cite (the cited revisions, which are the active ones while the claims are active),
    and the subject's latest policy (or none). ``removed`` names sources the holder took out.
    Returns the stored draft: the recipient-facing ``body`` plus the private ``review``.
    Raises ``PackageError`` (and audits the refusal) when there is no current investigation to
    package.
    """
    at = _clock(now)
    rfc = _stored_request(store, request_id, audience)
    try:
        with store.transaction():
            claims = _latest_claims(store, rfc)
            refs = _claim_dependencies(claims)
            evidence = [_load_ref(store, ref) for ref in refs]
            _, compiled = _current_policy(store, rfc.subject_ref)
            body, review = core_package.build_body(rfc, claims, evidence, compiled, now=at,
                                                   removed=removed)
            draft = core_package.make_draft(rfc, body, review,
                                            policy_version=compiled.policy_version,
                                            source_revisions=refs, now=at)
            fingerprint = _body_fingerprint(draft.body)
            same_body = [d for d in _live_drafts(store, rfc.request_id, rfc.audience)
                         if _body_fingerprint(d.body) == fingerprint
                         and d.policy_version == draft.policy_version
                         and core_approval.merge_refs(d.source_revisions) == refs]
            if same_body:
                return same_body[-1]   # built already (any clock): keep it and its status
            taken = {d.draft_id for d in store.list_drafts(request_id=rfc.request_id)}
            sequence = 0
            while draft.draft_id in taken:   # the same body was built and invalidated before
                sequence += 1
                draft = core_package.make_draft(rfc, body, review,
                                                policy_version=compiled.policy_version,
                                                source_revisions=refs, now=at,
                                                sequence=sequence)
            store.save_draft(draft)
            change = f" ({', '.join(sorted(str(r) for r in removed))} removed)" if removed else ""
            _supersede(store, rfc.request_id, rfc.audience, draft, change=change)
            _audit(store, action="build_review", actor=SYSTEM, outcome="ok", at=at,
                   target_id=draft.draft_id,
                   detail=f"draft for {rfc.request_id} to {rfc.audience}: "
                          f"{len(body.attachments)} attached, {len(review.excluded)} excluded, "
                          f"{len(review.withheld_fields)} withheld field entries; policy "
                          f"{compiled.policy_version}")
            return store.get_draft(draft.draft_id)
    except PackageError as exc:
        _audit(store, action="build_review", actor=SYSTEM, outcome="refused", at=at,
               target_id=rfc.request_id, detail=f"refused for {rfc.audience}: {exc}")
        raise


def approve(store: Store, draft_id: str, *, holder_id: str,
            now: datetime | None = None) -> Approval:
    """Record the holder's approval of one exact pending draft for its named recipient.

    The approval binds the body digest, audience, request, policy version and source revisions
    for 15 minutes. The draft becomes ``approved``. Approving again a draft this holder already
    approved (now ``approved`` or ``packaged``) returns that same approval, so a retried or
    replayed flow reaches the stored package through ``sign_and_package``; an expired approval
    is not renewed (build the review again). Raises ``ApprovalError`` (and audits the refusal)
    when the draft is not pending, its policy or sources changed, or it expired.
    """
    at = _clock(now)
    actor = holder_id if _is_ident(holder_id) else HOLDER
    try:
        with store.transaction():
            draft = _get(store.get_draft, draft_id, "draft")
            existing = _existing_approval(store, draft, holder_id)
            if existing is not None:
                _audit(store, action="approve", actor=actor, outcome="duplicate", at=at,
                       target_id=existing.approval_id,
                       detail=f"retry for draft {draft.draft_id}: returned the existing "
                              f"{ApprovalStatus(existing.status).value} approval")
                return existing
            rfc = _get(lambda rid: store.get_request(rid, audience=draft.audience),
                       draft.request_id, "request")
            _, compiled = _current_policy(store, rfc.subject_ref)
            revisions = core_approval.current_revisions(store, draft.source_revisions)
            # Consent-matrix refusals, in the matrix order: C11, C7, C8, then C6 (a superseded
            # draft), with the stored invalidation reason when it is itself a matrix sentence.
            problems: list[str] = []
            refusal: tuple[str, str] | None = None
            if at >= rfc.expires_at:
                problems.append(f"the request expired at {format_utc(rfc.expires_at)}")
                refusal = ("request_expired", _refusal_reason(
                    "request_expired", request_id=rfc.request_id,
                    expires_at=format_utc(rfc.expires_at)))
            if compiled.policy_version != draft.policy_version:
                problems.append(f"the disclosure policy changed since this draft was built "
                                f"({draft.policy_version}, now {compiled.policy_version}); "
                                "build the review again")
                refusal = refusal or ("policy_version_changed", _refusal_reason(
                    "policy_version_changed", old=draft.policy_version,
                    new=compiled.policy_version))
            changed = _source_change(draft, revisions)
            if changed is not None:
                problems.append(changed[1].removeprefix("dispatch refused: ").rstrip(".")
                                + "; build the review again")
                refusal = refusal or changed
            if DraftStatus(draft.status) == DraftStatus.INVALIDATED:
                why = draft.invalidated_reason or "this draft was superseded"
                problems.append(f"only a pending draft can be approved; this draft is "
                                f"invalidated ({why})")
                if refusal is None and _code_for(why):
                    refusal = (_code_for(why), why)
            if problems:
                code, reason = refusal if refusal else (None, None)
                raise ApprovalError(problems, code=code, reason=reason)
            approval = core_approval.approve(draft, holder_id=holder_id, now=at)
            store.save_approval(approval)
            stored = store.get_approval(approval.approval_id)
            if ApprovalStatus(stored.status) != ApprovalStatus.PENDING:
                raise ApprovalError([stored.invalidated_reason
                                     or "the approval could not be stored as pending"])
            store.update_draft_status(draft.draft_id, DraftStatus.APPROVED)
            _audit(store, action="approve", actor=actor, outcome="ok", at=at,
                   target_id=stored.approval_id,
                   detail=f"approved draft {draft.draft_id} for {stored.audience} (request "
                          f"{stored.request_id}) until {format_utc(stored.expires_at)}")
        return stored
    except ApprovalError as exc:
        _audit(store, action="approve", actor=actor, outcome="refused", at=at,
               target_id=draft_id, detail="refused: " + "; ".join(exc.problems))
        raise


def sign_and_package(store: Store, approval_id: str, *, keystore: DemoKeystore | None = None,
                     now: datetime | None = None) -> ContextPackage:
    """Revalidate an approval against current state, sign its draft and deliver the package.

    In one store transaction: reload the approval, its draft, the request, the current policy
    version and the current source revisions; refuse unless the approval is still valid for
    exactly that body; sign; save the package; mark the approval consumed and the draft
    packaged; record a ``sent`` delivery to the audience. A retry for an approval already
    consumed by a stored package returns that identical package with no second delivery. Any
    validation failure raises ``ApprovalError`` and writes a refused audit event; its ``code``
    and ``reason`` are the consent matrix's (agents/execution/out/consent_matrix.md), decided in
    the matrix order: C1 ``approval_missing``, C11 ``request_expired``, C10 ``approval_expired``,
    C4 ``approval_audience_mismatch``, C7 ``policy_version_changed``, C8
    ``source_revision_changed``, then C2/C3 ``approval_digest_mismatch`` (the body is no longer
    the request's current draft, or the investigation was run again and changed it).
    """
    at = _clock(now)
    try:
        with store.transaction():
            approval = _load_approval(store, approval_id)
            stored = _stored_package(store, approval)
            if stored is not None:
                _audit(store, action="sign_and_package", actor=SYSTEM, outcome="duplicate",
                       at=at, target_id=stored.package_id,
                       detail=f"retry for approval {approval.approval_id}: returned the stored "
                              "package; no new delivery")
                return stored
            if not approval.draft_id:
                raise ApprovalError(["the approval names no draft"])
            draft = _get(store.get_draft, approval.draft_id, "draft")
            rfc = _get(lambda rid: store.get_request(rid, audience=approval.audience),
                       approval.request_id, "request")
            _, compiled = _current_policy(store, rfc.subject_ref)
            refs = core_approval.merge_refs(approval.source_revisions, draft.source_revisions,
                                            core_approval.body_refs(draft.body))
            revisions = core_approval.current_revisions(store, refs)
            problems = _draft_problems(draft, approval)
            if at >= rfc.expires_at:
                problems.append(f"the request expired at {format_utc(rfc.expires_at)}")
            problems += core_approval.check_approval(
                approval, body=draft.body, audience=approval.audience,
                request_id=approval.request_id, policy_version=compiled.policy_version,
                revisions=revisions, now=at)
            # Defence in depth for consent matrix C2: the approved body must still be the
            # request's current draft, whatever the approval's own status says. C3: and still
            # the body the latest investigation run gives, so a rerun that changed an answer
            # (even only its rationale) revokes the approval before any new preview is built.
            live = _live_drafts(store, rfc.request_id, rfc.audience)
            current = live[-1] if live else None
            current_sha256, change = None, ""
            if current is not None and current.body_sha256 != approval.body_sha256:
                current_sha256 = current.body_sha256
                problems.append(_C2.format(approved12=approval.body_sha256[:12],
                                           actual12=current_sha256[:12], change=""))
            elif not problems:
                regenerated = _regenerated_body(store, rfc, draft, compiled)
                regenerated_sha256 = canon.digest(regenerated) if regenerated is not None else None
                if regenerated_sha256 != approval.body_sha256:
                    current_sha256 = regenerated_sha256 or "unknown"
                    change = core_approval.CHANGE_REGENERATED
                    problems.append("the investigation was run again after approval and changed "
                                    "the answers; review and approve the regenerated draft"
                                    if regenerated is not None else
                                    "the latest investigation of the request cannot be packaged; "
                                    "run it again and review the regenerated draft")
            if problems:
                refusal = core_approval.refusal_for(
                    approval, body=draft.body, audience=draft.audience,
                    request_id=draft.request_id, policy_version=compiled.policy_version,
                    revisions=revisions, now=at, request_expires_at=rfc.expires_at,
                    current_body_sha256=current_sha256, change=change)
                code, reason = refusal if refusal else (None, None)
                raise ApprovalError(problems, code=code, reason=reason)
            package = core_package.sign_package(draft.body, approval, _keystore(keystore),
                                                policy_version=compiled.policy_version,
                                                revisions=revisions, now=at)
            store.save_package(package, approval_id=approval.approval_id)
            store.update_approval(core_approval.consume(approval, package_id=package.package_id))
            store.update_draft_status(draft.draft_id, DraftStatus.PACKAGED)
            store.save_delivery(Delivery(
                delivery_id=canon.make_id("dlv", package.package_id, package.body.audience),
                package_id=package.package_id, request_id=package.body.request_id,
                audience=package.body.audience, package_sha256=canon.digest(package),
                delivered_at=at))
            _audit(store, action="sign_and_package", actor=SYSTEM, outcome="ok", at=at,
                   target_id=package.package_id,
                   detail=f"signed under approval {approval.approval_id} and sent to "
                          f"{package.body.audience} for request {package.body.request_id}")
        return package
    except ApprovalError as exc:
        _audit(store, action="sign_and_package", actor=SYSTEM, outcome="refused", at=at,
               target_id=approval_id, detail="refused: " + "; ".join(exc.problems))
        raise


def _package_request_id(package: Any) -> str | None:
    """``body.request_id`` of a package in any accepted input form, or ``None``. Never raises:
    a package this cannot read is rejected by the core verifier with the real reason."""
    try:
        data = package.to_json_dict() if isinstance(package, ContextPackage) else package
        if isinstance(data, (bytes, bytearray, str)):
            data = canon.parse_json(bytes(data) if isinstance(data, bytearray) else data)
        request_id = data["body"]["request_id"]
    except Exception:  # noqa: BLE001 - verification reports, it never raises
        return None
    return request_id if _is_ident(request_id) else None


def verify_package(package: Mapping[str, Any] | str | bytes | ContextPackage, *,
                   expected_audience: str, registry: TrustRegistry | None = None,
                   now: datetime | None = None, expected_subject: str | None = None,
                   expected_request_id: str | None = None,
                   store: Store | None = None) -> VerificationResult:
    """Recipient-side verification against the pinned registry. Never raises, never writes.

    Without ``registry`` it reads the pinned ``data/alibi/keys/trust_registry.json`` read-only
    (no key is created and the file is never rewritten); a missing or unreadable file rejects
    the package, and so does a ``now`` that is not timezone-aware. Three separate answers:
    ``authentication`` (schema, body digest, recipient binding, expiry, Alibi signature),
    ``sources`` (per-attachment trust; a failed claimed issuer signature rejects the package)
    and ``claims`` (status per question, arithmetic rechecked).

    ``expected_subject`` and ``expected_request_id`` bind the package to the recipient's own
    request: the body's ``case_ref`` must be the case binding of (audience, root request,
    subject) and ``request_id`` must match. With ``store``, both default from the stored RFC
    when the store holds the request the package names for ``expected_audience`` (the issuer
    is the audience), so a bank-side caller that opens the passport store binds subject and
    request without passing them. An explicit value always wins, and an unknown request
    leaves the defaults empty (the package then verifies as before, without subject binding).
    """
    if store is not None and (expected_subject is None or expected_request_id is None):
        request_id = _package_request_id(package)
        if request_id is not None:
            try:
                rfc = store.get_request(request_id, issuer=expected_audience)
            except StoreError:  # NotFound or an unreadable store: no defaults
                rfc = None
            if rfc is not None:
                expected_subject = expected_subject if expected_subject is not None else rfc.subject_ref
                expected_request_id = (expected_request_id if expected_request_id is not None
                                       else rfc.request_id)
    if registry is None:
        path = DEFAULT_KEY_DIR / REGISTRY_FILE
        try:
            registry = load_registry(path)
        except Exception as exc:  # noqa: BLE001 - verification reports, it never raises
            reason = _clip(f"no usable pinned trust registry at {path.as_posix()} "
                           f"({type(exc).__name__}); pass the registry to verify against", 300)
            return VerificationResult(accepted=False,
                                      authentication=AuthResult(valid=False, reasons=[reason]),
                                      errors=[reason])
    return core_package.verify_package(package, registry, expected_audience=expected_audience,
                                       now=now, expected_subject=expected_subject,
                                       expected_request_id=expected_request_id)


# --------------------------------------------------------------------------- fixtures


def load_fixture(store: Store, variant: str = "supported", *,
                 keystore: DemoKeystore | None = None,
                 connection_id: str = maya.CONNECTION_ID, include_rfcs: bool = True,
                 now: datetime | None = None) -> dict[str, Any]:
    """Load a committed Maya fixture (or ``retail_purchase``) through the real ingest paths.

    Merchant events are signed with the keystore's merchant key and ingested by webhook, bank
    txns go through the feed for ``connection_id``, self-supplied items through the import, and
    the RFCs (signed with the bank keys) through ``receive_rfc``. Lookup events stay outside
    the passport until an investigation retrieves them. Replaying is idempotent.

    ``now`` defaults to the fixture clock ``FIXTURE_NOW`` (2026-11-07T12:00:00Z), not the wall
    clock, because the fixture RFCs expire (RFC A at 2026-11-11T09:00:00Z). A ``now`` at or past
    an RFC's expiry raises ``IngestError`` before anything is loaded.

    Returns ``{variant, subject_ref, record_ids, txn_ids, request_ids, requests,
    settlement_lookup, txn_refresh, corrections, statuses, expected, now}``: ``requests`` lists
    each loaded request as ``{"request_id", "audience"}`` (what holder calls need, since
    request ids are per bank), ``corrections`` are signed payloads
    ready for ``ingest_merchant_event``, ``statuses`` maps each record and txn id to its put
    status, ``expected`` is the fixture's development gold, and ``now`` is the clock used (pass
    it to the later steps).
    """
    at = FIXTURE_NOW if now is None else _clock(now)
    fixture = maya.load_variant(variant)
    if include_rfcs:
        expired = [raw["request_id"] for raw in fixture["rfcs"]
                   if at >= parse_utc(raw["expires_at"])]
        if expired:
            first = min(parse_utc(raw["expires_at"]) for raw in fixture["rfcs"])
            raise IngestError(f"fixture request(s) {', '.join(expired)} expired by "
                              f"{format_utc(at)}; nothing was loaded. Pass a now before "
                              f"{format_utc(first)}, such as the fixture clock "
                              f"{format_utc(FIXTURE_NOW)}")
    keys = _keystore(keystore)
    registry = keys.registry()
    subject = fixture["subject_ref"]
    kid = fixture.get("merchant_kid") or maya.MERCHANT_KID
    record_ids: list[str] = []
    txn_ids: list[str] = []
    request_ids: list[str] = []
    requests: list[dict[str, str]] = []
    statuses: dict[str, str] = {}

    def note(put: PutResult, ids: list[str]) -> None:
        ident = put.record.record_id if isinstance(put.record, EvidenceRecord) \
            else put.record.txn_id
        ids.append(ident)
        statuses[ident] = put.status

    for raw in fixture["merchant_events"]:
        note(ingest_merchant_event(store, maya.sign_event(raw, keys, kid), registry=registry,
                                   subject_ref=subject, now=at), record_ids)
    txns = [_on_connection(txn, connection_id) for txn in fixture["bank_txns"]]
    if txns:
        for put in ingest_bank_txns(store, txns, subject_ref=subject,
                                    connection_id=connection_id, now=at):
            note(put, txn_ids)
    for raw in fixture["self_supplied"]:
        note(import_self_supplied(store, raw, subject_ref=subject, now=at), record_ids)
    if include_rfcs:
        accounts = {FIXTURE_ACCOUNT_BANK: [fixture["account_ref"]]}
        for raw in fixture["rfcs"]:
            rfc = receive_rfc(store, maya.sign_request(raw, keys),
                              authenticated_issuer=raw["issuer"], registry=registry,
                              account_refs=accounts.get(raw["issuer"]) if raw.get("txn_ids")
                              else None, now=at)
            request_ids.append(rfc.request_id)
            requests.append({"request_id": rfc.request_id, "audience": rfc.audience})
    return {
        "variant": variant,
        "subject_ref": subject,
        "record_ids": record_ids,
        "txn_ids": txn_ids,
        "request_ids": request_ids,
        "requests": requests,
        "settlement_lookup": maya.settlement_lookup(fixture, keys),
        "txn_refresh": maya.txn_refresh(fixture),
        "corrections": [maya.sign_event(raw, keys, kid) for raw in fixture["corrections"]],
        "statuses": statuses,
        "expected": fixture["expected"],
        "now": at,
    }
