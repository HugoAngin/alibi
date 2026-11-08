"""Holder approval of one exact package body.

An approval binds ``sha256(JCS(body))``, the recipient (audience), the request id, the
policy version and the revision of every source the draft depends on, for a short validity
window. A change to any of them, expiry, or a second use makes it invalid, so an approval is
never a reusable credential: a second request or recipient needs a newly built body and a
fresh approval.

``check_approval`` lists every problem; ``require_valid`` raises ``ApprovalError`` with that
list. ``current_revisions`` reads the active revisions from the store so the caller can pass
them in. Nothing here signs or writes to the store.
"""

from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from pydantic import ValidationError

from alibi.core.canon import CanonError, digest, make_id
from alibi.core.schemas import (
    Approval,
    ApprovalStatus,
    Calculation,
    Draft,
    DraftStatus,
    EvidenceRef,
    PackageBody,
    format_utc,
    utc_now,
)
from alibi.core.store import NotFound

__all__ = [
    "DEFAULT_APPROVAL_TTL",
    "ApprovalError",
    "approve",
    "body_refs",
    "calculation_refs",
    "check_approval",
    "consume",
    "current_revisions",
    "merge_refs",
    "require_valid",
]

DEFAULT_APPROVAL_TTL = timedelta(minutes=15)


class ApprovalError(Exception):
    """An approval cannot be granted or used. ``problems`` lists every reason (safe to show)."""

    def __init__(self, problems: Iterable[str]):
        self.problems = list(problems) or ["the approval is not valid"]
        super().__init__("; ".join(self.problems))


# --------------------------------------------------------------------------- ref helpers


def merge_refs(*groups: Iterable[EvidenceRef]) -> list[EvidenceRef]:
    """Refs from every group, de-duplicated by revision-pinned key and sorted by key."""
    unique: dict[str, EvidenceRef] = {}
    for group in groups:
        for ref in group:
            unique.setdefault(ref.key(), ref)
    return [unique[key] for key in sorted(unique)]


def calculation_refs(calc: Calculation | None) -> list[EvidenceRef]:
    """Every ref a calculation cites: cited refs, term refs, target and timing refs."""
    if calc is None:
        return []
    refs = [*calc.cited_refs, *(term.ref for term in calc.terms)]
    if calc.target_ref is not None:
        refs.append(calc.target_ref)
    if calc.timing is not None:
        refs += [calc.timing.settlement_ref, calc.timing.credit_ref]
    return merge_refs(refs)


def body_refs(body: PackageBody) -> list[EvidenceRef]:
    """Refs a package body depends on: attachments, claim evidence and calculation refs."""
    refs = [attachment.ref for attachment in body.attachments]
    for claim in body.claims:
        refs += [*claim.evidence_refs, *calculation_refs(claim.calculation)]
    return merge_refs(refs)


def _resolve_now(now: datetime | None) -> datetime:
    if now is None:
        return utc_now()
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise ApprovalError(["now must be a timezone-aware datetime"])
    return now.astimezone(timezone.utc)


def _summary(exc: ValidationError) -> str:
    first = exc.errors()[0]
    where = ".".join(str(part) for part in first.get("loc", ())) or "value"
    return f"{where}: {first.get('msg', 'invalid value')}"


# --------------------------------------------------------------------------- approval


def approve(draft: Draft, *, holder_id: str, now: datetime | None = None,
            ttl: timedelta = DEFAULT_APPROVAL_TTL) -> Approval:
    """Record the holder's decision to send exactly ``draft.body`` to ``draft.audience``.

    Only a ``pending`` draft can be approved. The digest is recomputed from the body, not
    trusted from the draft. ``source_revisions`` covers the draft's revisions and every ref
    the body cites. ``approval_id = make_id("apr", draft_id, holder_id, approved_at)``.
    """
    if not isinstance(draft, Draft):
        raise ApprovalError(["approve expects a Draft"])
    now = _resolve_now(now)
    problems: list[str] = []
    status = DraftStatus(draft.status)
    if status != DraftStatus.PENDING:
        problems.append(f"only a pending draft can be approved; this draft is {status.value}")
    body_sha256 = digest(draft.body)
    if body_sha256 != draft.body_sha256:
        problems.append("the draft body does not match its recorded digest")
    if draft.body.audience != draft.audience:
        problems.append("the draft body is addressed to a different recipient than the draft")
    if draft.body.request_id != draft.request_id:
        problems.append("the draft body answers a different request than the draft")
    if now >= draft.body.expires_at:
        problems.append("the package body has already expired")
    if not isinstance(ttl, timedelta) or ttl <= timedelta(0):
        problems.append("the approval window must be a positive duration")
    if not isinstance(holder_id, str) or not holder_id:
        problems.append("holder_id must name the account holder")
    if problems:
        raise ApprovalError(problems)
    try:
        return Approval(
            approval_id=make_id("apr", draft.draft_id, holder_id, format_utc(now)),
            request_id=draft.request_id,
            audience=draft.audience,
            body_sha256=body_sha256,
            policy_version=draft.policy_version,
            source_revisions=merge_refs(draft.source_revisions, body_refs(draft.body)),
            holder_id=holder_id,
            draft_id=draft.draft_id,
            approved_at=now,
            expires_at=now + ttl,
        )
    except ValidationError as exc:
        raise ApprovalError([f"invalid approval ({_summary(exc)})"]) from exc


