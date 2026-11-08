"""Template contexts for the holder screens and the audit page, built from the passport.

Every function returns a plain dict whose shape is documented in ``alibi/app/UI.md``. Values are
JSON-safe (strings, ints, bools, None, lists, dicts), so the same context is served as JSON when
the request asks for it. Nothing here writes to the store.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from alibi.app import common as c
from alibi.app.runtime import Runtime
from alibi.core import canon
from alibi.core.investigate_rules import reference_tokens
from alibi.core.reconcile import ReconcileError, reconcile_credit
from alibi.core.schemas import (
    ApprovalStatus,
    BankTxn,
    ContextPackage,
    Draft,
    DraftStatus,
    EvidenceRecord,
    EvidenceRef,
    RecordKind,
    RequestForContext,
    format_utc,
)
from alibi.core.store import NotFound
from alibi.gateway import DEFAULT_BUDGET

ROLE_NOTE = ("rules-only build: deterministic matching, lookup and arithmetic in code. The Gemini "
             "roles are not wired in this build and nothing is shown as an agent result.")
AGENT_ROLES = ("matcher", "detective", "challenger", "curator")


def base_context(rt: Runtime, page_title: str, area: str, nav: list[dict], actor: dict,
                 flashes: list[dict] | None = None) -> dict[str, Any]:
    return {
        "page_title": page_title, "area": area, "nav_items": nav, "actor": actor,
        "replay_mode": rt.settings.replay,
        "replay": ({"label": "Recorded run", "recorded_at": rt.now_text(),
                    "source": f"case {rt.settings.case_number} replay (ALIBI_REPLAY=1)"}
                   if rt.settings.replay else None),
        "flashes": flashes or [], "static_prefix": rt.settings.static_prefix,
        "now_utc": rt.now_text(),
    }


def holder_actor(rt: Runtime) -> dict[str, str]:
    return {"name": rt.settings.holder_name, "role": c.HOLDER_ROLE}


def _holder_base(rt: Runtime, title: str, nav_key: str, flashes=None) -> dict[str, Any]:
    return base_context(rt, title, "holder", c.holder_nav(nav_key), holder_actor(rt), flashes)


# --------------------------------------------------------------------------- passport home


def request_status(rt: Runtime, rfc: RequestForContext) -> str:
    store = rt.store
    if rt.clock() >= rfc.expires_at:
        return "expired"
    deliveries = [d for p in store.list_packages(request_id=rfc.request_id)
                  for d in store.list_deliveries(package_id=p.package_id)]
    if deliveries:
        latest = sorted(deliveries, key=lambda d: d.delivered_at)[-1]
        return "sent" if latest.status != "failed" else "refused"
    draft = c.current_draft(rt, rfc.request_id)
    if draft is not None:
        if DraftStatus(draft.status) == DraftStatus.APPROVED:
            return "approved"
        if DraftStatus(draft.status) == DraftStatus.PENDING:
            return "draft"
        if DraftStatus(draft.status) == DraftStatus.INVALIDATED:
            return "draft"
    last = rt.sidecar.get(f"dispatch:{rfc.request_id}")
    if last and last.get("outcome") == "refused":
        return "refused"
    if c.latest_run_claims(store, rfc.request_id):
        return "investigating"
    return "new"


def _request_row(rt: Runtime, rfc: RequestForContext) -> dict[str, Any]:
    return {"request_id": rfc.request_id, "recipient": rt.recipient_name(rfc.audience),
            "audience": rfc.audience, "parent_request_id": rfc.parent_request_id,
            "question_count": len(rfc.questions), "received_at": c.iso(rfc.issued_at),
            "expires_at": c.iso(rfc.expires_at), "status": request_status(rt, rfc),
            "href": f"/requests/{rfc.request_id}"}


def connections(rt: Runtime) -> list[dict[str, Any]]:
    store = rt.store
    records = store.list_sources(rt.subject_ref)
    txns = store.list_bank_txns(rt.subject_ref)
    out = []
    issuers: dict[str, list[EvidenceRecord]] = {}
    for r in records:
        if r.source_trust.value == "issuer_verified":
            issuers.setdefault(r.issuer_id, []).append(r)
    for issuer, rows in sorted(issuers.items()):
        out.append({"connection_id": f"conn-merchant-{issuer}", "name": c.issuer_name(issuer),
                    "kind": "merchant", "status": "connected", "trust": "issuer_verified",
                    "last_synced_at": c.iso(max(r.received_at for r in rows)),
                    "record_count": len(rows), "href": f"/passport#conn-merchant-{issuer}"})
    if txns:
        out.append({"connection_id": rt.connection_id, "name": "Bank A account feed (mock)",
                    "kind": "bank", "status": "connected", "trust": "bank_observed",
                    "last_synced_at": c.iso(max(t.provenance.retrieved_at for t in txns)),
                    "record_count": len(txns), "href": f"/passport#{rt.connection_id}"})
    own = [r for r in records if r.source_trust.value == "self_supplied"]
    if own:
        out.append({"connection_id": "conn-import", "name": "Holder imports", "kind": "import",
                    "status": "connected", "trust": "self_supplied",
                    "last_synced_at": c.iso(max(r.received_at for r in own)),
                    "record_count": len(own), "href": "/passport#conn-import"})
    return out


def txn_row(rt: Runtime, txn: BankTxn) -> dict[str, Any]:
    link = c.active_link(rt.store, txn.txn_id)
    state = link.link_state.value if link else "unmatched"
    count = len(link.candidates) if link else 0
    return {"txn_id": txn.txn_id, "revision": txn.revision, "booked_at": c.iso(txn.booked_at),
            "description": txn.description, "amount": c.money(txn.amount_pence, txn.currency),
            "direction": "credit" if txn.is_credit else "debit", "link_state": state,
            "linked_count": count, "href": f"/txn/{txn.txn_id}"}


def alert_row(rt: Runtime, alert) -> dict[str, Any]:
    return {"alert_id": alert.alert_id, "kind": alert.kind.value, "severity": alert.severity,
            "title": alert.title, "created_at": c.iso(alert.created_at),
            "status": "resolved" if alert.state != "open" else "open",
            "href": f"/alerts#{alert.alert_id}"}


def rules_rows(rt: Runtime) -> list[dict[str, Any]]:
    from alibi.app import actions
    out = []
    for rule in actions.active_rules(rt):
        out.append({"rule_id": rule["rule_id"], "text": rule["text"], "status": "active",
                    "summary": rule["summary"], "href": f"/rules#{rule['rule_id']}"})
    for held in actions.held_rules(rt):
        out.append({"rule_id": held["rule_id"], "text": held["text"], "status": "held",
                    "summary": held["reason"], "href": f"/rules#{held['rule_id']}"})
    return out


def sharing_history(rt: Runtime) -> list[dict[str, Any]]:
    store = rt.store
    rows = []
    for pkg in store.list_packages():
        deliveries = store.list_deliveries(package_id=pkg.package_id)
        delivery = deliveries[-1] if deliveries else None
        superseded = any(e.action == "evidence_superseded" for e in store.list_audit(target_id=pkg.package_id))
        if rt.clock() >= pkg.body.expires_at:
            status = "expired"
        elif superseded:
            status = "superseded"
        elif delivery is not None and delivery.status == "acknowledged":
            status = "acknowledged"
        elif delivery is not None and delivery.status == "failed":
            status = "failed"          # signed, but the recipient refused or was unreachable
        else:
            status = "delivered"
        rows.append({"package_id": pkg.package_id, "request_id": pkg.body.request_id,
                     "recipient": rt.recipient_name(pkg.body.audience), "audience": pkg.body.audience,
                     "sent_at": c.iso(delivery.delivered_at if delivery else pkg.issued_at),
                     "expires_at": c.iso(pkg.body.expires_at),
                     "item_count": len(pkg.body.attachments), "status": status,
                     "href": f"/requests/{pkg.body.request_id}/export"})
    rows.sort(key=lambda r: r["sent_at"] or "")
    return rows


def passport_home(rt: Runtime, flashes=None) -> dict[str, Any]:
    store = rt.store
    txns = store.list_bank_txns(rt.subject_ref)
    tx_rows = [txn_row(rt, t) for t in sorted(txns, key=lambda t: t.booked_at, reverse=True)]
    states = [r["link_state"] for r in tx_rows]
    histories = [r for r in store.list_sources(rt.subject_ref, active_only=False)]
    revisions: dict[str, int] = {}
    for r in histories:
        revisions[r.record_id] = revisions.get(r.record_id, 0) + 1
    alerts = sorted(store.list_alerts(rt.subject_ref), key=lambda a: (a.state != "open", a.created_at))
    requests = sorted(store.list_requests(subject_ref=rt.subject_ref), key=lambda r: r.issued_at)
    open_first = sorted(requests, key=lambda r: (request_status(rt, r) in ("sent", "expired"), r.issued_at))
    ctx = _holder_base(rt, "Passport", "home", flashes)
    ctx.update({
        "holder": {"name": rt.settings.holder_name, "subject_ref": rt.subject_ref},
        "connections": connections(rt),
        "transactions": tx_rows,
        "evidence_status": {"linked": states.count("linked"), "ambiguous": states.count("ambiguous"),
                            "unmatched": states.count("unmatched"), "total": len(tx_rows),
                            "last_checked_at": rt.sidecar.get("last_tick") or rt.now_text(),
                            "superseded_sources": sum(1 for n in revisions.values() if n > 1)},
        "alerts": [alert_row(rt, a) for a in alerts],
        "rules": rules_rows(rt),
        "requests": [_request_row(rt, r) for r in open_first],
        "sharing_history": sharing_history(rt),
        "case_number": rt.settings.case_number,
    })
    return ctx


# --------------------------------------------------------------------------- transaction detail


def _related_records(rt: Runtime, txn: BankTxn) -> list[EvidenceRecord]:
    store = rt.store
    found: dict[str, EvidenceRecord] = {}

    def add(ref: EvidenceRef | None):
        """Collect the ACTIVE revision of a cited record; history is shown from source_history."""
        if ref is None or ref.ref_type != "record":
            return
        try:
            rec = store.get_source(ref.ref_id)
        except NotFound:
            return
        found.setdefault(rec.record_id, rec)

    link = c.active_link(store, txn.txn_id)
    if link is not None:
        add(link.record_ref)
        for cand in link.candidates:
            add(cand)
    for rfc in store.list_requests(subject_ref=txn.subject_ref):
        if rfc.txn_ids and txn.txn_id in rfc.txn_ids:
            for claim in c.latest_run_claims(store, rfc.request_id):
                if any(r.ref_type == "txn" and r.ref_id == txn.txn_id for r in claim.evidence_refs):
                    for ref in claim.evidence_refs:
                        add(ref)
    tokens = reference_tokens(txn)
    if tokens:
        for rec in store.list_sources(txn.subject_ref):
            if (rec.payment_ref and rec.payment_ref.upper() in tokens) or \
                    (rec.settlement_ref and rec.settlement_ref.upper() in tokens):
                found.setdefault(rec.record_id, rec)
    return sorted(found.values(), key=lambda r: (r.occurred_at, r.record_id))


def _reconciliation(rt: Runtime, txn: BankTxn, records: list[EvidenceRecord]) -> dict[str, Any]:
    store = rt.store
    link = c.active_link(store, txn.txn_id)
    payout = None
    if link is not None and link.record_ref is not None:
        loaded = c.load_ref(store, link.record_ref)
        payout = loaded if isinstance(loaded, EvidenceRecord) and loaded.kind == RecordKind.PAYOUT else None
    settlement = next((r for r in records if r.kind == RecordKind.SETTLEMENT and payout is not None
                       and r.payment_ref == payout.payment_ref), None)
    compared = {"label": f"Bank credit {txn.txn_id}" if txn.is_credit else f"Bank debit {txn.txn_id}",
                "amount": c.money(txn.amount_pence, txn.currency)}
    base = {"title": "Reconciliation", "compared_to": compared,
            "computed_by": "code (alibi.core.reconcile)", "computed_at": rt.now_text()}
    if payout is None or not txn.is_credit:
        return {**base, "lines": [], "result": c.money(0, txn.currency),
                "difference": c.money(-txn.amount_pence, txn.currency), "matches": False,
                "expression": "no linked payout to reconcile against this transaction"}
    components = []
    for rid in payout.related_record_ids:
        try:
            rec = store.get_source(rid)
        except NotFound:
            continue
        if rec.kind in (RecordKind.ORDER, RecordKind.REFUND, RecordKind.FEE):
            components.append(rec)
    try:
        calc = reconcile_credit(txn, components=components, payout=payout, settlement=settlement)
    except ReconcileError as exc:
        return {**base, "lines": [], "result": c.money(0, txn.currency),
                "difference": c.money(-txn.amount_pence, txn.currency), "matches": False,
                "expression": f"could not reconcile: {exc}"}
    view = c.calculation_view(calc, computed_at=rt.clock())
    return {**base, "lines": view["terms"], "result": view["result"],
            "difference": c.money(calc.difference_pence or 0, calc.currency),
            "matches": bool(calc.balanced), "expression": view["expression"]}


def _changes(rt: Runtime, records: list[EvidenceRecord], txn: BankTxn | None) -> list[dict[str, Any]]:
    store = rt.store
    skip = {"payload_sha256", "received_at", "issuer_signature", "signed_body", "connector",
            "schema_version", "source_trust", "subject_ref"}
    out = []

    def fields(obj) -> list[dict[str, str]]:
        data = obj.to_json_dict()
        return [{"name": k, "value": c.compact(v, 200)} for k, v in sorted(data.items()) if k not in skip]

    def diff(older, newer):
        a = {f["name"]: f["value"] for f in fields(older)}
        b = {f["name"]: f["value"] for f in fields(newer)}
        return [{"field": k, "before": a.get(k, "—"), "after": b.get(k, "—")}
                for k in sorted(set(a) | set(b)) if a.get(k) != b.get(k)]

    histories = [store.source_history(r.record_id) for r in records]
    if txn is not None:
        histories.append(store.txn_history(txn.txn_id))
    for history in histories:
        for older, newer in zip(history, history[1:]):
            is_rec = isinstance(newer, EvidenceRecord)
            ident = newer.record_id if is_rec else newer.txn_id
            kind = newer.kind.value if is_rec else "bank_txn"
            received = newer.received_at if is_rec else newer.provenance.retrieved_at
            reason = getattr(newer, "correction_reason", None)
            token = f"{'record' if is_rec else 'txn'}:{ident}"
            invalidated = [e for e in store.list_audit() if e.action == "evidence_superseded"
                           and e.detail and token in e.detail]
            effect = (f"Revision r{newer.revision} is active; r{older.revision} is kept for history."
                      + (f" Issuer's reason: {reason}" if reason else "")
                      + (f" {len(invalidated)} delivered package(s) marked superseded." if invalidated else ""))
            base_href = f"/sources/{ident}" if is_rec else f"/txn/{ident}"
            out.append({
                "change_id": f"chg-{ident}-r{newer.revision}", "received_at": c.iso(received),
                "record_id": ident, "kind": kind, "effect": effect,
                "diff": diff(older, newer),
                "older": {"revision": older.revision,
                          "received_at": c.iso(older.received_at if is_rec else older.provenance.retrieved_at),
                          "href": f"{base_href}/{older.revision}", "fields": fields(older)},
                "newer": {"revision": newer.revision, "received_at": c.iso(received),
                          "href": f"{base_href}/{newer.revision}", "fields": fields(newer)},
            })
    out.sort(key=lambda ch: ch["received_at"] or "")
    return out


def transaction_detail(rt: Runtime, txn_id: str, flashes=None) -> dict[str, Any] | None:
    store = rt.store
    try:
        txn = store.get_bank_txn(txn_id)
    except NotFound:
        return None
    if txn.subject_ref != rt.subject_ref:
        return None
    records = _related_records(rt, txn)
    sources = []
    for rec in records:
        history = store.source_history(rec.record_id)
        for older in history:
            if older.revision < rec.revision:
                sources.append(c.source_item(older, superseded=True))
        sources.append(c.source_item(rec))
    claims = []
    for rfc in store.list_requests(subject_ref=txn.subject_ref):
        for claim in c.latest_run_claims(store, rfc.request_id):
            if any(r.ref_type == "txn" and r.ref_id == txn.txn_id for r in claim.evidence_refs):
                state = str(getattr(claim.state, "value", claim.state))
                stale_note = f"INVALIDATED ({claim.invalidated_reason}). " if state != "active" else ""
                claims.append({"claim_id": claim.claim_id, "question_id": claim.question_id,
                               "question": c.question_text(rfc, claim.question_id),
                               "status": claim.status.value, "rationale": stale_note + claim.rationale,
                               "state": state,
                               "request_id": rfc.request_id,
                               "href": f"/requests/{rfc.request_id}#trc_{claim.question_id}"})
    row = txn_row(rt, txn)
    ctx = _holder_base(rt, f"Transaction {txn.txn_id}", "home", flashes)
    ctx.update({
        "txn": {**row, "account_label": f"Account {txn.account_ref}",
                "connection_name": "Bank A account feed (mock)", "references": list(txn.refs),
                "provenance": {"connection_id": txn.provenance.connection_id,
                               "retrieved_at": c.iso(txn.provenance.retrieved_at),
                               "source_version": txn.provenance.source_version},
                "trust": "bank_observed"},
        "sources": sources,
        "reconciliation": _reconciliation(rt, txn, records),
        "claims": claims,
        "changes": _changes(rt, records, txn),
    })
    return ctx


def source_detail(rt: Runtime, record_id: str, revision: int | None) -> dict[str, Any] | None:
    """JSON-only view of one record revision (no template of its own in S4a)."""
    try:
        rec = rt.store.get_source(record_id, revision)
    except NotFound:
        return None
    if rec.subject_ref != rt.subject_ref:
        return None
    history = rt.store.source_history(record_id)
    item = c.source_item(rec, superseded=rec.revision < history[-1].revision)
    fields = {k: v for k, v in rec.to_json_dict().items()
              if k not in ("payload_sha256", "signed_body", "issuer_signature")}
    return {"version": "alibi-ui/1", "record": item, "fields": fields,
            "revisions": [r.revision for r in history]}


# --------------------------------------------------------------------------- alerts


def alerts_page(rt: Runtime, query: str = "", result: dict | None = None, flashes=None) -> dict[str, Any]:
    store = rt.store
    out = []
    for alert in sorted(store.list_alerts(rt.subject_ref), key=lambda a: (a.state != "open", a.created_at)):
        facts = []
        txn_href = "/passport#transactions"
        for ref in alert.refs:
            item = c.load_ref(store, ref)
            if isinstance(item, BankTxn):
                txn_href = f"/txn/{item.txn_id}"
                label, value = "Bank transaction", (f"{item.description} · {item.amount_pence} {item.currency} "
                                                    f"booked {format_utc(item.booked_at)}")
                href = f"/txn/{item.txn_id}"
            elif isinstance(item, EvidenceRecord):
                label = f"{c.KIND_LABELS.get(item.kind.value, item.kind.value)} record"
                value = c.record_summary(item)
                href = f"/sources/{item.record_id}/{item.revision}"
            else:
                label, value, href = ref.ref_type, ref.key(), "#"
            facts.append({"label": label, "value": value, "source_id": ref.ref_id,
                          "source_revision": ref.revision, "href": href})
        out.append({"alert_id": alert.alert_id, "kind": alert.kind.value, "severity": alert.severity,
                    "title": alert.title, "explanation": alert.explanation,
                    "created_at": c.iso(alert.created_at),
                    "status": "resolved" if alert.state != "open" else "open",
                    "txn_href": txn_href, "facts": facts,
                    "suggested_checks": list(alert.suggested_evidence)})
    ctx = _holder_base(rt, "Alerts", "alerts", flashes)
    ctx.update({"alerts": out,
                "merchant_lookup": {"action": "/alerts/lookup", "query": query, "result": result},
                "tick_action": "/tick", "last_tick": rt.sidecar.get("last_tick")})
    return ctx


# --------------------------------------------------------------------------- rules


def rules_page(rt: Runtime, interpretation: dict | None = None, prefill: str = "",
               flashes=None) -> dict[str, Any]:
    from alibi.app import actions
    ctx = _holder_base(rt, "Privacy rules", "rules", flashes)
    ctx.update({
        "rule_form": {"action": "/rules/interpret", "text": prefill,
                      "examples": ["don't share item names", "nothing older than 90 days",
                                   "only share with Bank A", "don't share refunds",
                                   "show dates to the day only"]},
        "interpretation": interpretation,
        "active_rules": [{**r, "retire_action": f"/rules/{r['rule_id']}/retire"}
                         for r in actions.active_rules(rt)],
        "held_rules": [{**h, "clarify_action": f"/rules/held/{h['rule_id']}/clarify",
                        "discard_action": f"/rules/held/{h['rule_id']}/discard"}
                       for h in actions.held_rules(rt)],
    })
    return ctx


# --------------------------------------------------------------------------- request detail


def _trace(rt: Runtime, rfc: RequestForContext) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    store = rt.store
    claims = c.latest_run_claims(store, rfc.request_id)
    if not claims:
        return [], {"run_id": None, "events": []}
    run_id = claims[0].run_id
    events = store.list_traces(run_id)
    by_question: dict[str, list] = {q.question_id: [] for q in rfc.questions}
    current = None
    for ev in events:
        if ev.question_id:
            current = ev.question_id
        if current is not None:
            by_question.setdefault(current, []).append(ev)
    steps = []
    for claim in claims:
        evs = by_question.get(claim.question_id, [])
        lookups = []
        lookup_refs: list[set[str]] = []
        for ev in evs:
            if ev.step in ("tool_call", "refusal") and ev.actor == "gateway" and ev.tool:
                lookups.append({"tool": ev.tool, "args_summary": c.compact(ev.args, 100),
                                "result_summary": (c.compact(ev.result, 140) if ev.outcome == "ok"
                                                   else f"{ev.outcome}: {ev.reason or ''}"),
                                "at": c.iso(ev.at), "outcome": ev.outcome})
                lookup_refs.append({r.key() for r in ev.refs})
        sources = []
        for ref in claim.evidence_refs:
            item = c.load_ref(store, ref)
            via = None
            if isinstance(item, EvidenceRecord) and item.connector == "merchant_lookup":
                for idx, (lk, refs) in enumerate(zip(lookups, lookup_refs)):
                    if lk["tool"] == "lookup_settlement" and ref.key() in refs:
                        via = idx
                        break
            sources.append({"record_id": ref.ref_id, "revision": ref.revision,
                            "ref_type": ref.ref_type, "trust": c.trust_of(store, ref),
                            "href": (f"/sources/{ref.ref_id}/{ref.revision}" if ref.ref_type == "record"
                                     else f"/txn/{ref.ref_id}"), "via_lookup": via})
        status_ev = next((ev for ev in reversed(evs) if ev.step == "status_change"), None)
        steps.append({
            "step_id": f"trc_{claim.question_id}", "question_id": claim.question_id,
            "question": c.question_text(rfc, claim.question_id),
            "claim": {"claim_id": claim.claim_id, "text": claim.statement, "status": claim.status.value},
            "sources": sources, "lookups": lookups,
            "calculation": c.calculation_view(claim.calculation, computed_at=claim.created_at),
            "status": claim.status.value,
            "status_reason": ((f"INVALIDATED ({claim.invalidated_reason}); was: "
                               if str(getattr(claim.state, "value", claim.state)) != "active" else "")
                              + (status_ev.reason if status_ev and status_ev.reason else claim.rationale))[:300],
            "state": str(getattr(claim.state, "value", claim.state)),
            "rationale": claim.rationale, "gap": c.gap_text(claim),
            "at": c.iso(status_ev.at if status_ev else claim.created_at),
        })
    calls = sum(1 for ev in events if ev.actor == "gateway" and ev.step == "tool_call" and ev.outcome == "ok")
    meta = {"run_id": run_id, "events": events, "calls_used": calls,
            "started_at": c.iso(events[0].at) if events else None,
            "completed_at": c.iso(events[-1].at) if events else None}
    return steps, meta


def _approval_view(rt: Runtime, rfc: RequestForContext, draft: Draft | None) -> dict[str, Any]:
    store = rt.store
    view = {"status": "none", "recipient": rt.recipient_name(rfc.audience), "audience": rfc.audience,
            "action": f"/requests/{rfc.request_id}/approve",
            "dispatch_action": f"/requests/{rfc.request_id}/dispatch",
            "digest": "", "policy_version": "",
            "valid_from": c.iso(rfc.issued_at), "valid_until": c.iso(rfc.expires_at),
            "approved_at": None, "invalidated_reason": None, "approval_id": None}
    if draft is None:
        return view
    view.update({"digest": f"sha256:{draft.body_sha256}", "policy_version": draft.policy_version,
                 "valid_from": c.iso(draft.created_at), "valid_until": c.iso(draft.body.expires_at),
                 "status": "pending"})
    approvals = [a for a in store.list_approvals(request_id=rfc.request_id) if a.draft_id == draft.draft_id]
    if approvals:
        latest = max(approvals, key=lambda a: (a.approved_at, a.approval_id))
        status = ApprovalStatus(latest.status)
        view.update({"approval_id": latest.approval_id, "approved_at": c.iso(latest.approved_at),
                     "valid_from": c.iso(latest.approved_at), "valid_until": c.iso(latest.expires_at),
                     "status": "invalidated" if status == ApprovalStatus.INVALIDATED else "approved",
                     "invalidated_reason": latest.invalidated_reason})
    elif DraftStatus(draft.status) == DraftStatus.INVALIDATED:
        view.update({"status": "invalidated", "invalidated_reason": draft.invalidated_reason})
    return view


def _review(rt: Runtime, rfc: RequestForContext, draft: Draft | None) -> dict[str, Any]:
    store = rt.store
    rid = rfc.request_id
    empty = {"claims": [], "included": [], "excluded": [], "generalised": [], "gaps": [],
             "withheld_fields": [], "claim_changes": [],
             "counts": {"included": 0, "excluded": 0, "generalised": 0, "gaps": 0},
             "approval": _approval_view(rt, rfc, None), "preview_href": f"/requests/{rid}/export",
             "draft_id": None, "draft_status": None}
    if draft is None:
        return empty
    body, review = draft.body, draft.review

    def label_for(ref: EvidenceRef) -> tuple[str, str, str]:
        item = c.load_ref(store, ref)
        if isinstance(item, EvidenceRecord):
            return (f"{c.KIND_LABELS.get(item.kind.value, item.kind.value)} {ref.ref_id} r{ref.revision}",
                    item.kind.value, item.source_trust.value)
        if isinstance(item, BankTxn):
            return f"bank transaction {ref.ref_id} r{ref.revision}", "bank_txn", "bank_observed"
        return f"{ref.ref_type} {ref.ref_id} r{ref.revision}", ref.ref_type, "unknown"

    def questions_in(reason: str) -> str:
        marker = "cited by "
        return reason.split(marker, 1)[1] if marker in reason else ", ".join(q.question_id for q in rfc.questions)

    included = []
    for entry in review.included:
        label, kind, trust = label_for(entry.ref)
        included.append({"item_id": f"inc-{entry.ref.ref_type}-{entry.ref.ref_id}",
                         "question_id": questions_in(entry.reason), "label": label, "kind": kind,
                         "record_id": entry.ref.ref_id, "revision": entry.ref.revision, "trust": trust,
                         "reason": entry.reason, "removable": True,
                         "remove_action": f"/requests/{rid}/remove/{entry.ref.ref_type}/{entry.ref.ref_id}"})
    excluded = [{"item_id": f"exc-{e.ref.ref_type}-{e.ref.ref_id}", "label": label_for(e.ref)[0],
                 "record_id": e.ref.ref_id, "revision": e.ref.revision,
                 "reason": e.reason, "rule_id": e.rule_id} for e in review.excluded]
    generalised = [{"item_id": f"gen-{e.ref.ref_type}-{e.ref.ref_id}-{(e.field or 'field').replace('.', '-')}",
                    "label": label_for(e.ref)[0], "original_summary": e.field or "field",
                    "generalised_as": e.reason, "rule_id": e.rule_id or "allowlist"} for e in review.generalised]
    gaps = [{"question_id": g.question_id, "description": g.text, "reason_code": c.gap_code(g.text)}
            for g in review.gaps]
    claims = [{"claim_id": f"pkg-{cl.question_id}", "question_id": cl.question_id,
               "question": c.question_text(rfc, cl.question_id), "status": cl.status.value,
               "rationale": cl.rationale, "href": f"#trc_{cl.question_id}"} for cl in body.claims]
    return {
        "claims": claims, "included": included, "excluded": excluded, "generalised": generalised,
        "gaps": gaps,
        "withheld_fields": [{"record_id": e.ref.ref_id, "revision": e.ref.revision, "field": e.field,
                             "reason": e.reason, "rule_id": e.rule_id} for e in review.withheld_fields],
        "claim_changes": [{"question_id": ch.question_id, "from_status": ch.from_status.value,
                           "to_status": ch.to_status.value, "reason": ch.reason} for ch in review.claim_changes],
        "counts": {"included": len(included), "excluded": len(excluded),
                   "generalised": len(generalised), "gaps": len(gaps)},
        "approval": _approval_view(rt, rfc, draft), "preview_href": f"/requests/{rid}/export",
        "draft_id": draft.draft_id, "draft_status": DraftStatus(draft.status).value,
    }


def request_detail(rt: Runtime, request_id: str, flashes=None) -> dict[str, Any] | None:
    store = rt.store
    try:
        rfc = store.get_request(request_id)
    except NotFound:
        return None
    if rfc.subject_ref != rt.subject_ref:
        return None
    steps, meta = _trace(rt, rfc)
    draft = c.current_draft(rt, request_id)
    # The outcome receive_rfc produced at ingest time (recorded by the adapter and the bank service).
    signature = (rt.sidecar.get("rfc_signatures") or {}).get(request_id) or {"verified": False, "key_id": "none"}
    status = request_status(rt, rfc)
    ran = bool(steps)
    last_run = rt.sidecar.get(f"run:{request_id}") or {}
    stale = [cl for cl in c.latest_run_claims(store, request_id) if str(getattr(cl.state, "value", cl.state)) != "active"]
    inv_status = ("failed" if last_run.get("failed") else "out_of_date" if stale
                  else "complete" if ran else "not_started")
    if stale:
        reasons = sorted({cl.invalidated_reason or "newer evidence arrived" for cl in stale})
        flashes = [*(flashes or []), {"kind": "info", "text": "The investigation is out of date: "
                                     + "; ".join(reasons) + ". Run it again before approving."}]
    mode = last_run.get("mode") or rt.settings.mode
    if mode == "agents":
        actors = [ev.actor for ev in events]
        stats = last_run.get("stats") or {}
        replay_note = last_run.get("replay") or ""
        roles = [{"name": role, "status": ("complete" if role in actors else "not_called") if ran else "not_started",
                  "calls": actors.count(role) if ran else 0,
                  "note": "Gemini role; the backend validated every reference and computed the verdict in code"}
                 for role in AGENT_ROLES]
        roles.append({"name": "code verdict", "status": "complete" if ran else "not_started",
                      "calls": stats.get("model_calls", 0) if ran else 0,
                      "note": (f"model calls {stats.get('model_calls', 0)}, lookups {stats.get('lookups', 0)}, "
                               f"tokens {stats.get('tokens', 0)}" + (f"; {replay_note}" if replay_note else ""))
                      if ran else ROLE_NOTE})
        mode_label = "agents (Gemini)" + (" - replay" if "REPLAY" in str(replay_note) else "")
    else:
        roles = [{"name": "rules", "status": "complete" if ran else "not_started",
                  "calls": meta.get("calls_used", 0) if ran else 0, "note": ROLE_NOTE}]
        roles += [{"name": role, "status": "skipped", "calls": 0,
                   "note": "not run in this mode (rules-only; start the app with ALIBI_MODE=agents)"} for role in AGENT_ROLES]
        mode_label = "rules-only" if mode == "rules" else mode
    ctx = _holder_base(rt, f"Request {rfc.request_id}", "requests", flashes)
    ctx.update({
        "request": {"request_id": rfc.request_id, "parent_request_id": rfc.parent_request_id,
                    "issuer": rt.recipient_name(rfc.issuer), "audience": rfc.audience,
                    "subject_ref": rfc.subject_ref, "received_at": c.iso(rfc.issued_at),
                    "expires_at": c.iso(rfc.expires_at),
                    "period": ({"from": c.iso(rfc.period.from_), "to": c.iso(rfc.period.to)}
                               if rfc.period else None),
                    "txn_ids": list(rfc.txn_ids or []),
                    "questions": [{"question_id": q.question_id, "text": q.text} for q in rfc.questions],
                    "status": status, "signature": {"verified": bool(signature.get("verified")),
                                                    "key_id": signature.get("key_id") or "none"},
                    "href": f"/requests/{rfc.request_id}"},
        "investigation": {"status": inv_status,
                          "can_run": status != "expired" and (status != "sent" or bool(stale)),
                          "run_action": f"/requests/{rfc.request_id}/run",
                          "started_at": meta.get("started_at"), "completed_at": meta.get("completed_at"),
                          "budget": {"calls_used": meta.get("calls_used", 0), "calls_cap": DEFAULT_BUDGET},
                          "roles": roles, "failure_reason": last_run.get("failure_reason"),
                          "mode": mode, "mode_label": mode_label, "run_id": meta.get("run_id")},
        "trace": steps,
        "review": _review(rt, rfc, draft),
    })
    return ctx


def requests_index(rt: Runtime, flashes=None) -> dict[str, Any]:
    ctx = _holder_base(rt, "Requests", "requests", flashes)
    requests = sorted(rt.store.list_requests(subject_ref=rt.subject_ref), key=lambda r: r.issued_at)
    ctx["requests"] = [_request_row(rt, r) for r in requests]
    return ctx


# --------------------------------------------------------------------------- export


def latest_package(rt: Runtime, request_id: str) -> ContextPackage | None:
    packages = rt.store.list_packages(request_id=request_id)
    return max(packages, key=lambda p: (p.issued_at, p.package_id)) if packages else None


def _attachment_rows(rt: Runtime, body) -> list[dict[str, Any]]:
    rows = []
    for att in body.attachments:
        content = att.content or {}
        kind = content.get("kind") or ("bank_txn" if att.ref.ref_type == "txn" else "record")
        # The summary comes from the disclosed content only, never from the passport record: a
        # withheld field (such as a reference to an excluded record) must not leak into the view.
        if att.ref.ref_type == "txn":
            summary = (f"{content.get('description', 'bank transaction')} · "
                       f"{content.get('amount_pence', '?')} {content.get('currency', '')}").strip()
        else:
            bits = [str(kind)]
            if content.get("status"):
                bits.append(f"status {content['status']}")
            if content.get("amount_pence") is not None:
                bits.append(f"{content['amount_pence']} {content.get('currency', '')}".strip())
            if content.get("payment_ref"):
                bits.append(f"reference {content['payment_ref']}")
            if content.get("effective_at"):
                bits.append(f"effective {content['effective_at']}")
            summary = " · ".join(bits)
        rows.append({"attachment_id": att.attachment_id, "kind": kind, "record_id": att.ref.ref_id,
                     "revision": att.ref.revision, "ref_type": att.ref.ref_type,
                     "issuer": c.issuer_name(att.issuer_id) if att.issuer_id else
                     ("Holder's bank (mock)" if att.ref.ref_type == "txn" else "not disclosed"),
                     "trust": att.source_trust.value,
                     "attestation": "issuer_signature" if att.issuer_signature is not None else "package_signer",
                     "redacted": att.form == "redacted_extract", "summary": summary,
                     "generalised_fields": list(att.generalised_fields)})
    return rows


def export_page(rt: Runtime, request_id: str, flashes=None) -> dict[str, Any] | None:
    store = rt.store
    try:
        rfc = store.get_request(request_id)
    except NotFound:
        return None
    pkg = latest_package(rt, request_id)
    draft = c.current_draft(rt, request_id)
    if pkg is None and draft is None:
        return None
    body = pkg.body if pkg is not None else draft.body
    signed = pkg is not None
    raw = canon.canonical_bytes(pkg.to_json_dict()) if signed else canon.canonical_bytes(body.to_json_dict())
    trust_by_ref = {att.ref.key(): att.source_trust.value for att in body.attachments}
    claims = []
    for cl in body.claims:
        claims.append({"question_id": cl.question_id, "question": c.question_text(rfc, cl.question_id),
                       "status": cl.status.value, "rationale": cl.rationale, "statement": cl.statement,
                       "evidence_refs": [{"record_id": r.ref_id, "revision": r.revision,
                                          "trust": trust_by_ref.get(r.key(), c.trust_of(store, r))}
                                         for r in cl.evidence_refs],
                       "calculation": ({"expression": c.expression_for(cl.calculation),
                                        "result": c.money(cl.calculation.total_pence, cl.calculation.currency)}
                                       if cl.calculation else None),
                       "gap": c.gap_text(cl)})
    flashes = list(flashes or [])
    if not signed:
        flashes.append({"kind": "info", "text": "No signed package exists for this request yet. This is "
                                                "the current draft body as the bank would see it; it is "
                                                "unsigned until you approve and dispatch it."})
    ctx = _holder_base(rt, f"Package {pkg.package_id if signed else 'draft'}", "requests", flashes)
    ctx.update({
        "package": {
            "package_id": pkg.package_id if signed else f"draft {draft.draft_id}",
            "request_id": body.request_id, "version": body.schema_version, "audience": body.audience,
            "recipient": rt.recipient_name(body.audience), "case_id": body.case_ref,
            "subject_ref_masked": c.mask_subject(rfc.subject_ref),
            "period": ({"from": c.iso(body.scope.period.from_), "to": c.iso(body.scope.period.to)}
                       if body.scope.period else None),
            "txn_ids": list(body.scope.txn_ids or []),
            "issued_at": c.iso(pkg.issued_at) if signed else "not issued",
            "expires_at": c.iso(body.expires_at),
            "digest": f"sha256:{pkg.body_sha256 if signed else draft.body_sha256}",
            "signature": ({"alg": pkg.signature.alg, "key_id": pkg.signature.kid,
                           "domain": pkg.signature.domain.value, "value": pkg.signature.sig} if signed
                          else {"alg": "none", "key_id": "none", "domain": "none",
                                "value": "unsigned draft; approve and dispatch to sign"}),
            "claims": claims, "attachments": _attachment_rows(rt, body), "signed": signed,
        },
        "export": {
            "json_href": f"/requests/{request_id}/export.json",
            "json_url": absolute_url(rt, f"/requests/{request_id}/export.json"),
            "json_filename": f"{pkg.package_id if signed else 'draft-' + request_id}.json",
            "json_size_bytes": len(raw),
            "verify_hint": (f"uv run --frozen python -m agents.eval.verify_package "
                            f"{pkg.package_id if signed else '<package>'}.json --registry "
                            f"{rt.settings.key_dir.as_posix()}/trust_registry.json --recipient {body.audience}"),
            "approval": _approval_view(rt, rfc, draft),
        },
    })
    return ctx


def export_json(rt: Runtime, request_id: str) -> tuple[bytes, str] | None:
    pkg = latest_package(rt, request_id)
    if pkg is None:
        return None
    return canon.canonical_bytes(pkg.to_json_dict()), f"{pkg.package_id}.json"


# --------------------------------------------------------------------------- dispatch


def dispatch_page(rt: Runtime, request_id: str, flashes=None) -> dict[str, Any] | None:
    try:
        rfc = rt.store.get_request(request_id)
    except NotFound:
        return None
    last = rt.sidecar.get(f"dispatch:{request_id}")
    send_action: str | None = f"/requests/{request_id}/dispatch"
    if not last:
        # Nothing has been attempted: a neutral state, never a refusal. The Send button is offered
        # only when a usable approval exists; before that the review page is the way forward.
        approvals = [a for a in rt.store.list_approvals(request_id=request_id)
                     if ApprovalStatus(a.status) in (ApprovalStatus.PENDING, ApprovalStatus.CONSUMED)]
        send_action = send_action if approvals else None
        last = {"outcome": "not_attempted", "package_id": "none", "request_id": request_id,
                "attempted_at": None, "receipt": None, "refusal": None,
                "note": ("Not yet sent. No dispatch has been attempted for this request"
                         + ("; the approved body is ready to send." if approvals
                            else "; approve the reviewed body first."))}
    ctx = _holder_base(rt, "Dispatch result", "requests", flashes)
    ctx["dispatch"] = {**last, "recipient": rt.recipient_name(rfc.audience), "audience": rfc.audience,
                       "retry_action": send_action,
                       "review_href": f"/requests/{request_id}#review",
                       "history_href": "/passport#sharing",
                       **dispatch_urls(rt, request_id, rfc.audience, last)}
    return ctx


def absolute_url(rt: Runtime, path: str) -> str:
    """``path`` on this backend's published origin (``ALIBI_API_ORIGIN``), for a server-side
    front end that follows links the app hands out."""
    return f"{rt.settings.api_origin.rstrip('/')}{path}"


def dispatch_urls(rt: Runtime, request_id: str, audience: str, result: dict) -> dict[str, str | None]:
    """Absolute links for a dispatch result: the recipient's view of the delivered package and the
    signed JSON. Both are None until a package was delivered."""
    delivered = result.get("outcome") == "success" and result.get("package_id") not in (None, "none", "pending")
    return {"package_url": absolute_url(rt, f"/bank/{audience}/packages/{result['package_id']}") if delivered else None,
            "export_url": absolute_url(rt, f"/requests/{request_id}/export.json") if delivered else None}


# --------------------------------------------------------------------------- audit


ROLE_BY_ACTOR = {"holder": "holder", "alibi": "backend", "rules": "investigation", "gateway": "investigation",
                 "merchant_webhook": "connector", "merchant_lookup": "connector", "bank_feed": "connector",
                 "bank_rfc": "bank connection", "store": "store", "app": "app", "bank_a": "bank",
                 "bank_b": "bank"}


def audit_entries(rt: Runtime) -> list[dict[str, Any]]:
    """The holder's audit: the passport's own events only. Bank-side entries (receipts,
    dispositions, refused access) stay with the bank; a disposition is never shown to the holder."""
    rows = []
    for ev in rt.store.list_audit():
        target = ev.target_id
        is_request = bool(target) and target.startswith("rfc")
        rows.append({"entry_id": ev.audit_id, "at": c.iso(ev.at), "actor": ev.actor,
                     "role": ROLE_BY_ACTOR.get(ev.actor, "holder" if ev.actor.startswith("holder") else "system"),
                     "action": ev.action, "request_id": target if is_request else None,
                     "event_id": None if is_request else target, "outcome": ev.outcome,
                     "detail": ev.detail or ""})
    rows.sort(key=lambda r: (r["at"] or "", r["entry_id"]), reverse=True)
    return rows


def audit_page(rt: Runtime, q: str = "", actor: str = "", page: int = 1, size: int = 50,
               flashes=None) -> dict[str, Any]:
    rows = audit_entries(rt)
    actors = sorted({r["actor"] for r in rows})
    if actor:
        rows = [r for r in rows if r["actor"] == actor]
    if q:
        needle = q.lower()
        rows = [r for r in rows if needle in " ".join(str(v) for v in r.values()).lower()]
    total = len(rows)
    pages = max(1, -(-total // size))
    page = min(max(1, page), pages)
    start = (page - 1) * size
    query = f"&q={q}" if q else ""
    actor_q = f"&actor={actor}" if actor else ""
    ctx = _holder_base(rt, "Audit", "audit", flashes)
    ctx.update({
        "filter": {"action": "/audit", "query": q, "actor": actor, "actors": actors},
        "entries": rows[start:start + size],
        "page": {"number": page, "size": size, "total": total,
                 "prev_href": f"/audit?page={page - 1}{query}{actor_q}" if page > 1 else None,
                 "next_href": f"/audit?page={page + 1}{query}{actor_q}" if page < pages else None},
    })
    return ctx
