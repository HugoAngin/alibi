"""Holder-side actions: run the investigation, edit the review, approve, dispatch, manage rules.

Every action calls ``alibi.api``; this module adds only routing of results and the refusal
vocabulary (``agents/execution/out/consent_matrix.md``). A ``Refusal`` carries the backend's own
sentence verbatim and a stable code; routes turn it into a 4xx JSON body or an HTML page.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx

from alibi import api
from alibi.app import common as c
from alibi.app.runtime import Runtime
from alibi.core import approval as core_approval
from alibi.core import canon
from alibi.core.approval import ApprovalError
from alibi.core.package import PackageError
from alibi.core.policy import PolicyError, compile_policy, parse_rule_text
from alibi.core.schemas import ApprovalStatus, DraftStatus, RequestForContext, format_utc
from alibi.core.store import NotFound, StoreError


@dataclass
class Refusal(Exception):
    code: str
    reason: str
    status: int = 409
    extra: dict[str, Any] = field(default_factory=dict)

    def body(self) -> dict[str, Any]:
        return {"code": self.code, "reason": self.reason, **self.extra}


def _code_for(problems: list[str]) -> str:
    text = " ".join(problems).lower()
    if "no holder approval" in text:
        return "approval_missing"
    if "changed after it was approved" in text:
        return "approval_digest_mismatch"
    if "the approval expired" in text:
        return "approval_expired"
    if "the request expired" in text or "body has already expired" in text:
        return "request_expired"
    if "for recipient" in text or "addressed to" in text:
        return "approval_audience_mismatch"
    if "disclosure policy changed" in text:
        return "policy_version_changed"
    if "changed after approval" in text or "superseded by revision" in text or "is missing (approved" in text:
        return "source_revision_changed"
    if "is for request" in text or "answers request" in text:
        return "approval_request_mismatch"
    if "already used" in text:
        return "approval_consumed"
    return "approval_invalid"


# --------------------------------------------------------------------------- investigation


def run_investigation(rt: Runtime, request_id: str) -> dict[str, Any]:
    """Run the rules investigation and build the private review draft."""
    store = rt.store
    with rt.lock:
        rfc = store.get_request(request_id)
        now = rt.clock()
        try:
            result = api.run_investigation(store, rfc.request_id, mode="rules",
                                           settlement_lookup=rt.settlement_lookup,
                                           registry=rt.registry, now=now)
            draft = api.build_review(store, rfc.request_id, now=now)
        except (PackageError, StoreError, ValueError) as exc:
            rt.sidecar.set(f"run:{request_id}", {"failed": True, "failure_reason": str(exc)[:300],
                                                 "at": rt.now_text()})
            raise Refusal("investigation_failed", str(exc), status=409) from exc
        rt.sidecar.set(f"run:{request_id}", {"failed": False, "run_id": result.run_id, "at": rt.now_text(),
                                             "mode": "rules", "calls_used": result.calls_used,
                                             "draft_id": draft.draft_id})
        return {"run_id": result.run_id, "mode": "rules", "calls_used": result.calls_used,
                "claims": {cl.question_id: cl.status.value for cl in result.claims},
                "draft_id": draft.draft_id, "unavailable": list(result.unavailable)}


def remove_item(rt: Runtime, request_id: str, ref_type: str, ref_id: str) -> dict[str, Any]:
    """Rebuild the review with one more source removed by the holder. The current approval, if
    any, no longer matches the new body, so dispatch will refuse until the holder approves again."""
    store = rt.store
    with rt.lock:
        current = c.latest_draft(store, request_id)
        removed: set[str] = set(rt.sidecar.get(f"removed:{request_id}") or [])
        token = f"{ref_type}:{ref_id}"
        removed.add(token)
        rt.sidecar.set(f"removed:{request_id}", sorted(removed))
        try:
            draft = api.build_review(store, request_id, now=rt.clock(), removed=sorted(removed))
        except PackageError as exc:
            raise Refusal("review_failed", str(exc)) from exc
        if current is not None and current.draft_id == draft.draft_id:
            # Same body (the item was not in it); nothing changed.
            pass
        rt.audit(action="remove_item", outcome="ok", target_id=request_id,
                 detail=f"holder removed {token}; draft {draft.draft_id} (digest {draft.body_sha256[:12]})")
        return {"draft_id": draft.draft_id, "digest": f"sha256:{draft.body_sha256}", "removed": sorted(removed)}


def restore_items(rt: Runtime, request_id: str) -> dict[str, Any]:
    rt.sidecar.set(f"removed:{request_id}", [])
    draft = api.build_review(rt.store, request_id, now=rt.clock())
    return {"draft_id": draft.draft_id, "digest": f"sha256:{draft.body_sha256}"}


def approve(rt: Runtime, request_id: str, *, digest: str, audience: str, policy_version: str,
            holder_id: str | None = None) -> dict[str, Any]:
    """Approve exactly the current draft. The posted digest must be the current body's digest and
    the audience the request's; otherwise the approval is refused before the backend is called."""
    store = rt.store
    with rt.lock:
        rfc = store.get_request(request_id)
        draft = c.latest_draft(store, request_id)
        if draft is None:
            raise Refusal("draft_missing", f"approval refused: request {request_id} has no draft to approve; "
                                           "run the investigation first.")
        posted = (digest or "").removeprefix("sha256:").strip()
        if posted != draft.body_sha256:
            raise Refusal("approval_digest_not_current",
                          f"approval refused: the preview you approved (sha256:{posted[:12]}) is no longer "
                          f"the current draft (sha256:{draft.body_sha256[:12]}). Review the current draft.",
                          extra={"expected_digest": f"sha256:{draft.body_sha256}", "actual_digest": f"sha256:{posted}"})
        if audience != rfc.audience:
            raise Refusal("approval_audience_mismatch",
                          f"approval refused: request {request_id} is addressed to {rfc.audience}, not {audience}.")
        _, compiled = store.latest_policy(rfc.subject_ref), None
        compiled = compile_policy(store.latest_policy(rfc.subject_ref))
        if policy_version and policy_version != compiled.policy_version:
            raise Refusal("policy_version_changed",
                          f"approval refused: privacy rules changed after this preview was built "
                          f"({policy_version} to {compiled.policy_version}). Review the re-filtered draft.")
        holder = holder_id or f"holder_{rt.subject_ref}".replace("-", "_")
        try:
            approval = api.approve(store, draft.draft_id, holder_id=holder, now=rt.clock())
        except ApprovalError as exc:
            raise Refusal(_code_for(exc.problems), "approval refused: " + "; ".join(exc.problems)) from exc
        return {"approval_id": approval.approval_id, "status": ApprovalStatus(approval.status).value,
                "digest": f"sha256:{approval.body_sha256}", "audience": approval.audience,
                "expires_at": format_utc(approval.expires_at)}


