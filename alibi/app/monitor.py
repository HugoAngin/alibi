"""Proactive monitoring over the passport: deterministic checks on pushed data only.

``tick(rt)`` re-runs every rule, raises alerts that are due, resolves alerts whose cause has
gone, and returns a summary. It is idempotent: alert ids derive from the kind and the
transaction. The rules follow ``agents/data/README.md`` (Alert family); the two the core
schema cannot hold are listed in ``alibi/BLOCKERS.md``.

The monitor never does a lookup. A payout record whose status is ``released_to_balance`` is a
release to the platform balance, never a settlement, so it does not explain a credit.
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

from agents.data import api as data_api
from alibi.app.runtime import Runtime
from alibi.core import canon
from alibi.core.investigate_rules import reference_tokens
from alibi.core.reconcile import FAILED_STATUSES, PENDING_STATUSES, SETTLED_STATUSES
from alibi.core.schemas import Alert, AlertKind, BankTxn, EvidenceRecord, RecordKind, format_utc

MIN_CREDIT_PENCE = 10000           # £100
UNEXPLAINED_AFTER = timedelta(hours=24)
WINDOW = timedelta(days=5)
UNUSUAL_FACTOR = 3
UNUSUAL_LOOKBACK = timedelta(days=60)
UNUSUAL_MIN_ROWS = 3
_PAYEE = re.compile(r"\(([^()]+)\)\s*$")


def _settlements(records: list[EvidenceRecord]) -> list[EvidenceRecord]:
    return [r for r in records if r.kind == RecordKind.SETTLEMENT]


def _releases(records: list[EvidenceRecord]) -> list[EvidenceRecord]:
    return [r for r in records if r.kind == RecordKind.PAYOUT]


def _explains(settlement: EvidenceRecord, credit: BankTxn) -> bool:
    return (settlement.status in SETTLED_STATUSES and settlement.amount_pence == credit.amount_pence
            and settlement.currency == credit.currency and settlement.effective_at is not None
            and settlement.effective_at <= credit.booked_at
            and credit.booked_at - settlement.effective_at <= WINDOW)


def _named(records: list[EvidenceRecord], tokens: frozenset[str]) -> list[EvidenceRecord]:
    return [r for r in records if (r.payment_ref and r.payment_ref.upper() in tokens)
            or (r.settlement_ref and r.settlement_ref.upper() in tokens)]


def evaluate(rt: Runtime) -> list[dict[str, Any]]:
    """Every alert that should exist now, as dicts ``{alert, open}``."""
    store, now = rt.store, rt.clock()
    records = store.list_sources(rt.subject_ref)
    txns = store.list_bank_txns(rt.subject_ref)
    settlements, releases = _settlements(records), _releases(records)
    wanted: list[dict[str, Any]] = []

    def alert(kind: AlertKind, txn: BankTxn, *, severity: str, title: str, explanation: str,
              refs: list, checks: list[str], raised_at, open_: bool) -> None:
        wanted.append({"open": open_, "alert": Alert(
            alert_id=canon.make_id("alrt", kind.value, txn.txn_id), subject_ref=rt.subject_ref,
            kind=kind, severity=severity, title=title[:300], explanation=explanation[:4000],
            refs=refs, suggested_evidence=[s[:300] for s in checks], created_at=raised_at)})

    for txn in txns:
        tokens = reference_tokens(txn)
        received = txn.provenance.retrieved_at
        if txn.is_credit:
            named_settlements = _named(settlements, tokens)
            named_releases = _named(releases, tokens)
            candidates = [s for s in settlements if _explains(s, txn)]
            explained = any(_explains(s, txn) for s in named_settlements) or \
                (not tokens and len(candidates) == 1) or (tokens and not named_settlements and len(candidates) == 1)
            due = now >= received + UNEXPLAINED_AFTER
            if txn.amount_pence >= MIN_CREDIT_PENCE and due:
                checks = []
                refs = [txn.ref()]
                for rel in named_releases:
                    refs.append(rel.ref())
                    checks.append(f"Retrieve the settlement record for payout reference {rel.payment_ref} "
                                  "from the merchant: the pushed message only shows a release to the "
                                  "platform balance, not a bank transfer.")
                if not checks:
                    same_amount = [r for r in releases if r.amount_pence == txn.amount_pence
                                   and r.currency == txn.currency and r.occurred_at <= txn.booked_at
                                   and txn.booked_at - r.occurred_at <= WINDOW]
                    for rel in same_amount:
                        refs.append(rel.ref())
                        checks.append(f"A release to the seller balance for the same amount names withdrawal "
                                      f"{rel.payment_ref or rel.record_id}; retrieve its settlement record from the "
                                      "merchant to see whether and when it reached the bank.")
                if not checks:
                    checks.append("Ask the payer for a remittance advice or a settlement record that names "
                                  "this credit's reference and amount.")
                    checks.append("Check the merchant connection for a payout that settled on or before "
                                  f"{format_utc(txn.booked_at)[:10]} for {txn.amount_pence} {txn.currency}.")
                explanation = (f"Credit {txn.txn_id} of {txn.amount_pence} {txn.currency} booked "
                               f"{format_utc(txn.booked_at)} has no settlement record in the passport that "
                               "names its reference and matches its amount and timing.")
                if named_releases:
                    explanation += (f" The merchant's message names withdrawal {named_releases[0].payment_ref}, "
                                    "but a release to the seller balance is not a bank settlement.")
                alert(AlertKind.UNEXPLAINED_CREDIT, txn, severity="warning",
                      title=f"Unexplained credit of {txn.amount_pence} {txn.currency}",
                      explanation=explanation, refs=refs, checks=checks,
                      raised_at=received + UNEXPLAINED_AFTER, open_=not explained)
            for s in named_settlements:
                if s.amount_pence != txn.amount_pence or s.currency != txn.currency:
                    alert(AlertKind.AMOUNT_DISCREPANCY, txn, severity="warning",
                          title=f"Settlement {s.payment_ref} differs from the credit",
                          explanation=(f"The credit's reference names settlement record {s.record_id} for "
                                       f"{s.amount_pence} {s.currency}, but the credit is {txn.amount_pence} "
                                       f"{txn.currency}."),
                          refs=[txn.ref(), s.ref()],
                          checks=["Compare the payout's components with the credit; ask the merchant for the "
                                  "settlement breakdown."], raised_at=max(received, s.received_at), open_=True)
                late = s.status in SETTLED_STATUSES and s.effective_at is not None and s.effective_at > txn.booked_at
                if s.status in PENDING_STATUSES or s.status in FAILED_STATUSES or late:
                    alert(AlertKind.SETTLEMENT_DISCREPANCY, txn, severity="warning",
                          title=f"Settlement {s.payment_ref} was not settled when the credit was booked",
                          explanation=(f"Settlement record {s.record_id} has status {s.status}"
                                       + (f", effective {format_utc(s.effective_at)}" if s.effective_at else "")
                                       + f"; the credit was booked {format_utc(txn.booked_at)}. A later or "
                                       "pending settlement does not explain an earlier credit."),
                          refs=[txn.ref(), s.ref()],
                          checks=["Ask the merchant which transfer this credit is, with its settlement time."],
                          raised_at=max(received, s.received_at), open_=True)
        # unusual inflow / outflow
        same_dir = [t for t in txns if t.txn_id != txn.txn_id and t.is_credit == txn.is_credit
                    and txn.booked_at - UNUSUAL_LOOKBACK <= t.booked_at < txn.booked_at]
        if len(same_dir) >= UNUSUAL_MIN_ROWS:
            biggest = max(abs(t.amount_pence) for t in same_dir)
            if abs(txn.amount_pence) >= UNUSUAL_FACTOR * biggest:
                kind = AlertKind.UNUSUAL_INFLOW if txn.is_credit else AlertKind.UNUSUAL_OUTFLOW
                alert(kind, txn, severity="info" if txn.is_credit else "warning",
                      title=f"Unusual {'inflow' if txn.is_credit else 'outflow'} of {abs(txn.amount_pence)} {txn.currency}",
                      explanation=(f"{txn.txn_id} is at least {UNUSUAL_FACTOR} times the largest "
                                   f"{'credit' if txn.is_credit else 'debit'} ({biggest} {txn.currency}) booked in "
                                   f"the previous {UNUSUAL_LOOKBACK.days} days ({len(same_dir)} rows)."),
                      refs=[txn.ref()], checks=["Confirm the source or payee from a record you hold."],
                      raised_at=received, open_=True)
        # merchant verification for debits
        if not txn.is_credit:
            payee = _PAYEE.search(txn.description)
            if payee:
                result = data_api.merchant_lookup(payee.group(1), format_utc(received))
                if result["status"] != "verified":
                    alert(AlertKind.MERCHANT_UNVERIFIED, txn, severity="info",
                          title=f"Payee not in the registry: {payee.group(1)}",
                          explanation=(f"The payee of {txn.txn_id} is not in the mock merchant registry. "
                                       "Unverified means unknown to the registry, not fraudulent."),
                          refs=[txn.ref()],
                          checks=["Look the merchant up before paying again; keep the receipt."],
                          raised_at=received, open_=True)
    return wanted


def tick(rt: Runtime) -> dict[str, Any]:
    """Apply ``evaluate`` to the store: save new alerts, resolve cleared ones."""
    store = rt.store
    with rt.lock:
        existing = {a.alert_id: a for a in store.list_alerts(rt.subject_ref)}
        raised, resolved, kept = [], [], []
        for item in evaluate(rt):
            alert, open_ = item["alert"], item["open"]
            current = existing.pop(alert.alert_id, None)
            if current is None:
                if open_:
                    store.save_alert(alert)
                    raised.append(alert.alert_id)
                continue
            if current.state == "open" and not open_:
                store.set_alert_state(alert.alert_id, "resolved")
                resolved.append(alert.alert_id)
            elif current.state == "resolved" and open_:
                store.set_alert_state(alert.alert_id, "open")
                raised.append(alert.alert_id)
            else:
                kept.append(alert.alert_id)
        for alert_id, current in existing.items():    # cause gone entirely
            if current.state == "open":
                store.set_alert_state(alert_id, "resolved")
                resolved.append(alert_id)
        rt.sidecar.set("last_tick", rt.now_text())
        rt.audit(action="tick", outcome="ok", target_id=rt.subject_ref,
                 detail=f"monitor: {len(raised)} raised, {len(resolved)} resolved, {len(kept)} unchanged")
    return {"raised": raised, "resolved": resolved, "kept": kept, "at": rt.now_text()}
