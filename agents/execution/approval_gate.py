"""Reference approval store and dispatch gate, implementing agents/execution/out/consent_matrix.md.

    from agents.execution.approval_gate import ApprovalGate, CONSENT_MATRIX, body_digest

    gate = ApprovalGate()
    gate.register_request("rfc-0001-01", audience="bank_a", expires_at="2026-11-15T00:00:00Z")
    d1 = gate.set_draft("rfc-0001-01", body, policy_version="v3", source_revisions={"rec-0001-021": 1})
    gate.approve("rfc-0001-01", digest=d1, audience="bank_a", policy_version="v3", holder="maya", now=t)
    result = gate.dispatch("rfc-0001-01", body, package_id="pkg-1", audience="bank_a", now=t)
    result["ok"], result["status"], result.get("code"), result.get("reason")

Every refusal is {"ok": False, "status": 409, "code", "reason", ...} with `reason` exactly the
sentence in consent_matrix.md, and leaves an audit entry. The gate holds the trusted current
state (draft digest, policy version, cited source revisions); a dispatch is checked against that
state, not against what the caller claims. It signs and sends nothing.

Check order at dispatch (first failure wins, so the reason is deterministic):
    0 package_id already sent: same digest -> deduplicated (C14), different -> package_id_reused (C15)
    1 approval exists for this request            approval_missing            C1, C12, C13
    2 request not expired                          request_expired             C11
    3 approval inside its window                   approval_expired            C10
    4 approval's request_id matches                approval_request_mismatch
    5 audience matches the request's audience      approval_audience_mismatch  C4
    6 policy version is current                    policy_version_changed      C7
    7 cited source revisions are current           source_revision_changed     C8 (uncited: C9 passes)
    8 body digest matches the approval             approval_digest_mismatch    C2, C3
An approval is single use: after a successful dispatch the next package needs a new approval.
Every dispatch refusal made with an approval in hand invalidates that approval.
"""
from __future__ import annotations

import hashlib
import itertools
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import rfc8785

APPROVAL_TTL = timedelta(minutes=15)
REFUSAL_STATUS = 409

# code -> (consent_matrix rows, reason template). Templates use str.format fields.
CONSENT_MATRIX = {
    "approval_missing": (("C1", "C12", "C13"),
                         "dispatch refused: no holder approval recorded for request {request_id}."),
    "approval_digest_mismatch": (("C2", "C3"),
                                 "dispatch refused: approved digest sha256:{approved12} does not match body digest "
                                 "sha256:{actual12}; the body changed after approval{change}. Review and approve the "
                                 "revised body."),
    "approval_audience_mismatch": (("C4",),
                                   "dispatch refused: approval is for {approved_audience}; this dispatch is addressed "
                                   "to {audience}. Each recipient needs its own approval."),
    "approval_audience_refused": (("C5",),
                                  "approval refused: request {request_id} is addressed to {request_audience}, "
                                  "not {audience}."),
    "approval_digest_not_current": (("C6",),
                                    "approval refused: the preview you approved (sha256:{approved12}) is no longer the "
                                    "current draft (sha256:{current12}). Review the current draft."),
    "policy_version_changed": (("C7",),
                               "dispatch refused: privacy rules changed after approval ({old} to {new}). The draft was "
                               "re-filtered; review and approve it again."),
    "source_revision_changed": (("C8",),
                                "dispatch refused: cited record {record_id} changed from r{old} to r{new} after "
                                "approval. The draft is stale; review the updated draft."),
    "approval_expired": (("C10",), "dispatch refused: approval expired at {valid_until}. Review and approve again."),
    "request_expired": (("C11",),
                        "dispatch refused: request {request_id} expired at {expires_at}. The bank must send a new "
                        "request."),
    "approval_request_mismatch": ((),
                                  "dispatch refused: approval {approval_id} is for request {approved_request}, not "
                                  "{request_id}. Each request needs its own approval."),
    "package_id_reused": (("C15",),
                          "dispatch refused: package {package_id} was already sent with a different digest."),
}
# C5 is listed in consent_matrix.md under code approval_audience_mismatch on the approve route;
# the gate reports it with that wire code so HTTP assertions match the matrix.
WIRE_CODE = {"approval_audience_refused": "approval_audience_mismatch"}
ACCEPTED_ROWS = {"C9": "dispatch.sent", "C14": "dispatch.deduplicated", "C16": "dispatch.sent"}


def body_digest(body: dict) -> str:
    """'sha256:' + hex SHA-256 of the RFC 8785 (JCS) canonical body."""
    return "sha256:" + hashlib.sha256(rfc8785.dumps(body)).hexdigest()


def _t(value) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _p12(digest: str) -> str:
    return digest.removeprefix("sha256:")[:12]


@dataclass
class _Request:
    request_id: str
    audience: str
    expires_at: datetime
    parent_request_id: str | None = None
    draft_digest: str | None = None
    policy_version: str | None = None
    source_revisions: dict = field(default_factory=dict)
    change_note: str = ""


