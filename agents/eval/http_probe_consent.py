"""Black-box HTTP probes for Alibi consent, disclosure, policy and recipient spoof.

Assigned set: SP-01..SP-16 (consent C1-C16, delivery idempotency, disclosure,
policy) plus SP-20 (recipient-scope spoof). Every attempt goes over HTTP through a
``ProbeSession`` from ``agents.eval.http_probe_runtime``; this module never opens a
socket itself and keeps no app state. Sources: ``agents/execution/out/PROBES.md`` +
``probes.json`` (expected outcomes), ``consent_matrix.md`` (C1-C16 refusal codes and
the F1/F2/F3 flows), ``shared/CONTRACTS.md`` (Approval, RFC scope, trust/attestation),
``alibi/app/UI.md`` on S4 (holder review, bank package and export JSON shapes).

Outcome contract (one per handler, mapped to the runtime constants):
  * ``BLOCKED``     - the safe behaviour was observed: a refusal probe was refused with
                      its documented code, or a required control step was accepted and
                      its real receipt / package / exclusion evidence checked out.
  * ``NOT_BLOCKED`` - a security regression was observed (a refusal probe succeeded, or a
                      control / isolation guarantee leaked).
  * ``NOT_RUN``     - prerequisites, an unpublished hook, or unambiguous evidence were
                      missing. A raised ``ProbeUnavailable`` (missing fixture/route,
                      transport failure, failed setup) also maps here. Transport errors
                      and generic router 404s are never counted as a refusal.

Config is passed to ``attempt`` as ``base_urls`` (service origins plus ``fixtures`` and
``routes`` sub-dicts) and ``tokens`` (actor -> bearer). Holder traffic uses ``app``.
Use ``fixtures.consent_bank_service`` for the native bank inbox and package routes.
Corrections use ``fixtures.merchant_correction_service`` when set, otherwise ``app``.
Use a fresh synthetic store per attempt so approvals and deliveries never cross probes.

Native bank (``consent_bank_service`` set). Inbox ``/inbox``, package ``/packages/{package_id}``,
raw envelope ``/packages/{package_id}/raw`` and delivery ``/deliver`` (route key
``bank_deliver``) are the published S2 routes. SP-03 uses the C12 path when ``rfc_bank_b`` is
set: dispatch Bank B's own request while only the Bank A approval exists. The native dispatch
route takes no audience, so an ignored client ``audience`` field is never scored as a refusal
or as a leak; only a Bank B inbox row is a leak. SP-09's changed-bytes retry is derived from the
signed package the bank actually stored (same ``package_id``, one changed body field) and is
posted to the bank's delivery route as the recipient, unless ``dispatch_retry_conflict`` is set.

Routes - documented S4 form actions are used as ``route`` defaults so a deployment may
override them by key, but they are already published:
  run="/requests/{request_id}/run"          review="/requests/{request_id}"
  approve="/requests/{request_id}/approve"   dispatch="/requests/{request_id}/dispatch"
  inbox="/bank/{recipient}/inbox"            package="/bank/{recipient}/packages/{package_id}"
Unpublished S2/S4 hooks have NO default (config-only; absence -> NOT_RUN, never guessed):
  merchant_correction   - POST a merchant correction event (SP-06, SP-08)
  policy_bump           - POST a privacy-rule change / retire (SP-07)
  rule_interpret        - POST an ambiguous plain-English rule (SP-16)
  race_audit            - GET ordered audit entries for the correction and dispatch
  dispatch_retry        - POST changed bytes under a previously delivered package_id
  signed_package, print_view, attachment_metadata, bank_trace - GET recipient views

Fixtures (``base_urls['fixtures']``), named explicitly; each probe requires only its own:
  rfc_bank_a            - bank_a transaction-scoped request id (canonical rfc-0001-01)
  rfc_followup          - follow-up request id whose parent is rfc_bank_a (rfc-0001-03)
  recipient_bank_a/_b   - audience ids ("bank_a" / "bank_b")
  policy_version        - current policy version posted on approve (e.g. "v3")
  expired_request       - request whose recorded approval window / RFC expiry is already
                          past while the RFC remains open (test clock) for SP-05
  correction_event      - JSON body of a signed merchant correction to a cited source
  unrelated_correction_request / unrelated_correction_event - SP-06 positive control
  cited_record_id       - record_id cited in the bank_a body and corrected (SP-08)
  cited_approved_revision - the revision the approval was bound to (SP-08)
  race_superseded_marker - holder-visible indication of the superseded delivered revision
  dispatch_retry_conflict - changed dispatch payload for the existing package_id
  policy_bump_payload   - form body that changes/retires a rule (SP-07)
  denied_value          - a value the active policy denies (must not reach the bank)
  denied_item_id        - item_id of that field in the private exclusion list (SP-10)
  denied_metadata_value - value that must be absent from attachment metadata (SP-12)
  withheld_entity       - merchant/category name a gap must not disclose (SP-13)
  partial_record_id     - record only partially approved; must ship as a redacted extract
  private_value         - holder-only value (exclusion reason / raw source) for SP-15
  export_allowed_marker - visible public evidence shared across the inspected export views
  ambiguous_rule        - form body of a conflicting rule for SP-16
  bank_b_package_id     - a package belonging to bank_b that must not leak to bank_a
  rfc_bank_b            - bank_b's own request id (canonical rfc-0001-02) for SP-03's C12 path
"""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from html import unescape
from threading import Barrier
from urllib.parse import quote, urlsplit

