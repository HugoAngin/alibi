"""The mock bank service and analyst console state for the two recipients.

One service, two credentials, two inboxes. The recipient comes from the bearer token (or the
session cookie it sets); the URL never grants access. A delivered package is verified with
``alibi.api.verify_package`` against the pinned registry, bound to the request the bank issued,
and kept with its verification result. A rejected package keeps only the rejection.

Bank-side state lives in the app sidecar (``data/alibi/app/case_<n>.state.json``), never in the
holder's passport: received packages, receipts, dispositions and the follow-up requests issued.
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

from alibi import api
from alibi.app import cases
from alibi.app import common as c
from alibi.app.actions import Refusal
from alibi.app.config import RECIPIENTS
from alibi.app.runtime import Runtime
from alibi.core import canon
from alibi.core.ingest import IngestError
from alibi.core.reconcile import SETTLED_STATUSES
from alibi.core.schemas import Period, format_utc, parse_utc

FOLLOWUP_TTL = timedelta(days=7)
TERMINAL = {"clear", "escalate"}
_IDENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}")


# --------------------------------------------------------------------------- authentication


def authenticate(rt: Runtime, recipient: str, *, bearer: str | None, cookie: str | None,
                 query_token: str | None) -> tuple[str, str | None]:
    """The recipient a credential names. Returns ``(recipient, token_to_set_as_cookie)``.
    Raises ``Refusal`` 401 without a credential and 403 when the URL names another recipient."""
    if recipient not in RECIPIENTS:
        raise Refusal("unknown_recipient", f"no recipient {recipient!r}", status=404)
    token = bearer or query_token or cookie
    who = rt.settings.recipient_for_token(token)
    if who is None:
        raise Refusal("not_authenticated", "a recipient credential is required: send it as a bearer token, "
                                           "or open the inbox once with ?token=<credential> to start a session",
                      status=401)
    if who != recipient:
        rt.bank_audit(who, action="access.refused", outcome="refused", target_id=recipient,
                      detail=f"{who} asked for {recipient}'s resources")
        raise Refusal("recipient_mismatch", f"the credential is for {who}, not {recipient}", status=403)
    return who, (query_token if query_token and query_token == token else None)


# --------------------------------------------------------------------------- state


def _packages(rt: Runtime, recipient: str) -> dict[str, dict]:
    return rt.sidecar.get(f"bank:{recipient}:packages") or {}


def _save_package(rt: Runtime, recipient: str, record: dict) -> None:
    rt.sidecar.update(f"bank:{recipient}:packages", lambda cur: {**(cur or {}), record["package_id"]: record})


def _followups(rt: Runtime, recipient: str) -> list[dict]:
    return rt.sidecar.get(f"bank:{recipient}:requests") or []


def _dispositions(rt: Runtime, recipient: str) -> list[dict]:
    return rt.sidecar.get(f"bank:{recipient}:dispositions") or []


def issued_requests(rt: Runtime, recipient: str) -> list[dict]:
    """Every request this recipient issued: the case's RFCs plus follow-ups made here."""
    out = [cases.to_rfc(r) for r in rt.case.get("rfcs", []) if r["audience"] == recipient]
    out += [f["request"] for f in _followups(rt, recipient)]
    return out


def _request_for(rt: Runtime, recipient: str, request_id: str) -> dict | None:
    return next((r for r in issued_requests(rt, recipient) if r["request_id"] == request_id), None)


# --------------------------------------------------------------------------- receive


