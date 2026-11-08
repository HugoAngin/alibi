"""Small shared helpers for building template contexts: money, labels, navigation, lookups."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from alibi.core.schemas import (
    BankTxn,
    Claim,
    Draft,
    EvidenceRecord,
    EvidenceRef,
    Link,
    RecordKind,
    RequestForContext,
    format_utc,
)
from alibi.core.store import NotFound, Store

ISSUER_NAMES = {
    "mock-marketplace": "Mock Marketplace (mock)",
    "second-platform": "Second Platform (mock)",
    "self_supplied": "Imported by holder",
    "threadline": "Threadline (mock)",
}
KIND_LABELS = {
    "order": "sale", "payout": "payout", "refund": "refund", "fee": "fee",
    "settlement": "settlement", "receipt": "receipt", "message": "message",
}
HOLDER_ROLE = "Account holder"


def money(minor: int, currency: str = "GBP") -> dict[str, Any]:
    return {"minor": int(minor), "currency": currency}


def iso(value: datetime | None) -> str | None:
    return None if value is None else format_utc(value)


def mask_subject(subject_ref: str) -> str:
    return f"***{subject_ref[-3:]}"


def issuer_name(issuer_id: str | None) -> str:
    if not issuer_id:
        return "unknown issuer"
    return ISSUER_NAMES.get(issuer_id, issuer_id)


def ref_label(ref: EvidenceRef) -> str:
    return f"{ref.ref_id} r{ref.revision}"


def record_summary(record: EvidenceRecord) -> str:
    """One honest line about a record from its explicit fields."""
    kind = record.kind
    if kind == RecordKind.PAYOUT and record.status == "released_to_balance":
        ref = f" (withdrawal {record.payment_ref} requested)" if record.payment_ref else ""
        return f"Funds released to the seller's platform balance{ref}. Not a bank transfer."
    if kind == RecordKind.SETTLEMENT:
        when = f", settled {format_utc(record.effective_at)}" if record.effective_at else ""
        return f"Bank transfer for payout {record.payment_ref or 'unknown'}: status {record.status}{when}."
    if kind == RecordKind.ORDER:
        item = record.items[0].description if record.items and record.items[0].description else "sale"
        return f"Sale: {item}."
    if kind == RecordKind.REFUND:
        rel = f" on {', '.join(record.related_record_ids)}" if record.related_record_ids else ""
        return f"Refund{rel}, status {record.status or 'none'}."
    if kind == RecordKind.FEE:
        rel = f" on {', '.join(record.related_record_ids)}" if record.related_record_ids else ""
        return f"Platform fee{rel}."
    if kind == RecordKind.RECEIPT:
        item = record.items[0].description if record.items and record.items[0].description else "purchase"
        return f"Purchase receipt: {item}."
    if kind == RecordKind.MESSAGE:
        return (record.text or "Merchant message.")[:160]
    return f"{kind.value} record"


def source_item(record: EvidenceRecord, *, superseded: bool = False) -> dict[str, Any]:
    return {
        "record_id": record.record_id, "revision": record.revision, "kind": record.kind.value,
        "issuer": issuer_name(record.issuer_id), "trust": record.source_trust.value,
        "occurred_at": iso(record.occurred_at),
        "amount": money(record.amount_pence or 0, record.currency or "GBP"),
        "status": record.status or ("imported" if record.source_trust.value == "self_supplied" else "recorded"),
        "summary": record_summary(record),
        "href": f"/sources/{record.record_id}/{record.revision}",
        "signature": {"present": record.issuer_signature is not None,
                      "key_id": record.issuer_signature.kid if record.issuer_signature else None,
                      "verified": True if record.issuer_signature else None},
        "superseded": superseded,
        "via_lookup": record.connector == "merchant_lookup",
        "connector": record.connector,
    }


def load_ref(store: Store, ref: EvidenceRef) -> EvidenceRecord | BankTxn | None:
    try:
        if ref.ref_type == "record":
            return store.get_source(ref.ref_id, ref.revision)
        return store.get_bank_txn(ref.ref_id, ref.revision)
    except NotFound:
        return None


def trust_of(store: Store, ref: EvidenceRef) -> str:
    item = load_ref(store, ref)
    if item is None:
        return "bank_observed" if ref.ref_type == "txn" else "unknown"   # never badge as issuer-signed
    return item.source_trust.value


def active_link(store: Store, txn_id: str) -> Link | None:
    links = store.list_links(txn_id=txn_id, state="active")
    return max(links, key=lambda l: (l.created_at, l.link_id)) if links else None


def latest_run_claims(store: Store, request_id: str) -> list[Claim]:
    claims = store.list_claims(request_id=request_id, state=None)
    if not claims:
        return []
    run_id = claims[-1].run_id
    return [c for c in claims if c.run_id == run_id]


def latest_draft(store: Store, request_id: str, preferred: str | None = None) -> Draft | None:
    """The request's current draft. ``preferred`` is the draft id the app recorded when it last
    built a review (a rebuild with the same body returns an existing row, so insertion order is not
    enough); otherwise the newest by ``created_at``, ties (a fixed clock) going to the row saved
    last, because ``list_drafts`` returns rows in insertion order."""
    drafts = store.list_drafts(request_id=request_id)
    if preferred:
        for draft in drafts:
            if draft.draft_id == preferred:
                return draft
    return sorted(drafts, key=lambda d: d.created_at)[-1] if drafts else None


def current_draft(rt, request_id: str) -> Draft | None:
    return latest_draft(rt.store, request_id, rt.sidecar.get(f"draft:{request_id}"))


def question_text(rfc: RequestForContext, question_id: str) -> str:
    for q in rfc.questions:
        if q.question_id == question_id:
            return q.text
    return question_id


def expression_for(calc) -> str:
    """A readable arithmetic line from a Calculation, grouped by (kind, sign), e.g.
    ``240000 - 18000 - 6000 = 216000``."""
    groups: dict[tuple[str, int], int] = {}
    order: list[tuple[str, int]] = []
    for term in calc.terms:
        key = (term.kind, term.sign)
        if key not in groups:
            groups[key] = 0
            order.append(key)
        groups[key] += term.amount_pence
    order.sort(key=lambda k: (0 if k[1] > 0 else 1, {"order": 0, "refund": 1, "fee": 2}.get(k[0], 3)))
    parts = []
    for idx, key in enumerate(order):
        sign = "+" if key[1] > 0 else "-"
        parts.append(f"{sign} {groups[key]}" if idx or sign == "-" else f"{groups[key]}")
    left = " ".join(parts) or "0"
    return f"{left} = {calc.total_pence}"


def calc_terms(calc) -> list[dict[str, Any]]:
    labels = {"order": "Sales", "refund": "Refunds", "fee": "Fees", "receipt": "Purchases",
              "payout": "Payout", "settlement": "Settlement", "bank_credit": "Bank credit",
              "bank_debit": "Bank debit"}
    grouped: dict[tuple[str, int], dict[str, Any]] = {}
    for term in calc.terms:
        key = (term.kind, term.sign)
        row = grouped.setdefault(key, {"label": labels.get(term.kind, term.kind), "kind": term.kind,
                                       "sign": "+" if term.sign > 0 else "-", "minor": 0,
                                       "currency": term.currency, "source_ids": []})
        row["minor"] += term.amount_pence
        row["source_ids"].append(f"{term.ref.ref_id} r{term.ref.revision}")
    rows = []
    for key in sorted(grouped, key=lambda k: (0 if k[1] > 0 else 1,
                                               {"order": 0, "refund": 1, "fee": 2}.get(k[0], 3))):
        row = grouped[key]
        rows.append({"label": row["label"], "kind": row["kind"], "sign": row["sign"],
                     "amount": money(row["minor"], row["currency"]), "source_ids": row["source_ids"]})
    return rows


def calculation_view(calc, *, computed_at: datetime | None = None) -> dict[str, Any] | None:
    if calc is None:
        return None
    return {
        "expression": expression_for(calc),
        "result": money(calc.total_pence, calc.currency),
        "computed_by": "code (alibi.core.reconcile)",
        "matches": bool(calc.balanced) if calc.target_ref is not None else True,
        "terms": calc_terms(calc),
        "target": calc.target_ref.key() if calc.target_ref else None,
        "difference": money(calc.difference_pence, calc.currency) if calc.difference_pence is not None else None,
        "computed_at": iso(computed_at),
    }


PINNED_GAP_CODES = frozenset({
    "settlement_not_found", "balance_release_not_settlement", "settlement_after_credit",
    "settlement_pending_at_booking", "settlement_failed", "ambiguous_candidates", "amount_mismatch",
    "missing_cited_record", "no_merchant_records",
    # engine codes outside the pinned nine
    "question_not_mapped", "evidence_unavailable", "not_a_credit", "payout_already_linked",
})


def gap_text(claim: Any) -> str | None:
    """The gap as a sentence for a screen: ``gap_detail`` when the claim carries one, else the
    code (``Claim.gap`` is the pinned code since S5; older rows and packages carry prose)."""
    detail = getattr(claim, "gap_detail", None) if not isinstance(claim, dict) else claim.get("gap_detail")
    gap = getattr(claim, "gap", None) if not isinstance(claim, dict) else claim.get("gap")
    return detail or gap


def gap_code(text: str | None) -> str:
    if not text:
        return "none"
    if text in PINNED_GAP_CODES:
        return text
    low = text.lower()
    if "withheld" in low:
        return "withheld_by_holder"
    if "could not map" in low or "not mapped" in low or "names several totals" in low:
        return "question_not_mapped"
    if "pending" in low:
        return "settlement_pending"
    if "failed" in low:
        return "settlement_failed"
    if "after the credit" in low or "after the booking" in low:
        return "settlement_after_credit"
    if "no settlement record" in low or "does not show a bank transfer" in low:
        return "settlement_not_found"
    if "ambiguous" in low or "cannot choose" in low:
        return "ambiguous_candidates"
    if "not reconcile" in low or "differs from the credit" in low:
        return "amount_mismatch"
    return "evidence_unavailable"


def holder_nav(active: str) -> list[dict[str, Any]]:
    items = [("home", "Passport", "/passport"), ("alerts", "Alerts", "/alerts"),
             ("rules", "Rules", "/rules"), ("requests", "Requests", "/requests"),
             ("audit", "Audit", "/audit")]
    return [{"key": k, "label": l, "href": h, "active": k == active} for k, l, h in items]


def analyst_nav(active: str, recipient: str) -> list[dict[str, Any]]:
    items = [("inbox", "Inbox", f"/bank/{recipient}/inbox"),
             ("requests", "Requests sent", f"/bank/{recipient}/inbox#requests-sent")]
    return [{"key": k, "label": l, "href": h, "active": k == active} for k, l, h in items]


def compact(value: Any, limit: int = 120) -> str:
    """A short one-line text for a trace args/result dict."""
    if isinstance(value, dict):
        text = ", ".join(f"{k}={compact(v, 40)}" for k, v in value.items())
    elif isinstance(value, list):
        text = "[" + ", ".join(compact(v, 24) for v in value[:6]) + (", …" if len(value) > 6 else "") + "]"
    else:
        text = str(value)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"
