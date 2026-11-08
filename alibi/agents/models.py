"""Typed outputs of the four Gemini roles and the policy parser, plus reference validation.

Each pydantic model mirrors the sibling JSON schema in ``alibi/agents/prompts/<role>.json``.
Undeclared fields the model returns are dropped (``extra="ignore"``) and reported by
``undeclared_fields``; enum values and required fields are checked strictly.

``validate_refs(output, material)`` is the backend's reference check: every ``record_id`` and
``revision`` an agent cites must name something in the retrieved ``Material``. Unknown refs
are removed from the output and returned as ``ValidationFailure`` entries so the runner can
log them as ``validation`` trace events with outcome ``refused``. Nothing an agent says can
add a record to the material.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from alibi.agents.material import Material

Status = Literal["supported", "contradicted", "unresolved"]
ActionType = Literal["lookup_settlement", "lookup_payouts_covering", "lookup_by_date",
                     "refresh_txn", "none"]
ACTION_TYPES: tuple[str, ...] = ("lookup_settlement", "lookup_payouts_covering",
                                 "lookup_by_date", "refresh_txn", "none")


class _Out(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


class RefOut(_Out):
    record_id: str
    revision: int = Field(ge=1)


# --------------------------------------------------------------------------- matcher


class MatcherCandidate(RefOut):
    relationship: Literal["exact", "part_of_bundle", "refund", "fee", "split_payment", "unrelated"]
    signals: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)


class MatcherOutput(_Out):
    entry: RefOut
    candidates: list[MatcherCandidate] = Field(default_factory=list)
    decision: Literal["linked", "ambiguous", "unmatched"]
    chosen_record_id: str = ""
    chosen_revision: int = Field(default=0, ge=0)
    rationale: str = ""


# --------------------------------------------------------------------------- detective


class DetectiveAlert(_Out):
    kind: Literal["unexplained_credit", "unusual_inflow", "unusual_outflow", "amount_discrepancy",
                  "settlement_discrepancy", "merchant_unverified"]
    summary: str = ""
    refs: list[RefOut] = Field(default_factory=list)
    suggested_evidence: str = ""


class ClaimRef(RefOut):
    role: Literal["confirms", "conflicts", "context"] = "context"


class ActionArgs(_Out):
    payout_reference: str | None = None
    merchant_id: str | None = None
    record_id: str | None = None
    txn_id: str | None = None
    payout_date: str | None = None


class NextCheck(_Out):
    type: Literal["lookup_settlement", "lookup_payouts_covering", "lookup_by_date", "refresh_txn",
                  "merchant_lookup", "none"]
    args: ActionArgs = Field(default_factory=ActionArgs)
    reason: str = ""


class DetectiveClaim(_Out):
    claim_id: str
    question_id: str
    statement: str = ""
    status: Status
    refs: list[ClaimRef] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    next_checks: list[NextCheck] = Field(default_factory=list)


class DetectiveOutput(_Out):
    alerts: list[DetectiveAlert] = Field(default_factory=list)
    claims: list[DetectiveClaim] = Field(default_factory=list)


# --------------------------------------------------------------------------- challenger


class WhyRef(RefOut):
    why: str = ""


class NextAction(_Out):
    type: ActionType
    args: ActionArgs = Field(default_factory=ActionArgs)
    reason: str = ""


class ChallengerOutput(_Out):
    claim_id: str
    proposed_status: Status
    bank_settlement_established: bool
    payout_reference: str = ""
    confirming_refs: list[WhyRef] = Field(default_factory=list)
    conflicting_refs: list[WhyRef] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    next_action: NextAction = Field(default_factory=lambda: NextAction(type="none"))
    trace: list[str] = Field(default_factory=list)
    rationale: str = ""


# --------------------------------------------------------------------------- curator


class Disclosure(RefOut):
    question_id: str
    form: Literal["full", "extract", "generalised"]
    fields: list[str] = Field(default_factory=list)
    reason: str = ""


class Exclusion(RefOut):
    reason: Literal["not_needed_for_any_question", "policy_denied", "policy_generalised",
                    "outside_time_scope", "duplicate_evidence", "unresolved_trust"]
    note: str = ""


class CuratorGap(_Out):
    question_id: str
    description: str = ""


class CuratorOutput(_Out):
    disclosures: list[Disclosure] = Field(default_factory=list)
    exclusions: list[Exclusion] = Field(default_factory=list)
    gaps: list[CuratorGap] = Field(default_factory=list)
    rationale: str = ""


# --------------------------------------------------------------------------- policy parse


class PolicyParams(_Out):
    max_age_days: int | None = Field(default=None, ge=0)
    recipients: list[str] | None = None
    replacement: str | None = None
    alert_kind: str | None = None
    enabled: bool | None = None


class PolicyRuleProposal(_Out):
    rule_id: str
    kind: Literal["deny", "generalise", "age_limit", "recipient_restrict", "alert_preference"]
    target: Literal["category", "merchant", "field", "record_kind", "all"]
    value: str = ""
    params: PolicyParams = Field(default_factory=PolicyParams)
    source_text: str = ""
    confidence: float = Field(ge=0, le=1)


class Clarification(_Out):
    source_text: str = ""
    question: str = ""


class PolicyParseOutput(_Out):
    rules: list[PolicyRuleProposal] = Field(default_factory=list)
    clarifications: list[Clarification] = Field(default_factory=list)
    plain_summary: str = ""


# --------------------------------------------------------------------------- one-pass baseline


class OnePassClaim(_Out):
    question_id: str
    statement: str = ""
    status: Status
    refs: list[ClaimRef] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)


class OnePassOutput(_Out):
    claims: list[OnePassClaim] = Field(default_factory=list)
    disclosures: list[Disclosure] = Field(default_factory=list)
    rationale: str = ""


ROLE_MODELS: dict[str, type[BaseModel]] = {
    "matcher": MatcherOutput,
    "detective": DetectiveOutput,
    "challenger": ChallengerOutput,
    "curator": CuratorOutput,
    "policy_parse": PolicyParseOutput,
    "one_pass": OnePassOutput,
}


# --------------------------------------------------------------------------- reference validation


@dataclass(frozen=True)
class ValidationFailure:
    """One reference an agent returned that the retrieved material does not contain."""

    role: str
    path: str
    record_id: str
    revision: int | None
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {"role": self.role, "path": self.path, "record_id": self.record_id,
                "revision": self.revision, "reason": self.reason}


def _filter_refs(role: str, path: str, refs: list[Any], material: Material,
                 failures: list[ValidationFailure]) -> list[Any]:
    kept = []
    for index, ref in enumerate(refs):
        if material.has(ref.record_id, ref.revision):
            kept.append(ref)
        else:
            failures.append(ValidationFailure(role, f"{path}[{index}]", ref.record_id, ref.revision,
                                              "not in the retrieved material"))
    return kept


def _date_ok(date: str) -> bool:
    return (len(date) == 10 and date[4] == "-" and date[7] == "-"
            and date[:4].isdigit() and date[5:7].isdigit() and date[8:].isdigit())


def _check_action(role: str, path: str, action: Any, material: Material,
                  failures: list[ValidationFailure]) -> Any:
    """An action whose arguments name something outside the material becomes ``none``."""
    args = action.args
    problems: list[tuple[str, str]] = []
    if action.type == "lookup_settlement":
        ref = args.payout_reference or ""
        if not ref or not material.knows_reference(ref):
            problems.append((ref or "<empty>",
                             "payout_reference is not carried by any retrieved record"))
        if args.record_id and not material.has(args.record_id, None):
            problems.append((args.record_id, "record_id is not in the retrieved material"))
    elif action.type == "lookup_payouts_covering":
        if not args.record_id or not material.has(args.record_id, None):
            problems.append((args.record_id or "<empty>",
                             "record_id is not in the retrieved material"))
    elif action.type == "lookup_by_date":
        date = args.payout_date or ""
        if not _date_ok(date):
            problems.append((date or "<empty>", "payout_date is not YYYY-MM-DD"))
    elif action.type == "refresh_txn":
        if not args.txn_id or not material.has(args.txn_id, None):
            problems.append((args.txn_id or "<empty>", "txn_id is not in the retrieved material"))
    elif action.type == "merchant_lookup":
        problems.append((args.merchant_id or "<empty>",
                         "merchant_lookup is not available through the gateway"))
    if problems:
        for ident, reason in problems:
            failures.append(ValidationFailure(role, path, ident, None, reason))
        return action.model_copy(update={"type": "none", "args": ActionArgs(),
                                         "reason": f"dropped: {problems[0][1]}"})
    return action


def _filter_disclosures(role: str, path: str, items: list[Any], material: Material,
                        shareable: set[tuple[str, int]] | None,
                        failures: list[ValidationFailure]) -> list[Any]:
    kept = []
    for index, item in enumerate(items):
        key = (item.record_id, item.revision)
        if not material.has(*key):
            failures.append(ValidationFailure(role, f"{path}[{index}]", *key,
                                              "not in the retrieved material"))
        elif shareable is not None and key not in shareable:
            failures.append(ValidationFailure(role, f"{path}[{index}]", *key,
                                              "not in the shareable evidence"))
        else:
            kept.append(item)
    return kept


def validate_refs(output: BaseModel, material: Material, *, role: str,
                  shareable: set[tuple[str, int]] | None = None
                  ) -> tuple[BaseModel, list[ValidationFailure]]:
    """Drop every reference not in ``material`` and return the cleaned output and failures.

    For the curator and the one-pass baseline, ``shareable`` (``{(record_id, revision)}``)
    narrows what may be disclosed: a cited record that is not shareable is dropped too. The
    matcher's ``linked`` decision is downgraded to ``unmatched`` when its chosen record is
    unknown, and the challenger's ``payout_reference`` is blanked when no retrieved record
    carries it. An action naming an unknown id becomes ``none``.
    """
    failures: list[ValidationFailure] = []
    out = output.model_copy(deep=True)

    if isinstance(out, MatcherOutput):
        if not material.has(out.entry.record_id, out.entry.revision):
            failures.append(ValidationFailure(role, "entry", out.entry.record_id,
                                              out.entry.revision, "entry is not the supplied entry"))
        out.candidates = _filter_refs(role, "candidates", out.candidates, material, failures)
        if out.decision == "linked":
            if not out.chosen_record_id or not material.has(out.chosen_record_id,
                                                            out.chosen_revision or None):
                failures.append(ValidationFailure(role, "chosen_record_id", out.chosen_record_id,
                                                  out.chosen_revision,
                                                  "chosen record is not in the retrieved material"))
                out.decision, out.chosen_record_id, out.chosen_revision = "unmatched", "", 0
        else:
            out.chosen_record_id, out.chosen_revision = "", 0

    elif isinstance(out, DetectiveOutput):
        for index, alert in enumerate(out.alerts):
            alert.refs = _filter_refs(role, f"alerts[{index}].refs", alert.refs, material, failures)
        for index, claim in enumerate(out.claims):
            claim.refs = _filter_refs(role, f"claims[{index}].refs", claim.refs, material, failures)
            claim.next_checks = [_check_action(role, f"claims[{index}].next_checks[{j}]", check,
                                               material, failures)
                                 for j, check in enumerate(claim.next_checks)]

    elif isinstance(out, ChallengerOutput):
        out.confirming_refs = _filter_refs(role, "confirming_refs", out.confirming_refs, material,
                                           failures)
        out.conflicting_refs = _filter_refs(role, "conflicting_refs", out.conflicting_refs, material,
                                            failures)
        if out.payout_reference and not material.knows_reference(out.payout_reference):
            failures.append(ValidationFailure(role, "payout_reference", out.payout_reference, None,
                                              "no retrieved record carries this reference"))
            out.payout_reference = ""
        out.next_action = _check_action(role, "next_action", out.next_action, material, failures)

    elif isinstance(out, CuratorOutput):
        out.disclosures = _filter_disclosures(role, "disclosures", out.disclosures, material,
                                              shareable, failures)
        out.exclusions = _filter_disclosures(role, "exclusions", out.exclusions, material, None,
                                             failures)

    elif isinstance(out, OnePassOutput):
        for index, claim in enumerate(out.claims):
            claim.refs = _filter_refs(role, f"claims[{index}].refs", claim.refs, material, failures)
        out.disclosures = _filter_disclosures(role, "disclosures", out.disclosures, material,
                                              shareable, failures)

    return out, failures


def parse_output(role: str, data: dict[str, Any]) -> BaseModel:
    """Parse a role's JSON answer into its typed model (raises ``pydantic.ValidationError``)."""
    return ROLE_MODELS[role].model_validate(data)


def undeclared_fields(role: str, data: dict[str, Any]) -> list[str]:
    """Top-level keys the model returned that the role's schema does not declare."""
    declared = set(ROLE_MODELS[role].model_fields)
    return sorted(key for key in data if key not in declared)
