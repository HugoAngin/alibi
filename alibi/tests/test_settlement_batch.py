"""Settlement batches: one bank credit carrying the settlement reference that an issuer gave
several of its own signed payouts (public bundled-payment cases 9-12 and focused negative
variants of case 9; rules mode, no network)."""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from types import SimpleNamespace

from agents.data.api import load_case
from alibi.adapter import run_case
from alibi.core.investigate_rules import _settlement_batch
from alibi.core.schemas import SourceTrust


def _rules(case, tmp_path, name="c"):
    return {p["request_id"]: p for p in run_case(case, "rules", root=tmp_path / name)}


def _case9() -> dict:
    case = copy.deepcopy(load_case(9))
    case.pop("gold", None)
    return case


def _payout_events(case):
    return [d["event"] for d in case["merchant_deliveries"] if d["event"]["kind"] == "payout"]


def test_public_bundled_cases_link_every_batch_member(tmp_path):
    for n in (9, 10, 11, 12):
        gold = load_case(n)["gold"]
        rid = next(iter(gold["answers"]))
        pred = _rules(n, tmp_path, f"c{n}")[rid]
        for qid, want in gold["answers"][rid].items():
            got = pred["claims"][qid]
            assert got["status"] == "supported", (n, qid, got.get("gap"))
            assert got["facts"] == want["facts"], (n, qid)
        assert sorted(pred["links"][0]["payout_record_ids"]) == \
            sorted(gold["links"][0]["payout_record_ids"]), n


def test_batch_that_does_not_sum_to_the_credit_is_not_supported(tmp_path):
    case = _case9()
    for d in case["bank_deliveries"]:
        if d["row"]["txn_id"] == "txn-0009-004":
            d["row"]["amount_minor"] += 1
    pred = _rules(case, tmp_path)["rfc-0009-01"]
    assert pred["claims"]["q.source_of_funds"]["status"] != "supported"
    assert pred["claims"]["q.breakdown"]["status"] != "supported"


def test_batch_member_without_settlement_is_not_supported(tmp_path):
    case = _case9()
    member = _payout_events(case)[1]
    member["status"] = "pending"
    member.pop("settled_at", None)
    pred = _rules(case, tmp_path)["rfc-0009-01"]
    assert pred["claims"]["q.source_of_funds"]["status"] != "supported"


def test_batch_member_after_booking_breaks_the_batch(tmp_path):
    case = _case9()
    member = _payout_events(case)[1]
    member["occurred_at"] = "2026-06-17T00:00:00Z"
    member["settled_at"] = "2026-06-17T01:00:00Z"
    pred = _rules(case, tmp_path)["rfc-0009-01"]
    assert pred["claims"]["q.source_of_funds"]["status"] != "supported"


def _rec(record_id, amount, ref, *, trust=SourceTrust.ISSUER_VERIFIED, batch="BATCH-1",
         issuer="mock-marketplace", at="2026-06-14T04:00:00Z", currency="GBP"):
    return SimpleNamespace(record_id=record_id, revision=1, amount_pence=amount, currency=currency,
                           payment_ref=ref, settlement_ref=batch, source_trust=trust,
                           issuer_id=issuer, occurred_at=datetime.fromisoformat(at.replace("Z", "+00:00")))


_TXN = SimpleNamespace(amount_pence=300, currency="GBP",
                       booked_at=datetime(2026, 6, 16, tzinfo=timezone.utc))
_TOKENS = frozenset({"BATCH-1"})


def test_batch_requires_issuer_verified_members_of_one_issuer():
    a, b = _rec("r1", 100, "PO-1"), _rec("r2", 200, "PO-2")
    assert [p.record_id for p in _settlement_batch(_TXN, [a, b], _TOKENS)] == ["r1", "r2"]
    own = _rec("r2", 200, "PO-2", trust=SourceTrust.SELF_SUPPLIED)
    assert _settlement_batch(_TXN, [a, own], _TOKENS) == []
    other = _rec("r2", 200, "PO-2", issuer="other-platform")
    assert _settlement_batch(_TXN, [a, other], _TOKENS) == []


def test_batch_rejects_wrong_sum_duplicate_refs_and_unnamed_batches():
    a = _rec("r1", 100, "PO-1")
    assert _settlement_batch(_TXN, [a, _rec("r2", 201, "PO-2")], _TOKENS) == []
    assert _settlement_batch(_TXN, [a, _rec("r2", 200, "PO-1")], _TOKENS) == []
    assert _settlement_batch(_TXN, [a, _rec("r2", 200, "PO-2")], frozenset({"BATCH-2"})) == []
    assert _settlement_batch(_TXN, [a, _rec("r2", 200, "PO-2", currency="EUR")], _TOKENS) == []
    assert _settlement_batch(_TXN, [a], _TOKENS) == []