def receive_package(rt: Runtime, recipient: str, raw: bytes) -> tuple[int, dict[str, Any]]:
    """Verify and store a delivered package. Identical bytes again return the stored receipt; the
    same package id with different bytes is refused. Returns ``(http_status, body)``."""
    if recipient not in RECIPIENTS:
        return 404, {"code": "unknown_recipient", "reason": f"no recipient {recipient!r}"}
    now = rt.now_text()
    try:
        obj = canon.parse_json(raw)
    except Exception:  # noqa: BLE001 - malformed bytes are refused, not kept
        obj = None
    if not isinstance(obj, dict) or not isinstance(obj.get("body"), dict):
        # Not even the shape of a package: refuse without storing, so an unauthenticated client
        # cannot fill the inbox with noise. Well-formed but invalid packages are kept as rejected.
        rt.bank_audit(recipient, action="dispatch.refused", outcome="refused", target_id=None,
                      detail="not a package object")
        return 400, {"code": "not_a_package", "reason": "the delivery is not a JSON package object"}
    digest = canon.digest(obj)
    package_id = obj.get("package_id") if isinstance(obj, dict) else None
    package_id = package_id if isinstance(package_id, str) and _IDENT.fullmatch(package_id) else None
    with rt.lock:
        if package_id is not None:
            existing = _packages(rt, recipient).get(package_id)
            if existing is not None:
                if existing["package_sha256"] == digest:
                    rt.bank_audit(recipient, action="dispatch.deduplicated", outcome="duplicate",
                                  target_id=package_id, detail="same package_id and digest; existing receipt returned")
                    return (200 if existing["accepted"] else 422), existing["receipt"]
                rt.bank_audit(recipient, action="dispatch.refused", outcome="refused", target_id=package_id,
                              detail="package_id_reused")
                return 409, {"code": "package_id_reused",
                             "reason": f"dispatch refused: package {package_id} was already sent with a different digest."}
        body = obj.get("body") if isinstance(obj, dict) and isinstance(obj.get("body"), dict) else {}
        request_id = body.get("request_id") if isinstance(body.get("request_id"), str) else None
        rfc = _request_for(rt, recipient, request_id) if request_id else None
        if rfc is None:
            result = api.verify_package(raw, expected_audience=recipient, registry=rt.registry, now=rt.clock(),
                                        expected_request_id=request_id or "unknown")
            reasons = list(result.errors) + [f"the package answers request {request_id or 'unknown'}, which "
                                             f"{recipient} did not issue"]
            accepted = False
        else:
            result = api.verify_package(raw, expected_audience=recipient, registry=rt.registry, now=rt.clock(),
                                        expected_subject=rfc["subject_ref"], expected_request_id=rfc["request_id"])
            reasons, accepted = list(result.errors), bool(result.accepted)
        if not accepted and not (result.authentication.package_id and package_id):
            # Not a package by schema: refuse without keeping a row, so an unauthenticated client
            # cannot fill the inbox with noise. Well-formed but invalid packages are kept as rejected.
            rt.bank_audit(recipient, action="dispatch.refused", outcome="refused", target_id=package_id,
                          detail="not a package: " + "; ".join(reasons)[:200])
            return 400, {"code": "not_a_package", "reason": "; ".join(reasons) or "the delivery is not a package"}
        pid = package_id
        receipt = {"receipt_id": canon.make_id("rcpt", recipient, pid, digest), "package_id": pid,
                   "package_sha256": digest, "recipient": recipient, "received_at": now,
                   "accepted": accepted, "reasons": [r[:300] for r in reasons]}
        record = {"package_id": pid, "request_id": request_id, "raw": obj if accepted else None,
                  "package_sha256": digest, "received_at": now, "accepted": accepted,
                  "verification": result.to_json_dict(), "receipt": receipt}
        _save_package(rt, recipient, record)
        rt.bank_audit(recipient, action="package.received", outcome="ok" if accepted else "refused",
                      target_id=pid, detail=("verified; receipt " + receipt["receipt_id"]) if accepted
                      else "rejected: " + "; ".join(reasons)[:220])
    if not accepted:
        return 422, {"code": "package_rejected", "reason": "; ".join(reasons), **receipt}
    return 200, receipt


# --------------------------------------------------------------------------- views


def _auth_status(verification: dict, accepted: bool) -> str:
    reasons = " ".join(verification.get("authentication", {}).get("reasons", []) + verification.get("errors", []))
    if accepted:
        return "valid"
    if "expired" in reasons:
        return "expired"
    if "addressed to" in reasons:
        return "wrong_audience"
    return "invalid"