def consume(approval: Approval, *, package_id: str) -> Approval:
    """A copy of a pending approval marked ``consumed`` by ``package_id`` (save it yourself)."""
    if not isinstance(approval, Approval):
        raise ApprovalError(["consume expects an Approval"])
    status = ApprovalStatus(approval.status)
    if status != ApprovalStatus.PENDING:
        raise ApprovalError([f"only a pending approval can be used; this one is {status.value}"])
    try:
        return Approval.model_validate({**approval.to_json_dict(),
                                        "status": ApprovalStatus.CONSUMED.value,
                                        "package_id": package_id})
    except ValidationError as exc:
        raise ApprovalError([f"invalid package id ({_summary(exc)})"]) from exc


def current_revisions(store: Any, refs: Iterable[EvidenceRef]) -> dict[str, int | None]:
    """Token (``record:<id>`` / ``txn:<id>``) -> active revision in ``store``, None if missing."""
    out: dict[str, int | None] = {}
    for ref in refs:
        token = ref.token()
        if token in out:
            continue
        try:
            if ref.ref_type == "record":
                out[token] = store.get_source(ref.ref_id).revision
            else:
                out[token] = store.get_bank_txn(ref.ref_id).revision
        except NotFound:
            out[token] = None
    return out


def _as_body(body: Any) -> PackageBody | None:
    if isinstance(body, PackageBody):
        return body
    try:
        return PackageBody.model_validate(body)
    except ValidationError:
        return None


def check_approval(approval: Approval, *, body: PackageBody | dict, audience: str,
                   request_id: str, policy_version: str,
                   revisions: Mapping[str, int | None],
                   now: datetime | None = None) -> list[str]:
    """Every reason ``approval`` does not authorise sending ``body`` now; empty when valid.

    Checks: status is ``pending`` (not consumed or invalidated), the validity window, the
    body digest, the audience and request id (argument and body), the policy version, every
    approved source revision against ``revisions`` (token -> active revision), and that the
    approval covers every source the body cites.
    """
    if not isinstance(approval, Approval):
        return ["no valid approval was given"]
    try:
        now = _resolve_now(now)
    except ApprovalError as exc:
        return list(exc.problems)
    problems: list[str] = []

    status = ApprovalStatus(approval.status)
    if status == ApprovalStatus.CONSUMED:
        used = f" for package {approval.package_id}" if approval.package_id else ""
        problems.append(f"the approval was already used{used}; approve the package again")
    elif status == ApprovalStatus.INVALIDATED:
        why = f": {approval.invalidated_reason}" if approval.invalidated_reason else ""
        problems.append(f"the approval was invalidated{why}")
    if now < approval.approved_at:
        problems.append(f"the approval is not valid before {format_utc(approval.approved_at)}")
    if now >= approval.expires_at:
        problems.append(f"the approval expired at {format_utc(approval.expires_at)}")

    parsed = _as_body(body)
    try:
        body_sha256 = digest(body)
    except CanonError:
        body_sha256 = None
    if parsed is None:
        problems.append("the package body is not a valid package body")
    if body_sha256 != approval.body_sha256:
        problems.append("the package body changed after it was approved")

    if audience != approval.audience:
        problems.append(f"the approval is for recipient {approval.audience}, not {audience}")
    if parsed is not None and parsed.audience != approval.audience:
        problems.append(f"the package body is addressed to {parsed.audience}, not the approved "
                        f"recipient {approval.audience}")
    if request_id != approval.request_id:
        problems.append(f"the approval is for request {approval.request_id}, not {request_id}")
    if parsed is not None and parsed.request_id != approval.request_id:
        problems.append(f"the package body answers request {parsed.request_id}, not the approved "
                        f"request {approval.request_id}")
    if policy_version != approval.policy_version:
        problems.append(f"the disclosure policy changed after approval (approved "
                        f"{approval.policy_version}, now {policy_version})")

    current = revisions if isinstance(revisions, Mapping) else {}
    approved_keys = set()
    for ref in approval.source_revisions:
        approved_keys.add(ref.key())
        active = current.get(ref.token())
        if active is None:
            problems.append(f"source {ref.token()} is missing (approved revision {ref.revision})")
        elif active != ref.revision:
            problems.append(f"source {ref.token()} changed after approval (approved revision "
                            f"{ref.revision}, now revision {active})")
    if parsed is not None:
        for ref in body_refs(parsed):
            if ref.key() not in approved_keys:
                problems.append(f"the approval does not cover source {ref.key()}")
    return problems


def require_valid(approval: Approval, *, body: PackageBody | dict, audience: str,
                  request_id: str, policy_version: str, revisions: Mapping[str, int | None],
                  now: datetime | None = None) -> None:
    """Raise ``ApprovalError`` listing every problem ``check_approval`` finds."""
    problems = check_approval(approval, body=body, audience=audience, request_id=request_id,
                              policy_version=policy_version, revisions=revisions, now=now)
    if problems:
        raise ApprovalError(problems)
