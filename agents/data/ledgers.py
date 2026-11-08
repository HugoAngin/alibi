"""Development case ledgers for Alibi: synthetic, labelled, deterministic.

    uv run --frozen python -m agents.data.ledgers

Writes one JSON file per case to agents/data/out/cases/ and a summary to out/index.json.
Follows docs/SPEC.md (snapshot of alibi/SPEC.md) and shared/CONTRACTS.md: integer pence in
GBP, UTC ISO 8601 with Z, half-open periods. Case numbers stay below shared/splits.json
lockbox_from (1000): these are development and demo cases, never sealed evaluation cases.

Cases 1 and 2 are the demo pair from SPEC "Four-minute demo": Maya's 240000 - 18000 - 6000
= 216000 credit, where the merchant's pushed message only reports a release to her seller
balance and the payout's settlement record must be fetched by follow-up lookup. In case 1 the
settlement supports the link; in case 2 the payout was still pending when the credit was booked.

Gold is built by construction and states what is true at the decision time (the first RFC's
issued_at), after every lookup available by then. test_ledgers.py re-derives it independently.
Rules used for gold are in agents/data/README.md ("Gold rules").
"""
from __future__ import annotations

import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

OUT = Path(__file__).resolve().parent / "out"
SCHEMA_VERSION = "1"
CURRENCY = "GBP"
ISSUER = "mock-marketplace"          # synthetic resale platform, simulated service
SETTLEMENT_WINDOW_DAYS = 5
FEE_PER_MILLE = 25                   # 2.5% commission per sale, rounded half up
LABEL = "SYNTHETIC - labelled test data, not a real person, account or institution"

Q_SOURCE = "q.source_of_funds"
Q_BREAKDOWN = "q.breakdown"
Q_PERIOD = "q.sales_refunds_in_period"
QUESTION_TEXT = {
    Q_SOURCE: ["Please explain the source of the credit of {amount} received on {date}.",
               "What is the origin of the incoming payment of {amount} booked on {date}?",
               "Provide evidence of where the {amount} credited to your account on {date} came from."],
    Q_BREAKDOWN: ["Please show how this amount was calculated.",
                  "How does the credit reconcile to your underlying sales, refunds and fees?"],
    Q_PERIOD: ["Please provide documented marketplace sales and refunds between {start} and {end}."],
}

TRUST = {"merchant": "issuer_verified", "lookup": "issuer_verified", "import": "self_supplied",
         "bank": "bank_observed"}


def iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def pounds(minor: int) -> str:
    return f"£{minor // 100:,}.{minor % 100:02d}"


def fee_for(amount: int) -> int:
    return (amount * FEE_PER_MILLE + 500) // 1000


class Case:
    """One case: deliveries in arrival order, lookups, imports, RFCs, a holder claim and gold."""

    def __init__(self, number: int, family: str, variant: str, title: str, subject: str,
                 rng: random.Random):
        self.number, self.family, self.variant, self.title = number, family, variant, title
        self.subject, self.rng = subject, rng
        self.merchant: list[dict] = []
        self.lookup: list[dict] = []
        self.imports: list[dict] = []
        self.bank: list[dict] = []
        self.ingest: list[dict] = []
        self.forbidden: list[str] = []
        self._n = {"evt": 0, "rec": 0, "txn": 0, "po": 0}

    def next_id(self, kind: str) -> str:
        self._n[kind] += 1
        return f"{kind}-{self.number:04d}-{self._n[kind]:03d}"

    def payout_ref(self) -> str:
        self._n["po"] += 1
        return f"PO-{self.number:04d}-{self._n['po']:02d}"

    # merchant ------------------------------------------------------------
    def event(self, kind: str, amount: int | None, at: datetime, *, record_id: str | None = None,
              revision: int = 1, channel: str = "merchant", received: datetime | None = None,
              **extra) -> dict:
        """amount None marks a non-monetary document (notice, statement): no amount fields."""
        ev = {"schema_version": SCHEMA_VERSION, "event_id": self.next_id("evt"),
              "record_id": record_id or self.next_id("rec"), "revision": revision,
              "issuer_id": ISSUER, "subject_ref": self.subject, "kind": kind,
              "occurred_at": iso(at)}
        if amount is not None:
            ev.update(amount_minor=amount, currency=CURRENCY)
        ev.update(extra)
        received = received or at + timedelta(minutes=5)
        if channel == "merchant":
            self.deliver("merchant", ev, received, "accepted")
        elif channel == "lookup":
            self.lookup.append({"available_from": iso(received), "event": ev})
        else:
            self.deliver("import", ev, received, "accepted")
        return ev

    def deliver(self, connector: str, payload: dict, received: datetime, outcome: str) -> None:
        target = {"merchant": self.merchant, "import": self.imports, "bank": self.bank}[connector]
        key = "row" if connector == "bank" else ("record" if connector == "import" else "event")
        ident = payload.get("event_id") or payload["txn_id"]
        self.ingest.append({"connector": connector, "index": len(target), "id": ident,
                            "outcome": outcome})
        target.append({"received_at": iso(received), key: payload})

    def sales(self, amounts: list[int], start: datetime, refunds: dict[int, int] | None = None,
              gap_hours: int = 30, order_numbers: list[int] | None = None) -> dict:
        refunds = refunds or {}
        ids = {"sale": [], "refund": [], "fee": []}
        tot = {"sales": 0, "refunds": 0, "fees": 0}
        t = start
        for i, amount in enumerate(amounts):
            extra = {"order_number": order_numbers[i]} if order_numbers else {}
            sale = self.event("sale", amount, t, item=f"second-hand clothing lot {i + 1}",
                              status="completed", **extra)
            fee = self.event("fee", fee_for(amount), t + timedelta(minutes=1),
                             related_record_id=sale["record_id"], fee_type="commission")
            ids["sale"].append(sale["record_id"])
            ids["fee"].append(fee["record_id"])
            tot["sales"] += amount
            tot["fees"] += fee["amount_minor"]
            if i in refunds:
                ref = self.event("refund", refunds[i], t + timedelta(hours=20),
                                 related_record_id=sale["record_id"], status="completed")
                ids["refund"].append(ref["record_id"])
                tot["refunds"] += refunds[i]
            t += timedelta(hours=gap_hours)
        tot["net"] = tot["sales"] - tot["refunds"] - tot["fees"]
        return {"ids": ids, "totals": tot, "end": t,
                "covers": ids["sale"] + ids["refund"] + ids["fee"]}

    def payout(self, amount: int, at: datetime, covers: list[str], *, status: str = "settled",
               settle_after: timedelta = timedelta(hours=20), channel: str = "merchant",
               reference: str | None = None, **extra) -> dict:
        ref = reference or self.payout_ref()
        settled = at + settle_after if status == "settled" else None
        fields = {"destination": "bank", "payout_reference": ref, "status": status,
                  "settled_at": iso(settled) if settled else None, "covers_record_ids": covers}
        fields.update(extra)
        # A record cannot be served before the time its status describes.
        as_of = ts_(extra["status_as_of"]) if extra.get("status_as_of") else None
        received = (settled or as_of or at) + timedelta(minutes=5) if channel != "import" else None
        return self.event("payout", amount, at, channel=channel, received=received, **fields)

    def balance_release(self, amount: int, at: datetime, covers: list[str],
                        mentions: str | None = None, text: str | None = None) -> dict:
        text = text or f"Good news! {pounds(amount)} from your recent sales is now available in your seller balance."
        if mentions:
            text += f" Withdrawal {mentions} to your linked bank account has been requested."
        return self.event("balance_release", amount, at, destination="platform_balance",
                          release_reference=f"REL-{self.number:04d}-01", covers_record_ids=covers,
                          message=text)

    def purchase(self, at: datetime) -> None:
        """A sensitive distractor: Maya's own purchase on the platform. Never disclose."""
        ev = self.event("purchase", self.rng.randrange(1500, 6001, 100), at,
                        item="prescription eyewear", category="health", status="completed")
        self.forbidden.append(f"merchant:{ev['record_id']}")

    # bank ----------------------------------------------------------------
    def bank_row(self, amount: int, at: datetime, description: str, counterparty: str,
                 reference: str | None = None, forbidden: bool = False) -> dict:
        row = {"txn_id": self.next_id("txn"), "revision": 1, "account_ref": f"acct-{self.subject}",
               "booked_at": iso(at), "amount_minor": amount, "currency": CURRENCY,
               "description": description + (f" {reference}" if reference else ""),
               "counterparty_name": counterparty, "payment_reference": reference}
        self.deliver("bank", row, at + timedelta(hours=1), "accepted")
        if forbidden:
            self.forbidden.append(f"bank:{row['txn_id']}")
        return row

    def credit(self, amount: int, at: datetime, reference: str | None,
               marketplace: bool = False) -> dict:
        if reference or marketplace:
            return self.bank_row(amount, at, "MOCK MARKETPLACE PAYOUT", "Mock Marketplace Payments",
                                 reference)
        return self.bank_row(amount, at, "FASTER PAYMENT", "Synthetic Sender")

    def everyday(self, start: datetime) -> None:
        """Unrelated account activity, all forbidden to disclose. One row is sensitive."""
        self.bank_row(-65000, start + timedelta(days=1, hours=9), "RENT", "Synthetic Lettings Ltd",
                      forbidden=True)
        self.bank_row(-1240, start + timedelta(days=3, hours=13), "CARD", "Synthetic Grocer",
                      forbidden=True)
        self.bank_row(-2350, start + timedelta(days=5, hours=11), "CARD", "Synthetic Pharmacy",
                      forbidden=True)

    # requests ----------------------------------------------------------------
    def rfc(self, credit: dict | None, issued: datetime, *, audience: str = "bank_a",
            period: tuple[datetime, datetime] | None = None, seq: int = 1) -> dict:
        if period:
            qids = [Q_PERIOD]
            fill = {"start": period[0].strftime("%d %B %Y"),
                    "end": (period[1] - timedelta(days=1)).strftime("%d %B %Y")}
        else:
            qids = [Q_SOURCE, Q_BREAKDOWN]
            fill = {"amount": pounds(credit["amount_minor"]),
                    "date": credit["booked_at"][:10]}
        return {"request_id": f"rfc-{self.number:04d}-{seq:02d}", "parent_request_id": None,
                "audience": audience, "subject_ref": self.subject,
                "txn_ids": [credit["txn_id"]] if credit and not period else [],
                "period": {"from": iso(period[0]), "to": iso(period[1])} if period else None,
                "question_ids": qids,
                "questions": [{"question_id": q, "text": self.rng.choice(QUESTION_TEXT[q]).format(**fill)}
                              for q in qids],
                "issued_at": iso(issued), "expires_at": iso(issued + timedelta(days=7))}

    # output ----------------------------------------------------------------
    def finish(self, *, rfcs: list[dict], credit: dict, stated: dict, payout_ref: str | None,
               link: str, payouts: list[dict], covered: list[str] | None, breakdown: dict | None,
               claim_status: str, source_status: str, gap: str | None, notes: str,
               extra_answers: dict | None = None, extra_gold: dict | None = None) -> dict:
        txn = f"bank:{credit['txn_id']}"
        po_refs = [f"{'import' if self._is_import(p) else 'merchant'}:{p['record_id']}" for p in payouts]
        cov_refs = [f"merchant:{r}" for r in covered or []]
        src = {"status": source_status, "gap": None if source_status != "unresolved" else gap,
               "facts": {"payout_references": [p.get("payout_reference", p.get("transfer_ref")) for p in payouts],
                         "settled_at": [p.get("settled_at", p.get("paid_at")) for p in payouts]}
                        if link == "linked" else {},
               "evidence_sets": [[txn] + po_refs] if link == "linked" else [[]]}
        bd_status = claim_status if source_status == "supported" and breakdown else "unresolved"
        brk = {"status": bd_status, "gap": gap if bd_status == "unresolved" else None,
               "facts": {"breakdown_minor": breakdown} if breakdown else {},
               "evidence_sets": [[txn] + po_refs + cov_refs] if breakdown else [[]]}
        answers = {rfcs[0]["request_id"]: {Q_SOURCE: src, Q_BREAKDOWN: brk}}
        answers.update(extra_answers or {})
        refs = sorted({r for a in answers.values() for q in a.values() for s in q["evidence_sets"]
                       for r in s} | set(self.forbidden))
        lookups = {d["event"]["record_id"] for d in self.lookup}
        return {
            "case_number": self.number, "family": self.family, "variant": self.variant,
            "title": self.title, "label": LABEL, "subject_ref": self.subject,
            "merchant_deliveries": self.merchant,
            "merchant_lookup": self.lookup,
            "imports": self.imports,
            "bank_deliveries": self.bank,
            "rfcs": rfcs,
            "holder_claim": {"txn_id": credit["txn_id"], "explanation": "marketplace_payout",
                             "payout_reference": payout_ref, "stated_minor": stated},
            "gold": {
                "decision_time": rfcs[0]["issued_at"],
                "links": [{"txn_id": credit["txn_id"], "state": link,
                           "payout_record_ids": [p["record_id"] for p in payouts] if link == "linked" else []}],
                "breakdown_minor": breakdown,
                "claim_status": claim_status,
                "answers": answers,
                "requires_retrieval": sorted(r.split(":", 1)[1] for r in refs
                                             if r.split(":", 1)[1] in lookups),
                "forbidden_refs": sorted(self.forbidden),
                "source_trust": {r: TRUST[r.split(":")[0]] if r.split(":", 1)[1] not in lookups
                                 else TRUST["lookup"] for r in refs},
                "ingest": sorted(self.ingest, key=lambda i: (["merchant", "import", "bank"].index(i["connector"]), i["index"])),
                "notes": notes,
                **(extra_gold or {}),
            },
        }

    def _is_import(self, ev: dict) -> bool:
        return any(d["record"]["record_id"] == ev["record_id"] for d in self.imports)


