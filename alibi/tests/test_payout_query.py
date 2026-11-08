"""Connector-backed payout query (``Gateway.lookup_payouts``): scope, signer and subject
checks, the rules engine's bounded clue fallback on public cases 42 and 43, and the agents'
covering/date actions routed through the gateway. Public development data only."""

import copy
import tempfile
from pathlib import Path

import pytest

from agents.data.api import load_case
from alibi.adapter import run_case
from alibi.agents.actions import ActionBudget, execute_action
from alibi.agents.material import Material
from alibi.agents.models import NextAction
from alibi.core.ingest import import_self_supplied, ingest_bank_txns, ingest_merchant_event
from alibi.core.schemas import KeyPurpose, RecordKind, SourceTrust
from alibi.core.signing import DemoKeystore
from alibi.core.store import NotFound, Store
from alibi.fixtures import maya
from alibi.gateway import (MAX_PAYOUT_RESULTS, Gateway, GatewayError, RejectedResult, ScopeError,
                           Unavailable)

SUBJ = maya.SUBJECT_REF


@pytest.fixture(scope="module")
def fx():
    return maya.load_variant("supported")


def _event(fx, record_id):
    return copy.deepcopy(next(e for e in fx["merchant_events"] if e["record_id"] == record_id))


@pytest.fixture
def passport(tmp_path, registry, keystore, fx, now):
    store = Store(tmp_path / "db" / "passport.db")
    for body in fx["merchant_events"]:
        ingest_merchant_event(store, registry, maya.sign_event(body, keystore), subject_ref=SUBJ,
                              now=now)
    ingest_bank_txns(store, fx["bank_txns"], subject_ref=SUBJ, connection_id=maya.CONNECTION_ID,
                     now=now)
    foreign = {**_event(fx, "ord_1001"), "record_id": "ord_9001", "event_id": "evt_ord_9001_r1",
               "subject_ref": "subj_other"}
    ingest_merchant_event(store, registry, maya.sign_event(foreign, keystore), now=now)
    yield store
    store.close()


def _payout(fx, **over):
    body = {**_event(fx, "po_5001"), "record_id": "po_5002", "event_id": "evt_po_5002_r1",
            "payment_ref": "PO-5002"}
    body.update(over)
    return body


def _gateway(passport, registry, now, query, **kw):
    return Gateway(passport, subject_ref=SUBJ, run_id="run_pq_test", now=now, registry=registry,
                   payout_query=query, **kw)


def _recording(answer):
    calls = []

    def query(key, value):
        calls.append((key, value))
        return answer() if callable(answer) else answer
    return query, calls


def test_covering_query_ingests_signed_payouts_of_this_subject(passport, registry, keystore, fx,
                                                                now):
    query, calls = _recording(lambda: [maya.sign_event(_payout(fx), keystore)])
    gw = _gateway(passport, registry, now, query)
    [record] = gw.lookup_payouts(covers_record_id="ord_1001")
    assert calls == [("covers_record_id", "ord_1001")]
    assert record.record_id == "po_5002" and record.kind == RecordKind.PAYOUT
    assert record.connector == "merchant_lookup"
    assert record.source_trust == SourceTrust.ISSUER_VERIFIED
    assert passport.get_source("po_5002") == record
    assert gw.trace[-1].result["count"] == 1 and gw.trace[-1].outcome == "ok"


def test_date_query_is_bounded_to_the_window_before_an_own_credit(passport, registry, keystore,
                                                                  fx, now):
    query, calls = _recording(lambda: [maya.sign_event(_payout(fx), keystore)])
    gw = _gateway(passport, registry, now, query)
    assert [r.record_id for r in gw.lookup_payouts(payout_date="2026-11-02")] == ["po_5002"]
    for day in ("2026-10-01", "2026-11-04"):   # far before the credit, after its booking day
        with pytest.raises(ScopeError, match="window"):
            gw.lookup_payouts(payout_date=day)
    with pytest.raises(GatewayError, match="YYYY-MM-DD"):
        gw.lookup_payouts(payout_date="2 November")
    with pytest.raises(GatewayError, match="real calendar day"):
        gw.lookup_payouts(payout_date="2026-02-30")
    with pytest.raises(GatewayError, match="exactly one"):
        gw.lookup_payouts(covers_record_id="ord_1001", payout_date="2026-11-02")
    assert calls == [("payout_date", "2026-11-02")]


def test_foreign_self_supplied_and_non_component_records_never_reach_the_connector(
        passport, registry, now):
    import_self_supplied(passport, {"record_id": "ord_mine", "kind": "order", "currency": "GBP",
                                    "occurred_at": "2026-10-30T08:30:00Z", "amount_pence": 100},
                         subject_ref=SUBJ, now=now)
    query, calls = _recording([])
    gw = _gateway(passport, registry, now, query)
    with pytest.raises(ScopeError, match="outside this run's subject"):
        gw.lookup_payouts(covers_record_id="ord_9001")   # another subject's order
    with pytest.raises(ScopeError, match="issuer-verified"):
        gw.lookup_payouts(covers_record_id="self_ord_mine")   # holder-supplied
    with pytest.raises(ScopeError, match="order, refund or fee"):
        gw.lookup_payouts(covers_record_id="msg_7001")
    assert calls == []
    assert [e.outcome for e in gw.trace] == ["refused"] * 3