from agents.eval.http_probe_runtime import (
    BLOCKED,
    NOT_BLOCKED,
    NOT_RUN,
    ProbeSession,
    ProbeUnavailable,
)
from alibi.core import canon

APP = "app"
HOLDER, BANK_A, BANK_B, MERCHANT = "holder", "bank_a", "bank_b", "merchant"
JSON_HEADERS = {"Accept": "application/json"}

# Documented S4 route templates (overridable by config key; see module docstring).
RUN = "/requests/{request_id}/run"
REVIEW = "/requests/{request_id}"
APPROVE = "/requests/{request_id}/approve"
DISPATCH = "/requests/{request_id}/dispatch"
INBOX = "/bank/{recipient}/inbox"
PACKAGE = "/bank/{recipient}/packages/{package_id}"


# --- transport helpers ------------------------------------------------------

def _json(response) -> dict:
    return ProbeSession.payload(response)


def _run_investigation(session: ProbeSession, request_id: str) -> None:
    session.require_success(session.http(
        "POST", APP, session.route("run", RUN, request_id=request_id), HOLDER,
        headers=JSON_HEADERS,
    ))


def _review(session: ProbeSession, request_id: str):
    resp = session.http("GET", APP, session.route("review", REVIEW, request_id=request_id), HOLDER,
                        headers=JSON_HEADERS)
    session.require_success(resp)
    return resp


def _review_section(payload: dict) -> dict:
    """The private review card, whether the endpoint wraps it under ``review`` or not."""
    section = payload.get("review")
    return section if isinstance(section, dict) else payload


def _request_meta(payload: dict) -> dict:
    """The request summary on the holder review: S4 publishes ``request``, older samples ``rfc``."""
    for key in ("request", "rfc"):
        if isinstance(payload.get(key), dict):
            return payload[key]
    return {}


def _current_digest(session: ProbeSession, request_id: str) -> tuple[dict, str]:
    payload = _json(_review(session, request_id))
    digest = _review_section(payload).get("approval", {}).get("digest")
    if not _digest_hex(digest):
        raise ProbeUnavailable("holder review did not expose a current approval digest")
    return payload, digest


def _approve(session: ProbeSession, request_id: str, digest: str, audience: str, policy_version: str):
    return session.http(
        "POST", APP, session.route("approve", APPROVE, request_id=request_id), HOLDER,
        data={"digest": digest, "audience": audience, "policy_version": policy_version},
        headers=JSON_HEADERS,
    )


def _dispatch(session: ProbeSession, request_id: str, **fields):
    return session.http(
        "POST", APP, session.route("dispatch", DISPATCH, request_id=request_id), HOLDER,
        data=fields or None, headers=JSON_HEADERS,
    )


def _correct_source(session: ProbeSession, path: str, event: dict):
    service = session.fixtures.get("merchant_correction_service", APP)
    return session.http("POST", service, path, MERCHANT, json=event, headers=JSON_HEADERS)


def _bank_actor(session: ProbeSession, recipient: str) -> str:
    return BANK_B if recipient == session.fixtures.get("recipient_bank_b", BANK_B) else BANK_A


def _inbox_target(session: ProbeSession, recipient: str) -> tuple[str, str]:
    service = session.fixtures.get("consent_bank_service")
    default = "/inbox" if service else INBOX
    return service or APP, session.route("inbox", default, recipient=recipient)


def _bank_inbox(session: ProbeSession, recipient: str):
    service, path = _inbox_target(session, recipient)
    return session.http("GET", service, path, _bank_actor(session, recipient), headers=JSON_HEADERS)


def _bank_package(session: ProbeSession, recipient: str, package_id: str, actor: str = BANK_A):
    resp = session.http(
        "GET", APP, session.route("package", PACKAGE, recipient=recipient, package_id=package_id), actor,
        headers=JSON_HEADERS,
    )
    session.require_success(resp)
    payload = _json(resp)
    authentication = payload.get("authentication")
    if (not isinstance(authentication, dict) or authentication.get("status") != "valid"
            or package_id not in resp.text):
        raise ProbeUnavailable("bank package did not show the accepted package and its valid authentication")
    return payload


def _native_package_path(session: ProbeSession, recipient: str, package_id: str) -> str:
    return session.route("package", "/packages/{package_id}", recipient=recipient, package_id=package_id)


def _native_raw(session: ProbeSession, service: str, recipient: str, package_id: str) -> dict:
    """The signed envelope the native bank stored for ``package_id``, read as the recipient."""
    path = _native_package_path(session, recipient, package_id)
    raw_path = session.route("package_raw", path + "/raw", recipient=recipient, package_id=package_id)
    response = session.http("GET", service, raw_path, _bank_actor(session, recipient), headers=JSON_HEADERS)
    session.require_success(response)
    return _json(response)