def _checks(verification: dict, extra_reasons: list[str]) -> list[dict[str, str]]:
    auth = verification.get("authentication", {})
    reasons = list(auth.get("reasons", [])) + list(verification.get("errors", [])) + extra_reasons

    def check(name: str, *needles: str, invert: bool = False) -> dict[str, str]:
        hits = [r for r in reasons if any(n in r for n in needles)]
        ok = not hits
        return {"name": name, "result": "pass" if ok else "fail",
                "detail": "; ".join(hits)[:300] if hits else "no problem found"}

    return [
        check("Package schema", "schema", "not valid JSON", "JSON object", "canonicalised"),
        check("Body digest", "body_sha256"),
        check("Recipient binding", "addressed to"),
        check("Request binding", "answers request"),
        check("Validity window", "expired", "future", "before it was issued"),
        check("Alibi package signature", "package signature"),
        check("Claimed issuer signatures", "issuer signature", "attachment"),
    ]


def _base(rt: Runtime, recipient: str, title: str, nav_key: str, flashes=None) -> dict[str, Any]:
    from alibi.app.views import base_context
    spec = RECIPIENTS[recipient]
    actor = {"name": spec["analyst"], "role": f"Analyst, {spec['name']}"}
    return base_context(rt, title, "analyst", c.analyst_nav(nav_key, recipient), actor, flashes)


def _claim_counts(record: dict) -> dict[str, int]:
    counts = {"supported": 0, "contradicted": 0, "unresolved": 0}
    for cl in record.get("verification", {}).get("claims", []):
        counts[cl.get("status", "unresolved")] = counts.get(cl.get("status", "unresolved"), 0) + 1
    return counts


def _latest_disposition(rt: Runtime, recipient: str, package_id: str) -> dict | None:
    rows = [d for d in _dispositions(rt, recipient) if d["package_id"] == package_id]
    return rows[-1] if rows else None


def inbox(rt: Runtime, recipient: str, flashes=None) -> dict[str, Any]:
    spec = RECIPIENTS[recipient]
    packages = []
    for record in sorted(_packages(rt, recipient).values(), key=lambda r: r["received_at"]):
        accepted = record["accepted"]
        raw = (record.get("raw") or {}) if accepted else {}    # a rejected package shows nothing of its body
        body = raw.get("body") or {}
        rfc = _request_for(rt, recipient, record.get("request_id") or "")
        disposition = _latest_disposition(rt, recipient, record["package_id"])
        packages.append({
            "package_id": record["package_id"], "request_id": record.get("request_id") or "unknown",
            "received_at": record["received_at"], "expires_at": body.get("expires_at", "—"),
            "subject_ref_masked": c.mask_subject(rfc["subject_ref"]) if rfc else "—",
            "authentication": _auth_status(record["verification"], accepted),
            "claim_counts": (_claim_counts(record) if accepted
                             else {"supported": 0, "contradicted": 0, "unresolved": 0}),
            "disposition": disposition["decision"] if disposition else None,
            "href": f"/bank/{recipient}/packages/{record['package_id']}",
        })
    answered = {p["request_id"] for p in packages}
    requests_sent = []
    for rfc in issued_requests(rt, recipient):
        requests_sent.append({"request_id": rfc["request_id"], "parent_request_id": rfc.get("parent_request_id"),
                              "sent_at": rfc["issued_at"],
                              "status": "answered" if rfc["request_id"] in answered else "awaiting_consent",
                              "question_count": len(rfc["questions"]),
                              "scope": ("txn_ids" if rfc.get("txn_ids") else "period"),
                              "txn_ids": list(rfc.get("txn_ids") or []), "period": rfc.get("period")})
    ctx = _base(rt, recipient, f"{spec['name']} inbox", "inbox", flashes)
    ctx.update({"recipient": {"id": recipient, "name": spec["name"], "analyst_name": spec["analyst"]},
                "packages": packages, "requests_sent": requests_sent})
    return ctx


def _attachment_index(body: dict) -> dict[str, dict]:
    out = {}
    for att in body.get("attachments", []):
        ref = att.get("ref", {})
        out[f"{ref.get('ref_type')}:{ref.get('ref_id')}@{ref.get('revision')}"] = att
    return out