def stated(tot: dict, **override) -> dict:
    s = {k: tot[k] for k in ("sales", "refunds", "fees")}
    s.update(override)
    s["net"] = s["sales"] - s["refunds"] - s["fees"]
    return s


def amounts_for(rng: random.Random, lo: int, hi: int) -> list[int]:
    return [rng.randrange(1500, 50001, 500) for _ in range(rng.randint(lo, hi))]


def refunds_for(rng: random.Random, amounts: list[int], k: int) -> dict[int, int]:
    return {i: rng.randrange(500, amounts[i] // 2 + 1, 500)
            for i in rng.sample(range(len(amounts)), k)}


def start_for(rng: random.Random) -> datetime:
    return datetime(2026, rng.randint(6, 9), rng.randint(1, 10), rng.randint(8, 12),
                    tzinfo=timezone.utc)


def seller(number: int, family: str, variant: str, title: str, rng: random.Random) -> tuple:
    c = Case(number, family, variant, title, f"seller-{number:04d}", rng)
    start = start_for(rng)
    c.everyday(start)
    return c, start


# --------------------------------------------------------------------------
# demo pair
# --------------------------------------------------------------------------

def demo(number: int, variant: str, rng: random.Random) -> dict:
    pending = variant == "settlement_pending"
    c = Case(number, "demo", variant,
             "Maya's £2,160 credit: payout still pending at booking" if pending else
             "Maya's £2,160 credit: settlement retrieved and consistent", "maya-001", rng)
    start = datetime(2026, 10, 5, 10, 0, tzinfo=timezone.utc)
    c.everyday(start)
    s = c.sales([30000, 25000, 40000, 35000, 20000, 45000, 15000, 30000], start,
                refunds={1: 10000, 4: 8000})
    assert s["totals"] == {"sales": 240000, "refunds": 18000, "fees": 6000, "net": 216000}
    c.purchase(start + timedelta(days=9))
    ref = f"PO-{number:04d}-01"
    c.balance_release(216000, datetime(2026, 10, 26, 8, 0, tzinfo=timezone.utc), s["covers"],
                      mentions=ref)
    initiated = datetime(2026, 10, 26, 9, 0, tzinfo=timezone.utc)
    booked = datetime(2026, 10, 28, 6, 30, tzinfo=timezone.utc)
    if pending:
        p = c.payout(216000, initiated, s["covers"], status="pending", channel="lookup",
                     reference=ref, status_as_of=iso(booked + timedelta(days=1)))
        cr = c.credit(216000, booked, None)
    else:
        p = c.payout(216000, initiated, s["covers"], settle_after=timedelta(hours=31),
                     channel="lookup", reference=ref)
        cr = c.credit(216000, booked, ref)
    first = c.rfc(cr, datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc))
    period = (datetime(2026, 10, 1, tzinfo=timezone.utc), datetime(2026, 11, 1, tzinfo=timezone.utc))
    second = c.rfc(None, datetime(2026, 11, 4, 14, 0, tzinfo=timezone.utc), audience="bank_b",
                   period=period, seq=2)
    in_period = s["ids"]["sale"] + s["ids"]["refund"]
    extra = {second["request_id"]: {Q_PERIOD: {
        "status": "supported", "gap": None,
        "facts": {"sales_minor": 240000, "refunds_minor": 18000, "sale_count": 8, "refund_count": 2},
        "evidence_sets": [[f"merchant:{r}" for r in in_period]]}}}
    if pending:
        return c.finish(
            rfcs=[first, second], credit=cr, stated=stated(s["totals"]), payout_ref=ref,
            link="unmatched", payouts=[], covered=None, breakdown=None,
            claim_status="contradicted", source_status="unresolved",
            gap="settlement_pending_at_booking", extra_answers=extra,
            notes="Contradiction variant. The pushed message only reports a balance release. The "
                  f"retrieved payout {ref} was still pending a day after the credit was booked, so "
                  "the claim that this credit settles it is contradicted and the credit's origin is "
                  "unresolved, although the amounts reconcile.")
    return c.finish(
        rfcs=[first, second], credit=cr, stated=stated(s["totals"]), payout_ref=ref,
        link="linked", payouts=[p], covered=s["covers"], breakdown=s["totals"],
        claim_status="supported", source_status="supported", gap=None, extra_answers=extra,
        notes="Supported control. The pushed message only reports a balance release; the payout's "
              f"settlement record ({ref}) comes from a follow-up lookup and its reference and timing "
              "match the credit. rfcs[1] is Bank B's different question, answered from the same "
              "passport with a fresh selection and approval.")


# --------------------------------------------------------------------------
# families
# --------------------------------------------------------------------------

def clean(number: int, variant: str, rng: random.Random) -> dict:
    omits = variant == "omits_refunds_fees"
    c, start = seller(number, "clean", variant, "One settled payout explains one credit" +
                      (" - holder omits refunds and fees" if omits else ""), rng)
    amounts = amounts_for(rng, 3, 8)
    s = c.sales(amounts, start, refunds=refunds_for(rng, amounts, rng.randint(0, 2)))
    if rng.random() < 0.5:
        c.purchase(start + timedelta(days=2))
    tot = s["totals"]
    p = c.payout(tot["net"], s["end"] + timedelta(days=1), s["covers"])
    cr = c.credit(tot["net"], s["end"] + timedelta(days=2, hours=rng.randint(1, 30)),
                  p["payout_reference"])
    st = stated(tot, sales=tot["net"], refunds=0, fees=0) if omits else stated(tot)
    return c.finish(
        rfcs=[c.rfc(cr, s["end"] + timedelta(days=7))], credit=cr, stated=st, payout_ref=None,
        link="linked", payouts=[p], covered=s["covers"], breakdown=tot,
        claim_status="contradicted" if omits else "supported", source_status="supported", gap=None,
        notes="The holder states gross sales equal to the credit; the linked payout's records show "
              "refunds and fees, so the stated breakdown is contradicted. The source is supported."
              if omits else "Single settled payout with matching reference, amount and timing.")


def bundled(number: int, variant: str, rng: random.Random) -> dict:
    """variant: a|b|c (batch of two payouts), later_same_amount, two_candidates."""
    if variant == "two_candidates":
        c, start = seller(number, "bundled_payment", variant,
                          "Two separate payouts each match the one unreferenced credit", rng)
        amounts = amounts_for(rng, 3, 3)
        a = c.sales(amounts, start)
        b = c.sales(amounts, a["end"] + timedelta(hours=6))      # same lots again: same net
        t = b["end"] + timedelta(days=1)
        c.payout(a["totals"]["net"], t, a["covers"])
        c.payout(b["totals"]["net"], t + timedelta(hours=3), b["covers"])
        cr = c.credit(a["totals"]["net"], t + timedelta(days=2), None)
        return c.finish(
            rfcs=[c.rfc(cr, t + timedelta(days=6))], credit=cr, stated=stated(a["totals"]),
            payout_ref=None, link="ambiguous", payouts=[], covered=None, breakdown=None,
            claim_status="unresolved", source_status="unresolved", gap="ambiguous_candidates",
            notes="Two compatible settled payouts and no reference on the credit: no unique "
                  "candidate, so no exact link. The Matcher should abstain.")
    trap = variant == "later_same_amount"
    c, start = seller(number, "bundled_payment", variant,
                      "Two payouts settled as one batch credit" +
                      (" - later payout has the same amount" if trap else ""), rng)
    a = c.sales(amounts_for(rng, 2, 4), start)
    b = c.sales(amounts_for(rng, 2, 4), a["end"] + timedelta(hours=6))
    t = b["end"] + timedelta(days=1)
    batch = f"BATCH-{number:04d}-01"
    pa = c.payout(a["totals"]["net"], t, a["covers"], settlement_batch_id=batch)
    pb = c.payout(b["totals"]["net"], t + timedelta(minutes=2), b["covers"],
                  settle_after=timedelta(hours=20, minutes=-2), settlement_batch_id=batch)
    total = a["totals"]["net"] + b["totals"]["net"]
    booked = t + timedelta(days=2)
    cr = c.credit(total, booked, batch)
    notes = "Neither payout alone equals the credit; the settlement batch the credit references does."
    if trap:
        later = c.sales(amounts_for(rng, 2, 2), booked + timedelta(days=1))
        c.payout(total, booked + timedelta(days=3), later["covers"])
        notes += " A later payout has exactly the credit's amount but settles after it, so it is not a candidate."
    tot = {k: a["totals"][k] + b["totals"][k] for k in a["totals"]}
    return c.finish(
        rfcs=[c.rfc(cr, booked + timedelta(days=5))], credit=cr, stated=stated(tot),
        payout_ref=None, link="linked", payouts=[pa, pb], covered=a["covers"] + b["covers"],
        breakdown=tot, claim_status="supported", source_status="supported", gap=None, notes=notes)