def _native_delivery(session: ProbeSession, service: str, recipient: str, receipt: dict,
                     approved_digest: str) -> dict:
    package_id = receipt["package_id"]
    actor = _bank_actor(session, recipient)
    path = _native_package_path(session, recipient, package_id)
    response = session.http("GET", service, path, actor, headers=JSON_HEADERS)
    session.require_success(response)
    view = _json(response)
    bank_receipt = view.get("receipt") or {}
    if (view.get("status") != "verified" or view.get("authentication", {}).get("valid") is not True
            or bank_receipt.get("accepted") is not True
            or bank_receipt.get("package_id") != package_id
            or bank_receipt.get("recipient") != recipient
            or not receipt.get("receipt_id")
            or bank_receipt.get("receipt_id") != receipt.get("receipt_id")
            or receipt.get("status") != "accepted"):
        raise ProbeUnavailable("native bank did not show the accepted dispatch receipt")
    package = _native_raw(session, service, recipient, package_id)
    body = package.get("body")
    if (package.get("package_id") != package_id or not isinstance(body, dict)
            or body.get("audience") != recipient or bank_receipt.get("package_sha256") != canon.digest(package)
            or _digest_hex(receipt.get("package_digest")) != bank_receipt.get("package_sha256")
            or package.get("body_sha256") != canon.digest(body)
            or _digest_hex(approved_digest) != package.get("body_sha256")):
        raise ProbeUnavailable("approved body, signed package and bank receipt did not agree")
    return {**view, "package": package, "gaps": [claim["gap"] for claim in body.get("claims", [])
                                                if claim.get("gap")]}


def _receipt(response) -> dict:
    """Extract the receipt identity, digest and status from the dispatch result."""
    payload = _json(response)
    level = payload.get("dispatch", payload)
    level = level if isinstance(level, dict) else {}
    receipt = level.get("receipt") if isinstance(level.get("receipt"), dict) else {}
    out = {}
    if level.get("package_id"):
        out["package_id"] = level["package_id"]
    out.setdefault("package_id", receipt.get("package_id"))
    out["receipt_id"] = receipt.get("receipt_id") or level.get("receipt_id")
    out["package_digest"] = receipt.get("package_digest") or level.get("package_digest")
    out["status"] = receipt.get("status") or level.get("status")
    return out


def _establish_delivery(session: ProbeSession, request_id: str, recipient: str,
                        policy_version: str) -> tuple[str, dict]:
    """Run, approve and dispatch, then read the accepted package as the recipient."""
    _run_investigation(session, request_id)
    _, digest = _current_digest(session, request_id)
    session.require_success(_approve(session, request_id, digest, recipient, policy_version))
    return _check_delivery(session, _dispatch(session, request_id), recipient, digest)


# --- content helpers --------------------------------------------------------

def _contains_value(obj, value) -> bool:
    text = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False, default=str)
    return bool(value) and str(value) in unescape(text)


def _has_any_key(obj, keys: set) -> bool:
    if isinstance(obj, dict):
        return bool(keys & obj.keys()) or any(_has_any_key(v, keys) for v in obj.values())
    if isinstance(obj, list):
        return any(_has_any_key(v, keys) for v in obj)
    return False


def _inbox_rows(response) -> list[dict]:
    ProbeSession.require_success(response)
    rows = _json(response).get("packages")
    if not isinstance(rows, list) or not all(
        isinstance(row, dict) and isinstance(row.get("package_id"), str)
        and isinstance(row.get("request_id"), str) for row in rows
    ):
        raise ProbeUnavailable("bank inbox did not expose a structured package list")
    return rows


def _attachments_for(payload: dict, record_id: str) -> list:
    """Every attachment-like dict in a package that cites ``record_id``."""
    found = []
    native_body = payload.get("package", {}).get("body", {})
    source_checks = {source.get("attachment_id"): source for source in payload.get("sources", [])
                     if isinstance(source, dict)}
    for item in native_body.get("attachments", []):
        ref = item.get("ref", {})
        if ref.get("ref_type") == "record" and ref.get("ref_id") == record_id:
            attestation = source_checks.get(item.get("attachment_id"), {}).get("attestation")
            found.append({"attestation": attestation, "redacted": item.get("form") == "redacted_extract"})
    for key in ("attachments", "source_trust", "evidence"):
        for item in payload.get(key, []) or []:
            if isinstance(item, dict) and item.get("record_id") == record_id:
                found.append(item)
    return found


def _digest_hex(value) -> str:
    if not isinstance(value, str):
        return ""
    digest = value.strip().lower().removeprefix("sha256:")
    return digest if re.fullmatch(r"[0-9a-f]{12,64}", digest) else ""


def _same_digest(a, b) -> bool:
    ha, hb = _digest_hex(a), _digest_hex(b)
    return bool(ha and hb and (ha.startswith(hb) or hb.startswith(ha)))