def _timing(body: dict, question_id: str, cited: set[str] | None = None) -> dict | None:
    """Settlement timing for one question, from the attachments its claim cites (all when
    ``cited`` is None): the release to the platform balance, the bank settlement and the credit."""
    if question_id == "q.sales_refunds_in_period":
        return None
    atts = body.get("attachments", [])
    if cited is not None:
        atts = [a for a in atts if f"{a.get('ref', {}).get('ref_type')}:{a.get('ref', {}).get('ref_id')}" in cited]
    contents = [a.get("content", {}) for a in atts]
    settlement = next((x for x in contents if x.get("kind") == "settlement"), None)
    release = next((x for x in contents if x.get("kind") == "payout"), None)
    credit = next((x for x in contents if "txn_id" in x), None)
    if settlement is None and release is None:
        return None
    settled_at = settlement.get("effective_at") if settlement and settlement.get("status") in SETTLED_STATUSES else None
    booked = credit.get("booked_at") if credit else None
    before = (settled_at <= booked) if settled_at and booked else None
    return {"payout_ref": (settlement or {}).get("payment_ref") or (release or {}).get("payment_ref") or "unknown",
            "released_to_balance_at": (release or {}).get("occurred_at"),
            "bank_settled_at": settled_at,
            "settlement_status": (settlement or {}).get("status"),
            "credit_booked_at": booked or "unknown", "settled_before_booking": before}


def _reconcile(body: dict, recipient_name: str) -> dict[str, Any]:
    """Verifier-side arithmetic from attached amounts only. With a bank credit attached: sales
    less refunds and fees against the credit. Otherwise (a period package) the attached records
    are summed with the signs the claim's calculation used and compared with the claimed total."""
    contents = [a.get("content", {}) for a in body.get("attachments", [])]
    labels = {"order": "Sales", "refund": "Refunds", "fee": "Fees", "receipt": "Purchases"}
    credit = next((x for x in contents if "txn_id" in x and isinstance(x.get("amount_pence"), int)), None)
    claim_calc = next((cl.get("calculation") for cl in body.get("claims", []) if cl.get("calculation")), None)
    if credit is not None:
        signs = {"order": 1, "refund": -1, "fee": -1}
        target, target_label = credit["amount_pence"], f"Bank credit {credit.get('txn_id')}"
    elif claim_calc:
        signs = {t["kind"]: t["sign"] for t in claim_calc.get("terms", []) if t.get("kind") in labels}
        target, target_label = claim_calc.get("total_pence"), f"Claimed total ({claim_calc.get('method')})"
    else:
        signs, target, target_label = {}, None, "nothing claimed"
    groups: dict[str, dict] = {}
    currency = (credit or claim_calc or {}).get("currency", "GBP")
    for x in contents:
        kind = x.get("kind")
        if kind in signs and isinstance(x.get("amount_pence"), int):
            g = groups.setdefault(kind, {"label": labels[kind], "kind": kind, "sign": "+" if signs[kind] > 0 else "-",
                                         "minor": 0, "source_ids": []})
            g["minor"] += x["amount_pence"]
            g["source_ids"].append(f"{x.get('record_id')} r{x.get('revision')}")
    order = [k for k in ("order", "refund", "fee", "receipt") if k in groups]
    total = sum(groups[k]["minor"] * (1 if groups[k]["sign"] == "+" else -1) for k in order)
    lines = [{"label": groups[k]["label"], "kind": k, "sign": groups[k]["sign"],
              "amount": c.money(groups[k]["minor"], currency), "source_ids": groups[k]["source_ids"]} for k in order]
    expression = " ".join((f"{groups[k]['minor']}" if i == 0 and groups[k]["sign"] == "+"
                           else f"{groups[k]['sign']} {groups[k]['minor']}") for i, k in enumerate(order))
    expression = f"{expression or '0'} = {total}" if lines else "nothing to reconcile: the package attaches no amounts"
    matches = target is not None and bool(lines) and total == target
    return {"title": "Reconciliation (recomputed by the verifier from the attached amounts)", "lines": lines,
            "expression": expression, "result": c.money(total, currency),
            "compared_to": {"label": target_label, "amount": c.money(target or 0, currency)},
            "difference": c.money(total - (target or 0), currency), "matches": matches,
            "computed_by": f"{recipient_name} verifier (code)", "computed_at": None}


