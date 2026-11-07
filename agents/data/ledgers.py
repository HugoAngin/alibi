"""Development case ledgers for Alibi: synthetic, labelled, deterministic.

Writes one JSON file per case to agents/data/out/cases/ plus out/index.json.

    uv run --frozen python -m agents.data.ledgers

Every amount is integer pence (GBP), every time is UTC ISO 8601 with Z, as pinned in
shared/CONTRACTS.md. Case numbers stay below shared/splits.json lockbox_from (1000):
these are development cases, never sealed evaluation cases.

Families (lead's 21:40Z assignment): clean, bundled_payment, balance_vs_settlement,
pending_correction, duplicate_record, missing_proof. Case 1 is the canonical Maya fixture
(240000 - 18000 - 6000 = 216000).

The `gold` block is built by construction, not by running a matcher. Gold rule used here
(data's assumption until alibi/SPEC.md says otherwise, see agents/data/README.md):
a bank credit is `linked` when exactly one compatible candidate exists, where a candidate is
one bank `payout` (or one settlement batch of payouts) with the same amount, occurred_at at or
before the credit's booked_at and at most 5 days before it. A `balance_release` is never a
candidate. Several candidates make it `ambiguous`, none make it `unmatched`.
"""
from __future__ import annotations

import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

OUT = Path(__file__).resolve().parent / "out"
SCHEMA_VERSION = "1"
CURRENCY = "GBP"
ISSUER = "mock-marketplace"          # synthetic marketplace, simulated service
SETTLEMENT_WINDOW_DAYS = 5
FEE_PER_MILLE = 25                   # 2.5% marketplace fee, rounded half up per sale
Q_SOURCE = "q.source_of_funds"
Q_BREAKDOWN = "q.breakdown"
Q_INCOME = "q.income_in_period"
LABEL = "SYNTHETIC - labelled test data, not a real person or account"


def iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fee_for(amount: int) -> int:
    return (amount * FEE_PER_MILLE + 500) // 1000