def _check_delivery(session: ProbeSession, response, recipient: str, expect_digest: str) -> tuple[str, dict]:
    session.require_success(response)
    receipt = _receipt(response)
    package_id = receipt.get("package_id")
    service = session.fixtures.get("consent_bank_service")
    if service and package_id:
        return package_id, _native_delivery(session, service, recipient, receipt, expect_digest)
    if not package_id or not _same_digest(receipt.get("package_digest"), expect_digest):
        raise ProbeUnavailable("dispatch did not return the package ID and matching receipt digest")
    return package_id, _bank_package(session, recipient, package_id)


def _fresh_inbox(session: ProbeSession, recipient: str, request_id: str) -> None:
    rows = _inbox_rows(_bank_inbox(session, recipient))
    if any(row["request_id"] == request_id for row in rows):
        raise ProbeUnavailable("probe needs an isolated request with no existing delivery")


def _no_delivery(session: ProbeSession, request_id: str, recipient: str) -> str:
    rows = _inbox_rows(_bank_inbox(session, recipient))
    return NOT_BLOCKED if any(row["request_id"] == request_id for row in rows) else BLOCKED


def _export_views(session: ProbeSession, request_id: str, recipient: str, package_id: str,
                  route_names: tuple[str, ...], denied: str) -> str:
    """Look at each actual export. A known visible marker rules out empty/opaque artefacts."""
    allowed = session.require("export_allowed_marker")
    missing = []
    for name in route_names:
        try:
            path = session.route(name, request_id=request_id, recipient=recipient, package_id=package_id)
            response = session.http("GET", APP, path, BANK_A, headers=JSON_HEADERS)
            session.require_success(response)
            if _contains_value(response.text, denied) or _contains_value(
                response.headers.get("content-disposition", ""), denied
            ):
                return NOT_BLOCKED
            if "application/pdf" in response.headers.get("content-type", "").lower():
                raise ProbeUnavailable(f"{name} needs an inspectable rendered print representation")
            if not _contains_value(response.text, allowed):
                raise ProbeUnavailable(f"{name} did not expose the expected public evidence")
        except ProbeUnavailable as exc:
            missing.append(str(exc))
    if missing:
        raise ProbeUnavailable("; ".join(missing))
    return BLOCKED


def _removable_action(review: dict) -> str | None:
    for item in _review_section(review).get("included", []):
        if item.get("removable") and item.get("remove_action"):
            return item["remove_action"]
    return None


# --- consent (SP-01..SP-09) -------------------------------------------------

def sp01(session: ProbeSession):
    """C1: dispatch with no approval recorded -> approval_missing (nothing reaches the inbox)."""
    rid = session.require("rfc_bank_a")
    recipient = session.fixtures.get("recipient_bank_a", BANK_A)
    _fresh_inbox(session, recipient, rid)
    _run_investigation(session, rid)
    _current_digest(session, rid)  # draft ready, never approved
    outcome = session.denial(_dispatch(session, rid), {"approval_missing"})
    return _no_delivery(session, rid, recipient) if outcome == BLOCKED else outcome


def sp02(session: ProbeSession):
    """C2: approve, remove one optional item, dispatch -> approval_digest_mismatch; then a
    fresh approval of the revised body dispatches with a real receipt at the new digest."""
    rid = session.require("rfc_bank_a")
    recipient = session.require("recipient_bank_a")
    policy_version = session.require("policy_version")
    _fresh_inbox(session, recipient, rid)
    _run_investigation(session, rid)
    review, d1 = _current_digest(session, rid)
    session.require_success(_approve(session, rid, d1, recipient, policy_version))
    action = _removable_action(review)
    if action is None:
        raise ProbeUnavailable("no optional included item to remove")
    session.require_success(session.http("POST", APP, action, HOLDER, headers=JSON_HEADERS))
    _, d2 = _current_digest(session, rid)
    if _same_digest(d1, d2):
        raise ProbeUnavailable("removing the item did not change the body digest")
    outcome = session.denial(_dispatch(session, rid), {"approval_digest_mismatch"})
    if outcome != BLOCKED:
        return outcome  # NOT_BLOCKED (changed body dispatched) or NOT_RUN
    if _no_delivery(session, rid, recipient) == NOT_BLOCKED:
        return NOT_BLOCKED
    # Required control: re-approve the revised body, confirm a real re-dispatch at d2.
    session.require_success(_approve(session, rid, d2, recipient, policy_version))
    _check_delivery(session, _dispatch(session, rid), recipient, d2)
    return BLOCKED