def package_view(rt: Runtime, recipient: str, package_id: str, *, disposition_id: str | None = None,
                 flashes=None) -> dict[str, Any] | None:
    record = _packages(rt, recipient).get(package_id)
    if record is None:
        rt.bank_audit(recipient, action="access.refused", outcome="refused", target_id=package_id,
                      detail="no such package for this recipient")
        return None
    spec = RECIPIENTS[recipient]
    verification = record["verification"]
    auth = verification.get("authentication", {})
    accepted = record["accepted"]
    raw = (record.get("raw") or {}) if accepted else {}        # a rejected package shows nothing of its body
    body = raw.get("body") or {}
    rfc = _request_for(rt, recipient, record.get("request_id") or "")
    ctx = _base(rt, recipient, f"Package {package_id}", "inbox", flashes)
    extra = [] if rfc else [f"request {record.get('request_id')} was not issued by {recipient}"]
    period = body.get("scope", {}).get("period") if body else None
    ctx.update({
        "recipient": {"id": recipient, "name": spec["name"], "analyst_name": spec["analyst"]},
        "package": {"package_id": package_id, "request_id": record.get("request_id") or "unknown",
                    "recipient": spec["name"], "subject_ref_masked": c.mask_subject(rfc["subject_ref"]) if rfc else "—",
                    "period": period, "txn_ids": list(body.get("scope", {}).get("txn_ids") or []) if body else [],
                    "issued_at": raw.get("issued_at", "—"), "expires_at": body.get("expires_at", "—")},
        "authentication": {"status": _auth_status(verification, accepted), "received_at": record["received_at"],
                           "verified_at": record["received_at"],
                           "package_digest": f"sha256:{record['package_sha256']}",
                           "signer_key_id": auth.get("kid") or "none",
                           "checks": _checks(verification, extra),
                           "note": ("Authentication says the bytes are Alibi's, unaltered, for this recipient. "
                                    "It says nothing about whether any claim is true.")},
        "rejected": not accepted,
        "rejection": (None if accepted else {"reasons": list(record["receipt"]["reasons"]),
                                             "received_at": record["received_at"],
                                             "package_digest": f"sha256:{record['package_sha256']}"}),
    })
    if not accepted:
        ctx.update({"source_trust": [], "claim_status": [], "evidence_by_question": [],
                    "reconciliation": _reconcile({}, spec["name"]), "contradictions": [], "gaps": [],
                    "disposition_form": _disposition_form(rt, recipient, record, rfc, final=True),
                    "followup_form": _followup_form(rt, recipient, record, rfc, None)})
        return ctx
    atts = _attachment_index(body)
    trust_rows = []
    by_att: dict[str, dict] = {}
    for src in verification.get("sources", []):
        att = next((a for a in body.get("attachments", []) if a.get("attachment_id") == src["attachment_id"]), {})
        ref = src["ref"]
        row = {"attachment_id": src["attachment_id"], "record_id": ref["ref_id"], "revision": ref["revision"],
               "issuer": c.issuer_name(att.get("issuer_id")) if att.get("issuer_id") else
               ("Holder's bank (mock)" if ref["ref_type"] == "txn" else "not disclosed"),
               "trust": src["claimed_trust"],
               "signature_check": ("pass" if src.get("signature_valid") is True else
                                   "fail" if src.get("signature_valid") is False else "none"),
               "detail": src["note"], "attestation": src["attestation"],
               "redacted": src["form"] == "redacted_extract", "form": src["form"],
               "lower_trust": src["lower_trust"]}
        trust_rows.append(row)
        by_att[f"{ref['ref_type']}:{ref['ref_id']}@{ref['revision']}"] = row
    claim_results = {cl["question_id"]: cl for cl in verification.get("claims", [])}
    questions = {q["question_id"]: q["text"] for q in body.get("questions", [])}
    claim_status, evidence_by_question, contradictions, gaps = [], [], [], []
    for cl in body.get("claims", []):
        qid = cl["question_id"]
        claim_status.append({"question_id": qid, "status": cl["status"], "summary": cl["statement"]})
        evidence = []
        for ref in cl.get("evidence_refs", []):
            key = f"{ref['ref_type']}:{ref['ref_id']}@{ref['revision']}"
            row = by_att.get(key)
            att = atts.get(key, {})
            content = att.get("content", {})
            summary = (content.get("description") if ref["ref_type"] == "txn" else
                       f"{content.get('kind', ref['ref_type'])} · status {content.get('status', 'n/a')}")
            evidence.append({"attachment_id": row["attachment_id"] if row else "not attached",
                             "record_id": ref["ref_id"], "revision": ref["revision"],
                             "trust": row["trust"] if row else ("bank_observed" if ref["ref_type"] == "txn" else "unknown"),
                             "summary": summary or "", "href": f"#trust-{row['attachment_id']}" if row else "#source-trust",
                             "attestation": row["attestation"] if row else "none",
                             "redacted": row["redacted"] if row else False})
        calc = cl.get("calculation")
        calc_view = None
        if calc:
            valid = claim_results.get(qid, {}).get("calculation_valid")
            calc_view = {"expression": _calc_expression(calc), "result": c.money(calc["total_pence"], calc["currency"]),
                         "computed_by": f"{spec['name']} verifier", "matches": bool(valid) if valid is not None else False,
                         "verifier_check": ("pass" if valid else "not checked" if valid is None else "fail")}
        evidence_by_question.append({"question_id": qid, "question": questions.get(qid, qid),
                                     "claim": {"status": cl["status"], "rationale": cl["rationale"]},
                                     "evidence": evidence, "calculation": calc_view, "gap": cl.get("gap"),
                                     "timing": _timing(body, qid, {f"{r['ref_type']}:{r['ref_id']}"
                                                                   for r in cl.get("evidence_refs", [])})})
        if cl["status"] == "contradicted":
            contradictions.append({"claim_id": f"claim-{qid}", "question_id": qid,
                                   "description": cl.get("gap") or cl["rationale"],
                                   "sources": [{"record_id": e["record_id"], "revision": e["revision"], "trust": e["trust"]}
                                               for e in evidence]})
        if cl.get("gap"):
            gaps.append({"question_id": qid, "description": cl["gap"], "reason_code": c.gap_code(cl["gap"])})
    ctx.update({
        "source_trust": trust_rows, "claim_status": claim_status, "evidence_by_question": evidence_by_question,
        "reconciliation": {**_reconcile(body, spec["name"]), "computed_at": rt.now_text()},
        "contradictions": contradictions, "gaps": gaps,
        "disposition_form": _disposition_form(rt, recipient, record, rfc),
        "followup_form": _followup_form(rt, recipient, record, rfc, disposition_id),
    })
    return ctx