@dataclass
class Approval:
    approval_id: str
    request_id: str
    audience: str
    digest: str
    policy_version: str
    source_revisions: dict
    holder: str
    valid_from: datetime
    valid_until: datetime
    status: str = "approved"  # approved | invalidated | used
    invalidated_reason: str | None = None


class ApprovalGate:
    def __init__(self, ttl: timedelta = APPROVAL_TTL):
        self.ttl = ttl
        self.requests: dict[str, _Request] = {}
        self.approvals: dict[str, Approval] = {}
        self.by_request: dict[str, str] = {}
        self.sent: dict[str, dict] = {}  # package_id -> receipt
        self.audit: list[dict] = []
        self.policy_version: str | None = None
        self.current_revisions: dict[str, int] = {}
        self._ids = itertools.count(1)

    # --- audit and refusals ---
    def _log(self, actor, role, now, request_id, event_id, action, outcome, detail):
        self.audit.append({"actor": actor, "role": role, "at": _iso(_t(now)), "request_id": request_id,
                           "event_id": event_id, "action": action, "outcome": outcome, "detail": detail})

    def _refuse(self, code, *, now, request_id, event_id, action, actor="system", role="backend", detail=None, **fmt):
        reason = CONSENT_MATRIX[code][1].format(request_id=request_id, **fmt)
        wire = WIRE_CODE.get(code, code)
        self._log(actor, role, now, request_id, event_id, action, "refused", detail or wire)
        out = {"ok": False, "status": REFUSAL_STATUS, "code": wire, "reason": reason}
        for k in ("approved", "actual"):
            if f"{k}_digest" in fmt:
                out[f"{'expected' if k == 'approved' else 'actual'}_digest"] = fmt[f"{k}_digest"]
        return out

    def _invalidate(self, approval: Approval, reason: str, now, detail: str):
        if approval.status == "approved":
            approval.status, approval.invalidated_reason = "invalidated", reason
            self._log("system", "backend", now, approval.request_id, approval.approval_id, "approval.invalidated", "ok", detail)

    # --- trusted state ---
    def register_request(self, request_id: str, *, audience: str, expires_at, parent_request_id: str | None = None):
        self.requests[request_id] = _Request(request_id, audience, _t(expires_at), parent_request_id)

    def set_draft(self, request_id: str, body: dict, *, policy_version: str, source_revisions: dict,
                  change_note: str = "") -> str:
        """Record the holder's current draft; returns its digest. `change_note` explains a later edit."""
        req = self.requests[request_id]
        req.draft_digest, req.policy_version = body_digest(body), policy_version
        req.source_revisions, req.change_note = dict(source_revisions), change_note
        self.policy_version = policy_version
        for rid, rev in source_revisions.items():
            self.current_revisions.setdefault(rid, rev)
        return req.draft_digest

    def policy_changed(self, new_version: str, *, now):
        old = self.policy_version
        self.policy_version = new_version
        for a in self.approvals.values():
            if a.status == "approved" and a.policy_version != new_version:
                self._invalidate(a, "policy_version_changed", now, f"policy {a.policy_version} -> {new_version}")
        return old

    def source_corrected(self, record_id: str, new_revision: int, *, now, event_id: str = ""):
        old = self.current_revisions.get(record_id)
        self.current_revisions[record_id] = new_revision
        drafts = sum(1 for r in self.requests.values() if record_id in r.source_revisions)
        self._log("system", "connector", now, None, event_id or record_id, "record.corrected", "ok",
                  f"{record_id} r{new_revision} appended; r{old} preserved; {drafts} draft{'' if drafts == 1 else 's'} invalidated")
        for a in self.approvals.values():
            if a.status == "approved" and record_id in a.source_revisions and a.source_revisions[record_id] != new_revision:
                self._invalidate(a, "source_revision_changed", now, "source_revision_changed")

    # --- holder approval ---
    def approve(self, request_id: str, *, digest: str, audience: str, policy_version: str, holder: str, now) -> dict:
        req = self.requests.get(request_id)
        if req is None:
            return self._refuse("approval_missing", now=now, request_id=request_id, event_id=request_id,
                                action="approval.refused", actor=holder, role="holder")
        if audience != req.audience:
            return self._refuse("approval_audience_refused", now=now, request_id=request_id, event_id=request_id,
                                action="approval.refused", actor=holder, role="holder", detail="audience_mismatch",
                                request_audience=req.audience, audience=audience)
        if digest != req.draft_digest:
            return self._refuse("approval_digest_not_current", now=now, request_id=request_id, event_id=request_id,
                                action="approval.refused", actor=holder, role="holder", detail="digest_not_current",
                                approved12=_p12(digest), current12=_p12(req.draft_digest or ""))
        old = self.by_request.get(request_id)
        if old:
            self._invalidate(self.approvals[old], "superseded", now, "superseded by a new approval")
        now_t = _t(now)
        approval = Approval(f"appr-{next(self._ids):04d}", request_id, audience, digest, policy_version,
                            dict(req.source_revisions), holder, now_t, now_t + self.ttl)
        self.approvals[approval.approval_id] = approval
        self.by_request[request_id] = approval.approval_id
        self._log(holder, "holder", now, request_id, approval.approval_id, "approval.recorded", "ok",
                  f"Approved digest sha256:{_p12(digest)}... for {audience}, valid {int(self.ttl.total_seconds() // 60)} min.")
        return {"ok": True, "status": 200, "approval_id": approval.approval_id, "valid_until": _iso(approval.valid_until)}

    # --- dispatch ---
    def dispatch(self, request_id: str, body: dict, *, package_id: str, audience: str, now,
                 approval_id: str | None = None, signer_key_id: str | None = None) -> dict:
        actual = body_digest(body)
        prior = self.sent.get(package_id)
        if prior is not None:
            if prior["digest"] == actual:
                self._log("system", "backend", now, request_id, package_id, "dispatch.deduplicated", "ok",
                          "same package_id and digest")
                return {"ok": True, "status": 200, "deduplicated": True, "receipt": prior}
            return self._refuse("package_id_reused", now=now, request_id=request_id, event_id=package_id,
                                action="dispatch.refused", package_id=package_id)

        aid = approval_id or self.by_request.get(request_id)
        approval = self.approvals.get(aid) if aid else None
        if approval is None or approval.status == "used":
            return self._refuse("approval_missing", now=now, request_id=request_id, event_id=package_id,
                                action="dispatch.refused")
        req = self.requests.get(request_id)
        now_t = _t(now)
        if req is not None and now_t >= req.expires_at:
            self._invalidate(approval, "request_expired", now, "request_expired")
            return self._refuse("request_expired", now=now, request_id=request_id, event_id=package_id,
                                action="dispatch.refused", expires_at=_iso(req.expires_at))
        if now_t >= approval.valid_until:
            self._invalidate(approval, "approval_expired", now, "approval_expired")
            return self._refuse("approval_expired", now=now, request_id=request_id, event_id=package_id,
                                action="dispatch.refused", valid_until=_iso(approval.valid_until))
        if approval.request_id != request_id:
            self._invalidate(approval, "approval_request_mismatch", now, "approval_request_mismatch")
            return self._refuse("approval_request_mismatch", now=now, request_id=request_id, event_id=package_id,
                                action="dispatch.refused", approval_id=approval.approval_id,
                                approved_request=approval.request_id)
        if audience != approval.audience or (req is not None and audience != req.audience):
            self._invalidate(approval, "approval_audience_mismatch", now, "approval_audience_mismatch")
            return self._refuse("approval_audience_mismatch", now=now, request_id=request_id, event_id=package_id,
                                action="dispatch.refused", approved_audience=approval.audience, audience=audience)
        if self.policy_version is not None and approval.policy_version != self.policy_version:
            self._invalidate(approval, "policy_version_changed", now, f"policy {approval.policy_version} -> {self.policy_version}")
            return self._refuse("policy_version_changed", now=now, request_id=request_id, event_id=package_id,
                                action="dispatch.refused", old=approval.policy_version, new=self.policy_version)
        for rid, rev in sorted(approval.source_revisions.items()):
            cur = self.current_revisions.get(rid, rev)
            if cur != rev:
                self._invalidate(approval, "source_revision_changed", now, "source_revision_changed")
                return self._refuse("source_revision_changed", now=now, request_id=request_id, event_id=package_id,
                                    action="dispatch.refused", record_id=rid, old=rev, new=cur)
        if actual != approval.digest:
            self._invalidate(approval, "approval_digest_mismatch", now, "approval_digest_mismatch")
            note = f" ({req.change_note})" if req is not None and req.change_note else ""
            return self._refuse("approval_digest_mismatch", now=now, request_id=request_id, event_id=package_id,
                                action="dispatch.refused", detail="approval_digest_mismatch: body changed after approval.",
                                approved12=_p12(approval.digest), actual12=_p12(actual), change=note,
                                approved_digest=approval.digest, actual_digest=actual)

        if approval.status != "approved":  # invalidated or superseded earlier: fail closed
            return self._refuse("approval_missing", now=now, request_id=request_id, event_id=package_id,
                                action="dispatch.refused", detail=f"approval_missing ({approval.invalidated_reason})")
        approval.status = "used"
        receipt = {"package_id": package_id, "request_id": request_id, "recipient": audience, "digest": actual,
                   "sent_at": _iso(now_t), "approval_id": approval.approval_id}
        self.sent[package_id] = receipt
        self._log("system", "backend", now, request_id, package_id, "dispatch.sent", "ok",
                  f"Approval revalidated; body signed with {signer_key_id}." if signer_key_id
                  else "Approval revalidated; body cleared for signing.")
        return {"ok": True, "status": 200, "deduplicated": False, "receipt": receipt}