def sp03(session: ProbeSession):
    """Reuse the bank_a approval to reach bank_b.

    With ``rfc_bank_b`` set (native S4 dispatch, which takes its audience from the request):
    C12, dispatch bank_b's own request while only the bank_a approval exists. Without it: C4,
    the same dispatch addressed to bank_b. A dispatch that succeeds but ignored the client
    audience proves neither a refusal nor a leak, so only a bank_b inbox row is a leak."""
    rid = session.require("rfc_bank_a")
    recipient_a = session.require("recipient_bank_a")
    recipient_b = session.require("recipient_bank_b")
    policy_version = session.require("policy_version")
    other = session.fixtures.get("rfc_bank_b")
    targets = (rid,) if other is None else (rid, other)
    rows = _inbox_rows(_bank_inbox(session, recipient_b))
    if any(row["request_id"] in targets for row in rows):
        raise ProbeUnavailable("probe needs an isolated request with no existing delivery")
    _run_investigation(session, rid)
    _, d1 = _current_digest(session, rid)
    session.require_success(_approve(session, rid, d1, recipient_a, policy_version))
    if other is None:
        resp = _dispatch(session, rid, audience=recipient_b)
        codes = {"approval_audience_mismatch"}
    else:
        _run_investigation(session, other)
        payload, _ = _current_digest(session, other)
        if _request_meta(payload).get("audience") != recipient_b:
            raise ProbeUnavailable("holder review did not show the bank_b request's audience")
        resp = _dispatch(session, other)
        codes = {"approval_missing", "approval_audience_mismatch"}
    outcome = session.denial(resp, codes)
    leaked = any(row["request_id"] in targets for row in _inbox_rows(_bank_inbox(session, recipient_b)))
    if leaked:
        return NOT_BLOCKED
    if outcome == NOT_BLOCKED and other is None:
        raise ProbeUnavailable("dispatch ignored the client audience; no audience refusal was observed")
    return outcome


def sp04(session: ProbeSession):
    """C13: approve the parent, then dispatch the follow-up (new request_id, same parent)
    without its own approval -> approval_request_mismatch / approval_missing."""
    parent = session.require("rfc_bank_a")
    followup = session.require("rfc_followup")
    recipient_a = session.require("recipient_bank_a")
    policy_version = session.require("policy_version")
    _fresh_inbox(session, recipient_a, followup)
    _run_investigation(session, parent)
    _, d_parent = _current_digest(session, parent)
    session.require_success(_approve(session, parent, d_parent, recipient_a, policy_version))
    _run_investigation(session, followup)
    if _request_meta(_json(_review(session, followup))).get("parent_request_id") != parent:
        raise ProbeUnavailable("holder review did not identify the expected follow-up parent")
    resp = _dispatch(session, followup)
    outcome = session.denial(resp, {"approval_request_mismatch", "approval_missing"})
    return _no_delivery(session, followup, recipient_a) if outcome == BLOCKED else outcome


def sp05(session: ProbeSession):
    """C10: a still-open RFC with a seeded expired approval cannot dispatch."""
    rid = session.require("expired_request")
    recipient = session.fixtures.get("recipient_bank_a", BANK_A)
    _fresh_inbox(session, recipient, rid)
    outcome = session.denial(_dispatch(session, rid), {"approval_expired"})
    return _no_delivery(session, rid, recipient) if outcome == BLOCKED else outcome


def sp06(session: ProbeSession):
    """C8/C9: a cited correction invalidates approval, an unrelated one allows delivery."""
    rid = session.require("rfc_bank_a")
    recipient_a = session.require("recipient_bank_a")
    policy_version = session.require("policy_version")
    correction = session.require("correction_event")
    route = session.route("merchant_correction")
    _fresh_inbox(session, recipient_a, rid)
    _run_investigation(session, rid)
    _, d1 = _current_digest(session, rid)
    session.require_success(_approve(session, rid, d1, recipient_a, policy_version))
    session.require_success(_correct_source(session, route, correction))
    outcome = session.denial(_dispatch(session, rid), {"source_revision_changed"})
    if outcome != BLOCKED:
        return outcome
    if _no_delivery(session, rid, recipient_a) == NOT_BLOCKED:
        return NOT_BLOCKED

    control = session.require("unrelated_correction_request")
    unrelated = session.require("unrelated_correction_event")
    _fresh_inbox(session, recipient_a, control)
    _run_investigation(session, control)
    _, digest = _current_digest(session, control)
    session.require_success(_approve(session, control, digest, recipient_a, policy_version))
    session.require_success(_correct_source(session, route, unrelated))
    sent = _dispatch(session, control)
    if session.denial(sent, {"source_revision_changed"}) == BLOCKED:
        return NOT_BLOCKED
    _check_delivery(session, sent, recipient_a, digest)
    return BLOCKED


def sp07(session: ProbeSession):
    """C7: approve, change a privacy rule (new policy version), dispatch -> policy_version_changed."""
    rid = session.require("rfc_bank_a")
    recipient_a = session.require("recipient_bank_a")
    policy_version = session.require("policy_version")
    bump = session.require("policy_bump_payload")
    route = session.route("policy_bump")
    _fresh_inbox(session, recipient_a, rid)
    _run_investigation(session, rid)
    _, d1 = _current_digest(session, rid)
    session.require_success(_approve(session, rid, d1, recipient_a, policy_version))
    session.require_success(session.http("POST", APP, route, HOLDER, data=bump, headers=JSON_HEADERS))
    outcome = session.denial(_dispatch(session, rid), {"policy_version_changed"})
    return _no_delivery(session, rid, recipient_a) if outcome == BLOCKED else outcome