def _calc_expression(calc: dict) -> str:
    groups: dict[tuple[str, int], int] = {}
    for term in calc.get("terms", []):
        key = (term["kind"], term["sign"])
        groups[key] = groups.get(key, 0) + term["amount_pence"]
    ordered = sorted(groups.items(), key=lambda kv: (0 if kv[0][1] > 0 else 1, kv[0][0]))
    parts = []
    for i, ((kind, sign), amount) in enumerate(ordered):
        parts.append(f"{amount}" if i == 0 and sign > 0 else f"{'+' if sign > 0 else '-'} {amount}")
    return f"{' '.join(parts) or '0'} = {calc.get('total_pence')}"


def _disposition_form(rt: Runtime, recipient: str, record: dict, rfc: dict | None, *, final: bool = False) -> dict:
    spec = RECIPIENTS[recipient]
    rows = [d for d in _dispositions(rt, recipient) if d["package_id"] == record["package_id"]]
    current = rows[-1] if rows else None
    questions = [{"question_id": q["question_id"], "text": q["text"]} for q in (rfc or {}).get("questions", [])]
    return {"action": f"/bank/{recipient}/packages/{record['package_id']}/disposition",
            "analyst": spec["analyst"], "analyst_name": spec["analyst"],
            "options": [{"value": v, "label": l} for v, l in (("clear", "Clear"), ("escalate", "Escalate"),
                                                               ("request_more", "Request more"))],
            "questions": questions, "current": current, "history": rows[:-1],
            "final": final or bool(current and current["decision"] in TERMINAL)}