class Case:
    """Builds one case: deliveries in arrival order, an RFC, a holder claim and gold."""

    def __init__(self, number: int, family: str, variant: str, title: str, subject: str):
        self.number, self.family, self.variant, self.title = number, family, variant, title
        self.subject = subject
        self.merchant: list[dict] = []
        self.bank: list[dict] = []
        self.ingest: list[dict] = []
        self.active: dict[str, int] = {}
        self._evt = 0
        self._rec = 0
        self._txn = 0

    # ids -----------------------------------------------------------------
    def new_record_id(self) -> str:
        self._rec += 1
        return f"rec-{self.number:04d}-{self._rec:03d}"

    def new_event_id(self) -> str:
        self._evt += 1
        return f"evt-{self.number:04d}-{self._evt:03d}"

    def new_txn_id(self) -> str:
        self._txn += 1
        return f"txn-{self.number:04d}-{self._txn:03d}"

    # merchant ------------------------------------------------------------
    def event(self, kind: str, amount: int, at: datetime, *, record_id: str | None = None,
              revision: int = 1, received: datetime | None = None, **extra) -> dict:
        ev = {
            "schema_version": SCHEMA_VERSION,
            "event_id": self.new_event_id(),
            "record_id": record_id or self.new_record_id(),
            "revision": revision,
            "issuer_id": ISSUER,
            "subject_ref": self.subject,
            "kind": kind,
            "occurred_at": iso(at),
            "amount_minor": amount,
            "currency": CURRENCY,
        }
        ev.update(extra)
        self.deliver_merchant(ev, received or at + timedelta(minutes=5), "accepted")
        if revision > self.active.get(ev["record_id"], 0):
            self.active[ev["record_id"]] = revision
        return ev

    def deliver_merchant(self, ev: dict, received: datetime, outcome: str) -> None:
        self.ingest.append({"connector": "merchant", "index": len(self.merchant),
                            "event_id": ev["event_id"], "outcome": outcome})
        self.merchant.append({"received_at": iso(received), "event": ev})

    # bank ----------------------------------------------------------------
    def bank_row(self, amount: int, at: datetime, description: str, counterparty: str,
                 *, txn_id: str | None = None, revision: int = 1) -> dict:
        row = {
            "txn_id": txn_id or self.new_txn_id(),
            "revision": revision,
            "account_ref": f"acct-{self.subject}",
            "booked_at": iso(at),
            "amount_minor": amount,
            "currency": CURRENCY,
            "description": description,
            "counterparty_name": counterparty,
        }
        self.deliver_bank(row, at + timedelta(hours=1), "accepted")
        return row

    def deliver_bank(self, row: dict, received: datetime, outcome: str) -> None:
        self.ingest.append({"connector": "bank", "index": len(self.bank),
                            "txn_id": row["txn_id"], "outcome": outcome})
        self.bank.append({"received_at": iso(received), "row": row})

    # sales history ---------------------------------------------------------
    def sales(self, amounts: list[int], start: datetime, refunds: dict[int, int] | None = None,
              gap_hours: int = 30) -> dict:
        """Sales with a 2.5% fee each, and partial refunds keyed by sale index."""
        refunds = refunds or {}
        ids = {"sale": [], "refund": [], "fee": []}
        totals = {"sales": 0, "refunds": 0, "fees": 0}
        t = start
        for i, amount in enumerate(amounts):
            sale = self.event("sale", amount, t, item=f"second-hand clothing lot {i + 1}")
            fee = self.event("fee", fee_for(amount), t + timedelta(minutes=1),
                             related_record_id=sale["record_id"], fee_type="marketplace_commission")
            ids["sale"].append(sale["record_id"])
            ids["fee"].append(fee["record_id"])
            totals["sales"] += amount
            totals["fees"] += fee["amount_minor"]
            if i in refunds:
                ref = self.event("refund", refunds[i], t + timedelta(hours=20),
                                 related_record_id=sale["record_id"], status="completed")
                ids["refund"].append(ref["record_id"])
                totals["refunds"] += refunds[i]
            t += timedelta(hours=gap_hours)
        totals["net"] = totals["sales"] - totals["refunds"] - totals["fees"]
        return {"ids": ids, "totals": totals, "end": t}

    def payout(self, amount: int, at: datetime, covers: list[str], **extra) -> dict:
        return self.event("payout", amount, at, destination="bank", covers_record_ids=covers,
                          **extra)

    def balance_release(self, amount: int, at: datetime, covers: list[str]) -> dict:
        return self.event("balance_release", amount, at, destination="platform_balance",
                          covers_record_ids=covers)

    # output ----------------------------------------------------------------
    def finish(self, *, rfcs: list[dict], claim: dict, links: list[dict],
               breakdown: dict | None, claim_status: str, notes: str) -> dict:
        return {
            "case_number": self.number,
            "family": self.family,
            "variant": self.variant,
            "title": self.title,
            "label": LABEL,
            "subject_ref": self.subject,
            "merchant_deliveries": self.merchant,
            "bank_deliveries": self.bank,
            "rfcs": rfcs,
            "holder_claim": claim,
            "gold": {
                "links": links,
                "active_revisions": dict(sorted(self.active.items())),
                "breakdown_minor": breakdown,
                "claim_status": claim_status,
                "ingest": sorted(self.ingest, key=lambda i: (i["connector"] != "merchant", i["index"])),
                "notes": notes,
            },
        }


def rfc(case: Case, txn_id: str | None, issued: datetime, *, audience: str = "bank_a",
        questions: tuple[str, ...] = (Q_SOURCE, Q_BREAKDOWN), period: tuple | None = None,
        parent: str | None = None, seq: int = 1) -> dict:
    return {
        "request_id": f"rfc-{case.number:04d}-{seq:02d}",
        "parent_request_id": parent,
        "audience": audience,
        "subject_ref": case.subject,
        "txn_ids": [txn_id] if txn_id else [],
        "period": {"from": iso(period[0]), "to": iso(period[1])} if period else None,
        "question_ids": list(questions),
        "issued_at": iso(issued),
        "expires_at": iso(issued + timedelta(days=7)),
    }