def sp08(session: ProbeSession):
    """Start correction and dispatch concurrently, then check the resulting order and state."""
    rid = session.require("rfc_bank_a")
    recipient_a = session.require("recipient_bank_a")
    policy_version = session.require("policy_version")
    correction = session.require("correction_event")
    cited = session.require("cited_record_id")
    approved_revision = session.require("cited_approved_revision")
    if not isinstance(correction, dict) or not correction.get("event_id"):
        raise ProbeUnavailable("correction_event needs its audit event ID")
    correction_path = session.route("merchant_correction")
    audit_path = session.route("race_audit", request_id=rid)
    _fresh_inbox(session, recipient_a, rid)
    _run_investigation(session, rid)
    _, d1 = _current_digest(session, rid)
    session.require_success(_approve(session, rid, d1, recipient_a, policy_version))
    ready = Barrier(2)

    def correct():
        ready.wait()
        return _correct_source(session, correction_path, correction)

    with ThreadPoolExecutor(max_workers=1) as pool:
        correction_result = pool.submit(correct)
        ready.wait()
        resp = _dispatch(session, rid)
        session.require_success(correction_result.result())

    refused = session.denial(resp, {"source_revision_changed"})
    if refused == NOT_RUN:
        return NOT_RUN
    if refused == BLOCKED:
        if _no_delivery(session, rid, recipient_a) == NOT_BLOCKED:
            return NOT_BLOCKED
    else:
        _, package = _check_delivery(session, resp, recipient_a, d1)
        attachments = _attachments_for(package, cited)
        revisions = [a.get("revision") for a in attachments if a.get("revision") is not None]
        if not revisions:
            raise ProbeUnavailable("the delivered package did not expose cited revisions")
        if any(revision != approved_revision for revision in revisions):
            return NOT_BLOCKED
        superseded = session.require("race_superseded_marker")
        if superseded not in _review(session, rid).text:
            raise ProbeUnavailable("holder review did not show the delivered revision as superseded")

    audit = session.http("GET", APP, audit_path, HOLDER, headers=JSON_HEADERS)
    session.require_success(audit)
    entries = _json(audit).get("entries")
    if not isinstance(entries, list):
        raise ProbeUnavailable("race audit did not expose ordered entries")
    expected = (["record.corrected", "dispatch.refused"] if refused == BLOCKED
                else ["dispatch.sent", "record.corrected"])
    actions = [row.get("action") for row in entries if isinstance(row, dict) and (
        row.get("action") == "record.corrected" and row.get("event_id") == correction["event_id"]
        or row.get("request_id") == rid and row.get("action") in {"dispatch.refused", "dispatch.sent"}
    )]
    if len(actions) != 2:
        raise ProbeUnavailable("race audit did not identify both actions exactly once")
    return BLOCKED if actions == expected else NOT_BLOCKED


def sp09(session: ProbeSession):
    """C14/C15: identical retry returns the original receipt and changed bytes are refused."""
    rid = session.require("rfc_bank_a")
    recipient_a = session.require("recipient_bank_a")
    policy_version = session.require("policy_version")
    _fresh_inbox(session, recipient_a, rid)
    _run_investigation(session, rid)
    _, digest = _current_digest(session, rid)
    session.require_success(_approve(session, rid, digest, recipient_a, policy_version))
    first = _dispatch(session, rid)
    package_id, delivered = _check_delivery(session, first, recipient_a, digest)
    retry = _dispatch(session, rid)
    if not 200 <= retry.status_code < 300:
        # The retry neither echoed the receipt nor clearly regressed.
        raise ProbeUnavailable(f"identical retry answered HTTP {retry.status_code}, not the original receipt")
    if _receipt(retry) != _receipt(first):
        return NOT_BLOCKED
    if _one_delivery(session, rid, recipient_a, package_id) == NOT_BLOCKED:
        return NOT_BLOCKED
    service = session.fixtures.get("consent_bank_service")
    if service and session.fixtures.get("dispatch_retry_conflict") is None:
        response = _native_conflict(session, service, recipient_a, delivered.get("package"), package_id)
    else:
        path = session.route("dispatch_retry", request_id=rid, package_id=package_id)
        conflict = session.require("dispatch_retry_conflict")
        response = session.http("POST", APP, path, HOLDER, json=conflict, headers=JSON_HEADERS)
    outcome = session.denial(response, {"package_id_reused"})
    if outcome != BLOCKED:
        return outcome
    if _one_delivery(session, rid, recipient_a, package_id) == NOT_BLOCKED:
        return NOT_BLOCKED
    if service and canon.digest(_native_raw(session, service, recipient_a, package_id)) != canon.digest(
            delivered["package"]):
        return NOT_BLOCKED  # the refused bytes still replaced the stored package
    return BLOCKED