def test_wrong_signer_issuer_subject_or_cover_rejects_the_whole_answer(
        passport, registry, keystore, fx, now, tmp_path):
    other = DemoKeystore(tmp_path / "other_keys", specs={
        "other_merchant": (KeyPurpose.RECORD_SIGNING, "other_market")}).ensure()
    merged = registry.merged(other.registry())
    good = maya.sign_event(_payout(fx), keystore)
    cases = {
        "another issuer": [good, maya.sign_event(_payout(fx, record_id="po_x", event_id="evt_po_x",
                                                          issuer_id="other_market"),
                                                 other, kid="other_merchant")],
        "another subject": [maya.sign_event(_payout(fx, subject_ref="subj_other"), keystore)],
        "does not cover": [maya.sign_event(_payout(fx, related_record_ids=["ord_1002"]),
                                           keystore)],
        "other than a payout": [maya.sign_event(_event(fx, "ord_1002"), keystore)],
        "signature does not verify": [{**good, "event": {**good["event"], "amount_pence": 1}}],
        f"more than {MAX_PAYOUT_RESULTS}": [good] * (MAX_PAYOUT_RESULTS + 1),
    }
    for message, answer in cases.items():
        gw = _gateway(passport, merged, now, lambda key, value, a=answer: a)
        with pytest.raises(RejectedResult, match=message):
            gw.lookup_payouts(covers_record_id="ord_1001")
        assert gw.trace[-1].outcome == "refused", message
    with pytest.raises(NotFound):
        passport.get_source("po_5002")   # nothing from a rejected answer was kept


def test_missing_connector_or_registry_is_unavailable(passport, registry, now):
    with pytest.raises(Unavailable, match="no payout query"):
        _gateway(passport, registry, now, None).lookup_payouts(covers_record_id="ord_1001")
    with pytest.raises(Unavailable, match="registry"):
        Gateway(passport, subject_ref=SUBJ, run_id="run_pq", now=now,
                payout_query=lambda k, v: []).lookup_payouts(covers_record_id="ord_1001")


def _rules(number):
    case = copy.deepcopy(load_case(number))
    case.pop("gold", None)   # the run never sees gold
    [pred] = run_case(case, "rules", root=Path(tempfile.mkdtemp()))
    return case, pred


@pytest.mark.parametrize("number, variant", [(42, "orders_and_weekday"), (43, "date_only")])
def test_public_clue_cases_retrieve_the_lookup_only_payout(number, variant):
    case, pred = _rules(number)
    assert case["variant"] == variant
    # The payout the clue names, from the case's own public data: the lookup-only payout that
    # settled before the credit and carries its amount (not gold, not a case-id table).
    [link] = pred["links"]
    credit = next(d["row"] for d in case["bank_deliveries"] if d["row"]["txn_id"] == link["txn_id"])
    [want] = [l["event"]["record_id"] for l in case["merchant_lookup"]
              if l["event"]["amount_minor"] == credit["amount_minor"]]
    assert link["state"] == "linked" and link["payout_record_ids"] == [want]
    assert {c["status"] for c in pred["claims"].values()} == {"supported"}
    assert 1 <= pred["retrieval"]["lookups"] <= 4


class _FakeGateway:
    def __init__(self, records):
        self.payout_query = object()
        self.records, self.calls = records, []

    def lookup_payouts(self, **key):
        self.calls.append(key)
        return self.records

    def list_records(self, **_):
        raise AssertionError("the passport listing must not be used with a payout query")


def test_agent_covering_and_date_actions_use_the_gateway_query(passport, fx, now):
    order, payout = passport.get_source("ord_1001"), passport.get_source("po_5001")
    material = Material(SUBJ)
    material.add_record(order)
    gw = _FakeGateway([payout])
    covering = execute_action(gw, material, NextAction(type="lookup_payouts_covering",
                                                       args={"record_id": "ord_1001"}),
                              ActionBudget())
    dated = execute_action(gw, material, NextAction(type="lookup_by_date",
                                                    args={"payout_date": "2026-11-02"}),
                           ActionBudget())
    assert covering.outcome == "ok" and dated.outcome == "ok"
    assert gw.calls == [{"covers_record_id": "ord_1001"}, {"payout_date": "2026-11-02"}]
    assert material.has("po_5001")
    assert material.lookup_count == 2
    assert all(lookup.connector_query for lookup in material.lookups)
    refused = execute_action(gw, material, NextAction(type="lookup_payouts_covering",
                                                      args={"record_id": "ord_9001"}),
                             ActionBudget())
    assert refused.outcome == "refused" and len(gw.calls) == 2
    assert material.lookup_count == 2
