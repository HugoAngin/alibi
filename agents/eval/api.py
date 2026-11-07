"""Public evaluation interface and local sharing demonstrator."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agents.eval.case_io import request_context
from agents.eval.probe_capture import score_probe_steps
from agents.eval.score_capture import compare_captures, score_capture
from agents.eval.sharing_delivery import BankInbox, Delivery, body_digest, parse_envelope, sign_package
from agents.eval.sharing_privacy import ApprovalError, ApprovalStore, TrustedState

__all__ = ["compare_captures", "dispatch_approved", "request_context", "score_capture", "score_probe_steps"]


def dispatch_approved(
    body: dict,
    approval_id: str | None,
    *,
    approvals: ApprovalStore,
    state: TrustedState,
    recipient: str,
    inbox: BankInbox,
    signing_key: Ed25519PrivateKey,
    key_id: str,
    now: datetime,
    retry_bytes: bytes | None = None,
) -> tuple[bytes, Delivery]:
    """Revalidate consent and deliver the exact approved body to the selected mock bank.

    The caller owns the trusted request and source state. Runtime integration must keep
    that state stable across authorisation and dispatch, and persist the signed bytes
    before an external send so a retry can reuse them.
    """
    if approval_id is None:
        raise ApprovalError("approval_missing")
    body = deepcopy(body)
    approval = approvals.validate(approval_id, body, state, now=now)
    if recipient != approval.recipient:
        raise ApprovalError("recipient_mismatch")

    approved_digest = f"sha256:{approval.body_digest}"
    if retry_bytes is None:
        raw = sign_package(body, signing_key, key_id, approved_body_digest=approved_digest)
    else:
        raw = bytes(retry_bytes)
        if body_digest(parse_envelope(raw)["body"]) != approved_digest:
            raise ApprovalError("body_changed")
    return raw, inbox.deliver(recipient, raw, now=now)