def _native_conflict(session: ProbeSession, service: str, recipient: str, package, package_id: str):
    """POST the stored signed package with one changed body field under the same package_id."""
    origin = session.base_urls.get(service)
    if not isinstance(origin, str) or urlsplit(origin).hostname not in {"127.0.0.1", "::1", "localhost"}:
        raise ProbeUnavailable("native changed-bytes retry needs a disposable loopback bank")
    body = package.get("body") if isinstance(package, dict) else None
    if not isinstance(body, dict) or package.get("package_id") != package_id:
        raise ProbeUnavailable("native bank did not return the delivered signed package")
    changed = deepcopy(package)
    changed["body"]["probe_changed_bytes"] = "sp09"
    if canon.digest(changed) == canon.digest(package):
        raise ProbeUnavailable("changed retry bytes did not change the package digest")
    path = session.route("bank_deliver", "/deliver", recipient=recipient, package_id=package_id)
    return session.http("POST", service, path, _bank_actor(session, recipient),
                        content=canon.canonical_bytes(changed),
                        headers={**JSON_HEADERS, "Content-Type": "application/json"})


def _one_delivery(session: ProbeSession, request_id: str, recipient: str, package_id: str) -> str:
    inbox = _inbox_rows(_bank_inbox(session, recipient))
    history_path = session.route("sharing_history", "/passport")
    history = session.http("GET", APP, history_path, HOLDER, headers=JSON_HEADERS)
    session.require_success(history)
    rows = _json(history).get("sharing_history")
    if not isinstance(rows, list):
        raise ProbeUnavailable("holder sharing history did not expose a structured list")
    for result_rows in (inbox, rows):
        ids = [row.get("package_id") for row in result_rows
               if isinstance(row, dict) and row.get("request_id") == request_id]
        if not ids:
            raise ProbeUnavailable("delivered package was absent from inbox or sharing history")
        if ids != [package_id]:
            return NOT_BLOCKED
    return BLOCKED


# --- disclosure (SP-10..SP-15) ---------------------------------------------

def sp10(session: ProbeSession):
    """A policy-denied field is absent from the bank-bound body and recorded in the private
    exclusion list with a rule id."""
    rid = session.require("rfc_bank_a")
    denied_value = session.require("denied_value")
    item_id = session.require("denied_item_id")
    recipient = session.require("recipient_bank_a")
    policy_version = session.require("policy_version")
    _run_investigation(session, rid)
    review, digest = _current_digest(session, rid)
    section = _review_section(review)
    if _contains_value(section.get("included", []), denied_value):
        return NOT_BLOCKED  # denied field survived into the body
    excluded = next((e for e in section.get("excluded", [])
                     if isinstance(e, dict) and e.get("item_id") == item_id), None)
    if excluded is None or not excluded.get("rule_id"):
        raise ProbeUnavailable("holder review did not identify the exclusion and its rule")
    session.require_success(_approve(session, rid, digest, recipient, policy_version))
    _, package = _check_delivery(session, _dispatch(session, rid), recipient, digest)
    return NOT_BLOCKED if _contains_value(package, denied_value) else BLOCKED


def sp11(session: ProbeSession):
    """No denied token appears in review claims or any delivered export."""
    rid = session.require("rfc_bank_a")
    denied_value = session.require("denied_value")
    recipient = session.require("recipient_bank_a")
    policy_version = session.require("policy_version")
    _run_investigation(session, rid)
    review, digest = _current_digest(session, rid)
    section = _review_section(review)
    claims = section.get("claims", [])
    if not claims:
        return NOT_RUN
    bank_bound = {"claims": claims, "included": section.get("included", [])}
    if _contains_value(bank_bound, denied_value):
        return NOT_BLOCKED
    session.require_success(_approve(session, rid, digest, recipient, policy_version))
    package_id, package = _check_delivery(session, _dispatch(session, rid), recipient, digest)
    if _contains_value(package, denied_value):
        return NOT_BLOCKED
    return _export_views(session, rid, recipient, package_id, ("signed_package", "print_view"), denied_value)


def sp12(session: ProbeSession):
    """Denied values are absent from attachment metadata / filenames on the real package."""
    rid = session.require("rfc_bank_a")
    recipient_a = session.require("recipient_bank_a")
    policy_version = session.require("policy_version")
    denied_value = session.require("denied_metadata_value")
    package_id, package = _establish_delivery(session, rid, recipient_a, policy_version)
    if _contains_value(package, denied_value):
        return NOT_BLOCKED
    return _export_views(session, rid, recipient_a, package_id,
                         ("signed_package", "print_view", "attachment_metadata"), denied_value)


def sp13(session: ProbeSession):
    """A gap is described generically: it must not name the withheld merchant / category."""
    rid = session.require("rfc_bank_a")
    withheld = session.require("withheld_entity")
    recipient = session.require("recipient_bank_a")
    policy_version = session.require("policy_version")
    _run_investigation(session, rid)
    review, digest = _current_digest(session, rid)
    gaps = _review_section(review).get("gaps", [])
    if not gaps:
        return NOT_RUN
    if _contains_value(gaps, withheld):
        return NOT_BLOCKED
    session.require_success(_approve(session, rid, digest, recipient, policy_version))
    _, package = _check_delivery(session, _dispatch(session, rid), recipient, digest)
    if not package.get("gaps"):
        raise ProbeUnavailable("bank package did not expose the expected gap")
    return NOT_BLOCKED if _contains_value(package["gaps"], withheld) else BLOCKED