def _followup_form(rt: Runtime, recipient: str, record: dict, rfc: dict | None, disposition_id: str | None) -> dict:
    parent = rfc or {}
    disposition = None
    if disposition_id:
        disposition = next((d for d in _dispositions(rt, recipient) if d["disposition_id"] == disposition_id), None)
    prefill = ""
    if disposition and disposition.get("requested_evidence"):
        prefill = "Please provide: " + "; ".join(disposition["requested_evidence"]) + "."
    return {"action": f"/bank/{recipient}/requests", "parent_request_id": parent.get("request_id", record.get("request_id") or ""),
            "disposition_id": disposition_id,
            "subject_ref_masked": c.mask_subject(parent["subject_ref"]) if parent else "—",
            "scope": "txn_ids" if parent.get("txn_ids") else "period",
            "txn_ids": list(parent.get("txn_ids") or []), "period": parent.get("period"),
            "question_prefill": prefill,
            "consent_note": ("A follow-up goes through the bank connection as a new request with its own "
                             "request id. The holder must approve any new disclosure separately.")}


# --------------------------------------------------------------------------- disposition


def record_disposition(rt: Runtime, recipient: str, package_id: str, *, decision: str, reason: str,
                       question_ids: list[str], requested_evidence: list[str]) -> dict[str, Any]:
    spec = RECIPIENTS[recipient]
    record = _packages(rt, recipient).get(package_id)
    if record is None:
        raise Refusal("package_not_found", f"no package {package_id} for {recipient}", status=404)
    if not record["accepted"]:
        raise Refusal("package_rejected", "a rejected package cannot receive a disposition; it was never evidence")
    if decision not in ("clear", "escalate", "request_more"):
        raise Refusal("bad_decision", f"decision must be clear, escalate or request_more, not {decision!r}", status=400)
    if not reason.strip():
        raise Refusal("reason_required", "a short reason is required for every disposition", status=400)
    with rt.lock:
        previous = _latest_disposition(rt, recipient, package_id)
        if previous and previous["decision"] in TERMINAL:
            raise Refusal("disposition_final", f"package {package_id} already has a final disposition "
                                               f"({previous['decision']} by {previous['analyst']} at {previous['decided_at']})")
        now = rt.now_text()
        disp = {"disposition_id": canon.make_id("disp", recipient, package_id, decision, now),
                "package_id": package_id, "package_digest": f"sha256:{record['package_sha256']}",
                "request_id": record.get("request_id"), "recipient": recipient, "analyst": spec["analyst"],
                "decision": decision, "reason": reason.strip()[:1000],
                "question_ids": [q for q in question_ids if q], "requested_evidence": [e for e in requested_evidence if e],
                "decided_at": now, "supersedes": previous["disposition_id"] if previous else None,
                "followup_request_id": None}
        rt.sidecar.append(f"bank:{recipient}:dispositions", disp)
        rt.bank_audit(recipient, action="disposition.recorded", outcome="ok", target_id=package_id,
                      detail=f"{decision} by {spec['analyst']} on {record.get('request_id')}", actor=spec["analyst"])
    return disp


# --------------------------------------------------------------------------- follow-up


def _next_request_id(rt: Runtime, recipient: str, parent_id: str) -> str:
    known = {r["request_id"] for r in issued_requests(rt, recipient)}
    known |= {r.request_id for r in rt.store.list_requests(subject_ref=rt.subject_ref)}
    match = re.fullmatch(r"(.*)-(\d+)", parent_id)
    base, n = (match.group(1), int(match.group(2))) if match else (parent_id, 0)
    width = len(match.group(2)) if match else 2
    while True:
        n += 1
        candidate = f"{base}-{n:0{width}d}"
        if candidate not in known:
            return candidate