def claim(txn_id: str, sales: int, refunds: int, fees: int,
          explanation: str = "marketplace_net_proceeds") -> dict:
    return {"txn_id": txn_id, "explanation": explanation,
            "stated_minor": {"sales": sales, "refunds": refunds, "fees": fees,
                             "net": sales - refunds - fees}}


def linked(txn_id: str, payout_ids: list[str]) -> dict:
    return {"txn_id": txn_id, "state": "linked", "payout_record_ids": payout_ids}


def unmatched(txn_id: str) -> dict:
    return {"txn_id": txn_id, "state": "unmatched", "payout_record_ids": []}


def everyday_rows(case: Case, start: datetime) -> None:
    """Unrelated account activity, so the credit is not the only row."""
    case.bank_row(-65000, start + timedelta(days=1, hours=9), "RENT OCT", "Synthetic Lettings Ltd")
    case.bank_row(-1240, start + timedelta(days=3, hours=13), "CARD GROCERY", "Synthetic Grocer")
    case.bank_row(-899, start + timedelta(days=6, hours=18), "CARD STREAMING", "Synthetic Media")


# --------------------------------------------------------------------------
# families
# --------------------------------------------------------------------------

def canonical() -> dict:
    """Case 1: Maya. £2,400 sales - £180 refunds - £60 fees = £2,160 credit."""
    c = Case(1, "clean", "canonical_maya", "Maya's £2,160 marketplace credit", "maya-001")
    start = datetime(2026, 10, 5, 10, 0, tzinfo=timezone.utc)
    everyday_rows(c, start)
    amounts = [30000, 25000, 40000, 35000, 20000, 45000, 15000, 30000]   # 240000
    s = c.sales(amounts, start, refunds={1: 10000, 4: 8000})            # 18000, fees 6000
    assert s["totals"] == {"sales": 240000, "refunds": 18000, "fees": 6000, "net": 216000}
    covers = s["ids"]["sale"] + s["ids"]["refund"] + s["ids"]["fee"]
    p = c.payout(216000, datetime(2026, 10, 26, 8, 0, tzinfo=timezone.utc), covers)
    credit = c.bank_row(216000, datetime(2026, 10, 28, 6, 30, tzinfo=timezone.utc),
                        "MOCK MARKETPLACE PAYOUT", "Mock Marketplace Payments")
    first = rfc(c, credit["txn_id"], datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc))
    second = rfc(c, None, datetime(2026, 11, 4, 14, 0, tzinfo=timezone.utc), audience="bank_b",
                 questions=(Q_INCOME,), seq=2,
                 period=(datetime(2026, 10, 1, tzinfo=timezone.utc),
                         datetime(2026, 11, 1, tzinfo=timezone.utc)))
    return c.finish(
        rfcs=[first, second], claim=claim(credit["txn_id"], 240000, 18000, 6000),
        links=[linked(credit["txn_id"], [p["record_id"]])], breakdown=s["totals"],
        claim_status="supported",
        notes="Demo fixture. rfcs[1] is a different question from a second recipient (bank_b), "
              "answered from the same persisted passport with a fresh approval.")


def random_amounts(rng: random.Random, n: int) -> list[int]:
    return [rng.randrange(1500, 50001, 500) for _ in range(n)]