def balance_vs_settlement(number: int, variant: str, rng: random.Random) -> dict:
    """variant: settled | settled_via_lookup | release_only | settles_after_credit | pending_at_booking."""
    titles = {"settled": "Balance release, then a pushed bank settlement",
              "settled_via_lookup": "Balance release pushed; settlement only by lookup",
              "release_only": "Released to platform balance, never settled to the bank",
              "settles_after_credit": "The only same-amount payout settles after the credit",
              "pending_at_booking": "Retrieved payout still pending when the credit was booked"}
    c, start = seller(number, "balance_vs_settlement", variant, titles[variant], rng)
    amounts = amounts_for(rng, 3, 6)
    s = c.sales(amounts, start, refunds=refunds_for(rng, amounts, 1))
    tot = s["totals"]
    released = s["end"] + timedelta(days=1)
    booked = released + timedelta(days=2)
    issued = booked + timedelta(days=5)
    ref = f"PO-{number:04d}-01"
    if variant in ("settled", "settled_via_lookup"):
        c.balance_release(tot["net"], released, s["covers"], mentions=ref)
        p = c.payout(tot["net"], released + timedelta(hours=2), s["covers"], reference=ref,
                     channel="lookup" if variant == "settled_via_lookup" else "merchant")
        cr = c.credit(tot["net"], booked, ref)
        return c.finish(
            rfcs=[c.rfc(cr, issued)], credit=cr, stated=stated(tot), payout_ref=None,
            link="linked", payouts=[p], covered=s["covers"], breakdown=tot,
            claim_status="supported", source_status="supported", gap=None,
            notes="The credit links to the settled bank payout, not to the balance release." +
                  (" The payout is only available by follow-up lookup." if variant == "settled_via_lookup" else ""))
    c.balance_release(tot["net"], released, s["covers"],
                      mentions=ref if variant != "release_only" else None)
    if variant == "release_only":
        cr = c.credit(tot["net"], booked, None)
        status, gap, cited = "unresolved", "balance_release_not_settlement", None
        notes = ("A release to the platform balance with the same amount is not a bank settlement. "
                 "No payout exists, so the credit's origin is unresolved.")
    elif variant == "settles_after_credit":
        cr = c.credit(tot["net"], booked, None)
        c.payout(tot["net"], booked + timedelta(days=2), s["covers"], reference=ref, channel="lookup")
        status, gap, cited = "contradicted", "settlement_after_credit", ref
        notes = (f"The holder cites {ref}, but it settles after the credit was booked, so it cannot "
                 "explain it. The settlement claim is contradicted; the origin is unresolved.")
    else:
        cr = c.credit(tot["net"], booked, None)
        c.payout(tot["net"], released + timedelta(hours=2), s["covers"], status="pending",
                 reference=ref, channel="lookup", status_as_of=iso(booked + timedelta(days=1)))
        status, gap, cited = "contradicted", "settlement_pending_at_booking", ref
        notes = (f"The holder cites {ref}; the retrieved record shows it still pending after the "
                 "credit was booked. The settlement claim is contradicted; the origin is unresolved.")
    return c.finish(
        rfcs=[c.rfc(cr, issued)], credit=cr, stated=stated(tot), payout_ref=cited,
        link="unmatched", payouts=[], covered=None, breakdown=None, claim_status=status,
        source_status="unresolved", gap=gap, notes=notes)


