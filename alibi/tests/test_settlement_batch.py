"""Settlement batches: one bank credit carrying the settlement reference that an issuer gave
several of its own signed payouts (public bundled-payment cases 9-12 and focused negative
variants of case 9; rules mode, no network)."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from types import SimpleNamespace

from agents.data.api import load_case
from alibi.adapter import run_case
from alibi.core.investigate_rules import MAX_REFERENCE_LOOKUPS, _settlement_batch
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


# --------------------------------------------------------------------------- issuer-text references


def _first(case, tmp_path, name="c"):
    preds = _rules(case, tmp_path, name)
    return next(iter(preds.values()))


def test_public_hidden_reference_cases_retrieve_the_issuer_named_payout(tmp_path):
    for n in (41, 44, 48, 49, 50):
        gold = load_case(n)["gold"]
        rid = next(iter(gold["answers"]))
        pred = _rules(n, tmp_path, f"c{n}")[rid]
        assert pred["retrieval"]["lookups"] <= 2 * MAX_REFERENCE_LOOKUPS, n
        for qid, want in gold["answers"][rid].items():
            got = pred["claims"][qid]
            assert got["status"] == "supported", (n, qid, got.get("gap"))
            assert got["facts"] == want["facts"], (n, qid)
        assert pred["links"][0]["payout_record_ids"] == gold["links"][0]["payout_record_ids"], n


def test_issuer_named_payout_still_pending_is_not_linked(tmp_path):
    for n in (2, 20):
        gold = load_case(n)["gold"]
        rid = next(iter(gold["answers"]))
        pred = _rules(n, tmp_path, f"c{n}")[rid]
        assert pred["links"][0]["state"] == "unmatched", n
        for qid in ("q.source_of_funds", "q.breakdown"):
            assert pred["claims"][qid]["status"] == "unresolved"
            assert pred["claims"][qid]["gap"].startswith("settlement_pending_at_booking"), n


def test_self_supplied_text_never_opens_a_lookup(tmp_path):
    case = copy.deepcopy(load_case(41))
    case.pop("gold", None)
    notice = next(d for d in case["merchant_deliveries"] if d["event"]["kind"] == "notice")
    case["merchant_deliveries"].remove(notice)
    record = dict(notice["event"], import_source="holder pasted email")
    case["imports"].append({"received_at": notice["received_at"], "record": record})
    pred = _first(case, tmp_path)
    assert pred["claims"]["q.source_of_funds"]["status"] != "supported"
    assert "PO-0041-02" not in json.dumps(pred["links"])


def test_issuer_named_payout_must_still_match_the_amount(tmp_path):
    case = copy.deepcopy(load_case(48))
    case.pop("gold", None)
    for d in case["bank_deliveries"]:
        if d["row"]["txn_id"] == "txn-0048-005":
            d["row"]["amount_minor"] += 1
    pred = _first(case, tmp_path)
    assert pred["claims"]["q.source_of_funds"]["status"] != "supported"
    assert pred["links"][0]["state"] != "linked"


def test_alias_in_holder_csv_stays_unresolved(tmp_path):
    """Case 45 maps the bank's withdrawal id only in the holder's own import: no trust, no
    lookup of the payout it names, so the source stays unresolved (gold expects supported;
    this is a deliberate safe miss)."""
    pred = _first(45, tmp_path)
    assert pred["claims"]["q.source_of_funds"]["status"] == "unresolved"


# --------------------------------------------------------------------------- spaced references


def test_public_case73_spaced_transfer_reference_is_looked_up(tmp_path):
    gold = load_case(73)["gold"]
    rid = next(iter(gold["answers"]))
    pred = _rules(73, tmp_path)[rid]
    assert pred["retrieval"]["lookups"] <= MAX_REFERENCE_LOOKUPS
    for qid, want in gold["answers"][rid].items():
        assert pred["claims"][qid]["status"] == "supported", qid
        assert pred["claims"][qid]["facts"] == want["facts"], qid


def test_lookup_key_spaces_only_in_query_keys():
    import pytest
    from alibi.connectors.clients import MerchantClient

    class Recorder:
        def __init__(self):
            self.calls = []

        def get(self, path, params=None, headers=None):
            self.calls.append((path, params))
            raise RuntimeError("stop after the request is formed")

    http = Recorder()
    client = MerchantClient(http)
    with pytest.raises(RuntimeError):
        client.lookup(payout_reference="SPLAT W32 2695")
    assert http.calls == [("/settlements", {"payout_reference": "SPLAT W32 2695"})]
    for bad in ("SPLAT  W32", " SPLAT", "SPLAT ", "PO/0001", "a\nb", "x" * 65):
        with pytest.raises(ValueError):
            client.lookup(payout_reference=bad)
    with pytest.raises(ValueError):
        client.lookup(record_id="rec 0001")
    assert len(http.calls) == 1