# --------------------------------------------------------------------------- dispatch


def _latest_approval(rt: Runtime, request_id: str, current_digest: str | None = None):
    """The approval dispatch should check: one that binds the current body if the holder gave
    one, else the newest usable approval (insertion order breaks ties on a fixed clock)."""
    approvals = rt.store.list_approvals(request_id=request_id)
    usable = [a for a in approvals if ApprovalStatus(a.status) in (ApprovalStatus.PENDING, ApprovalStatus.CONSUMED)]
    if current_digest is not None:
        matching = [a for a in usable if a.body_sha256 == current_digest]
        if matching:
            return sorted(matching, key=lambda a: a.approved_at)[-1]
    pool = usable or approvals
    return sorted(pool, key=lambda a: a.approved_at)[-1] if pool else None


def dispatch(rt: Runtime, request_id: str, deliver) -> dict[str, Any]:
    """Revalidate the approval against the current draft, sign, and deliver to the bank.

    ``deliver(audience, package_json) -> (status_code, body_dict)`` posts to the bank service.
    The result (success with receipt, or a refusal with the backend's sentence) is stored for the
    dispatch page and returned. A refusal is raised as ``Refusal`` after being recorded.
    """
    store = rt.store
    with rt.lock:
        rfc = store.get_request(request_id)
        now = rt.clock()
        attempted = rt.now_text()
        current = c.latest_draft(store, request_id)
        approval = _latest_approval(rt, request_id, current.body_sha256 if current else None)
        recipient = rt.recipient_name(rfc.audience)
        result: dict[str, Any] = {"outcome": "refused", "package_id": "none", "request_id": request_id,
                                  "recipient": recipient, "attempted_at": attempted, "receipt": None,
                                  "refusal": None}

        def refuse(code: str, reason: str, expected: str = "—", actual: str = "—") -> None:
            result["refusal"] = {"code": code, "reason": reason, "checked_at": attempted,
                                 "expected_digest": expected, "actual_digest": actual}
            rt.sidecar.set(f"dispatch:{request_id}", result)
            rt.audit(action="dispatch", outcome="refused", target_id=request_id, detail=f"{code}: {reason}")
            raise Refusal(code, reason, extra={"expected_digest": expected, "actual_digest": actual})

        if approval is None:
            refuse("approval_missing", f"dispatch refused: no holder approval recorded for request {request_id}.")
        if current is None:
            refuse("draft_missing", f"dispatch refused: request {request_id} has no draft; run the investigation first.")
        result["package_id"] = approval.package_id or "pending"
        if approval.body_sha256 != current.body_sha256:
            compiled = compile_policy(store.latest_policy(rfc.subject_ref))
            refs = core_approval.merge_refs(approval.source_revisions, current.source_revisions,
                                            core_approval.body_refs(current.body))
            revisions = core_approval.current_revisions(store, refs)
            problems = core_approval.check_approval(approval, body=current.body, audience=rfc.audience,
                                                    request_id=request_id,
                                                    policy_version=compiled.policy_version,
                                                    revisions=revisions, now=now)
            removed = rt.sidecar.get(f"removed:{request_id}") or []
            note = f" ({', '.join(removed)} removed)" if removed else " (draft regenerated)"
            reason = ("dispatch refused: " + "; ".join(problems or ["the package body changed after it was approved"])
                      + note + ". Review and approve the revised body.")
            refuse(_code_for(problems or ["changed after it was approved"]), reason,
                   f"sha256:{approval.body_sha256}", f"sha256:{current.body_sha256}")
        try:
            package = api.sign_and_package(store, approval.approval_id, keystore=rt.keystore, now=now)
        except ApprovalError as exc:
            refuse(_code_for(exc.problems), "dispatch refused: " + "; ".join(exc.problems),
                   f"sha256:{approval.body_sha256}", f"sha256:{current.body_sha256}")
        result["package_id"] = package.package_id
        payload = package.to_json_dict()
        status_code, body = deliver(rfc.audience, payload)
        deliveries = store.list_deliveries(package_id=package.package_id)
        delivery = deliveries[-1] if deliveries else None
        if status_code >= 400 or not body.get("accepted", False):
            reasons = body.get("reasons") or [body.get("reason") or f"bank service answered {status_code}"]
            if delivery is not None:
                store.save_delivery(delivery.model_copy(update={"status": "failed"}))
            refuse(body.get("code", "delivery_rejected"),
                   "dispatch refused by the recipient: " + "; ".join(str(r) for r in reasons),
                   f"sha256:{package.body_sha256}", f"sha256:{package.body_sha256}")
        receipt = {"receipt_id": body.get("receipt_id", "unknown"),
                   "package_digest": body.get("package_sha256", canon.digest(payload)),
                   "received_at": body.get("received_at", attempted), "status": "accepted",
                   "signer_key_id": package.signature.kid, "expires_at": format_utc(package.body.expires_at)}
        if delivery is not None:
            store.save_delivery(delivery.model_copy(update={"status": "acknowledged",
                                                           "receipt_id": receipt["receipt_id"]
                                                           if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}", receipt["receipt_id"]) else None}))
        result.update({"outcome": "success", "receipt": receipt, "refusal": None})
        rt.sidecar.set(f"dispatch:{request_id}", result)
        rt.audit(action="dispatch", outcome="ok", target_id=package.package_id,
                 detail=f"delivered to {rfc.audience} for {request_id}; receipt {receipt['receipt_id']}")
        return result


def deliver_in_process(rt: Runtime):
    """A ``deliver`` callable that hands the package to this process's own bank service."""
    from alibi.app import bank

    def deliver(audience: str, payload: dict) -> tuple[int, dict]:
        return bank.receive_package(rt, audience, canon.canonical_bytes(payload))
    return deliver


def deliver_over_http(base_url: str, timeout: float = 10.0, post=None):
    """A ``deliver`` callable that POSTs the signed package to ``{base_url}/bank/{audience}/receive``.
    ``post`` defaults to ``httpx.post``; tests pass a TestClient's ``post`` to route in-process."""
    send = post or httpx.post

    def deliver(audience: str, payload: dict) -> tuple[int, dict]:
        try:
            response = send(f"{base_url}/bank/{audience}/receive",
                            content=canon.canonical_bytes(payload),
                            headers={"content-type": "application/json"}, timeout=timeout)
        except httpx.HTTPError as exc:
            return 503, {"code": "bank_unreachable", "reason": f"the bank service at {base_url} could not be "
                                                               f"reached ({type(exc).__name__})"}
        try:
            body = response.json()
        except ValueError:
            body = {"code": "bank_bad_response", "reason": "the bank service did not answer with JSON"}
        return response.status_code, body
    return deliver


# --------------------------------------------------------------------------- rules


def active_rules(rt: Runtime) -> list[dict[str, Any]]:
    policy = rt.store.latest_policy(rt.subject_ref)
    if policy is None:
        return []
    out = []
    for rule in policy.rules:
        parsed = parse_rule_text(rule.source_text) if rule.source_text else None
        summary = parsed.interpretation if parsed and parsed.status == "resolved" else _describe(rule)
        out.append({"rule_id": rule.rule_id, "text": rule.source_text or _describe(rule),
                    "kind": rule.type, "summary": summary, "version": policy.version_tag(),
                    "status": "active", "confirmed_at": format_utc(policy.created_at),
                    "confirmed": policy.confirmed, "rule": rule.to_json_dict()})
    return out


def _describe(rule) -> str:
    data = rule.to_json_dict()
    if data["type"] == "deny":
        target = data.get("fields") or [f"{k} records" for k in data.get("kinds", [])]
        return f"Never share {', '.join(target)}."
    if data["type"] == "generalise":
        return f"Share {', '.join(data['fields'])} at {data['level']} level only."
    if data["type"] == "age":
        return f"Share nothing older than {data['max_age_days']} days."
    if data["type"] == "recipient":
        return f"Share only with {', '.join(data['allow'])}."
    return data["type"]


def held_rules(rt: Runtime) -> list[dict[str, Any]]:
    return list(rt.sidecar.get("held_rules") or [])


def interpret_rule(rt: Runtime, text: str) -> dict[str, Any]:
    """Parse one plain-English rule. Resolved: a pending interpretation the holder confirms or
    rejects. Unresolved: held for clarification with the parser's reason."""
    parsed = parse_rule_text(text)
    now = rt.now_text()
    if parsed.status != "resolved" or parsed.rule is None:
        rule_id = canon.make_id("held", text.strip(), now)
        rt.sidecar.append("held_rules", {"rule_id": rule_id, "text": text.strip(),
                                          "reason": parsed.reason or "not understood", "created_at": now})
        rt.audit(action="interpret_rule", outcome="refused", target_id=rule_id,
                 detail="rule text held for clarification (1 of 1 unresolved)")
        return {"status": "held", "rule_id": rule_id, "reason": parsed.reason}
    rule = parsed.rule.to_json_dict()
    fields = [{"name": k, "value": c.compact(v, 80)} for k, v in rule.items()
              if k not in ("rule_id", "source_text", "type") and v not in ([], None, "")]
    interp = {"rule_id": rule["rule_id"], "text": text.strip(), "plain_lines": [parsed.interpretation],
              "policy": {"kind": rule["type"], "fields": fields,
                         "json": canon.canonical_bytes(rule).decode("utf-8")},
              "warnings": ["Rules only restrict sharing. Deny wins over every other rule.",
                           "Confirming re-filters every pending draft; an approved preview must be approved again."],
              "confirm_action": f"/rules/{rule['rule_id']}/confirm",
              "reject_action": f"/rules/{rule['rule_id']}/reject", "rule": rule}
    rt.sidecar.update("pending_rules", lambda cur: {**(cur or {}), rule["rule_id"]: interp})
    rt.audit(action="interpret_rule", outcome="ok", target_id=rule["rule_id"],
             detail=f"resolved as {rule['type']}; awaiting the holder's confirmation")
    return {"status": "resolved", "rule_id": rule["rule_id"], "interpretation": interp}


def pending_interpretation(rt: Runtime, rule_id: str) -> dict[str, Any] | None:
    return (rt.sidecar.get("pending_rules") or {}).get(rule_id)


def confirm_rule(rt: Runtime, rule_id: str) -> dict[str, Any]:
    interp = pending_interpretation(rt, rule_id)
    if interp is None:
        raise Refusal("rule_not_pending", f"no pending interpretation {rule_id}", status=404)
    policy = rt.store.latest_policy(rt.subject_ref)
    rules = [r.to_json_dict() for r in policy.rules] if policy else []
    rules = [r for r in rules if r["rule_id"] != rule_id] + [interp["rule"]]
    try:
        saved = api.record_policy(rt.store, rt.subject_ref, rules, confirmed=True, now=rt.clock())
    except PolicyError as exc:
        raise Refusal("policy_rejected", str(exc)) from exc
    rt.sidecar.update("pending_rules", lambda cur: {k: v for k, v in (cur or {}).items() if k != rule_id})
    return {"policy_version": saved.version_tag(), "rules": len(saved.rules)}


def reject_rule(rt: Runtime, rule_id: str) -> None:
    rt.sidecar.update("pending_rules", lambda cur: {k: v for k, v in (cur or {}).items() if k != rule_id})
    rt.audit(action="reject_rule", outcome="ok", target_id=rule_id, detail="holder rejected the interpretation")


def retire_rule(rt: Runtime, rule_id: str) -> dict[str, Any]:
    policy = rt.store.latest_policy(rt.subject_ref)
    if policy is None or not any(r.rule_id == rule_id for r in policy.rules):
        raise Refusal("rule_not_active", f"no active rule {rule_id}", status=404)
    rules = [r.to_json_dict() for r in policy.rules if r.rule_id != rule_id]
    saved = api.record_policy(rt.store, rt.subject_ref, rules, confirmed=True, now=rt.clock())
    return {"policy_version": saved.version_tag(), "rules": len(saved.rules)}


def discard_held(rt: Runtime, rule_id: str) -> None:
    rt.sidecar.update("held_rules", lambda cur: [h for h in (cur or []) if h["rule_id"] != rule_id])


def held_text(rt: Runtime, rule_id: str) -> str:
    for held in held_rules(rt):
        if held["rule_id"] == rule_id:
            return held["text"]
    return ""
