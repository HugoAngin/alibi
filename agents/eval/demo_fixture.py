"""Builders for the public Alibi mock feeds and the analyst preview."""

from copy import deepcopy
from datetime import date, timedelta

from agents.eval.case_io import request_context
from agents.eval.settlement import fixed_candidate_pool, match_credit, reconcile_credit


def build_mock_merchant(case: dict) -> dict:
    """Preserve separate push and lookup feeds, including their delivery times."""
    return {name: deepcopy(case[name]) for name in ("merchant_deliveries", "merchant_lookup")}


def build_mock_bank(case: dict) -> dict:
    """Preserve the timed bank feed and both recipient-bound requests."""
    return {name: deepcopy(case[name]) for name in ("bank_deliveries", "rfcs")}


def _display_period(booked_at: str) -> dict:
    day = date.fromisoformat(booked_at[:10])
    return {"from": f"{day}T00:00:00Z", "to": f"{day + timedelta(days=1)}T00:00:00Z"}


def _evidence_row(ref: dict, bank: dict, merchant: dict) -> dict:
    kind, record_id = ref["source_id"].split(":", 1)
    record = bank[record_id] if kind == "bank" else merchant[record_id]
    trust = {"bank": "bank_observed", "merchant": "issuer_verified", "import": "self_supplied"}[kind]
    attachment_id = f"att-{record_id}"
    return {
        "attachment_id": attachment_id, "record_id": record_id, "revision": ref["revision"],
        "issuer": "Mock bank" if kind == "bank" else record.get("issuer_id", "Imported by holder"),
        "trust": trust, "attestation": "none", "redacted": False, "signature_check": "not_checked",
        "summary": f"{record.get('kind', 'Bank credit').replace('_', ' ')} · "
                   f"{record['amount_minor']} minor units {record['currency']}",
        "detail": "Static source preview. The live verifier checks the delivered package.",
        "href": f"#trust-{attachment_id}",
    }


def build_analyst_preview(case: dict, *, analyst_name: str = "Demo analyst") -> dict:
    """Project the supported public Bank A case into S4's analyst package context.

    The caller supplies S4's common navigation and page context. Package and source
    authentication are deliberately unset in this preview.
    """
    [request] = [rfc for rfc in case["rfcs"] if rfc["audience"] == "bank_a"]
    [txn_id] = request["txn_ids"]
    view, lookup = request_context(case, request["request_id"])
    bank, merchant, _ = fixed_candidate_pool(view, lookup)
    credit = bank[txn_id]
    link = match_credit(credit, merchant)
    result = reconcile_credit(credit, link, merchant, view["holder_claim"])
    if result["status"] != "supported" or len(link["payouts"]) != 1:
        raise ValueError("analyst preview requires the supported one-payout demo case")

    [payout] = link["payouts"]
    release = next(row for row in merchant.values() if row["kind"] == "balance_release"
                   and payout["payout_reference"] in row.get("message", ""))
    timing = {
        "payout_ref": payout["payout_reference"],
        "released_to_balance_at": release["occurred_at"],
        "bank_settled_at": payout["settled_at"], "credit_booked_at": credit["booked_at"],
        "settled_before_booking": payout["settled_at"] <= credit["booked_at"],
    }

    recipient = request["audience"]
    package_id = f"preview-{request['request_id']}"
    currency = credit["currency"]
    totals = result["breakdown_minor"]
    amount = {"minor": totals["net"], "currency": currency}
    matches = totals["net"] == credit["amount_minor"]
    expression = f"{totals['sales']} - {totals['refunds']} - {totals['fees']} = {totals['net']}"
    calculation = {"expression": expression, "result": amount, "computed_by": "fixture arithmetic",
                   "matches": matches}
    evidence = {ref["source_id"]: _evidence_row(ref, bank, merchant)
                for ref in result["evidence_refs"]}
    summaries = {
        "q.source_of_funds": f"Payout {payout['payout_reference']} settled before the bank booked the credit.",
        "q.breakdown": "The cited sales, refunds and fees reconcile to the booked credit.",
    }
    evidence_by_question = []
    for question in request["questions"]:
        question_id = question["question_id"]
        breakdown = question_id == "q.breakdown"
        refs = result["evidence_refs"] if breakdown else link["evidence_refs"]
        evidence_by_question.append({
            "question_id": question_id, "question": question["text"],
            "claim": {"status": "supported", "rationale": summaries[question_id]},
            "evidence": [dict(evidence[ref["source_id"]]) for ref in refs],
            "calculation": calculation if breakdown else None, "gap": None,
            "timing": timing,
        })
    lines = [
        {"label": label, "kind": kind, "sign": sign,
         "amount": {"minor": totals[key], "currency": currency},
         "source_ids": [f"{record_id} r{merchant[record_id]['revision']}"
                        for record_id in payout["covers_record_ids"] if merchant[record_id]["kind"] == kind]}
        for label, key, kind, sign in (("Sales", "sales", "sale", "+"),
                                       ("Refunds", "refunds", "refund", "-"),
                                       ("Fees", "fees", "fee", "-"))
    ]
    period = _display_period(credit["booked_at"])
    subject_ref_masked = f"***{request['subject_ref'][-3:]}"
    return {
        "recipient": {"id": recipient, "name": "Bank A", "analyst_name": analyst_name},
        "package": {"package_id": package_id, "request_id": request["request_id"], "recipient": "Bank A",
                    "subject_ref_masked": subject_ref_masked, "period": period, "txn_ids": [txn_id],
                    "period_display_only": True, "issued_at": "—", "expires_at": "—"},
        "authentication": {"status": "preview", "received_at": "—", "verified_at": "—",
                           "package_digest": "—", "signer_key_id": "—", "checks": [],
                           "note": "Static preview. No package has been signed, sent or verified."},
        "source_trust": list(evidence.values()),
        "claim_status": [{"question_id": question, "status": "supported", "summary": summary}
                         for question, summary in summaries.items()],
        "evidence_by_question": evidence_by_question,
        "reconciliation": {
            "title": "Reconciliation", "lines": lines, **calculation,
            "compared_to": {"label": "Bank credit", "amount": {"minor": credit["amount_minor"],
                                                             "currency": currency}},
            "difference": {"minor": totals["net"] - credit["amount_minor"], "currency": currency},
            "computed_at": request["issued_at"],
        },
        "contradictions": [], "gaps": [],
        "disposition_form": {"action": "/preview/disabled", "analyst_name": analyst_name, "current": None,
                             "options": [{"value": value, "label": label} for value, label in (
                                 ("clear", "Clear"), ("escalate", "Escalate"), ("request_more", "Request more"))]},
        "followup_form": {"action": "/preview/disabled", "parent_request_id": request["request_id"],
                          "subject_ref_masked": subject_ref_masked, "period": {"from": "", "to": ""},
                          "question_prefill": "",
                          "consent_note": "A follow-up needs the holder's separate approval."},
    }