def create_followup(rt: Runtime, recipient: str, *, parent_request_id: str, scope: str | None,
                    txn_ids: list[str], period_from: str | None, period_to: str | None, question: str,
                    disposition_id: str | None = None) -> dict[str, Any]:
    """Issue a follow-up RFC through the bank connection. Scope defaults to the parent's and may
    not widen it; the holder's passport receives it through ``receive_rfc`` like any request."""
    parent = _request_for(rt, recipient, parent_request_id)
    if parent is None:
        raise Refusal("parent_not_found", f"{recipient} did not issue request {parent_request_id}", status=404)
    if not question.strip():
        raise Refusal("question_required", "a follow-up needs a question", status=400)
    parent_kind = "txn_ids" if parent.get("txn_ids") else "period"
    kind = scope or parent_kind
    body: dict[str, Any] = {}
    if kind == "txn_ids":
        wanted = [t.strip() for t in txn_ids if t.strip()] or list(parent.get("txn_ids") or [])
        if parent_kind != "txn_ids" or not set(wanted) <= set(parent.get("txn_ids") or []):
            raise Refusal("scope_wider_than_parent", f"follow-up scope must stay within {parent_request_id}; send a "
                                                     "new request to ask about a wider scope.")
        body["txn_ids"] = wanted
    else:
        p_from = (period_from or "").strip() or (parent.get("period") or {}).get("from")
        p_to = (period_to or "").strip() or (parent.get("period") or {}).get("to")
        if parent_kind != "period" or not p_from or not p_to:
            raise Refusal("scope_wider_than_parent", f"follow-up scope must stay within {parent_request_id}; send a "
                                                     "new request to ask about a wider scope.")
        try:
            period = Period.model_validate({"from": p_from, "to": p_to})
            parent_period = Period.model_validate(parent["period"])
        except Exception as exc:  # noqa: BLE001 - a malformed period is a 400
            raise Refusal("bad_period", "period must be two UTC ISO-8601 times with a timezone, such as "
                                        "2026-10-01T00:00:00Z, with from before to", status=400) from exc
        if period.from_ < parent_period.from_ or period.to > parent_period.to:
            raise Refusal("scope_wider_than_parent", f"follow-up scope must stay within {parent_request_id}; send a "
                                                     "new request to ask about a wider scope.")
        body["period"] = {"from": p_from, "to": p_to}
    with rt.lock:
        now = rt.clock()
        request_id = _next_request_id(rt, recipient, parent_request_id)
        n = len([f for f in _followups(rt, recipient) if f["request"].get("parent_request_id") == parent_request_id]) + 1
        rfc = {"issuer": recipient, "request_id": request_id, "parent_request_id": parent_request_id,
               "subject_ref": parent["subject_ref"], "audience": recipient,
               "questions": [{"question_id": f"q.follow_up_{n}", "text": question.strip()[:1000]}],
               "issued_at": format_utc(now), "expires_at": format_utc(now + FOLLOWUP_TTL), **body}
        try:
            stored = api.receive_rfc(rt.store, cases.sign_request(rfc, rt.keystore), authenticated_issuer=recipient,
                                     registry=rt.registry, account_refs=rt.account_refs if body.get("txn_ids") else None,
                                     now=now)
        except IngestError as exc:
            rt.bank_audit(recipient, action="rfc.refused", outcome="refused", target_id=request_id, detail=exc.reason)
            raise Refusal("rfc_refused", f"the holder's passport refused the follow-up: {exc.reason}") from exc
        rt.sidecar.append(f"bank:{recipient}:requests", {"request": rfc, "sent_at": format_utc(now),
                                                        "disposition_id": disposition_id})
        if disposition_id:
            def link(cur):
                return [{**d, "followup_request_id": request_id} if d["disposition_id"] == disposition_id else d
                        for d in (cur or [])]
            rt.sidecar.update(f"bank:{recipient}:dispositions", link)
        rt.bank_audit(recipient, action="rfc.sent", outcome="ok", target_id=request_id,
                      detail=f"follow-up to {parent_request_id}; scope {kind}; disposition {disposition_id or 'none'}")
    return {"request_id": stored.request_id, "parent_request_id": parent_request_id, "audience": recipient,
            "scope": kind, "txn_ids": body.get("txn_ids", []), "period": body.get("period"),
            "issued_at": rfc["issued_at"], "expires_at": rfc["expires_at"]}
