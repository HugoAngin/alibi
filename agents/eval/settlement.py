"""Deterministic matching and arithmetic shared by the Alibi benchmark drafts."""

from collections import defaultdict
from datetime import datetime, timedelta
import re

WINDOW = timedelta(days=5)
REFERENCE = re.compile(
    r"\b(?:withdrawal|payout|settlement)\s+(?:ref(?:erence)?[\s:#-]+)?"
    r"([A-Z0-9]+(?:[-_][A-Z0-9]+)+)\b", re.IGNORECASE,
)
CANONICAL_REFERENCE = re.compile(r"\bPO-\d{4}-\d{2}\b")
BUCKETS = {"sale": "sales", "refund": "refunds", "fee": "fees"}
IMPORT_FIELDS = {"record_id", "event_id", "revision", "subject_ref", "occurred_at", "amount_minor", "currency"}


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _active(records: list[dict], subject: str, source_kind: str) -> dict:
    by_id, seen = {}, set()
    for row in records:
        if row["subject_ref"] != subject or row["event_id"] in seen:
            continue
        seen.add(row["event_id"])
        current = by_id.get(row["record_id"])
        if current is None or row["revision"] > current["revision"]:
            by_id[row["record_id"]] = {**row, "source_kind": source_kind}
    return by_id


def _source(kind: str, identity: str, row: dict) -> dict:
    return {"source_id": f"{kind}:{row[identity]}", "revision": row["revision"]}


def _candidate_import(row: dict) -> bool:
    # A free-form import stays reviewable, but cannot stand in for a typed event.
    if not isinstance(row, dict) or not IMPORT_FIELDS <= row.keys():
        return False
    if type(row["revision"]) is not int or row["revision"] < 1:
        return False
    if type(row["amount_minor"]) is not int or row["amount_minor"] <= 0:
        return False
    if any(not isinstance(row[key], str) or not row[key]
           for key in ("record_id", "event_id", "subject_ref", "occurred_at", "currency")):
        return False
    kind = row.get("kind")
    if not isinstance(kind, str):
        return False
    try:
        if _time(row["occurred_at"]).utcoffset() != timedelta(0):
            return False
        if kind == "payout":
            covered = row.get("covers_record_ids")
            return (row.get("destination") == "bank" and isinstance(row.get("payout_reference"), str)
                    and isinstance(covered, list) and all(isinstance(record, str) for record in covered)
                    and (row.get("settlement_batch_id") is None or isinstance(row["settlement_batch_id"], str))
                    and (row.get("status") in ("pending", "failed")
                         or (row.get("status") == "settled" and _time(row["settled_at"]).utcoffset() == timedelta(0))))
    except (AttributeError, KeyError, ValueError):
        return False
    return kind in BUCKETS


def fixed_candidate_pool(snapshot: dict, lookup) -> tuple[dict, dict, dict]:
    """Replay visible evidence and run the same fixed retrieval for both baselines."""
    [request] = snapshot["rfcs"]
    subject = snapshot["subject_ref"]
    bank = {}
    for entry in snapshot["bank_deliveries"]:
        row = entry["row"]
        bank.setdefault(row["txn_id"], row)
    merchant = [entry["event"] for entry in snapshot["merchant_deliveries"]]
    claim = snapshot["holder_claim"]
    active = _active(merchant, subject, "merchant")
    references = set()
    for txn_id in request.get("txn_ids") or ():
        credit = bank.get(txn_id)
        if credit is None:
            continue
        if credit.get("payment_reference"):
            references.add(credit["payment_reference"])
        if claim["txn_id"] == txn_id and claim.get("payout_reference"):
            references.add(claim["payout_reference"])
        references.update(CANONICAL_REFERENCE.findall(credit.get("description", "")))
        booked = _time(credit["booked_at"])
        for record in active.values():
            if (record["kind"] == "balance_release" and record["amount_minor"] == credit["amount_minor"]
                    and record["currency"] == credit["currency"]
                    and booked - WINDOW <= _time(record["occurred_at"]) <= booked):
                references.update(REFERENCE.findall(record.get("message", "")))

    if request.get("txn_ids"):
        visible_documents = [*active.values(), *(entry["record"] for entry in snapshot["imports"])]
        for row in visible_documents:
            if row.get("subject_ref") == subject:
                for key in ("message", "text"):
                    if isinstance(value := row.get(key), str):
                        references.update(CANONICAL_REFERENCE.findall(value))

    calls = 0
    for reference in sorted(references):
        merchant.extend(lookup(payout_reference=reference))
        calls += 1
    active = _active(merchant, subject, "merchant")
    imports = [entry["record"] for entry in snapshot["imports"] if _candidate_import(entry["record"])]
    self_supplied = _active(imports, subject, "import")
    candidate_pool = {**self_supplied, **active}
    missing = {
        record_id
        for txn_id in request.get("txn_ids") or ()
        if txn_id in bank
        for payout in match_credit(bank[txn_id], candidate_pool)["payouts"]
        for record_id in payout["covers_record_ids"]
        if record_id not in candidate_pool
    }
    for record_id in sorted(missing):
        merchant.extend(lookup(record_id=record_id))
        calls += 1
    return bank, {**self_supplied, **_active(merchant, subject, "merchant")}, {
        "lookups": calls,
        "observed_inputs": len(merchant) + len(snapshot["bank_deliveries"]) + len(snapshot["imports"]),
    }