def pending_correction(number: int, variant: str, rng: random.Random) -> dict:
    """variant: refund_completed | sale_adjusted | stale_redelivery | holder_uses_old | payout_status_corrected."""
    titles = {"refund_completed": "Signed correction: pending refund completes at a new amount",
              "sale_adjusted": "Signed correction: sale price adjusted by a newer revision",
              "stale_redelivery": "Stale revision redelivered after the correction",
              "holder_uses_old": "Holder states the superseded refund amount",
              "payout_status_corrected": "Signed correction: payout marked failed after settling"}
    c, start = seller(number, "pending_correction", variant, titles[variant], rng)
    amounts = amounts_for(rng, 3, 6)
    s = c.sales(amounts, start)
    tot = dict(s["totals"])
    t = s["end"]
    covers = list(s["covers"])
    old_refund = None
    if variant == "sale_adjusted":
        old, new = amounts[0], amounts[0] - rng.randrange(500, amounts[0] // 2 + 1, 500)
        c.event("sale", new, t, record_id=s["ids"]["sale"][0], revision=2,
                item="second-hand clothing lot 1", status="completed",
                correction_reason="price adjusted after buyer dispute")
        tot["sales"] += new - old           # commission stays on the original price (assumption 3)
    elif variant != "payout_status_corrected":
        sale_id = s["ids"]["sale"][-1]
        old_refund = rng.randrange(1000, amounts[-1] // 2 + 1, 500)
        final = old_refund - 500
        first = c.event("refund", old_refund, t, related_record_id=sale_id, status="pending")
        c.event("refund", final, t + timedelta(days=1), record_id=first["record_id"], revision=2,
                related_record_id=sale_id, status="completed",
                correction_reason="refund finalised at a lower amount")
        if variant == "stale_redelivery":
            stale = dict(first, event_id=c.next_id("evt"))
            c.deliver("merchant", stale, t + timedelta(days=1, hours=3), "stale_revision_ignored")
        tot["refunds"] += final
        covers.append(first["record_id"])
    tot["net"] = tot["sales"] - tot["refunds"] - tot["fees"]
    paid = t + timedelta(days=2)
    p = c.payout(tot["net"], paid, covers)
    booked = paid + timedelta(days=2)
    cr = c.credit(tot["net"], booked, p["payout_reference"])
    rf = [c.rfc(cr, booked + timedelta(days=5))]
    if variant == "payout_status_corrected":
        fields = {k: p[k] for k in ("destination", "payout_reference", "covers_record_ids")}
        c.event("payout", p["amount_minor"], booked + timedelta(days=1), record_id=p["record_id"],
                revision=2, status="failed", settled_at=None,
                correction_reason="transfer returned by receiving bank", **fields)
        return c.finish(
            rfcs=rf, credit=cr, stated=stated(tot), payout_ref=p["payout_reference"],
            link="unmatched", payouts=[], covered=None, breakdown=None,
            claim_status="contradicted", source_status="unresolved", gap="settlement_failed",
            notes="Revision 2 of the payout (a signed status correction) says the transfer failed. "
                  "Code must invalidate the earlier link: the settlement claim is contradicted and the "
                  "credit's origin unresolved. This is the code-based regression test, not the LLM showcase.")
    if variant == "holder_uses_old":
        st, status = stated(tot, refunds=old_refund), "contradicted"
        notes = "The holder quotes the superseded pending refund; the active revision differs."
    else:
        st, status = stated(tot), "supported"
        notes = "Arithmetic uses the active (highest) revision of each record; older revisions are kept."
    return c.finish(rfcs=rf, credit=cr, stated=st, payout_ref=None, link="linked", payouts=[p],
                    covered=covers, breakdown=tot, claim_status=status, source_status="supported",
                    gap=None, notes=notes)


def duplicate_record(number: int, variant: str, rng: random.Random) -> dict:
    """variant: exact_redelivery | conflicting_bytes | bank_row_redelivery | double_count_trap."""
    titles = {"exact_redelivery": "Same merchant event delivered twice",
              "conflicting_bytes": "Same event_id redelivered with different bytes",
              "bank_row_redelivery": "Same bank row delivered twice",
              "double_count_trap": "Every sale arrives twice; counting duplicates overstates sales"}
    c, start = seller(number, "duplicate_record", variant, titles[variant], rng)
    amounts = amounts_for(rng, 3, 6)
    s = c.sales(amounts, start, refunds=refunds_for(rng, amounts, 1))
    t = s["end"]
    sales = [d["event"] for d in c.merchant if d["event"]["kind"] == "sale"]
    if variant in ("exact_redelivery", "double_count_trap"):
        for i, ev in enumerate(sales if variant == "double_count_trap" else sales[:1]):
            c.deliver("merchant", dict(ev), t + timedelta(hours=i + 1), "duplicate_ignored")
    elif variant == "conflicting_bytes":
        c.deliver("merchant", dict(sales[0], amount_minor=sales[0]["amount_minor"] * 2),
                  t + timedelta(hours=1), "rejected_conflict")
    tot = s["totals"]
    p = c.payout(tot["net"], t + timedelta(days=1), s["covers"])
    cr = c.credit(tot["net"], t + timedelta(days=3), p["payout_reference"])
    if variant == "bank_row_redelivery":
        c.deliver("bank", dict(cr), t + timedelta(days=3, hours=5), "duplicate_ignored")
    notes = {"exact_redelivery": "Identical bytes for an existing event_id leave one active record.",
             "conflicting_bytes": "The same event_id with different bytes is rejected; the original stands.",
             "bank_row_redelivery": "An identical bank row redelivered is one row and one credit.",
             "double_count_trap": "Counting the duplicate sales would overstate the total."}[variant]
    return c.finish(rfcs=[c.rfc(cr, t + timedelta(days=8))], credit=cr, stated=stated(tot),
                    payout_ref=None, link="linked", payouts=[p], covered=s["covers"], breakdown=tot,
                    claim_status="supported", source_status="supported", gap=None, notes=notes)


def missing_proof(number: int, variant: str, rng: random.Random) -> dict:
    """variant: no_merchant_data | no_payout | amount_mismatch | missing_covered_record | late_record."""
    titles = {"no_merchant_data": "Credit with no merchant records at all",
              "no_payout": "Sales on file but no payout record",
              "amount_mismatch": "Referenced payout amount differs from the credit",
              "missing_covered_record": "Payout cites a fee record that never arrived",
              "late_record": "Payout record arrives only after the request"}
    c, start = seller(number, "missing_proof", variant, titles[variant], rng)
    amounts = amounts_for(rng, 3, 6)
    if variant == "no_merchant_data":
        tot = {"sales": sum(amounts), "refunds": 0, "fees": sum(fee_for(a) for a in amounts)}
        tot["net"] = tot["sales"] - tot["fees"]
        booked = start + timedelta(days=12)
        cr = c.credit(tot["net"], booked, None)
        return c.finish(rfcs=[c.rfc(cr, booked + timedelta(days=5))], credit=cr, stated=stated(tot),
                        payout_ref=None, link="unmatched", payouts=[], covered=None, breakdown=None,
                        claim_status="unresolved", source_status="unresolved",
                        gap="no_merchant_records",
                        notes="The holder's explanation has no records behind it. Alibi must not invent support.")
    s = c.sales(amounts, start)
    tot = s["totals"]
    paid = s["end"] + timedelta(days=1)
    booked = paid + timedelta(days=2)
    issued = booked + timedelta(days=5)
    if variant == "no_payout":
        cr = c.credit(tot["net"], booked, None)
        return c.finish(rfcs=[c.rfc(cr, issued)], credit=cr, stated=stated(tot), payout_ref=None,
                        link="unmatched", payouts=[], covered=None, breakdown=None,
                        claim_status="unresolved", source_status="unresolved", gap="settlement_not_found",
                        notes="Sales and fees are on file, but no payout links them to the credit.")
    if variant == "amount_mismatch":
        p = c.payout(tot["net"], paid, s["covers"])
        cr = c.credit(tot["net"] + rng.randrange(1000, 20001, 500), booked, p["payout_reference"])
        return c.finish(rfcs=[c.rfc(cr, issued)], credit=cr, stated=stated(tot), payout_ref=None,
                        link="unmatched", payouts=[], covered=None, breakdown=None,
                        claim_status="unresolved", source_status="unresolved", gap="amount_mismatch",
                        notes="The credit names a real payout, but the amounts differ, so part of the "
                              "credit has no explanation. Surface it as a discrepancy.")
    if variant == "late_record":
        p = c.payout(tot["net"], paid, s["covers"])
        late = dict(c.merchant.pop()["event"])
        c.ingest.pop()
        cr = c.credit(tot["net"], booked, p["payout_reference"])
        c.deliver("merchant", late, issued + timedelta(days=1), "accepted")
        return c.finish(rfcs=[c.rfc(cr, issued)], credit=cr, stated=stated(tot), payout_ref=None,
                        link="unmatched", payouts=[], covered=None, breakdown=None,
                        claim_status="unresolved", source_status="unresolved", gap="settlement_not_found",
                        notes="The payout webhook arrives a day after the request. At decision time the "
                              "settlement is not observable, so gold is unresolved.")
    missing_fee = s["ids"]["fee"][-1]
    idx = next(i for i, d in enumerate(c.merchant) if d["event"]["record_id"] == missing_fee)
    c.merchant.pop(idx)
    c.ingest = [i for i in c.ingest if not (i["connector"] == "merchant" and i["index"] == idx)]
    for i in c.ingest:
        if i["connector"] == "merchant" and i["index"] > idx:
            i["index"] -= 1
    p = c.payout(tot["net"], paid, s["covers"])
    cr = c.credit(tot["net"], booked, p["payout_reference"])
    return c.finish(rfcs=[c.rfc(cr, issued)], credit=cr, stated=stated(tot), payout_ref=None,
                    link="linked", payouts=[p], covered=None, breakdown=None,
                    claim_status="unresolved", source_status="supported", gap="missing_cited_record",
                    notes=f"The payout links to the credit, but it cites {missing_fee}, which never "
                          "arrived, so the breakdown cannot be verified from records.")


def self_supplied(number: int, variant: str, rng: random.Random) -> dict:
    """Payout proof only as Maya's own export, tagged self_supplied by the backend."""
    c, start = seller(number, "self_supplied_import", variant,
                      "Payout proof only as a self-supplied customer export", rng)
    amounts = amounts_for(rng, 3, 6)
    s = c.sales(amounts, start)
    tot = s["totals"]
    paid = s["end"] + timedelta(days=1)
    booked = paid + timedelta(days=2)
    p = c.payout(tot["net"], paid, s["covers"], channel="import",
                 import_source="customer CSV export", claimed_trust="issuer_verified")
    c.imports[-1]["received_at"] = iso(booked + timedelta(days=1))
    cr = c.credit(tot["net"], booked, p["payout_reference"])
    return c.finish(rfcs=[c.rfc(cr, booked + timedelta(days=5))], credit=cr, stated=stated(tot),
                    payout_ref=None, link="linked", payouts=[p], covered=s["covers"], breakdown=tot,
                    claim_status="supported", source_status="supported", gap=None,
                    notes="The only payout evidence is Maya's import. It claims issuer_verified, but the "
                          "backend tags it self_supplied whatever it says. Link and claim stand at lower "
                          "trust; the analyst decides how much it establishes.")


def hidden_reference(number: int, variant: str, rng: random.Random) -> dict:
    """The payout reference is not a regex away (lead 23:40Z, post 6714579064).

    Same pinned gold rules as every other family; only how the reference is observed changes.
    Each case has an earlier payout (lookup-only) as a plausible distractor, and the right payout
    is lookup-only too, so the system must find the right retrieval key.
    """
    titles = {
        "decoy_in_release": "Release names an older withdrawal; the right one only in an order confirmation",
        "orders_and_weekday": "No reference: only the orders a batch covered and the weekday it went out",
        "date_only": "No reference: only the date the payout was sent",
        "alias_statement": "Bank shows a withdrawal ID; a merchant statement maps it to the payout",
        "alias_import": "Bank shows a withdrawal ID; Maya's own CSV export maps it to the payout",
        "email_layout": "Reference only in an email-style notice, written PO/nnnn/nn",
        "csv_layout": "Reference only in a semicolon statement, without the PO prefix",
        "two_refs_amount": "Two withdrawals named; only the amount picks the right one",
        "two_refs_timing": "Two equal withdrawals named; only settlement timing picks the right one",
    }
    c, start = seller(number, "hidden_reference", variant, titles[variant], rng)
    order = [1200 + rng.randrange(0, 600, 2)]

    def batch(t0: datetime, amounts: list[int] | None = None, refunds: bool = False) -> dict:
        amts = amounts or amounts_for(rng, 3, 5)
        orders = []
        for _ in amts:
            order[0] += rng.randint(1, 4)
            orders.append(order[0])
        b = c.sales(amts, t0, order_numbers=orders,
                    refunds=refunds_for(rng, amts, rng.randint(0, 1)) if refunds else None)
        b["orders"], b["amounts"] = orders, amts
        return b

    alias = variant in ("alias_statement", "alias_import")
    w_e, w_m = (f"W-{x}" for x in rng.sample(range(1000, 10000), 2))
    # Earlier batch, its payout (lookup-only) and the bank credit it produced.
    e = batch(start)
    pe = c.payout(e["totals"]["net"], e["end"] + timedelta(days=1), e["covers"], channel="lookup",
                  withdrawal_id=w_e)
    e_booked = ts_(pe["settled_at"]) + timedelta(hours=6)
    c.credit(e["totals"]["net"], e_booked, w_e if alias else None, marketplace=True)
    # Main batch: the credit the RFC asks about.
    m = batch(e_booked + timedelta(days=3), e["amounts"] if variant == "two_refs_timing" else None,
              refunds=variant != "two_refs_timing")
    net = m["totals"]["net"]
    if variant != "two_refs_timing":
        assert net != e["totals"]["net"]
    released = m["end"] + timedelta(days=1)
    init = released + timedelta(hours=2)
    pm = c.payout(net, init, m["covers"], channel="lookup", withdrawal_id=w_m)
    booked = ts_(pm["settled_at"]) + timedelta(hours=rng.randint(4, 30))
    available = f"{pounds(net)} from your recent sales is now available in your seller balance."
    path = [{"payout_reference": pm["payout_reference"]}]
    clue_at = released + timedelta(hours=3)
    if variant == "decoy_in_release":
        c.balance_release(net, released, m["covers"],
                          text=available + f" Your previous withdrawal {pe['payout_reference']} has completed.")
        clue = c.event("notice", None, m["end"], notice_type="order_confirmation",
                       text=f"Order #{m['orders'][-1]} was delivered and confirmed by the buyer. "
                            f"Earnings from this order are included in withdrawal {pm['payout_reference']}.")
    elif variant == "orders_and_weekday":
        clue = c.balance_release(net, init + timedelta(hours=1), m["covers"],
                                 text=f"{pounds(net)} is on its way. The batch covering orders "
                                      f"#{m['orders'][0]} and #{m['orders'][-1]} went out on "
                                      f"{init.strftime('%A')}; funds usually arrive within 1-2 working days.")
        path = [{"covers_record_id": m["ids"]["sale"][0]}]
    elif variant == "date_only":
        clue = c.balance_release(net, init + timedelta(hours=1), m["covers"],
                                 text=f"Your payout was sent on {init.strftime('%A')} "
                                      f"{init.day} {init.strftime('%B')}. Funds usually arrive "
                                      "within 1-2 working days.")
        path = [{"payout_date": init.strftime("%Y-%m-%d")}]
    elif alias:
        c.balance_release(net, released, m["covers"], text=available)
        rows = [(w_e, pe), (w_m, pm)]
        csv = "withdrawal_id,payout_id,requested_at,amount\n" + "".join(
            f"{w},{p['payout_reference']},{p['occurred_at'][:10]},{pounds(p['amount_minor'])}\n"
            for w, p in rows)
        if variant == "alias_statement":
            clue = c.event("statement", None, booked + timedelta(hours=1),
                           statement_type="withdrawals_export", text=csv)
        else:
            clue = c.event("statement", None, booked + timedelta(hours=2), channel="import",
                           import_source="seller dashboard CSV export", text=csv)
    elif variant == "email_layout":
        c.balance_release(net, released, m["covers"], text=available)
        n, k = pm["payout_reference"].split("-")[1:]
        clue = c.event("notice", None, clue_at, notice_type="email",
                       text="From: payouts@mock-marketplace.example\n"
                            "Subject: Your money is on its way\n\n"
                            f"Hi,\nWe have sent {pounds(net)} to your bank account ending 00.\n"
                            f"Our ref: PO/{n}/{k}\nThanks for selling with us.")
    elif variant == "csv_layout":
        c.balance_release(net, released, m["covers"], text=available)

        def euro(minor: int) -> str:
            return f"{minor // 100:,}".replace(",", ".") + f",{minor % 100:02d}"
        lines = ["Payout No.;Date;Net amount (GBP)"] + [
            f"{p['payout_reference'][3:].replace('-', '/')};"
            f"{ts_(p['occurred_at']).strftime('%d.%m.%Y')};{euro(p['amount_minor'])}" for p in (pe, pm)]
        clue = c.event("statement", None, clue_at, statement_type="payout_summary",
                       text="\n".join(lines) + "\n")
    else:
        first, second = rng.sample([pe, pm], 2)
        clue = c.balance_release(net, released, m["covers"],
                                 text=available + f" Withdrawals {first['payout_reference']} and "
                                      f"{second['payout_reference']} have been requested.")
        path = [{"payout_reference": first["payout_reference"]},
                {"payout_reference": second["payout_reference"]}]
    cr = c.credit(net, booked, w_m if alias else None, marketplace=True)
    clue_ref = f"{'import' if variant == 'alias_import' else 'merchant'}:{clue['record_id']}"
    tot = m["totals"]
    return c.finish(
        rfcs=[c.rfc(cr, booked + timedelta(days=5))], credit=cr, stated=stated(tot),
        payout_ref=None, link="linked", payouts=[pm], covered=m["covers"], breakdown=tot,
        claim_status="supported", source_status="supported", gap=None,
        extra_gold={"retrieval_path": path, "clue_refs": [clue_ref]},
        notes=f"{titles[variant]}. The deciding payout is lookup-only; gold.retrieval_path gives "
              "api.lookup() keys that fetch it, and gold.clue_refs the pushed or imported document "
              "that points there. An earlier payout is a plausible distractor that fails the amount, "
              "timing or reference rule.")


def ts_(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


PLAN = [
    (demo, ["settled_control", "settlement_pending"]),
    (clean, ["a", "b", "c", "d", "e", "omits_refunds_fees"]),
    (bundled, ["a", "b", "c", "later_same_amount", "two_candidates"]),
    (balance_vs_settlement, ["settled", "settled_via_lookup", "settled_via_lookup", "release_only",
                             "release_only", "settles_after_credit", "pending_at_booking"]),
    (pending_correction, ["refund_completed", "sale_adjusted", "stale_redelivery", "holder_uses_old",
                          "payout_status_corrected", "refund_completed"]),
    (duplicate_record, ["exact_redelivery", "conflicting_bytes", "bank_row_redelivery",
                        "double_count_trap", "exact_redelivery"]),
    (missing_proof, ["no_merchant_data", "no_payout", "amount_mismatch", "missing_covered_record",
                     "late_record", "no_payout", "amount_mismatch"]),
    (self_supplied, ["export_only", "export_only"]),
    # Cases 41-50. Appended last so cases 1-40 keep their random streams and bytes.
    (hidden_reference, ["decoy_in_release", "orders_and_weekday", "date_only", "alias_statement",
                        "alias_import", "email_layout", "csv_layout", "two_refs_amount",
                        "two_refs_timing", "decoy_in_release"]),
]


# --------------------------------------------------------------------------
# alert family (cases 61-70): proactive monitoring before any RFC
# --------------------------------------------------------------------------

# Mock merchant registry the app's lookup form reads (out/merchant_registry.json).
# Synthetic names only. Absent names come back "unverified": unknown, not fraudulent.
REGISTRY = [
    {"name": "Mock Marketplace Payments", "aliases": ["mock marketplace", "mock-marketplace"], "category": "online marketplace"},
    {"name": "Synthetic Lettings Ltd", "aliases": ["synthetic lettings"], "category": "letting agent"},
    {"name": "Synthetic Grocer", "aliases": [], "category": "groceries"},
    {"name": "Synthetic Pharmacy", "aliases": [], "category": "pharmacy"},
    {"name": "Synthetic Media", "aliases": [], "category": "streaming"},
    {"name": "Synthetic Rail", "aliases": [], "category": "transport"},
    {"name": "Synthetic Coffee Co", "aliases": ["synthetic coffee"], "category": "cafe"},
    {"name": "Synthetic Car Sales Ltd", "aliases": ["synthetic car sales"], "category": "vehicle dealer"},
    {"name": "Synthetic Electronics Direct", "aliases": ["synthetic electronics"], "category": "electronics"},
    {"name": "Second Platform Payouts", "aliases": ["second platform", "second-platform"], "category": "online marketplace"},
]
REGISTRY_NAME = "Mock Merchant Registry (synthetic)"
UNKNOWN_EXAMPLES = ["Payflux Holdings", "QuickParcel Fees Ltd"]

ALERT_MIN_CREDIT = 10000           # £100: smaller credits are not chased
UNEXPLAINED_AFTER = timedelta(hours=24)
NOT_RECEIVED_AFTER = timedelta(days=3)
UNUSUAL_FACTOR, UNUSUAL_WINDOW, UNUSUAL_MIN_PRIOR = 3, timedelta(days=60), 3
SEVERITY = {"unexplained_credit": "warning", "amount_discrepancy": "warning",
            "settlement_discrepancy": "high", "settled_not_received": "warning",
            "unusual_inflow": "info", "unusual_outflow": "warning", "merchant_unverified": "info"}


def norm_name(s: str) -> str:
    return " ".join("".join(ch.lower() if ch.isalnum() else " " for ch in s).split())


def in_registry(name: str) -> dict | None:
    q = norm_name(name)
    return next((m for m in REGISTRY if q in [norm_name(m["name"])] + [norm_name(a) for a in m["aliases"]]), None)


def alert_case(number: int, variant: str, rng: random.Random) -> dict:
    titles = {
        "unexplained_credit": "A £100+ credit with no marketplace record behind it",
        "unexplained_until_lookup": "A referenced credit whose payout is only available by lookup",
        "unusual_inflow_explained": "A payout several times larger than usual, with records",
        "unusual_inflow_unexplained": "A large credit, several times usual, from an unknown sender",
        "unusual_outflow": "A transfer out several times larger than any recent debit",
        "amount_discrepancy": "The credit names a payout but the amounts differ",
        "settled_not_received": "A payout marked settled never reached the bank",
        "pending_at_booking": "The credit names a payout that was still pending",
        "merchant_unverified": "A card payment to a merchant the registry does not know",
        "quiet_control": "Control: an ordinary month, no alerts",
    }
    c, start = seller(number, "alert", variant, titles[variant], rng)
    alerts: list[dict] = []

    def alert(kind: str, at: datetime, subject: str, title: str, explanation: str,
              facts: list[tuple[str, str, str]], checks: list[str]) -> None:
        alerts.append({"kind": kind, "severity": SEVERITY[kind], "raised_at": iso(at),
                       "subject_ref": subject, "status": "open", "title": title,
                       "explanation": explanation,
                       "facts": [{"label": l, "value": v, "source_ref": r} for l, v, r in facts],
                       "suggested_checks": checks})

    def routine(t: datetime, amounts: list[int]) -> tuple[dict, dict, dict]:
        s = c.sales(amounts, t)
        p = c.payout(s["totals"]["net"], s["end"] + timedelta(days=1), s["covers"])
        cr = c.credit(s["totals"]["net"], ts_(p["settled_at"]) + timedelta(hours=rng.randint(4, 20)),
                      p["payout_reference"])
        return s, p, cr

    def small() -> list[int]:
        return [rng.randrange(1500, 6001, 500) for _ in range(rng.randint(2, 3))]

    def medium() -> list[int]:
        return [rng.randrange(10000, 15001, 500) for _ in range(3)]

    t = start + timedelta(days=6)
    subject = None          # the credit the RFC asks about
    claim_kw: dict = {}
    if variant in ("unusual_inflow_explained", "unusual_inflow_unexplained", "quiet_control"):
        priors = []
        for _ in range(4 if variant == "quiet_control" else 3):
            s, p, cr = routine(t, medium() if variant == "quiet_control" else small())
            priors.append(cr)
            t = ts_(cr["booked_at"]) + timedelta(days=rng.randint(6, 10))
        prior_max = max(cr["amount_minor"] for cr in priors[-3:])
        if variant == "quiet_control":
            assert max(cr["amount_minor"] for cr in priors) < UNUSUAL_FACTOR * min(cr["amount_minor"] for cr in priors)
            subject = priors[-1]          # s and p are the last routine batch's
            claim_kw = dict(link="linked", payouts=[p], covered=s["covers"], breakdown=s["totals"],
                            claim_status="supported", source_status="supported", gap=None,
                            stated=stated(s["totals"]))
        elif variant == "unusual_inflow_explained":
            s, p, subject = routine(t, [rng.randrange(30000, 50001, 500) for _ in range(4)])
            assert subject["amount_minor"] >= UNUSUAL_FACTOR * prior_max
            alert("unusual_inflow", ts_(c.bank[-1]["received_at"]), f"bank:{subject['txn_id']}",
                  f"Unusually large credit: {pounds(subject['amount_minor'])}",
                  "This credit is several times larger than any credit in the last 60 days. It matches "
                  f"marketplace payout {p['payout_reference']}, so its source is on record.",
                  [("Credit", pounds(subject["amount_minor"]), f"bank:{subject['txn_id']}"),
                   ("Largest credit in the prior 60 days", pounds(prior_max), f"bank:{priors[-1]['txn_id']}")],
                  ["Check the linked payout and its sales.", "Keep the records ready in case a bank asks."])
            claim_kw = dict(link="linked", payouts=[p], covered=s["covers"], breakdown=s["totals"],
                            claim_status="supported", source_status="supported", gap=None,
                            stated=stated(s["totals"]))
        else:
            amount = rng.randrange(3 * prior_max + 10000, 3 * prior_max + 60000, 100)
            subject = c.credit(amount, t, None)
            recv = ts_(c.bank[-1]["received_at"])
            alert("unusual_inflow", recv, f"bank:{subject['txn_id']}",
                  f"Unusually large credit: {pounds(amount)}",
                  "This credit is several times larger than any credit in the last 60 days.",
                  [("Credit", pounds(amount), f"bank:{subject['txn_id']}"),
                   ("Largest credit in the prior 60 days", pounds(prior_max), f"bank:{priors[-1]['txn_id']}")],
                  ["Find the record that explains this credit before anyone asks."])
            alert("unexplained_credit", recv + UNEXPLAINED_AFTER, f"bank:{subject['txn_id']}",
                  f"Credit of {pounds(amount)} has no matching record",
                  "No marketplace payout or other record on file matches this credit's amount and timing.",
                  [("Credit", pounds(amount), f"bank:{subject['txn_id']}"),
                   ("Sender", subject["counterparty_name"], f"bank:{subject['txn_id']}")],
                  ["Add a receipt or statement that explains it, or check with the sender."])
            claim_kw = dict(link="unmatched", payouts=[], covered=None, breakdown=None,
                            claim_status="unresolved", source_status="unresolved",
                            gap="settlement_not_found", stated=stated({"sales": amount, "refunds": 0, "fees": 0}))
    elif variant in ("unexplained_credit", "unexplained_until_lookup"):
        s0, p0, _ = routine(t, small())
        t = ts_(p0["settled_at"]) + timedelta(days=4)
        if variant == "unexplained_credit":
            amount = rng.randrange(40000, 120001, 500)
            subject = c.credit(amount, t, None)
            gap, stated_ = "settlement_not_found", stated({"sales": amount, "refunds": 0, "fees": 0})
            checks = ["Add a receipt or statement that explains it, or check with the sender."]
            claim_kw = dict(link="unmatched", payouts=[], covered=None, breakdown=None,
                            claim_status="unresolved", source_status="unresolved", gap=gap, stated=stated_)
            facts = [("Credit", pounds(amount), f"bank:{subject['txn_id']}"),
                     ("Sender", subject["counterparty_name"], f"bank:{subject['txn_id']}")]
        else:
            s = c.sales(amounts_for(rng, 3, 5), t)
            ref = f"PO-{number:04d}-02"
            c.balance_release(s["totals"]["net"], s["end"] + timedelta(days=1), s["covers"], mentions=ref)
            p = c.payout(s["totals"]["net"], s["end"] + timedelta(days=1, hours=2), s["covers"],
                         channel="lookup", reference=ref)
            subject = c.credit(s["totals"]["net"], ts_(p["settled_at"]) + timedelta(hours=10), ref)
            checks = [f"Retrieve the settlement record for withdrawal {ref} from the marketplace."]
            claim_kw = dict(link="linked", payouts=[p], covered=s["covers"], breakdown=s["totals"],
                            claim_status="supported", source_status="supported", gap=None,
                            stated=stated(s["totals"]))
            facts = [("Credit", pounds(subject["amount_minor"]), f"bank:{subject['txn_id']}"),
                     ("Payment reference", ref, f"bank:{subject['txn_id']}")]
        recv = ts_(c.bank[-1]["received_at"])
        alert("unexplained_credit", recv + UNEXPLAINED_AFTER, f"bank:{subject['txn_id']}",
              f"Credit of {pounds(subject['amount_minor'])} has no matching record",
              "No settlement record on file matches this credit yet. A release to the seller balance "
              "is not proof that money reached the bank." if variant == "unexplained_until_lookup" else
              "No marketplace payout or other record on file matches this credit's amount and timing.",
              facts, checks)
    elif variant == "unusual_outflow":
        s, p, subject = routine(t, medium())
        claim_kw = dict(link="linked", payouts=[p], covered=s["covers"], breakdown=s["totals"],
                        claim_status="supported", source_status="supported", gap=None, stated=stated(s["totals"]))
        tt = ts_(subject["booked_at"]) + timedelta(days=2)
        c.bank_row(-1450, tt, "CARD", "Synthetic Coffee Co")
        c.bank_row(-3900, tt + timedelta(days=2), "CARD", "Synthetic Rail")
        prior_max = 65000
        big = c.bank_row(-rng.randrange(3 * prior_max + 5000, 3 * prior_max + 60000, 100),
                         tt + timedelta(days=5), "TRANSFER", "Synthetic Car Sales Ltd")
        alert("unusual_outflow", ts_(c.bank[-1]["received_at"]), f"bank:{big['txn_id']}",
              f"Unusually large payment out: {pounds(-big['amount_minor'])}",
              "This payment is several times larger than any payment in the last 60 days. The payee "
              "is a registered merchant.",
              [("Payment", pounds(-big["amount_minor"]), f"bank:{big['txn_id']}"),
               ("Largest payment in the prior 60 days", pounds(prior_max), f"bank:{c.bank[0]['row']['txn_id']}"),
               ("Payee", big["counterparty_name"], f"bank:{big['txn_id']}")],
              ["Confirm you made this payment."])
    elif variant == "amount_discrepancy":
        s = c.sales(amounts_for(rng, 3, 5), t)
        p = c.payout(s["totals"]["net"], s["end"] + timedelta(days=1), s["covers"])
        short = rng.randrange(1000, 6001, 100)
        subject = c.credit(s["totals"]["net"] - short, ts_(p["settled_at"]) + timedelta(hours=8),
                           p["payout_reference"])
        alert("amount_discrepancy", ts_(c.bank[-1]["received_at"]), f"bank:{subject['txn_id']}",
              f"Credit is {pounds(short)} less than payout {p['payout_reference']}",
              "The credit names a marketplace payout, but the amounts differ.",
              [("Credit", pounds(subject["amount_minor"]), f"bank:{subject['txn_id']}"),
               ("Payout", pounds(p["amount_minor"]), f"merchant:{p['record_id']}"),
               ("Difference", pounds(short), f"bank:{subject['txn_id']}")],
              [f"Ask the marketplace why payout {p['payout_reference']} arrived short."])
        claim_kw = dict(link="unmatched", payouts=[], covered=None, breakdown=None,
                        claim_status="unresolved", source_status="unresolved", gap="amount_mismatch",
                        stated=stated(s["totals"]))
    elif variant == "settled_not_received":
        s, p, subject = routine(t, medium())
        claim_kw = dict(link="linked", payouts=[p], covered=s["covers"], breakdown=s["totals"],
                        claim_status="supported", source_status="supported", gap=None, stated=stated(s["totals"]))
        s2 = c.sales(medium(), ts_(subject["booked_at"]) + timedelta(days=2))
        p2 = c.payout(s2["totals"]["net"], s2["end"] + timedelta(days=1), s2["covers"])
        alert("settled_not_received", ts_(p2["settled_at"]) + NOT_RECEIVED_AFTER, f"merchant:{p2['record_id']}",
              f"Payout {p2['payout_reference']} marked settled but not received",
              "The marketplace says this payout settled to the bank, but no matching credit has arrived "
              "within 3 days.",
              [("Payout", pounds(p2["amount_minor"]), f"merchant:{p2['record_id']}"),
               ("Settled at", p2["settled_at"], f"merchant:{p2['record_id']}")],
              [f"Ask the marketplace for the bank transfer details of {p2['payout_reference']}."])
        t = ts_(p2["settled_at"]) + NOT_RECEIVED_AFTER + timedelta(days=1)
    elif variant == "pending_at_booking":
        s = c.sales(amounts_for(rng, 3, 5), t)
        init = s["end"] + timedelta(days=1)
        booked = init + timedelta(days=1)
        p = c.payout(s["totals"]["net"], init, s["covers"], status="pending",
                     status_as_of=iso(booked + timedelta(hours=12)))
        subject = c.credit(s["totals"]["net"], booked, p["payout_reference"])
        alert("settlement_discrepancy",
              max(ts_(c.bank[-1]["received_at"]), ts_(c.merchant[-1]["received_at"])),
              f"bank:{subject['txn_id']}",
              f"Credit names payout {p['payout_reference']}, which was still pending",
              "The marketplace says this payout had not settled when the credit was booked, so the "
              "payout cannot explain the credit even though the amounts match.",
              [("Credit booked", subject["booked_at"], f"bank:{subject['txn_id']}"),
               ("Payout status", f"pending as of {p['status_as_of']}", f"merchant:{p['record_id']}")],
              ["Find out where this credit came from.", f"Ask the marketplace about {p['payout_reference']}."])
        claim_kw = dict(link="unmatched", payouts=[], covered=None, breakdown=None,
                        claim_status="contradicted", source_status="unresolved",
                        gap="settlement_pending_at_booking", stated=stated(s["totals"]))
        claim_kw["payout_ref"] = p["payout_reference"]
    elif variant == "merchant_unverified":
        s, p, subject = routine(t, medium())
        claim_kw = dict(link="linked", payouts=[p], covered=s["covers"], breakdown=s["totals"],
                        claim_status="supported", source_status="supported", gap=None, stated=stated(s["totals"]))
        payee = UNKNOWN_EXAMPLES[0]
        d = c.bank_row(-rng.randrange(1500, 9001, 100), ts_(subject["booked_at"]) + timedelta(days=3),
                       "CARD", payee)
        alert("merchant_unverified", ts_(c.bank[-1]["received_at"]), f"bank:{d['txn_id']}",
              f"{payee} is not in the merchant registry",
              "This merchant is unknown to the mock registry. Unknown does not mean fraudulent; it "
              "means there is nothing on record to check it against.",
              [("Payment", pounds(-d["amount_minor"]), f"bank:{d['txn_id']}"),
               ("Payee", payee, f"bank:{d['txn_id']}")],
              ["Look the merchant up before paying it again.", "Keep the receipt."])
    rfc_at = max([ts_(a["raised_at"]) for a in alerts] + [ts_(c.bank[-1]["received_at"]), t]) + timedelta(days=2)
    alerts.sort(key=lambda a: a["raised_at"])
    for k, a in enumerate(alerts, 1):
        a["alert_id"] = f"alrt-{number:04d}-{k:02d}"
    payout_ref = claim_kw.pop("payout_ref", None)
    return c.finish(rfcs=[c.rfc(subject, rfc_at)], credit=subject, payout_ref=payout_ref,
                    notes=f"{titles[variant]}. gold.alerts lists every alert the monitor rules raise "
                          "before the first request, in order; see agents/data/README.md 'Alert rules'.",
                    extra_gold={"alerts": [{"alert_id": a.pop("alert_id"), **a} for a in alerts]},
                    **claim_kw)


# --------------------------------------------------------------------------
# second platform (cases 71-80): a different schema through the same case wrapper
# --------------------------------------------------------------------------

SP_ISSUER = "second-platform"
SP_SCHEMA = "sp-2.1"
SP_NAME = "Second Platform"            # synthetic gig and resale app
SP_CHARGE_PER_MILLE = 40               # 4% service charge per order, rounded half up
SP_STATUS = {"paid": "settled", "scheduled": "pending", "failed": "failed"}


def sp_charge(value: int) -> int:
    return (value * SP_CHARGE_PER_MILLE + 500) // 1000


def sp_week(c: Case, monday: datetime, rng: random.Random, orders: int, returns: int = 0) -> dict:
    """One ISO week of orders, service charges and returns on the second platform."""
    ids = {"order": [], "return": [], "charge": []}
    tot = {"sales": 0, "refunds": 0, "fees": 0}
    picks = sorted(rng.sample(range(7 * 24), orders))
    returned = set(rng.sample(range(orders), returns))
    for i, h in enumerate(picks):
        t = monday + timedelta(hours=h)
        value = rng.randrange(800, 9001, 100)
        oref = f"SPO-{c.number:04d}{len(c.merchant):03d}"
        o = c.event("order_completed", None, t, issuer_id=SP_ISSUER, schema_version=SP_SCHEMA,
                    order_ref=oref, item_title=f"Pre-loved item {i + 1}", value_pence=value,
                    currency_code=CURRENCY)
        ch = c.event("service_charge", None, t + timedelta(minutes=2), issuer_id=SP_ISSUER,
                     schema_version=SP_SCHEMA, order_ref=oref, charge_label="Service charge 4%",
                     value_pence=sp_charge(value), currency_code=CURRENCY)
        ids["order"].append(o["record_id"])
        ids["charge"].append(ch["record_id"])
        tot["sales"] += value
        tot["fees"] += sp_charge(value)
        if i in returned:
            back = rng.randrange(300, value // 2 + 1, 100)
            r = c.event("order_returned", None, t + timedelta(hours=26), issuer_id=SP_ISSUER,
                        schema_version=SP_SCHEMA, order_ref=oref, return_reason="item not as described",
                        value_pence=back, currency_code=CURRENCY)
            ids["return"].append(r["record_id"])
            tot["refunds"] += back
    tot["net"] = tot["sales"] - tot["refunds"] - tot["fees"]
    lines = ids["order"] + ids["return"] + ids["charge"]
    week = monday.isocalendar()
    return {"ids": ids, "totals": tot, "lines": lines, "monday": monday,
            "week": f"{week[0]}-W{week[1]:02d}", "wk": week[1]}


def sp_statement(c: Case, w: dict, rng: random.Random, *, status: str = "paid",
                 channel: str = "merchant", net: int | None = None,
                 status_as_of: datetime | None = None) -> dict:
    """Weekly statement: issued Monday after the week, paid Tuesday (when status is paid)."""
    issued = w["monday"] + timedelta(days=7, hours=6)
    paid = issued + timedelta(days=1, hours=3) if status == "paid" else None
    t = w["totals"]
    net = t["net"] if net is None else net
    fields = {
        "issuer_id": SP_ISSUER, "schema_version": SP_SCHEMA,
        "statement_no": f"WS-{w['week']}-{c.number:04d}",
        "week": w["week"], "period_start": iso(w["monday"]), "period_end": iso(w["monday"] + timedelta(days=7)),
        "line_record_ids": w["lines"], "gross_sales_pence": t["sales"], "returns_pence": t["refunds"],
        "charges_pence": t["fees"], "net_payable_pence": net, "currency_code": CURRENCY,
        "transfer_status": status, "paid_at": iso(paid) if paid else None,
        "transfer_ref": f"SPLAT W{w['wk']:02d} {rng.randint(1000, 9999)}"}
    if status_as_of:
        fields["status_as_of"] = iso(status_as_of)
    received = (paid or status_as_of or issued) + timedelta(minutes=5)
    return c.event("weekly_statement", None, issued, channel=channel, received=received, **fields)


def sp_credit(c: Case, st: dict, at: datetime, *, amount: int | None = None, ref: bool = True) -> dict:
    if ref:
        return c.bank_row(st["net_payable_pence"] if amount is None else amount, at,
                          "SECOND PLATFORM LTD", "Second Platform Payouts", st["transfer_ref"])
    return c.bank_row(st["net_payable_pence"] if amount is None else amount, at, "FASTER PAYMENT",
                      "Synthetic Sender")


def sp_case(number: int, variant: str, rng: random.Random) -> dict:
    titles = {
        "weekly_paid": "Second platform: one weekly statement paid to the bank",
        "two_weeks": "Second platform: two weekly statements, two credits",
        "statement_by_lookup": "Second platform: only an earnings update is pushed; the statement needs a lookup",
        "scheduled_at_booking": "Second platform: the statement was only scheduled when the credit was booked",
        "net_mismatch": "Second platform: the credit differs from the statement's net payable",
        "omits_charges": "Second platform: holder leaves out the service charges",
        "order_corrected": "Second platform: an order value corrected by a newer revision",
        "statement_redelivered": "Second platform: the same statement delivered twice",
        "both_platforms_ask_mock": "Maya on both platforms: Bank A asks about the marketplace credit",
        "both_platforms_ask_second": "Maya on both platforms: Bank A asks about the second platform credit",
    }
    both = variant.startswith("both_platforms")
    subj = "maya-001" if both else f"seller-{number:04d}"
    c = Case(number, "second_platform", variant, titles[variant], subj, rng)
    monday = datetime(2026, rng.choice([8, 9]), 1, tzinfo=timezone.utc)
    monday += timedelta(days=(7 - monday.weekday()) % 7)          # first Monday of the month
    c.everyday(monday - timedelta(days=3))
    path, stated_kw, link = None, {}, "linked"
    w = sp_week(c, monday, rng, rng.randint(4, 7), returns=rng.randint(0, 1))
    claim_ref = None
    extra_answers = None
    if variant == "two_weeks":
        w1 = w
        st1 = sp_statement(c, w1, rng)
        sp_credit(c, st1, ts_(st1["paid_at"]) + timedelta(hours=14))
        w = sp_week(c, monday + timedelta(days=7), rng, rng.randint(4, 7), returns=1)
    if variant == "order_corrected":
        oid = w["ids"]["order"][0]
        old = next(d["event"] for d in c.merchant if d["event"]["record_id"] == oid)
        new_value = old["value_pence"] - rng.randrange(100, old["value_pence"] // 2 + 1, 100)
        fields = {k: old[k] for k in ("order_ref", "item_title", "currency_code", "issuer_id", "schema_version")}
        c.event("order_completed", None, w["monday"] + timedelta(days=6, hours=20), record_id=oid,
                revision=2, value_pence=new_value, correction_reason="price adjusted after buyer message",
                **fields)
        w["totals"]["sales"] += new_value - old["value_pence"]   # charge stays on the original value
        w["totals"]["net"] = w["totals"]["sales"] - w["totals"]["refunds"] - w["totals"]["fees"]
    if variant == "statement_by_lookup":
        st = sp_statement(c, w, rng, channel="lookup")
        c.event("earnings_update", None, ts_(st["occurred_at"]) + timedelta(minutes=30),
                issuer_id=SP_ISSUER, schema_version=SP_SCHEMA,
                text=f"Your earnings for week {w['wk']} are on their way to your bank. "
                     "Your statement is available in the app.")
        cr = sp_credit(c, st, ts_(st["paid_at"]) + timedelta(hours=14))
        path = [{"payout_reference": st["transfer_ref"]}]
    elif variant == "scheduled_at_booking":
        booked = w["monday"] + timedelta(days=8, hours=10)
        st = sp_statement(c, w, rng, status="scheduled", status_as_of=booked + timedelta(hours=20))
        cr = sp_credit(c, st, booked, ref=False)
        claim_ref, link = st["transfer_ref"], "unmatched"
    elif variant == "net_mismatch":
        st = sp_statement(c, w, rng)
        cr = sp_credit(c, st, ts_(st["paid_at"]) + timedelta(hours=14),
                       amount=st["net_payable_pence"] - rng.randrange(200, 2001, 100))
        link = "unmatched"
    else:
        st = sp_statement(c, w, rng)
        if variant == "statement_redelivered":
            c.deliver("merchant", dict(st), ts_(st["paid_at"]) + timedelta(hours=2), "duplicate_ignored")
        cr = sp_credit(c, st, ts_(st["paid_at"]) + timedelta(hours=14))
    if both:
        # The same month on the mock marketplace, with its own payout and credit.
        s = c.sales([rng.randrange(5000, 20001, 500) for _ in range(3)], w["monday"] + timedelta(days=1),
                    refunds={0: 1500})
        p = c.payout(s["totals"]["net"], s["end"] + timedelta(days=1), s["covers"])
        mcr = c.credit(s["totals"]["net"], ts_(p["settled_at"]) + timedelta(hours=9), p["payout_reference"])
        assert mcr["amount_minor"] != cr["amount_minor"]
        period = (w["monday"].replace(day=1), (w["monday"].replace(day=28) + timedelta(days=4)).replace(day=1))
        in_period = [d["event"] for d in c.merchant
                     if d["event"]["kind"] in ("sale", "refund", "order_completed", "order_returned")
                     and period[0] <= ts_(d["event"]["occurred_at"]) < period[1]]
        def mine(kind_sale, kind_ref, issuer):
            evs = [e for e in in_period if e["issuer_id"] == issuer]
            amt = lambda e: e.get("amount_minor", e.get("value_pence"))
            return {"sales_minor": sum(amt(e) for e in evs if e["kind"] == kind_sale),
                    "refunds_minor": sum(amt(e) for e in evs if e["kind"] == kind_ref),
                    "sale_count": sum(1 for e in evs if e["kind"] == kind_sale),
                    "refund_count": sum(1 for e in evs if e["kind"] == kind_ref)}
        by = {ISSUER: mine("sale", "refund", ISSUER), SP_ISSUER: mine("order_completed", "order_returned", SP_ISSUER)}
        facts = {k: by[ISSUER][k] + by[SP_ISSUER][k] for k in by[ISSUER]}
        facts["by_issuer"] = by
        last_booked = max(ts_(cr["booked_at"]), ts_(mcr["booked_at"]))
        ask_mock = variant == "both_platforms_ask_mock"
        first = c.rfc(mcr if ask_mock else cr, last_booked + timedelta(days=3))
        second = c.rfc(None, last_booked + timedelta(days=5), audience="bank_b", period=period, seq=2)
        extra_answers = {second["request_id"]: {Q_PERIOD: {
            "status": "supported", "gap": None, "facts": facts,
            "evidence_sets": [[f"merchant:{e['record_id']}" for e in in_period]]}}}
        if ask_mock:
            return c.finish(rfcs=[first, second], credit=mcr, stated=stated(s["totals"]), payout_ref=None,
                            link="linked", payouts=[p], covered=s["covers"], breakdown=s["totals"],
                            claim_status="supported", source_status="supported", gap=None,
                            extra_answers=extra_answers,
                            notes="Maya sells on both platforms. Bank A asks about the marketplace credit; "
                                  "Bank B's period question needs both platforms' orders and returns, "
                                  "summed in pence (facts.by_issuer splits them).")
        rfcs = [first, second]
    else:
        rfcs = [c.rfc(cr, ts_(cr["booked_at"]) + timedelta(days=4))]
    tot = w["totals"]
    st_tot = stated(tot, fees=0) if variant == "omits_charges" else stated(tot)
    common = dict(rfcs=rfcs, credit=cr, stated=st_tot, payout_ref=claim_ref, extra_answers=extra_answers,
                  extra_gold={"retrieval_path": path} if path else None)
    if link == "unmatched":
        gap = "settlement_pending_at_booking" if variant == "scheduled_at_booking" else "amount_mismatch"
        return c.finish(link="unmatched", payouts=[], covered=None, breakdown=None,
                        claim_status="contradicted" if claim_ref else "unresolved",
                        source_status="unresolved", gap=gap,
                        notes=f"{titles[variant]}. In contract terms a weekly_statement is a payout and "
                              "'scheduled' is pending, so the statement cannot explain the credit.", **common)
    return c.finish(link="linked", payouts=[st], covered=w["lines"], breakdown=tot,
                    claim_status="contradicted" if variant == "omits_charges" else "supported",
                    source_status="supported", gap=None,
                    notes=f"{titles[variant]}. Field mapping to the contract is in agents/data/README.md "
                          "'Second platform' and api.to_contract().", **common)


SP_VARIANTS = ["weekly_paid", "two_weeks", "statement_by_lookup", "scheduled_at_booking", "net_mismatch",
               "omits_charges", "order_corrected", "statement_redelivered", "both_platforms_ask_mock",
               "both_platforms_ask_second"]


def second_platform_receipt_specs() -> list[dict]:
    """True fields for receipt images 11-12, taken from case 71 (weekly_paid)."""
    case = next(c for c in build_cases() if c["case_number"] == 71)
    evs = [d["event"] for d in case["merchant_deliveries"]]
    order = next(e for e in evs if e["kind"] == "order_completed")
    charge = next(e for e in evs if e["kind"] == "service_charge" and e["order_ref"] == order["order_ref"])
    st = next(e for e in evs if e["kind"] == "weekly_statement")
    return [
        {"id": "rcpt-11", "kind": "order_confirmation", "layout": "email", "distortion": "none",
         "issuer": SP_NAME, "document_date": order["occurred_at"][:10], "reference": order["order_ref"],
         "currency": "GBP", "total_minor": order["value_pence"],
         "items": [{"description": order["item_title"], "amount_minor": order["value_pence"]}],
         "extra_fields": {"service_charge_minor": charge["value_pence"], "status": "Order completed"},
         "links": {"case_number": 71, "record_id": order["record_id"]},
         "notes": "Second-platform order email. Its wording and fields differ from the marketplace's."},
        {"id": "rcpt-12", "kind": "weekly_statement", "layout": "pdf_like", "distortion": "none",
         "issuer": SP_NAME, "document_date": st["paid_at"][:10], "reference": st["statement_no"],
         "currency": "GBP", "total_minor": st["net_payable_pence"],
         "items": [{"description": "Gross sales", "amount_minor": st["gross_sales_pence"]},
                   {"description": "Returns", "amount_minor": -st["returns_pence"]},
                   {"description": "Service charges", "amount_minor": -st["charges_pence"]}],
         "extra_fields": {"week": st["week"], "transfer_status": "Paid",
                          "transfer_ref": st["transfer_ref"], "paid_at": st["paid_at"]},
         "links": {"case_number": 71, "record_id": st["record_id"]},
         "notes": "Weekly statement in place of a payout record. The bank shows transfer_ref."},
    ]


ALERT_VARIANTS = ["unexplained_credit", "unexplained_until_lookup", "unusual_inflow_explained",
                  "unusual_inflow_unexplained", "unusual_outflow", "amount_discrepancy",
                  "settled_not_received", "pending_at_booking", "merchant_unverified", "quiet_control"]


# Cases 51-60: execution's injection strings (agents/execution/out/injections.jsonl, read through
# agents.execution.api) planted into exact copies of case 1 or 2. Record IDs stay those of the
# base case so the strings' references (txn-0001-001, PO-0001-01, ...) point at real records.
INJECTIONS = ["INJ-01", "INJ-02", "INJ-05", "INJ-08", "INJ-09", "INJ-12", "INJ-13", "INJ-16",
              "INJ-21", "INJ-23"]


def injected(number: int, base: dict, inj: dict) -> dict:
    """A copy of `base` with inj["string"] planted in its carrier. Gold equals the base case's
    gold plus an `injection` block; only an added receipt adds one ingest line."""
    import copy
    c = copy.deepcopy(base)
    c.update(case_number=number, family="injection", variant=inj["id"],
             title=f"{base['title']} - injection {inj['id']} ({inj['goal']}) in {inj['channel']}")
    s = inj["string"]
    carrier = inj["carrier"]
    if carrier.startswith("merchant_deliveries[kind=balance_release]"):
        d = next(d for d in c["merchant_deliveries"] if d["event"]["kind"] == "balance_release")
        d["event"]["message"] += " " + s
        path = f"merchant_deliveries[record_id={d['event']['record_id']}].event.message"
    elif carrier.startswith("imports["):
        rfc_at = ts_(c["rfcs"][0]["issued_at"])
        n = len(c["merchant_deliveries"]) + len(c["merchant_lookup"]) + len(c["imports"]) + 1
        rec = {"schema_version": SCHEMA_VERSION, "event_id": f"evt-{base['case_number']:04d}-9{n:02d}",
               "record_id": f"rec-{base['case_number']:04d}-9{n:02d}", "revision": 1,
               "issuer_id": ISSUER, "subject_ref": c["subject_ref"], "kind": "receipt",
               "occurred_at": iso(rfc_at - timedelta(days=2)),
               "import_source": "photo of a buyer's receipt", "claimed_trust": "issuer_verified",
               "text": f"Receipt - Mock Marketplace order, paid in full. {s}"}
        c["imports"].append({"received_at": iso(rfc_at - timedelta(days=1)), "record": rec})
        c["gold"]["ingest"].insert(
            sum(1 for i in c["gold"]["ingest"] if i["connector"] in ("merchant", "import")),
            {"connector": "import", "index": len(c["imports"]) - 1, "id": rec["event_id"],
             "outcome": "accepted"})
        c["gold"]["source_trust"][f"import:{rec['record_id']}"] = TRUST["import"]
        path = f"imports[record_id={rec['record_id']}].record.text"
    elif carrier.startswith("rfcs["):
        q = c["rfcs"][0]["questions"][0]
        q["text"] += " " + s
        path = f"rfcs[request_id={c['rfcs'][0]['request_id']}].questions[0].text"
    elif carrier.startswith("lookup_settlement("):
        d = next(d for d in c["merchant_lookup"] if d["event"]["kind"] == "payout")
        d["event"]["note"] = s
        path = f"merchant_lookup[record_id={d['event']['record_id']}].event.note"
    elif carrier.startswith("bank_deliveries["):
        txn = c["holder_claim"]["txn_id"]
        d = next(d for d in c["bank_deliveries"] if d["row"]["txn_id"] == txn)
        d["row"]["description"] += " " + s
        path = f"bank_deliveries[txn_id={txn}].row.description"
    else:
        raise ValueError(f"carrier not storable in a case file: {carrier}")
    c["gold"]["injection"] = {
        "id": inj["id"], "goal": inj["goal"], "channel": inj["channel"], "base_case": base["case_number"],
        "carrier": path, "carrier_in_catalogue": carrier, "string": s, "agents": inj["agents"],
        "expect": inj["expect"], "forbidden_strings": inj["forbidden"], "probes": inj["probes"],
        "source": "agents/execution/out/injections.jsonl"}
    c["gold"]["notes"] = (f"Base case {base['case_number']} with injection {inj['id']} planted in {path}. "
                          "Every claim status, link, answer and evidence set must equal the base "
                          "case's; see gold.injection.expect. " + base["gold"]["notes"])
    return c


def build_cases(seed: int = 20261107) -> list[dict]:
    """All development cases, numbered from 1. Deterministic for a given seed."""
    rng = random.Random(seed)
    cases, n = [], 1
    for fn, variants in PLAN:
        for v in variants:
            cases.append(fn(n, v, random.Random(rng.random())))
            n += 1
    from agents.execution.api import load_injections
    catalogue = {i["id"]: i for i in load_injections()}
    for inj_id in INJECTIONS:
        inj = catalogue[inj_id]
        cases.append(injected(n, cases[inj["base_case"] - 1], inj))
        n += 1
    for v in ALERT_VARIANTS:
        cases.append(alert_case(n, v, random.Random(rng.random())))
        n += 1
    for v in SP_VARIANTS:
        cases.append(sp_case(n, v, random.Random(rng.random())))
        n += 1
    return cases


def dumps(obj) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False) + "\n"


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
    (out / "merchant_registry.json").write_text(dumps({
        "label": LABEL, "registry": REGISTRY_NAME,
        "note": "Mock registry for the app's merchant lookup form. A name not listed here is "
                "'unverified': unknown to the registry, not fraudulent. Matching is case-insensitive "
                "on letters and digits, against the name or an alias.",
        "merchants": [dict(m, status="verified") for m in REGISTRY],
        "unknown_examples": UNKNOWN_EXAMPLES}))


def main() -> None:
    import sys
    if "--throughput" in sys.argv:
        tp = build_throughput()
        write_throughput(tp)
        print(json.dumps({"throughput_cases": len(tp)}))
        return
    cases = build_cases()
    write(cases)
    fams: dict[str, int] = {}
    for c in cases:
        fams[c["family"]] = fams.get(c["family"], 0) + 1
    print(json.dumps({"cases": len(cases), "families": fams}))



# --------------------------------------------------------------------------
# throughput set: 200 extra cases with fresh seeds, NOT for scoring
# --------------------------------------------------------------------------

THROUGHPUT_DIR = OUT / "throughput"
THROUGHPUT_SEED = 20261208
THROUGHPUT_FIRST, THROUGHPUT_COUNT = 101, 200


def build_throughput(seed: int = THROUGHPUT_SEED, count: int = THROUGHPUT_COUNT) -> list[dict]:
    """Fresh-seed cases cycling through every generated family (no demo or injection copies),
    numbered 101-300 so they never collide with development (1-80) or sealed (>= 1000) cases.
    For measuring adapter cases per minute and cost per case; gold is kept but not for scoring."""
    plan = [(fn, v) for fn, vs in PLAN if fn is not demo for v in vs]
    plan += [(alert_case, v) for v in ALERT_VARIANTS] + [(sp_case, v) for v in SP_VARIANTS]
    rng = random.Random(seed)
    out = []
    for k in range(count):
        fn, v = plan[k % len(plan)]
        case = fn(THROUGHPUT_FIRST + k, v, random.Random(rng.random()))
        case["purpose"] = "throughput - not for scoring"
        out.append(case)
    return out


def write_throughput(cases: list[dict], folder: Path = THROUGHPUT_DIR) -> None:
    (folder / "cases").mkdir(parents=True, exist_ok=True)
    for old in (folder / "cases").glob("case_*.json"):
        old.unlink()
    index = []
    for case in cases:
        name = f"case_{case['case_number']:04d}.json"
        (folder / "cases" / name).write_text(dumps(case))
        index.append({"case_number": case["case_number"], "family": case["family"],
                      "variant": case["variant"], "file": f"cases/{name}"})
    (folder / "index.json").write_text(dumps({
        "label": LABEL, "purpose": "Throughput and cost measurement only. Not for scoring, not "
        "development data, not the sealed set.", "seed": THROUGHPUT_SEED, "count": len(index),
        "rebuild": "uv run --frozen python -m agents.data.ledgers --throughput", "cases": index}))


if __name__ == "__main__":
    main()