def random_refunds(rng: random.Random, amounts: list[int], k: int) -> dict[int, int]:
    idx = rng.sample(range(len(amounts)), k)
    return {i: rng.randrange(500, amounts[i] // 2 + 1, 500) for i in idx}


def start_for(rng: random.Random) -> datetime:
    return datetime(2026, rng.randint(6, 9), rng.randint(1, 10), rng.randint(8, 18),
                    tzinfo=timezone.utc)


def clean(number: int, variant: str, rng: random.Random, contradicted: bool = False) -> dict:
    c = Case(number, "clean", variant,
             "One payout settles one credit" + (" - holder omits refunds and fees" if contradicted else ""),
             f"seller-{number:04d}")
    start = start_for(rng)
    everyday_rows(c, start)
    amounts = random_amounts(rng, rng.randint(3, 8))
    s = c.sales(amounts, start, refunds=random_refunds(rng, amounts, rng.randint(0, 2)))
    t = s["end"] + timedelta(days=2)
    covers = s["ids"]["sale"] + s["ids"]["refund"] + s["ids"]["fee"]
    net = s["totals"]["net"]
    p = c.payout(net, t, covers)
    credit = c.bank_row(net, t + timedelta(days=rng.randint(1, 3)), "MOCK MARKETPLACE PAYOUT",
                        "Mock Marketplace Payments")
    tot = s["totals"]
    stated = claim(credit["txn_id"], net, 0, 0) if contradicted else \
        claim(credit["txn_id"], tot["sales"], tot["refunds"], tot["fees"])
    return c.finish(
        rfcs=[rfc(c, credit["txn_id"], t + timedelta(days=6))], claim=stated,
        links=[linked(credit["txn_id"], [p["record_id"]])], breakdown=tot,
        claim_status="contradicted" if contradicted else "supported",
        notes="Holder states gross sales equal to the credit, but the linked payout shows refunds "
              "and fees, so the evidence conflicts with the claim." if contradicted else
              "Single exact candidate; arithmetic over the payout's covered records.")


def bundled(number: int, variant: str, rng: random.Random, trap: bool = False) -> dict:
    """Two payouts in one settlement batch arrive as one bank credit."""
    c = Case(number, "bundled_payment", variant,
             "Two payouts bundled into one bank credit" + (" - later same-amount payout" if trap else ""),
             f"seller-{number:04d}")
    start = start_for(rng)
    everyday_rows(c, start)
    a = c.sales(random_amounts(rng, rng.randint(2, 4)), start)
    b = c.sales(random_amounts(rng, rng.randint(2, 4)), a["end"] + timedelta(hours=6))
    t = b["end"] + timedelta(days=1)
    batch = f"batch-{number:04d}-01"
    pa = c.payout(a["totals"]["net"], t, a["ids"]["sale"] + a["ids"]["fee"], settlement_batch_id=batch)
    pb = c.payout(b["totals"]["net"], t + timedelta(minutes=2), b["ids"]["sale"] + b["ids"]["fee"],
                  settlement_batch_id=batch)
    total = a["totals"]["net"] + b["totals"]["net"]
    booked = t + timedelta(days=2)
    credit = c.bank_row(total, booked, "MOCK MARKETPLACE PAYOUT BATCH", "Mock Marketplace Payments")
    notes = "Neither payout alone equals the credit; the settlement batch does."
    if trap:
        later = c.sales(random_amounts(rng, 2), booked + timedelta(days=1))
        # A later payout with exactly the credit's amount: occurs after the credit, never explains it.
        c.payout(total, booked + timedelta(days=4), later["ids"]["sale"] + later["ids"]["fee"],
                 note="same amount as the earlier credit by construction")
        notes += " A later payout has the same amount but occurs after the credit, so it is not a candidate."
    tot = {k: a["totals"][k] + b["totals"][k] for k in a["totals"]}
    return c.finish(
        rfcs=[rfc(c, credit["txn_id"], booked + timedelta(days=4, hours=2))],
        claim=claim(credit["txn_id"], tot["sales"], tot["refunds"], tot["fees"]),
        links=[linked(credit["txn_id"], [pa["record_id"], pb["record_id"]])], breakdown=tot,
        claim_status="supported", notes=notes)


def two_candidates(number: int, variant: str, rng: random.Random) -> dict:
    """Two unbatched payouts, each equal to the one credit and both in the window: ambiguous."""
    c = Case(number, "bundled_payment", variant,
             "Two separate payouts each match the one credit", f"seller-{number:04d}")
    start = start_for(rng)
    everyday_rows(c, start)
    amounts = random_amounts(rng, 3)
    a = c.sales(amounts, start)
    b = c.sales(amounts, a["end"] + timedelta(hours=6))   # same lots resold: same net
    t = b["end"] + timedelta(days=1)
    c.payout(a["totals"]["net"], t, a["ids"]["sale"] + a["ids"]["fee"])
    c.payout(b["totals"]["net"], t + timedelta(hours=3), b["ids"]["sale"] + b["ids"]["fee"])
    booked = t + timedelta(days=2)
    credit = c.bank_row(a["totals"]["net"], booked, "MOCK MARKETPLACE PAYOUT",
                        "Mock Marketplace Payments")
    tot = a["totals"]
    return c.finish(
        rfcs=[rfc(c, credit["txn_id"], booked + timedelta(days=4))],
        claim=claim(credit["txn_id"], tot["sales"], tot["refunds"], tot["fees"]),
        links=[{"txn_id": credit["txn_id"], "state": "ambiguous", "payout_record_ids": []}],
        breakdown=None, claim_status="unresolved",
        notes="Two compatible payouts and no batch id: no unique candidate, so no exact link. "
              "The second payout's bank credit has not arrived yet.")


def balance_vs_settlement(number: int, variant: str, rng: random.Random) -> dict:
    """variant: settled | release_only | settles_after_credit."""
    titles = {"settled": "Balance release, then bank settlement",
              "release_only": "Funds released to platform balance, never settled to the bank",
              "settles_after_credit": "Bank settlement occurs after the credit it seems to explain"}
    c = Case(number, "balance_vs_settlement", variant, titles[variant], f"seller-{number:04d}")
    start = start_for(rng)
    everyday_rows(c, start)
    amounts = random_amounts(rng, rng.randint(3, 6))
    s = c.sales(amounts, start, refunds=random_refunds(rng, amounts, 1))
    covers = s["ids"]["sale"] + s["ids"]["refund"] + s["ids"]["fee"]
    net = s["totals"]["net"]
    released = s["end"] + timedelta(days=1)
    c.balance_release(net, released, covers)
    booked = released + timedelta(days=2)
    tot = s["totals"]
    if variant == "settled":
        p = c.payout(net, released + timedelta(days=1), covers)
        credit = c.bank_row(net, booked, "MOCK MARKETPLACE PAYOUT", "Mock Marketplace Payments")
        links, status, bd = [linked(credit["txn_id"], [p["record_id"]])], "supported", tot
        notes = "The credit links to the bank payout, not to the earlier balance release."
    elif variant == "release_only":
        # The credit comes from elsewhere with the same amount; the release is no bank settlement.
        credit = c.bank_row(net, booked, "FASTER PAYMENT", "Synthetic Sender")
        links, status, bd = [unmatched(credit["txn_id"])], "unresolved", None
        notes = ("A balance release with the same amount is not a bank settlement, so it cannot "
                 "explain the credit. No proof is available.")
    else:
        credit = c.bank_row(net, booked, "FASTER PAYMENT", "Synthetic Sender")
        c.payout(net, booked + timedelta(days=3), covers)
        links, status, bd = [unmatched(credit["txn_id"])], "unresolved", None
        notes = ("The only bank payout with this amount occurs three days after the credit, so it "
                 "cannot explain it.")
    return c.finish(
        rfcs=[rfc(c, credit["txn_id"], booked + timedelta(days=5))],
        claim=claim(credit["txn_id"], tot["sales"], tot["refunds"], tot["fees"]),
        links=links, breakdown=bd, claim_status=status, notes=notes)


def pending_correction(number: int, variant: str, rng: random.Random) -> dict:
    """variant: refund_completed | sale_adjusted | stale_redelivery | holder_uses_old."""
    titles = {"refund_completed": "Pending refund corrected to its completed amount",
              "sale_adjusted": "Sale price corrected by a newer revision",
              "stale_redelivery": "Stale revision redelivered after the correction",
              "holder_uses_old": "Holder states the pre-correction refund amount"}
    c = Case(number, "pending_correction", variant, titles[variant], f"seller-{number:04d}")
    start = start_for(rng)
    everyday_rows(c, start)
    amounts = random_amounts(rng, rng.randint(3, 6))
    s = c.sales(amounts, start)
    tot = dict(s["totals"])
    t = s["end"]
    covers = s["ids"]["sale"] + s["ids"]["fee"]
    if variant == "sale_adjusted":
        sale_id = s["ids"]["sale"][0]
        old = amounts[0]
        new = old - rng.randrange(500, old // 2 + 1, 500)
        c.event("sale", new, t, record_id=sale_id, revision=2, item="second-hand clothing lot 1",
                correction_reason="price adjusted after dispute")
        # The fee is charged on the original price and not corrected (assumption, documented).
        tot["sales"] += new - old
        old_refund_stated = None
    else:
        sale_id = s["ids"]["sale"][-1]
        pending_amt = rng.randrange(1000, amounts[-1] // 2 + 1, 500)
        final_amt = pending_amt - 500
        first = c.event("refund", pending_amt, t, related_record_id=sale_id, status="pending")
        corrected = c.event("refund", final_amt, t + timedelta(days=1), record_id=first["record_id"],
                            revision=2, related_record_id=sale_id, status="completed")
        if variant == "stale_redelivery":
            # Revision 1 arrives again, later than revision 2. It must not replace it.
            stale = dict(first)
            stale["event_id"] = c.new_event_id()
            c.deliver_merchant(stale, t + timedelta(days=1, hours=3), "stale_revision_ignored")
        tot["refunds"] += final_amt
        old_refund_stated = pending_amt
        covers = covers + [corrected["record_id"]]
    tot["net"] = tot["sales"] - tot["refunds"] - tot["fees"]
    paid = t + timedelta(days=2)
    p = c.payout(tot["net"], paid, covers)
    credit = c.bank_row(tot["net"], paid + timedelta(days=2), "MOCK MARKETPLACE PAYOUT",
                        "Mock Marketplace Payments")
    if variant == "holder_uses_old":
        stated = claim(credit["txn_id"], tot["sales"], old_refund_stated, tot["fees"])
        status = "contradicted"
        notes = "The holder quotes the superseded pending refund; the active revision differs."
    else:
        stated = claim(credit["txn_id"], tot["sales"], tot["refunds"], tot["fees"])
        status = "supported"
        notes = "Arithmetic uses the active (highest) revision of each record; old revisions are kept."
    return c.finish(
        rfcs=[rfc(c, credit["txn_id"], paid + timedelta(days=5))], claim=stated,
        links=[linked(credit["txn_id"], [p["record_id"]])], breakdown=tot, claim_status=status,
        notes=notes)


def duplicate_record(number: int, variant: str, rng: random.Random) -> dict:
    """variant: exact_redelivery | conflicting_bytes | bank_row_redelivery | double_count_trap."""
    titles = {"exact_redelivery": "Same merchant event delivered twice",
              "conflicting_bytes": "Same event_id redelivered with different bytes",
              "bank_row_redelivery": "Same bank row delivered twice",
              "double_count_trap": "Duplicated sales would overstate the total"}
    c = Case(number, "duplicate_record", variant, titles[variant], f"seller-{number:04d}")
    start = start_for(rng)
    everyday_rows(c, start)
    amounts = random_amounts(rng, rng.randint(3, 6))
    s = c.sales(amounts, start, refunds=random_refunds(rng, amounts, 1))
    first_sale = c.merchant[0]["event"]  # sales() delivers the first sale first
    t = s["end"]
    if variant in ("exact_redelivery", "double_count_trap"):
        dupes = [d["event"] for d in c.merchant if d["event"]["kind"] == "sale"]
        dupes = dupes if variant == "double_count_trap" else dupes[:1]
        for i, ev in enumerate(dupes):
            c.deliver_merchant(dict(ev), t + timedelta(hours=i + 1), "duplicate_ignored")
    elif variant == "conflicting_bytes":
        bad = dict(first_sale)
        bad["amount_minor"] = first_sale["amount_minor"] * 2
        c.deliver_merchant(bad, t + timedelta(hours=1), "rejected_conflict")
    tot = s["totals"]
    covers = s["ids"]["sale"] + s["ids"]["refund"] + s["ids"]["fee"]
    paid = t + timedelta(days=1)
    p = c.payout(tot["net"], paid, covers)
    credit = c.bank_row(tot["net"], paid + timedelta(days=2), "MOCK MARKETPLACE PAYOUT",
                        "Mock Marketplace Payments")
    if variant == "bank_row_redelivery":
        c.deliver_bank(dict(credit), paid + timedelta(days=2, hours=5), "duplicate_ignored")
    notes = {"exact_redelivery": "Identical bytes for an existing event_id are accepted once.",
             "conflicting_bytes": "Same event_id with different bytes is rejected; the original stands.",
             "bank_row_redelivery": "Identical bank row redelivered; one row, one credit.",
             "double_count_trap": "Every sale arrives twice; counting duplicates would overstate sales."}[variant]
    return c.finish(
        rfcs=[rfc(c, credit["txn_id"], paid + timedelta(days=5))],
        claim=claim(credit["txn_id"], tot["sales"], tot["refunds"], tot["fees"]),
        links=[linked(credit["txn_id"], [p["record_id"]])], breakdown=tot,
        claim_status="supported", notes=notes)


def missing_proof(number: int, variant: str, rng: random.Random) -> dict:
    """variant: no_merchant_data | no_payout | amount_mismatch | missing_covered_record."""
    titles = {"no_merchant_data": "Credit with no merchant records at all",
              "no_payout": "Sales exist but no payout record",
              "amount_mismatch": "Payout amount differs from the credit",
              "missing_covered_record": "Payout cites a fee record that never arrived"}
    c = Case(number, "missing_proof", variant, titles[variant], f"seller-{number:04d}")
    start = start_for(rng)
    everyday_rows(c, start)
    amounts = random_amounts(rng, rng.randint(3, 6))
    claimed_net = None
    if variant == "no_merchant_data":
        tot = {"sales": sum(amounts), "refunds": 0, "fees": sum(fee_for(a) for a in amounts)}
        tot["net"] = tot["sales"] - tot["fees"]
        booked = start + timedelta(days=12)
        credit = c.bank_row(tot["net"], booked, "FASTER PAYMENT", "Synthetic Sender")
        links, bd, status = [unmatched(credit["txn_id"])], None, "unresolved"
        notes = "The holder's explanation has no records behind it. Alibi must not invent support."
    else:
        s = c.sales(amounts, start)
        tot = s["totals"]
        covers = s["ids"]["sale"] + s["ids"]["fee"]
        paid = s["end"] + timedelta(days=1)
        booked = paid + timedelta(days=2)
        if variant == "no_payout":
            credit = c.bank_row(tot["net"], booked, "MOCK MARKETPLACE PAYOUT",
                                "Mock Marketplace Payments")
            links, bd, status = [unmatched(credit["txn_id"])], None, "unresolved"
            notes = "Sales and fees are on file but no payout record links them to the credit."
        elif variant == "amount_mismatch":
            c.payout(tot["net"], paid, covers)
            claimed_net = tot["net"] + rng.randrange(1000, 20001, 500)
            credit = c.bank_row(claimed_net, booked, "MOCK MARKETPLACE PAYOUT",
                                "Mock Marketplace Payments")
            links, bd, status = [unmatched(credit["txn_id"])], None, "unresolved"
            notes = "No payout has the credit's amount; part of the credit has no explanation."
        else:
            # Drop the last fee event from the deliveries: the payout still cites it.
            missing_fee = s["ids"]["fee"][-1]
            kept = [d for d in c.merchant if d["event"]["record_id"] != missing_fee]
            c.merchant = kept
            c.ingest = [i for i in c.ingest if i["connector"] != "merchant"] + [
                {"connector": "merchant", "index": k, "event_id": d["event"]["event_id"],
                 "outcome": "accepted"} for k, d in enumerate(kept)]
            c.active.pop(missing_fee)
            p = c.payout(tot["net"], paid, covers)
            credit = c.bank_row(tot["net"], booked, "MOCK MARKETPLACE PAYOUT",
                                "Mock Marketplace Payments")
            links, bd, status = [linked(credit["txn_id"], [p["record_id"]])], None, "unresolved"
            notes = (f"The payout links to the credit, but it cites {missing_fee}, which never "
                     "arrived, so the fee total cannot be verified from records.")
    stated = claim(credit["txn_id"], tot["sales"], tot["refunds"], tot["fees"])
    return c.finish(rfcs=[rfc(c, credit["txn_id"], booked + timedelta(days=5))], claim=stated,
                    links=links, breakdown=bd, claim_status=status, notes=notes)


def build_cases(seed: int = 20261107) -> list[dict]:
    """All development cases, numbered from 1. Deterministic for a given seed."""
    rng = random.Random(seed)
    cases = [canonical()]
    plan = [
        (clean, ["a", "b", "c"], {}), (clean, ["omits_refunds_fees"], {"contradicted": True}),
        (bundled, ["a", "b"], {}), (bundled, ["later_same_amount"], {"trap": True}),
        (two_candidates, ["two_candidates"], {}),
    ]
    n = 2
    for fn, variants, kw in plan:
        for v in variants:
            cases.append(fn(n, v, random.Random(rng.random()), **kw))
            n += 1
    for fn, variants in [
        (balance_vs_settlement, ["settled", "release_only", "settles_after_credit", "settled"]),
        (pending_correction, ["refund_completed", "sale_adjusted", "stale_redelivery", "holder_uses_old"]),
        (duplicate_record, ["exact_redelivery", "conflicting_bytes", "bank_row_redelivery", "double_count_trap"]),
        (missing_proof, ["no_merchant_data", "no_payout", "amount_mismatch", "missing_covered_record"]),
    ]:
        for v in variants:
            cases.append(fn(n, v, random.Random(rng.random())))
            n += 1
    return cases


def dumps(obj) -> str:
    return json.dumps(obj, indent=2, sort_keys=False, ensure_ascii=False) + "\n"


def write(cases: list[dict], out: Path = OUT) -> None:
    folder = out / "cases"
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.glob("case_*.json"):
        old.unlink()
    index = []
    for case in cases:
        name = f"case_{case['case_number']:04d}.json"
        (folder / name).write_text(dumps(case))
        index.append({"case_number": case["case_number"], "family": case["family"],
                      "variant": case["variant"], "title": case["title"], "file": f"cases/{name}",
                      "claim_status": case["gold"]["claim_status"],
                      "link_states": [l["state"] for l in case["gold"]["links"]]})
    (out / "index.json").write_text(dumps({"label": LABEL, "count": len(index), "cases": index}))


def main() -> None:
    cases = build_cases()
    write(cases)
    fams: dict[str, int] = {}
    for c in cases:
        fams[c["family"]] = fams.get(c["family"], 0) + 1
    print(json.dumps({"cases": len(cases), "families": fams}))


if __name__ == "__main__":
    main()