def match_credit(credit: dict, merchant: dict) -> dict:
    """Find one compatible payout or entire settlement batch."""
    groups = defaultdict(list)
    for row in merchant.values():
        if row["kind"] == "payout" and row["destination"] == "bank":
            batch = row.get("settlement_batch_id")
            key = ("batch", row.get("source_kind", "merchant"), batch) if batch else ("single", "", row["record_id"])
            groups[key].append(row)

    booked = _time(credit["booked_at"])
    candidates = []
    for (kind, _, identity), group in groups.items():
        reference = identity if kind == "batch" else group[0]["payout_reference"]
        credit_reference = credit.get("payment_reference")
        if credit_reference not in (None, reference) and not any(
            row.get("withdrawal_id") == credit_reference for row in group
        ):
            continue
        if sum(row["amount_minor"] for row in group) != credit["amount_minor"]:
            continue
        if all(row["status"] == "settled" and row.get("settled_at")
               and row["currency"] == credit["currency"]
               and booked - WINDOW <= _time(row["settled_at"]) <= booked for row in group):
            candidates.append(sorted(group, key=lambda row: row["record_id"]))

    payouts = candidates[0] if len(candidates) == 1 else []
    return {
        "txn_id": credit["txn_id"],
        "state": {0: "unmatched", 1: "linked"}.get(len(candidates), "ambiguous"),
        "payout_record_ids": [row["record_id"] for row in payouts],
        "payouts": payouts,
        "evidence_refs": [_source("bank", "txn_id", credit),
                          *(_source(row.get("source_kind", "merchant"), "record_id", row)
                            for row in payouts)] if payouts else [],
    }


def reconcile_credit(credit: dict, link: dict, merchant: dict, holder_claim: dict) -> dict:
    """Recompute a linked credit from cited components and classify the holder's claim."""
    payout_reference = holder_claim.get("payout_reference")
    same_claim = holder_claim["txn_id"] == credit["txn_id"]
    booked = _time(credit["booked_at"])
    covered = list(dict.fromkeys(
        record_id for row in link["payouts"] for record_id in row["covers_record_ids"]
    ))
    if covered and all(record_id in merchant and merchant[record_id]["kind"] in BUCKETS
                       and merchant[record_id]["currency"] == credit["currency"] for record_id in covered):
        breakdown = dict.fromkeys(BUCKETS.values(), 0)
        for record_id in covered:
            row = merchant[record_id]
            breakdown[BUCKETS[row["kind"]]] += row["amount_minor"]
        breakdown["net"] = breakdown["sales"] - breakdown["refunds"] - breakdown["fees"]
        if same_claim:
            status = "supported" if holder_claim["stated_minor"] == breakdown else "contradicted"
        else:
            status = "unresolved"
        gap = None
        if breakdown["net"] != credit["amount_minor"]:
            status, gap = "contradicted", "amount_mismatch"
        return {
            "status": status, "gap": gap, "breakdown_minor": breakdown,
            "evidence_refs": link["evidence_refs"] + [
                _source(merchant[record_id].get("source_kind", "merchant"), "record_id", merchant[record_id])
                for record_id in covered
            ],
        }

    if same_claim and payout_reference:
        for row in merchant.values():
            if row["kind"] != "payout" or row.get("payout_reference") != payout_reference:
                continue
            status = row["status"]
            gap = {"pending": "settlement_pending_at_booking", "failed": "settlement_failed"}.get(status)
            if row.get("settled_at") and _time(row["settled_at"]) > booked:
                gap = "settlement_after_credit"
            if gap:
                return {"status": "contradicted", "gap": gap, "breakdown_minor": None,
                        "evidence_refs": [_source("bank", "txn_id", credit),
                                          _source(row.get("source_kind", "merchant"), "record_id", row)]}

    gap = {"linked": "missing_cited_record", "ambiguous": "ambiguous_candidates"}.get(
        link["state"], "settlement_not_found",
    )
    return {"status": "unresolved", "gap": gap, "breakdown_minor": None, "evidence_refs": []}


def period_totals(period: dict, merchant: dict) -> dict:
    """Sum active sales and refunds in the half-open requested period."""
    start, end = _time(period["from"]), _time(period["to"])
    facts = dict.fromkeys(("sales_minor", "refunds_minor", "sale_count", "refund_count"), 0)
    refs = []
    for row in merchant.values():
        kind = row["kind"]
        if kind in ("sale", "refund") and start <= _time(row["occurred_at"]) < end:
            facts[f"{kind}s_minor"] += row["amount_minor"]
            facts[f"{kind}_count"] += 1
            refs.append(_source(row.get("source_kind", "merchant"), "record_id", row))
    return {"status": "supported" if refs else "unresolved",
            "gap": None if refs else "no_merchant_records", "facts": facts if refs else {},
            "evidence_refs": refs}