def sp14(session: ProbeSession):
    """A partially-approved issuer record ships as a redacted extract (attestation
    package_signer, redacted true), never as the full issuer-signed record."""
    rid = session.require("rfc_bank_a")
    recipient_a = session.require("recipient_bank_a")
    policy_version = session.require("policy_version")
    record_id = session.require("partial_record_id")
    _, package = _establish_delivery(session, rid, recipient_a, policy_version)
    attachments = _attachments_for(package, record_id)
    if not attachments:
        return NOT_RUN
    for att in attachments:
        attestation, redacted = att.get("attestation"), att.get("redacted")
        if attestation == "issuer_signature" or redacted is False:
            return NOT_BLOCKED  # full issuer-signed record exported despite partial approval
        if attestation != "package_signer" or redacted is not True:
            return NOT_RUN  # attestation/redaction not exposed; cannot confirm the extract
    return BLOCKED


def sp15(session: ProbeSession):
    """The bank-facing package carries claim -> source -> calculation only: no exclusion list,
    model inputs, raw sources, or holder-only values."""
    rid = session.require("rfc_bank_a")
    recipient_a = session.require("recipient_bank_a")
    policy_version = session.require("policy_version")
    private_value = session.require("private_value")
    package_id, package = _establish_delivery(session, rid, recipient_a, policy_version)
    forbidden = {"excluded", "exclusions", "exclusion_list", "model_inputs", "model_input",
                 "raw_sources", "private_review", "generalised"}
    if _has_any_key(package, forbidden) or _contains_value(package, private_value):
        return NOT_BLOCKED
    path = session.route("bank_trace", package_id=package_id, recipient=recipient_a)
    trace = session.http("GET", APP, path, BANK_A, headers=JSON_HEADERS)
    session.require_success(trace)
    payload = _json(trace)
    if _has_any_key(payload, forbidden) or _contains_value(payload, private_value):
        return NOT_BLOCKED
    if not all(payload.get(key) for key in ("claims", "sources", "calculations")):
        raise ProbeUnavailable("bank trace did not show claims, source refs and calculations")
    return BLOCKED


# --- policy (SP-16) ---------------------------------------------------------

def sp16(session: ProbeSession):
    """An ambiguous / conflicting rule is held for review, not auto-applied; deny wins."""
    ambiguous = session.require("ambiguous_rule")
    route = session.route("rule_interpret")  # unpublished hook, config-only
    resp = session.http("POST", APP, route, HOLDER, data=ambiguous, headers=JSON_HEADERS)
    session.require_success(resp)
    payload = _json(resp)
    interp = payload.get("interpretation") if isinstance(payload.get("interpretation"), dict) else payload
    status = interp.get("status") or payload.get("status")
    if status == "active" or payload.get("applied") is True:
        return NOT_BLOCKED  # ambiguous rule applied without clarification
    if (status == "held" and interp.get("clarification_required") is True
            and interp.get("unclassified_withheld") is True
            and interp.get("conflict_resolution") == "deny"):
        return BLOCKED
    raise ProbeUnavailable("rule interpretation did not expose the hold, clarification and deny decision")


# --- isolation / recipient spoof (SP-20) ------------------------------------

def sp20(session: ProbeSession):
    """Spoof recipient through query and headers separately, keeping the bank_a credential."""
    recipient_a = session.require("recipient_bank_a")
    recipient_b = session.require("recipient_bank_b")
    bank_b_package = session.require("bank_b_package_id")
    b_rows = _inbox_rows(_bank_inbox(session, recipient_b))
    if not any(row["package_id"] == bank_b_package for row in b_rows):
        raise ProbeUnavailable("bank_b control did not show the target package")
    baseline = _inbox_rows(_bank_inbox(session, recipient_a))
    if any(row["package_id"] == bank_b_package for row in baseline):
        return NOT_BLOCKED

    query = f"?recipient={quote(recipient_b, safe='')}&audience={quote(recipient_b, safe='')}"
    service, path = _inbox_target(session, recipient_a)
    attempts = (
        (path + query, None),
        (path, {"X-Alibi-Recipient": recipient_b, "X-Alibi-Audience": recipient_b}),
    )
    outcomes = []
    for target, headers in attempts:
        spoofed = session.http("GET", service, target, BANK_A, headers={**JSON_HEADERS, **(headers or {})})
        if bank_b_package in spoofed.text:
            return NOT_BLOCKED
        if not 200 <= spoofed.status_code < 300:
            outcomes.append(NOT_RUN)
        else:
            outcomes.append(BLOCKED if _inbox_rows(spoofed) == baseline else NOT_RUN)
    return BLOCKED if all(outcome == BLOCKED for outcome in outcomes) else NOT_RUN


HANDLERS = {
    "SP-01": sp01, "SP-02": sp02, "SP-03": sp03, "SP-04": sp04, "SP-05": sp05,
    "SP-06": sp06, "SP-07": sp07, "SP-08": sp08, "SP-09": sp09, "SP-10": sp10,
    "SP-11": sp11, "SP-12": sp12, "SP-13": sp13, "SP-14": sp14, "SP-15": sp15,
    "SP-16": sp16, "SP-20": sp20,
}
