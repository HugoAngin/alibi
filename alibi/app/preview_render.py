"""Render every Alibi template once with sample data.

This module is the contract between the static UI and the wiring session.
``sample_contexts()`` returns one context per preview page, built from the
canonical fixture (sales 240000, refunds 18000, fees 6000, bank credit 216000,
all in pence, GBP). ``alibi/app/UI.md`` documents the same shapes in prose.

Run from the repo root:

    uv run --frozen python -m alibi.app.preview_render

It writes one HTML file per page to ``data/alibi/ui_preview/`` and copies the
stylesheet next to them so the files open directly in a browser.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

APP_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = APP_DIR / "templates"
STATIC_DIR = APP_DIR / "static"
REPO_ROOT = APP_DIR.parent.parent
DEFAULT_OUT_DIR = REPO_ROOT / "data" / "alibi" / "ui_preview"

CURRENCY = "GBP"
SALES = 240000
REFUNDS = 18000
FEES = 6000
CREDIT = 216000
assert SALES - REFUNDS - FEES == CREDIT


def money(minor: int, currency: str = CURRENCY) -> dict:
    """Money is always a dict of integer minor units plus an ISO currency code."""
    return {"minor": minor, "currency": currency}


# --------------------------------------------------------------------------
# Shared pieces
# --------------------------------------------------------------------------

HOLDER = {"name": "Maya", "subject_ref": "acct_maya_001"}

BANK_A = {"id": "bank_a", "name": "Bank A"}
BANK_B = {"id": "bank_b", "name": "Bank B"}

REPLAY = {
    "label": "Recorded run",
    "recorded_at": "2026-11-07T21:40:00Z",
    "source": "data/alibi/replay/demo_bank_a.jsonl",
}


def holder_nav(active: str) -> list[dict]:
    items = [
        ("home", "Passport", "/"),
        ("alerts", "Alerts", "/alerts"),
        ("rules", "Rules", "/rules"),
        ("requests", "Requests", "/requests"),
        ("audit", "Audit", "/audit"),
    ]
    return [{"key": k, "label": l, "href": h, "active": k == active} for k, l, h in items]


def analyst_nav(active: str, bank: dict) -> list[dict]:
    items = [
        ("inbox", "Inbox", f"/bank/{bank['id']}/inbox"),
        ("requests", "Requests sent", f"/bank/{bank['id']}/requests"),
    ]
    return [{"key": k, "label": l, "href": h, "active": k == active} for k, l, h in items]


def base_context(
    page_title: str,
    area: str,
    nav_items: list[dict],
    *,
    actor: dict,
    replay_mode: bool = False,
    flashes: list[dict] | None = None,
) -> dict:
    """Variables every template inherits from base.html."""
    return {
        "page_title": page_title,
        "area": area,  # "holder" | "analyst" | "public"
        "nav_items": nav_items,
        "actor": actor,  # {"name": str, "role": str}
        "replay_mode": replay_mode,
        "replay": REPLAY if replay_mode else None,
        "flashes": flashes or [],  # [{"kind": "info"|"success"|"error", "text": str}]
        "static_prefix": "/static",
        "now_utc": "2026-11-07T22:00:00Z",
    }


MAYA = {"name": "Maya", "role": "Account holder"}
ANALYST_A = {"name": "J. Okafor", "role": "Analyst, Bank A"}

# Source records referenced from several screens. ``trust`` is one of
# issuer_verified | bank_observed | self_supplied.
SRC_ORDER = {
    "record_id": "ord_1001",
    "revision": 1,
    "kind": "order",
    "issuer": "ResaleHub (mock)",
    "trust": "issuer_verified",
    "occurred_at": "2026-10-28T14:05:00Z",
    "amount": money(SALES),
    "status": "completed",
    "summary": "4 orders bundled into payout PO-7781",
    "href": "/sources/ord_1001/1",
    "signature": {"present": True, "key_id": "resalehub-demo-2026", "verified": True},
    "superseded": False,
}
SRC_REFUND = {
    "record_id": "ref_2001",
    "revision": 1,
    "kind": "refund",
    "issuer": "ResaleHub (mock)",
    "trust": "issuer_verified",
    "occurred_at": "2026-10-30T10:20:00Z",
    "amount": money(-REFUNDS),
    "status": "completed",
    "summary": "Refund on order ord_1001/3",
    "href": "/sources/ref_2001/1",
    "signature": {"present": True, "key_id": "resalehub-demo-2026", "verified": True},
    "superseded": False,
}
SRC_FEE = {
    "record_id": "fee_3001",
    "revision": 1,
    "kind": "fee",
    "issuer": "ResaleHub (mock)",
    "trust": "issuer_verified",
    "occurred_at": "2026-11-02T06:00:00Z",
    "amount": money(-FEES),
    "status": "charged",
    "summary": "Platform fee for payout PO-7781",
    "href": "/sources/fee_3001/1",
    "signature": {"present": True, "key_id": "resalehub-demo-2026", "verified": True},
    "superseded": False,
}
SRC_PAYOUT_R1 = {
    "record_id": "po_7781",
    "revision": 1,
    "kind": "payout",
    "issuer": "ResaleHub (mock)",
    "trust": "issuer_verified",
    "occurred_at": "2026-11-02T06:05:00Z",
    "amount": money(CREDIT),
    "status": "released_to_balance",
    "summary": "Funds released to seller balance. Does not show a bank transfer.",
    "href": "/sources/po_7781/1",
    "signature": {"present": True, "key_id": "resalehub-demo-2026", "verified": True},
    "superseded": True,
}
SRC_PAYOUT_R2 = {
    **SRC_PAYOUT_R1,
    "revision": 2,
    "occurred_at": "2026-11-03T08:45:00Z",
    "status": "settled",
    "summary": "Payout PO-7781 settled to bank, settlement ref STL-9001",
    "href": "/sources/po_7781/2",
    "superseded": False,
}
SRC_SETTLEMENT = {
    "record_id": "stl_9001",
    "revision": 1,
    "kind": "settlement",
    "issuer": "ResaleHub (mock)",
    "trust": "issuer_verified",
    "occurred_at": "2026-11-03T08:45:00Z",
    "amount": money(CREDIT),
    "status": "settled",
    "summary": "Bank settlement for PO-7781, reference FASTPAY PO-7781",
    "href": "/sources/stl_9001/1",
    "signature": {"present": True, "key_id": "resalehub-demo-2026", "verified": True},
    "superseded": False,
}
SRC_RECEIPT = {
    "record_id": "rcpt_self_01",
    "revision": 1,
    "kind": "receipt",
    "issuer": "Imported by holder",
    "trust": "self_supplied",
    "occurred_at": "2026-10-28T14:10:00Z",
    "amount": money(SALES),
    "status": "imported",
    "summary": "Seller export CSV, 4 orders, uploaded by Maya",
    "href": "/sources/rcpt_self_01/1",
    "signature": {"present": False, "key_id": None, "verified": None},
    "superseded": False,
}

TXN_CREDIT = {
    "txn_id": "txn_0001",
    "revision": 1,
    "booked_at": "2026-11-03T09:12:00Z",
    "description": "FASTPAY RESALEHUB PAYOUT PO-7781",
    "amount": money(CREDIT),
    "direction": "credit",
    "link_state": "linked",
    "linked_count": 5,
    "href": "/transactions/txn_0001",
}
TXN_RENT = {
    "txn_id": "txn_0007",
    "revision": 1,
    "booked_at": "2026-11-01T07:30:00Z",
    "description": "STANDING ORDER RENT",
    "amount": money(-65000),
    "direction": "debit",
    "link_state": "unmatched",
    "linked_count": 0,
    "href": "/transactions/txn_0007",
}
TXN_AMBIG = {
    "txn_id": "txn_0012",
    "revision": 1,
    "booked_at": "2026-11-05T11:02:00Z",
    "description": "CARD PURCHASE THREADS LTD",
    "amount": money(-4200),
    "direction": "debit",
    "link_state": "ambiguous",
    "linked_count": 2,
    "href": "/transactions/txn_0012",
}

RECONCILIATION = {
    "title": "Payout PO-7781 reconciliation",
    "lines": [
        {"label": "Sales", "kind": "order", "sign": "+", "amount": money(SALES), "source_ids": ["ord_1001 r1"]},
        {"label": "Refunds", "kind": "refund", "sign": "-", "amount": money(REFUNDS), "source_ids": ["ref_2001 r1"]},
        {"label": "Fees", "kind": "fee", "sign": "-", "amount": money(FEES), "source_ids": ["fee_3001 r1"]},
    ],
    "expression": "240000 - 18000 - 6000 = 216000",
    "result": money(CREDIT),
    "compared_to": {"label": "Bank credit txn_0001", "amount": money(CREDIT)},
    "difference": money(0),
    "matches": True,
    "computed_by": "code",
    "computed_at": "2026-11-03T09:15:00Z",
}

QUESTIONS = [
    {"question_id": "q1", "text": "What is the source of the credit of 2,160.00 GBP booked on 2026-11-03?"},
    {"question_id": "q2", "text": "Did the funds reach the bank on 2026-11-02 as the merchant message states?"},
    {"question_id": "q3", "text": "Were any of the underlying sales made to a related party?"},
]

CLAIMS = [
    {
        "claim_id": "clm_q1",
        "question_id": "q1",
        "question": QUESTIONS[0]["text"],
        "status": "supported",
        "rationale": "Payout PO-7781 settled to the bank at 2026-11-03T08:45Z, before the credit was booked. "
        "Cited sales less refunds and fees equal the credit.",
        "href": "/requests/rfc_a_0001/trace#q1",
    },
    {
        "claim_id": "clm_q2",
        "question_id": "q2",
        "question": QUESTIONS[1]["text"],
        "status": "contradicted",
        "rationale": "The merchant message of 2026-11-02 describes release to the seller balance. "
        "Settlement record stl_9001 shows the bank transfer at 2026-11-03T08:45Z.",
        "href": "/requests/rfc_a_0001/trace#q2",
    },
    {
        "claim_id": "clm_q3",
        "question_id": "q3",
        "question": QUESTIONS[2]["text"],
        "status": "unresolved",
        "rationale": "The marketplace does not expose buyer identity. No record answers this question.",
        "href": "/requests/rfc_a_0001/trace#q3",
    },
]

RFC_A = {
    "request_id": "rfc_a_0001",
    "parent_request_id": None,
    "issuer": BANK_A["name"],
    "audience": BANK_A["id"],
    "subject_ref": HOLDER["subject_ref"],
    "received_at": "2026-11-06T10:00:00Z",
    "expires_at": "2026-11-13T10:00:00Z",
    "period": {"from": "2026-10-01T00:00:00Z", "to": "2026-11-07T00:00:00Z"},
    "txn_ids": ["txn_0001"],
    "questions": QUESTIONS,
    "status": "draft",
    "signature": {"verified": True, "key_id": "bank-a-demo-2026"},
    "href": "/requests/rfc_a_0001",
}

APPROVAL = {
    "status": "pending",  # none | pending | approved | invalidated
    "recipient": BANK_A["name"],
    "audience": BANK_A["id"],
    "action": "/requests/rfc_a_0001/approve",
    "digest": "sha256:3f9c1e7a0b2d4c6e8f1a3b5c7d9e0f2a4b6c8d0e1f3a5b7c9d1e3f5a7b9c1d3e",
    "policy_version": "pol_v3",
    "valid_from": "2026-11-07T21:50:00Z",
    "valid_until": "2026-11-07T22:05:00Z",
    "approved_at": None,
    "invalidated_reason": None,
}

PACKAGE_A = {
    "package_id": "pkg_a_0001",
    "request_id": "rfc_a_0001",
    "version": "alibi-package/1",
    "audience": BANK_A["id"],
    "recipient": BANK_A["name"],
    "case_id": "case_0001",
    "subject_ref_masked": "acct_****_001",
    "period": {"from": "2026-10-01T00:00:00Z", "to": "2026-11-07T00:00:00Z"},
    "issued_at": "2026-11-07T21:52:00Z",
    "expires_at": "2026-11-14T21:52:00Z",
    "digest": APPROVAL["digest"],
    "signature": {
        "alg": "Ed25519",
        "key_id": "alibi-demo-2026",
        "domain": "alibi:package",
        "value": "base64:MEUCIQDemoSignatureValueTruncatedForPreview0000000000000000000000000000==",
    },
    "claims": [
        {
            "question_id": "q1",
            "question": QUESTIONS[0]["text"],
            "status": "supported",
            "rationale": CLAIMS[0]["rationale"],
            "evidence_refs": [
                {"record_id": "po_7781", "revision": 2, "trust": "issuer_verified"},
                {"record_id": "stl_9001", "revision": 1, "trust": "issuer_verified"},
                {"record_id": "ext_recon_01", "revision": 1, "trust": "issuer_verified"},
            ],
            "calculation": {"expression": "240000 - 18000 - 6000 = 216000", "result": money(CREDIT)},
            "gap": None,
        },
        {
            "question_id": "q2",
            "question": QUESTIONS[1]["text"],
            "status": "contradicted",
            "rationale": CLAIMS[1]["rationale"],
            "evidence_refs": [
                {"record_id": "po_7781", "revision": 1, "trust": "issuer_verified"},
                {"record_id": "stl_9001", "revision": 1, "trust": "issuer_verified"},
            ],
            "calculation": None,
            "gap": None,
        },
        {
            "question_id": "q3",
            "question": QUESTIONS[2]["text"],
            "status": "unresolved",
            "rationale": CLAIMS[2]["rationale"],
            "evidence_refs": [],
            "calculation": None,
            "gap": "No available record identifies buyers. Evidence unavailable, not withheld.",
        },
    ],
    "attachments": [
        {
            "attachment_id": "att_01",
            "kind": "payout",
            "record_id": "po_7781",
            "revision": 2,
            "issuer": "ResaleHub (mock)",
            "trust": "issuer_verified",
            "attestation": "issuer_signature",  # issuer_signature | package_signer | none
            "redacted": False,
            "summary": "Full issuer-signed payout record PO-7781",
        },
        {
            "attachment_id": "att_02",
            "kind": "settlement",
            "record_id": "stl_9001",
            "revision": 1,
            "issuer": "ResaleHub (mock)",
            "trust": "issuer_verified",
            "attestation": "issuer_signature",
            "redacted": False,
            "summary": "Full issuer-signed settlement record STL-9001",
        },
        {
            "attachment_id": "att_03",
            "kind": "extract",
            "record_id": "ext_recon_01",
            "revision": 1,
            "issuer": "Alibi (redacted extract of ord_1001, ref_2001, fee_3001)",
            "trust": "issuer_verified",
            "attestation": "package_signer",
            "redacted": True,
            "summary": "Totals only: sales, refunds, fees. Item descriptions withheld by rule rule_01.",
        },
        {
            "attachment_id": "att_04",
            "kind": "receipt",
            "record_id": "rcpt_self_01",
            "revision": 1,
            "issuer": "Imported by holder",
            "trust": "self_supplied",
            "attestation": "none",
            "redacted": False,
            "summary": "Seller export CSV uploaded by the holder. No issuer signature.",
        },
    ],
}


# --------------------------------------------------------------------------
# Per-page contexts
# --------------------------------------------------------------------------


def ctx_passport_home() -> dict:
    ctx = base_context("Passport", "holder", holder_nav("home"), actor=MAYA)
    ctx.update(
        {
            "holder": HOLDER,
            "connections": [
                {
                    "connection_id": "conn_resalehub",
                    "name": "ResaleHub (mock marketplace)",
                    "kind": "merchant",  # merchant | bank | import
                    "status": "connected",  # connected | syncing | error | disconnected
                    "trust": "issuer_verified",
                    "last_synced_at": "2026-11-07T21:30:00Z",
                    "record_count": 6,
                    "href": "/connections/conn_resalehub",
                },
                {
                    "connection_id": "conn_bank_feed",
                    "name": "Current account feed (mock bank)",
                    "kind": "bank",
                    "status": "connected",
                    "trust": "bank_observed",
                    "last_synced_at": "2026-11-07T21:31:00Z",
                    "record_count": 14,
                    "href": "/connections/conn_bank_feed",
                },
                {
                    "connection_id": "conn_import",
                    "name": "Self-supplied imports",
                    "kind": "import",
                    "status": "connected",
                    "trust": "self_supplied",
                    "last_synced_at": "2026-11-04T18:00:00Z",
                    "record_count": 1,
                    "href": "/connections/conn_import",
                },
            ],
            "transactions": [TXN_CREDIT, TXN_RENT, TXN_AMBIG],
            "evidence_status": {
                "linked": 9,
                "ambiguous": 1,
                "unmatched": 4,
                "total": 14,
                "last_checked_at": "2026-11-07T21:31:00Z",
                "superseded_sources": 1,
            },
            "alerts": [
                {
                    "alert_id": "alrt_001",
                    "kind": "settlement_discrepancy",
                    "severity": "warning",  # info | warning | high
                    "title": "Merchant message says balance release, not bank settlement",
                    "created_at": "2026-11-02T06:10:00Z",
                    "status": "resolved",  # open | resolved
                    "href": "/alerts#alrt_001",
                },
                {
                    "alert_id": "alrt_002",
                    "kind": "merchant_unverified",
                    "severity": "info",
                    "title": "THREADS LTD is not in the merchant registry",
                    "created_at": "2026-11-05T11:05:00Z",
                    "status": "open",
                    "href": "/alerts#alrt_002",
                },
            ],
            "rules": [
                {
                    "rule_id": "rule_01",
                    "text": "Never share what I sold, only the totals.",
                    "status": "active",  # active | held | rejected
                    "summary": "Deny item descriptions and categories; share totals.",
                    "href": "/rules#rule_01",
                },
                {
                    "rule_id": "rule_02",
                    "text": "Only share stuff from this year with banks.",
                    "status": "held",
                    "summary": "Held: 'this year' is ambiguous (calendar or tax year).",
                    "href": "/rules#rule_02",
                },
            ],
            "requests": [
                {
                    "request_id": "rfc_a_0001",
                    "recipient": BANK_A["name"],
                    "question_count": 3,
                    "received_at": "2026-11-06T10:00:00Z",
                    "expires_at": "2026-11-13T10:00:00Z",
                    "status": "draft",  # new | investigating | draft | approved | sent | refused | expired
                    "href": "/requests/rfc_a_0001",
                },
                {
                    "request_id": "rfc_b_0001",
                    "recipient": BANK_B["name"],
                    "question_count": 1,
                    "received_at": "2026-11-07T09:00:00Z",
                    "expires_at": "2026-11-14T09:00:00Z",
                    "status": "new",
                    "href": "/requests/rfc_b_0001",
                },
            ],
            "sharing_history": [
                {
                    "package_id": "pkg_a_0000",
                    "request_id": "rfc_a_0000",
                    "recipient": BANK_A["name"],
                    "sent_at": "2026-10-15T12:00:00Z",
                    "expires_at": "2026-10-22T12:00:00Z",
                    "item_count": 2,
                    "status": "acknowledged",  # delivered | acknowledged | superseded | expired
                    "href": "/requests/rfc_a_0000/export",
                }
            ],
        }
    )
    return ctx


def ctx_transaction_detail() -> dict:
    ctx = base_context("Transaction txn_0001", "holder", holder_nav("home"), actor=MAYA)
    ctx.update(
        {
            "txn": {
                **TXN_CREDIT,
                "account_label": "Current account ····4421",
                "connection_name": "Current account feed (mock bank)",
                "references": ["PO-7781", "FASTPAY"],
                "provenance": {
                    "connection_id": "conn_bank_feed",
                    "retrieved_at": "2026-11-03T09:14:00Z",
                    "source_version": "feed-cursor-00042",
                },
                "trust": "bank_observed",
            },
            "sources": [SRC_ORDER, SRC_REFUND, SRC_FEE, SRC_PAYOUT_R2, SRC_SETTLEMENT, SRC_RECEIPT],
            "reconciliation": RECONCILIATION,
            "claims": CLAIMS,
            "changes": [
                {
                    "change_id": "chg_001",
                    "received_at": "2026-11-03T08:50:00Z",
                    "record_id": "po_7781",
                    "kind": "payout",
                    "effect": "Source-of-funds claim for q1 moved from unresolved to supported. "
                    "Pending draft for rfc_a_0001 invalidated and rebuilt.",
                    "older": {
                        "revision": 1,
                        "received_at": "2026-11-02T06:05:00Z",
                        "href": "/sources/po_7781/1",
                        "fields": [
                            {"name": "status", "value": "released_to_balance"},
                            {"name": "occurred_at", "value": "2026-11-02T06:05:00Z"},
                            {"name": "settlement_ref", "value": "(none)"},
                            {"name": "amount", "value": "216000 GBP"},
                        ],
                    },
                    "newer": {
                        "revision": 2,
                        "received_at": "2026-11-03T08:50:00Z",
                        "href": "/sources/po_7781/2",
                        "fields": [
                            {"name": "status", "value": "settled"},
                            {"name": "occurred_at", "value": "2026-11-03T08:45:00Z"},
                            {"name": "settlement_ref", "value": "STL-9001"},
                            {"name": "amount", "value": "216000 GBP"},
                        ],
                    },
                    "diff": [
                        {"field": "status", "before": "released_to_balance", "after": "settled"},
                        {"field": "occurred_at", "before": "2026-11-02T06:05:00Z", "after": "2026-11-03T08:45:00Z"},
                        {"field": "settlement_ref", "before": "(none)", "after": "STL-9001"},
                    ],
                }
            ],
        }
    )
    return ctx


def ctx_alerts(with_lookup_result: bool) -> dict:
    ctx = base_context("Alerts", "holder", holder_nav("alerts"), actor=MAYA)
    ctx.update(
        {
            "alerts": [
                {
                    "alert_id": "alrt_001",
                    "kind": "settlement_discrepancy",
                    "severity": "warning",
                    "title": "Merchant message says balance release, not bank settlement",
                    "explanation": "Payout PO-7781 was released to your seller balance on 2026-11-02. "
                    "A balance release does not show that money reached your bank. "
                    "The settlement record arrived on 2026-11-03 and now explains the credit.",
                    "created_at": "2026-11-02T06:10:00Z",
                    "status": "resolved",
                    "txn_href": "/transactions/txn_0001",
                    "facts": [
                        {"label": "Payout status (rev 1)", "value": "released_to_balance", "source_id": "po_7781", "source_revision": 1, "href": "/sources/po_7781/1"},
                        {"label": "Payout amount", "value": "216000 GBP", "source_id": "po_7781", "source_revision": 1, "href": "/sources/po_7781/1"},
                        {"label": "Bank credit", "value": "216000 GBP booked 2026-11-03T09:12:00Z", "source_id": "txn_0001", "source_revision": 1, "href": "/transactions/txn_0001"},
                        {"label": "Settlement (rev 1)", "value": "settled 2026-11-03T08:45:00Z", "source_id": "stl_9001", "source_revision": 1, "href": "/sources/stl_9001/1"},
                    ],
                    "suggested_checks": [
                        "Open the settlement record and compare its time with the credit booking time.",
                        "Check that the payout reference PO-7781 appears on the bank description.",
                    ],
                },
                {
                    "alert_id": "alrt_002",
                    "kind": "merchant_unverified",
                    "severity": "info",
                    "title": "THREADS LTD is not in the merchant registry",
                    "explanation": "A card purchase of 42.00 GBP went to a merchant the mock registry does not list. "
                    "Unknown is not the same as bad: it means Alibi cannot verify the merchant.",
                    "created_at": "2026-11-05T11:05:00Z",
                    "status": "open",
                    "txn_href": "/transactions/txn_0012",
                    "facts": [
                        {"label": "Merchant on statement", "value": "THREADS LTD", "source_id": "txn_0012", "source_revision": 1, "href": "/transactions/txn_0012"},
                        {"label": "Registry result", "value": "unverified (not found)", "source_id": "lookup_0003", "source_revision": 1, "href": "/alerts#lookup_0003"},
                    ],
                    "suggested_checks": [
                        "Look the merchant up before paying them again.",
                        "Import a receipt if you have one; it will be labelled self-supplied.",
                    ],
                },
                {
                    "alert_id": "alrt_003",
                    "kind": "unexplained_credit",
                    "severity": "high",
                    "title": "Credit of 90.00 GBP with no linked record",
                    "explanation": "A credit arrived with no matching sale, refund or payout. "
                    "Alibi lists it so you can find the record before anyone asks.",
                    "created_at": "2026-11-06T08:00:00Z",
                    "status": "open",
                    "txn_href": "/transactions/txn_0015",
                    "facts": [
                        {"label": "Bank credit", "value": "9000 GBP booked 2026-11-06T07:58:00Z", "source_id": "txn_0015", "source_revision": 1, "href": "/transactions/txn_0015"},
                        {"label": "Candidates", "value": "0 within 14 days", "source_id": "match_run_0019", "source_revision": 1, "href": "/audit#match_run_0019"},
                    ],
                    "suggested_checks": ["Check whether this was a private sale or a transfer from a friend."],
                },
            ],
            "merchant_lookup": {
                "action": "/alerts/lookup",
                "query": "THREADS LTD" if with_lookup_result else "",
                "result": (
                    {
                        "query": "THREADS LTD",
                        "status": "unverified",  # verified | unverified
                        "name": None,
                        "registry": "Alibi mock merchant registry (demo, pinned)",
                        "checked_at": "2026-11-07T21:45:00Z",
                        "notes": "No entry matched. Unverified means unknown to this registry, not fraudulent.",
                    }
                    if with_lookup_result
                    else None
                ),
            },
        }
    )
    return ctx


def ctx_rules(with_interpretation: bool) -> dict:
    ctx = base_context("Privacy rules", "holder", holder_nav("rules"), actor=MAYA)
    ctx.update(
        {
            "rule_form": {
                "action": "/rules/interpret",
                "text": "Never share what I sold, only the totals." if with_interpretation else "",
                "examples": [
                    "Never share what I sold, only the totals.",
                    "Don't show Bank B anything older than six months.",
                    "Round my refunds to the nearest ten pounds.",
                ],
            },
            "interpretation": (
                {
                    "rule_id": "rule_01",
                    "text": "Never share what I sold, only the totals.",
                    "policy": {
                        "kind": "deny",  # deny | generalise | age | recipient | alert_pref
                        "fields": [
                            {"name": "kind", "value": "deny"},
                            {"name": "target", "value": "merchant_record.items[].description, merchant_record.items[].category"},
                            {"name": "keep", "value": "merchant_record.totals"},
                            {"name": "recipients", "value": "all"},
                            {"name": "effective_from", "value": "2026-11-07T21:00:00Z"},
                        ],
                        "json": '{"kind": "deny", "target": ["merchant_record.items[].description", "merchant_record.items[].category"], "keep": ["merchant_record.totals"], "recipients": "all"}',
                    },
                    "plain_lines": [
                        "Item descriptions and categories will never be included in any package.",
                        "Order totals, refund totals and fee totals can still be shared.",
                        "This applies to every recipient.",
                    ],
                    "warnings": ["Where this rule leaves a gap, the package will say a category was withheld without naming it."],
                    "confirm_action": "/rules/rule_01/confirm",
                    "reject_action": "/rules/rule_01/reject",
                }
                if with_interpretation
                else None
            ),
            "active_rules": [
                {
                    "rule_id": "rule_01",
                    "text": "Never share what I sold, only the totals.",
                    "kind": "deny",
                    "summary": "Deny item descriptions and categories; share totals.",
                    "version": "pol_v3",
                    "status": "active",
                    "confirmed_at": "2026-11-07T21:05:00Z",
                    "retire_action": "/rules/rule_01/retire",
                },
                {
                    "rule_id": "rule_03",
                    "text": "Round refunds to the nearest ten pounds for Bank B.",
                    "kind": "generalise",
                    "summary": "Generalise refund amounts to 1000-pence buckets for recipient bank_b.",
                    "version": "pol_v3",
                    "status": "active",
                    "confirmed_at": "2026-11-07T21:06:00Z",
                    "retire_action": "/rules/rule_03/retire",
                },
            ],
            "held_rules": [
                {
                    "rule_id": "rule_02",
                    "text": "Only share stuff from this year with banks.",
                    "reason": "'this year' could mean the calendar year or the tax year. Say which.",
                    "created_at": "2026-11-07T21:07:00Z",
                    "clarify_action": "/rules/rule_02/clarify",
                    "discard_action": "/rules/rule_02/discard",
                }
            ],
        }
    )
    return ctx


def ctx_request_detail() -> dict:
    ctx = base_context("Request rfc_a_0001", "holder", holder_nav("requests"), actor=MAYA)
    ctx.update(
        {
            "request": RFC_A,
            "investigation": {
                "status": "complete",  # not_started | running | complete | failed
                "can_run": True,
                "run_action": "/requests/rfc_a_0001/investigate",
                "started_at": "2026-11-07T21:40:00Z",
                "completed_at": "2026-11-07T21:43:00Z",
                "budget": {"calls_used": 7, "calls_cap": 12},
                "roles": [
                    {"name": "Matcher", "status": "skipped", "calls": 0, "note": "Deterministic join found a unique candidate."},
                    {"name": "Detective", "status": "complete", "calls": 2, "note": "Proposed PO-7781 as the source."},
                    {"name": "Challenger", "status": "complete", "calls": 3, "note": "Requested settlement record for PO-7781."},
                    {"name": "Curator", "status": "complete", "calls": 2, "note": "Selected 4 items, excluded 2, generalised 1."},
                ],
                "failure_reason": None,
            },
            "trace": [
                {
                    "step_id": "trc_q1",
                    "question_id": "q1",
                    "question": QUESTIONS[0]["text"],
                    "claim": {"claim_id": "clm_q1", "text": "The credit is marketplace payout PO-7781.", "status": "supported"},
                    "sources": [
                        {"record_id": "ord_1001", "revision": 1, "trust": "issuer_verified", "href": "/sources/ord_1001/1"},
                        {"record_id": "ref_2001", "revision": 1, "trust": "issuer_verified", "href": "/sources/ref_2001/1"},
                        {"record_id": "fee_3001", "revision": 1, "trust": "issuer_verified", "href": "/sources/fee_3001/1"},
                        {"record_id": "po_7781", "revision": 2, "trust": "issuer_verified", "href": "/sources/po_7781/2"},
                        {"record_id": "stl_9001", "revision": 1, "trust": "issuer_verified", "href": "/sources/stl_9001/1"},
                    ],
                    "lookups": [
                        {
                            "tool": "merchant.get_settlement",
                            "args_summary": "payout_ref=PO-7781",
                            "result_summary": "stl_9001 r1, status settled, 2026-11-03T08:45:00Z",
                            "at": "2026-11-07T21:41:30Z",
                        }
                    ],
                    "calculation": {
                        "expression": "240000 - 18000 - 6000 = 216000",
                        "result": money(CREDIT),
                        "computed_by": "code",
                        "matches": True,
                    },
                    "status": "supported",
                    "status_reason": "Settlement time precedes booking time and cited amounts reconcile.",
                    "at": "2026-11-07T21:42:00Z",
                },
                {
                    "step_id": "trc_q2",
                    "question_id": "q2",
                    "question": QUESTIONS[1]["text"],
                    "claim": {"claim_id": "clm_q2", "text": "Funds reached the bank on 2026-11-02.", "status": "contradicted"},
                    "sources": [
                        {"record_id": "po_7781", "revision": 1, "trust": "issuer_verified", "href": "/sources/po_7781/1"},
                        {"record_id": "stl_9001", "revision": 1, "trust": "issuer_verified", "href": "/sources/stl_9001/1"},
                    ],
                    "lookups": [],
                    "calculation": None,
                    "status": "contradicted",
                    "status_reason": "Revision 1 shows release to balance only. Settlement is dated 2026-11-03.",
                    "at": "2026-11-07T21:42:10Z",
                },
                {
                    "step_id": "trc_q3",
                    "question_id": "q3",
                    "question": QUESTIONS[2]["text"],
                    "claim": {"claim_id": "clm_q3", "text": "No related-party sales.", "status": "unresolved"},
                    "sources": [],
                    "lookups": [
                        {
                            "tool": "merchant.get_orders",
                            "args_summary": "payout_ref=PO-7781, fields=buyer",
                            "result_summary": "buyer fields not exposed by connector",
                            "at": "2026-11-07T21:42:20Z",
                        }
                    ],
                    "calculation": None,
                    "status": "unresolved",
                    "status_reason": "Necessary evidence is unavailable.",
                    "at": "2026-11-07T21:42:30Z",
                },
            ],
            "review": {
                "claims": CLAIMS,
                "included": [
                    {"item_id": "inc_01", "question_id": "q1", "label": "Payout record PO-7781", "kind": "payout", "record_id": "po_7781", "revision": 2, "trust": "issuer_verified", "reason": "Directly explains the credit.", "removable": False, "remove_action": None},
                    {"item_id": "inc_02", "question_id": "q1", "label": "Settlement record STL-9001", "kind": "settlement", "record_id": "stl_9001", "revision": 1, "trust": "issuer_verified", "reason": "Shows the transfer reached the bank before booking.", "removable": False, "remove_action": None},
                    {"item_id": "inc_03", "question_id": "q1", "label": "Reconciliation extract (totals only)", "kind": "extract", "record_id": "ext_recon_01", "revision": 1, "trust": "issuer_verified", "reason": "Supports the arithmetic without item detail.", "removable": True, "remove_action": "/requests/rfc_a_0001/review/remove/inc_03"},
                    {"item_id": "inc_04", "question_id": "q1", "label": "Seller export CSV (self-supplied)", "kind": "receipt", "record_id": "rcpt_self_01", "revision": 1, "trust": "self_supplied", "reason": "Optional corroboration. Lower trust; the bank sees it as self-supplied.", "removable": True, "remove_action": "/requests/rfc_a_0001/review/remove/inc_04"},
                ],
                "excluded": [
                    {"item_id": "exc_01", "label": "Item descriptions on ord_1001", "reason": "Rule rule_01 denies item descriptions.", "rule_id": "rule_01"},
                    {"item_id": "exc_02", "label": "Transaction txn_0007 (rent)", "reason": "Not needed for any question. Default-minimum disclosure.", "rule_id": None},
                    {"item_id": "exc_03", "label": "Transaction txn_0012 (card purchase)", "reason": "Outside the request's transaction scope.", "rule_id": None},
                ],
                "generalised": [
                    {"item_id": "gen_01", "label": "Order categories", "original_summary": "4 orders: jackets, trainers, dresses", "generalised_as": "4 orders, category withheld", "rule_id": "rule_01"}
                ],
                "gaps": [
                    {"question_id": "q3", "description": "No record identifies buyers, so related-party status cannot be answered.", "reason_code": "evidence_unavailable"}
                ],
                "counts": {"included": 4, "excluded": 3, "generalised": 1, "gaps": 1},
                "approval": APPROVAL,
                "preview_href": "/requests/rfc_a_0001/export",
            },
        }
    )
    return ctx


def ctx_export() -> dict:
    ctx = base_context("Export pkg_a_0001", "holder", holder_nav("requests"), actor=MAYA)
    ctx.update(
        {
            "package": PACKAGE_A,
            "export": {
                "json_href": "/requests/rfc_a_0001/package.json",
                "json_filename": "pkg_a_0001.json",
                "json_size_bytes": 7421,
                "verify_hint": "uv run --frozen python -m alibi.verify pkg_a_0001.json",
                "approval": {**APPROVAL, "status": "approved", "approved_at": "2026-11-07T21:51:00Z"},
            },
        }
    )
    return ctx


def ctx_dispatch(outcome: str) -> dict:
    ctx = base_context("Dispatch result", "holder", holder_nav("requests"), actor=MAYA)
    success = outcome == "success"
    ctx["dispatch"] = {
        "outcome": outcome,  # success | refused
        "package_id": "pkg_a_0001",
        "request_id": "rfc_a_0001",
        "recipient": BANK_A["name"],
        "attempted_at": "2026-11-07T21:53:00Z",
        "receipt": (
            {
                "receipt_id": "rcpt_a_0001",
                "package_digest": APPROVAL["digest"],
                "received_at": "2026-11-07T21:53:02Z",
                "status": "accepted",
                "signer_key_id": "bank-a-demo-2026",
                "expires_at": PACKAGE_A["expires_at"],
            }
            if success
            else None
        ),
        "refusal": (
            None
            if success
            else {
                "code": "approval_digest_mismatch",
                "reason": "dispatch refused: approved digest sha256:3f9c1e7a... does not match body digest sha256:b71e04d2...; "
                "the body changed after approval (inc_03 removed). Review and approve the revised body.",
                "checked_at": "2026-11-07T21:53:00Z",
                "expected_digest": APPROVAL["digest"],
                "actual_digest": "sha256:b71e04d2c9a1f8e3b6d5a7c2e9f1b4d8a6c3e5f7a9b1c3d5e7f9a1b3c5d7e9f1",
            }
        ),
        "retry_action": None if success else "/requests/rfc_a_0001/dispatch",
        "review_href": "/requests/rfc_a_0001#review",
        "history_href": "/#sharing",
    }
    return ctx


def ctx_analyst_inbox() -> dict:
    ctx = base_context("Bank A inbox", "analyst", analyst_nav("inbox", BANK_A), actor=ANALYST_A)
    ctx.update(
        {
            "recipient": {**BANK_A, "analyst_name": ANALYST_A["name"]},
            "packages": [
                {
                    "package_id": "pkg_a_0001",
                    "request_id": "rfc_a_0001",
                    "received_at": "2026-11-07T21:53:02Z",
                    "expires_at": "2026-11-14T21:52:00Z",
                    "subject_ref_masked": "acct_****_001",
                    "authentication": "valid",  # valid | invalid | expired | wrong_audience
                    "claim_counts": {"supported": 1, "contradicted": 1, "unresolved": 1},
                    "disposition": None,  # None | clear | escalate | request_more
                    "href": "/bank/bank_a/packages/pkg_a_0001",
                },
                {
                    "package_id": "pkg_a_0000",
                    "request_id": "rfc_a_0000",
                    "received_at": "2026-10-15T12:00:00Z",
                    "expires_at": "2026-10-22T12:00:00Z",
                    "subject_ref_masked": "acct_****_001",
                    "authentication": "expired",
                    "claim_counts": {"supported": 2, "contradicted": 0, "unresolved": 0},
                    "disposition": "clear",
                    "href": "/bank/bank_a/packages/pkg_a_0000",
                },
                {
                    "package_id": "pkg_x_0009",
                    "request_id": "rfc_b_0001",
                    "received_at": "2026-11-07T20:10:00Z",
                    "expires_at": "2026-11-14T20:10:00Z",
                    "subject_ref_masked": "acct_****_001",
                    "authentication": "wrong_audience",
                    "claim_counts": {"supported": 0, "contradicted": 0, "unresolved": 0},
                    "disposition": None,
                    "href": "/bank/bank_a/packages/pkg_x_0009",
                },
            ],
            "requests_sent": [
                {"request_id": "rfc_a_0001", "parent_request_id": None, "sent_at": "2026-11-06T10:00:00Z", "status": "answered", "question_count": 3},
                {"request_id": "rfc_a_0002", "parent_request_id": "rfc_a_0001", "sent_at": "2026-11-07T22:10:00Z", "status": "awaiting_consent", "question_count": 1},
            ],
        }
    )
    return ctx


def ctx_analyst_package() -> dict:
    ctx = base_context("Package pkg_a_0001", "analyst", analyst_nav("inbox", BANK_A), actor=ANALYST_A)
    ctx.update(
        {
            "recipient": {**BANK_A, "analyst_name": ANALYST_A["name"]},
            "package": PACKAGE_A,
            "authentication": {
                "status": "valid",  # valid | invalid | expired | wrong_audience | replayed
                "received_at": "2026-11-07T21:53:02Z",
                "verified_at": "2026-11-07T21:53:02Z",
                "package_digest": APPROVAL["digest"],
                "signer_key_id": "alibi-demo-2026",
                "checks": [
                    {"name": "Schema alibi-package/1", "result": "pass", "detail": "All required fields present."},
                    {"name": "Recipient binding", "result": "pass", "detail": "audience=bank_a matches this inbox."},
                    {"name": "Expiry", "result": "pass", "detail": "Valid until 2026-11-14T21:52:00Z."},
                    {"name": "Alibi package signature", "result": "pass", "detail": "Ed25519 over JCS body, key alibi-demo-2026."},
                    {"name": "Digest", "result": "pass", "detail": "Body digest matches signed digest."},
                    {"name": "Replay", "result": "pass", "detail": "First receipt of this package ID."},
                ],
                "note": "Authentication shows the package is intact and from the Alibi demo key. It does not make any claim true.",
            },
            "source_trust": [
                {"attachment_id": "att_01", "record_id": "po_7781", "revision": 2, "issuer": "ResaleHub (mock)", "trust": "issuer_verified", "signature_check": "pass", "detail": "Issuer key resalehub-demo-2026 pinned in demo registry."},
                {"attachment_id": "att_02", "record_id": "stl_9001", "revision": 1, "issuer": "ResaleHub (mock)", "trust": "issuer_verified", "signature_check": "pass", "detail": "Issuer key resalehub-demo-2026 pinned in demo registry."},
                {"attachment_id": "att_03", "record_id": "ext_recon_01", "revision": 1, "issuer": "Alibi redacted extract", "trust": "issuer_verified", "signature_check": "pass", "detail": "Extract attested by the package signer; underlying records issuer-signed."},
                {"attachment_id": "att_04", "record_id": "rcpt_self_01", "revision": 1, "issuer": "Imported by holder", "trust": "self_supplied", "signature_check": "none", "detail": "Explicitly self-supplied. No issuer signature expected. Reviewable at lower trust."},
            ],
            "claim_status": [
                {"question_id": "q1", "status": "supported", "summary": "Source of credit: payout PO-7781, settled before booking."},
                {"question_id": "q2", "status": "contradicted", "summary": "Funds did not reach the bank on 2026-11-02."},
                {"question_id": "q3", "status": "unresolved", "summary": "Related-party status: evidence unavailable."},
            ],
            "evidence_by_question": [
                {
                    "question_id": "q1",
                    "question": QUESTIONS[0]["text"],
                    "claim": {"status": "supported", "rationale": CLAIMS[0]["rationale"]},
                    "evidence": [
                        {"attachment_id": "att_01", "record_id": "po_7781", "revision": 2, "trust": "issuer_verified", "summary": "Payout PO-7781 settled, STL-9001", "href": "/bank/bank_a/packages/pkg_a_0001/attachments/att_01"},
                        {"attachment_id": "att_02", "record_id": "stl_9001", "revision": 1, "trust": "issuer_verified", "summary": "Settlement at 2026-11-03T08:45:00Z", "href": "/bank/bank_a/packages/pkg_a_0001/attachments/att_02"},
                        {"attachment_id": "att_03", "record_id": "ext_recon_01", "revision": 1, "trust": "issuer_verified", "summary": "Totals extract", "href": "/bank/bank_a/packages/pkg_a_0001/attachments/att_03"},
                        {"attachment_id": "att_04", "record_id": "rcpt_self_01", "revision": 1, "trust": "self_supplied", "summary": "Seller export CSV", "href": "/bank/bank_a/packages/pkg_a_0001/attachments/att_04"},
                    ],
                    "calculation": {"expression": "240000 - 18000 - 6000 = 216000", "result": money(CREDIT), "computed_by": "verifier", "matches": True},
                    "gap": None,
                },
                {
                    "question_id": "q2",
                    "question": QUESTIONS[1]["text"],
                    "claim": {"status": "contradicted", "rationale": CLAIMS[1]["rationale"]},
                    "evidence": [
                        {"attachment_id": "att_01", "record_id": "po_7781", "revision": 1, "trust": "issuer_verified", "summary": "Revision 1: released_to_balance on 2026-11-02", "href": "/bank/bank_a/packages/pkg_a_0001/attachments/att_01"},
                        {"attachment_id": "att_02", "record_id": "stl_9001", "revision": 1, "trust": "issuer_verified", "summary": "Settlement on 2026-11-03", "href": "/bank/bank_a/packages/pkg_a_0001/attachments/att_02"},
                    ],
                    "calculation": None,
                    "gap": None,
                },
                {
                    "question_id": "q3",
                    "question": QUESTIONS[2]["text"],
                    "claim": {"status": "unresolved", "rationale": CLAIMS[2]["rationale"]},
                    "evidence": [],
                    "calculation": None,
                    "gap": "No available record identifies buyers. Evidence unavailable, not withheld.",
                },
            ],
            "reconciliation": {**RECONCILIATION, "computed_by": "verifier", "computed_at": "2026-11-07T21:53:03Z"},
            "contradictions": [
                {
                    "claim_id": "clm_q2",
                    "question_id": "q2",
                    "description": "Merchant message (po_7781 r1) says funds were released on 2026-11-02; settlement (stl_9001 r1) shows bank transfer on 2026-11-03T08:45:00Z.",
                    "sources": [
                        {"record_id": "po_7781", "revision": 1, "trust": "issuer_verified"},
                        {"record_id": "stl_9001", "revision": 1, "trust": "issuer_verified"},
                    ],
                }
            ],
            "gaps": [
                {"question_id": "q3", "description": "No record identifies buyers.", "reason_code": "evidence_unavailable"}
            ],
            "disposition_form": {
                "action": "/bank/bank_a/packages/pkg_a_0001/disposition",
                "options": [
                    {"value": "clear", "label": "Clear"},
                    {"value": "escalate", "label": "Escalate"},
                    {"value": "request_more", "label": "Request more"},
                ],
                "analyst_name": ANALYST_A["name"],
                "current": None,  # None | {"disposition", "analyst_name", "recorded_at", "note"}
            },
            "followup_form": {
                "action": "/bank/bank_a/requests",
                "parent_request_id": "rfc_a_0001",
                "subject_ref_masked": "acct_****_001",
                "period": {"from": "2026-10-01T00:00:00Z", "to": "2026-11-07T00:00:00Z"},
                "question_prefill": "",
                "consent_note": "A follow-up is a new request with its own ID. Any new disclosure needs the holder's separate approval.",
            },
        }
    )
    return ctx


def ctx_audit(replay_mode: bool) -> dict:
    ctx = base_context("Audit", "holder", holder_nav("audit"), actor=MAYA, replay_mode=replay_mode)
    ctx.update(
        {
            "filter": {"action": "/audit", "query": "", "actor": "", "actors": ["system", "maya", "bank_a", "bank_b"]},
            "entries": [
                {"entry_id": "aud_0101", "at": "2026-11-07T21:53:02Z", "actor": "bank_a", "role": "recipient", "action": "receipt.accepted", "request_id": "rfc_a_0001", "event_id": "rcpt_a_0001", "outcome": "ok", "detail": "Package pkg_a_0001 digest matched; receipt stored."},
                {"entry_id": "aud_0100", "at": "2026-11-07T21:53:00Z", "actor": "system", "role": "backend", "action": "dispatch.sent", "request_id": "rfc_a_0001", "event_id": "pkg_a_0001", "outcome": "ok", "detail": "Approval revalidated; body signed with alibi-demo-2026."},
                {"entry_id": "aud_0099", "at": "2026-11-07T21:52:30Z", "actor": "system", "role": "backend", "action": "dispatch.refused", "request_id": "rfc_a_0001", "event_id": "pkg_a_0001", "outcome": "refused", "detail": "approval_digest_mismatch: body changed after approval."},
                {"entry_id": "aud_0098", "at": "2026-11-07T21:51:00Z", "actor": "maya", "role": "holder", "action": "approval.recorded", "request_id": "rfc_a_0001", "event_id": "appr_0001", "outcome": "ok", "detail": "Approved digest sha256:3f9c1e7a... for bank_a, valid 15 min."},
                {"entry_id": "aud_0097", "at": "2026-11-07T21:43:00Z", "actor": "system", "role": "agents", "action": "investigation.complete", "request_id": "rfc_a_0001", "event_id": "inv_0001", "outcome": "ok", "detail": "7 of 12 calls used. 1 scoped lookup: merchant.get_settlement."},
                {"entry_id": "aud_0096", "at": "2026-11-07T21:41:30Z", "actor": "system", "role": "gateway", "action": "tool.refused", "request_id": "rfc_a_0001", "event_id": "tool_0004", "outcome": "refused", "detail": "Injected instruction asked for acct_other_002; out of subject scope."},
                {"entry_id": "aud_0095", "at": "2026-11-06T10:00:00Z", "actor": "bank_a", "role": "recipient", "action": "rfc.received", "request_id": "rfc_a_0001", "event_id": "rfc_a_0001", "outcome": "ok", "detail": "Signature valid, key bank-a-demo-2026."},
                {"entry_id": "aud_0094", "at": "2026-11-03T08:50:00Z", "actor": "system", "role": "connector", "action": "record.corrected", "request_id": None, "event_id": "evt_po_7781_2", "outcome": "ok", "detail": "po_7781 r2 appended; r1 preserved; 1 claim and 1 draft invalidated."},
                {"entry_id": "aud_0093", "at": "2026-11-02T06:05:00Z", "actor": "system", "role": "connector", "action": "record.ingested", "request_id": None, "event_id": "evt_po_7781_1", "outcome": "ok", "detail": "po_7781 r1 from resalehub-demo-2026."},
                {"entry_id": "aud_0092", "at": "2026-11-02T06:05:01Z", "actor": "system", "role": "connector", "action": "record.duplicate_ignored", "request_id": None, "event_id": "evt_po_7781_1", "outcome": "ok", "detail": "Replay of identical bytes; one active record kept."},
            ],
            "page": {"number": 1, "size": 10, "total": 101, "prev_href": None, "next_href": "/audit?page=2"},
        }
    )
    return ctx


# --------------------------------------------------------------------------
# Registry of preview pages: (output name, template path, context)
# --------------------------------------------------------------------------


def sample_contexts() -> list[tuple[str, str, dict]]:
    return [
        ("passport_home", "passport/home.html", ctx_passport_home()),
        ("transaction_detail", "passport/transaction.html", ctx_transaction_detail()),
        ("alerts", "passport/alerts.html", ctx_alerts(with_lookup_result=False)),
        ("alerts_lookup_unverified", "passport/alerts.html", ctx_alerts(with_lookup_result=True)),
        ("rules", "passport/rules.html", ctx_rules(with_interpretation=False)),
        ("rules_interpretation", "passport/rules.html", ctx_rules(with_interpretation=True)),
        ("request_detail", "requests/detail.html", ctx_request_detail()),
        ("export", "requests/export.html", ctx_export()),
        ("dispatch_success", "requests/dispatch.html", ctx_dispatch("success")),
        ("dispatch_refused", "requests/dispatch.html", ctx_dispatch("refused")),
        ("analyst_inbox", "analyst/inbox.html", ctx_analyst_inbox()),
        ("analyst_package", "analyst/package.html", ctx_analyst_package()),
        ("audit", "audit/list.html", ctx_audit(replay_mode=False)),
        ("audit_replay", "audit/list.html", ctx_audit(replay_mode=True)),
    ]


def make_env() -> Environment:
    """The Jinja environment the app should use. StrictUndefined makes a missing
    variable an error instead of silent blank output."""
    return Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(["html"]),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )


def render_all(out_dir: Path | None = None) -> dict[str, str]:
    """Render every preview page. Returns {output name: html}. Writes files only
    when out_dir is given."""
    env = make_env()
    rendered: dict[str, str] = {}
    for name, template_path, ctx in sample_contexts():
        html = env.get_template(template_path).render(**ctx)
        rendered[name] = html
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        static_out = out_dir / "static"
        static_out.mkdir(exist_ok=True)
        for css in STATIC_DIR.glob("*.css"):
            shutil.copyfile(css, static_out / css.name)
        for name, html in rendered.items():
            # Preview files sit next to a static/ copy, so rewrite the prefix.
            (out_dir / f"{name}.html").write_text(html.replace('"/static/', '"static/'), encoding="utf-8")
        index = "\n".join(f'<li><a href="{n}.html">{n}</a></li>' for n in rendered)
        (out_dir / "index.html").write_text(
            "<!doctype html><meta charset=utf-8><title>Alibi UI preview</title>"
            f"<h1>Alibi UI preview</h1><ul>{index}</ul>",
            encoding="utf-8",
        )
    return rendered


def main(argv: list[str]) -> int:
    out_dir = Path(argv[1]) if len(argv) > 1 else DEFAULT_OUT_DIR
    rendered = render_all(out_dir)
    for name in rendered:
        print(f"rendered {name}.html")
    print(f"wrote {len(rendered)} pages to {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
