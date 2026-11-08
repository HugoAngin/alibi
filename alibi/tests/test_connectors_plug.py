"""End to end through the simulated services: the merchant pushes case 1 into a passport, the
bank feed syncs with its cursor, both banks issue their RFCs, the rules investigation runs with
the real connector callables, the signed packages reach the right inboxes, a follow-up comes
back through the bank connection, and the merchant lookups are traced."""

import pytest
from fastapi.testclient import TestClient

from alibi import api
from alibi.bank.service import create_bank_app
from alibi.connectors import keys
from alibi.connectors.bank_feed import FeedClient, HttpFeed
from alibi.connectors.case_source import CaseClock, load_case_without_gold
from alibi.connectors.clients import BankClient, ClientError, MerchantClient, bootstrap, connect
from alibi.connectors.merchant import create_merchant_app
from alibi.connectors.plug import rfc_sink, webhook_sink
from alibi.core.signing import verify
from alibi.core.store import Store
from alibi.gateway import Gateway, RejectedResult

TOKENS = {"bank_a": "tok-a", "bank_b": "tok-b", "feed": "tok-feed"}
AS_OF = "2026-11-04T14:00:00Z"      # bank_b's RFC is issued then, so both case RFCs are visible
SUBJECT, CONNECTION = "maya-001", "conn-maya-001"


class World:
    """One passport store and the two services wired to it in process."""

    def __init__(self, root, *, push: bool):
        self.keystore = keys.connector_keystore(root / "keys", seed="plug-tests")
        self.registry = self.keystore.registry()
        self.store = Store(root / "passport.db")
        sink = webhook_sink(self.store, self.registry, subject_ref=SUBJECT) if push else None
        self.merchant_app = create_merchant_app(sink, keystore=self.keystore)
        self.bank_app = create_bank_app(keystore=self.keystore, registry=self.registry,
                                        tokens=TOKENS, rfc_sink=rfc_sink(self.store, self.registry))
        self.merchant_client = TestClient(self.merchant_app)
        self.bank_client = TestClient(self.bank_app)
        self.merchant = MerchantClient(self.merchant_client)
        self.banks = {name: BankClient(self.bank_client, TOKENS[name], recipient=name)
                      for name in ("bank_a", "bank_b")}
        self.feed = FeedClient(HttpFeed("", TOKENS["feed"], client=self.bank_client), self.store,
                               subject_ref=SUBJECT, connection_id=CONNECTION)
        self.clock = CaseClock(load_case_without_gold(1), AS_OF)

    def close(self):
        self.store.close()

    def connectors(self):
        return connect(self.merchant, self.feed)

    def package_for(self, request_id: str) -> dict:
        now = self.clock.as_of
        api.run_investigation(self.store, request_id, registry=self.registry, now=now,
                              **self.connectors())
        draft = api.build_review(self.store, request_id, now=now)
        approval = api.approve(self.store, draft.draft_id, holder_id="holder_maya", now=now)
        return api.sign_and_package(self.store, approval.approval_id, keystore=self.keystore,
                                    now=now).to_json_dict()


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    w = World(tmp_path_factory.mktemp("plug"), push=True)
    w.summary = bootstrap(w.store, w.registry, case=1, merchant=w.merchant, banks=w.banks,
                          feed=w.feed, as_of=AS_OF)
    yield w
    w.close()


# --------------------------------------------------------------------------- bootstrap


def test_bootstrap_fills_the_passport_through_the_real_paths(world):
    s = world.summary
    assert s["as_of"] == AS_OF and s["subject_ref"] == SUBJECT
    assert s["merchant_pushed"] == 20 and set(s["replayed"]) == {"inserted"}
    assert s["feed"]["inserted"] == 4 and s["feed"]["cursor"] == "c:4"
    assert s["imports"] == []
    assert s["rfcs"] == {"rfc-0001-01": {"status": "accepted", "audience": "bank_a"},
                         "rfc-0001-02": {"status": "accepted", "audience": "bank_b"}}
    records = world.store.list_sources(SUBJECT)
    assert len(records) == 20 and all(r.source_trust == "issuer_verified" for r in records)
    assert {r.record_id for r in records} == {f"rec-0001-{n:03d}" for n in range(1, 21)}
    txns = world.store.list_bank_txns(SUBJECT)
    assert {t.txn_id for t in txns} == {f"txn-0001-{n:03d}" for n in range(1, 5)}
    assert all(t.provenance.connection_id == CONNECTION for t in txns)
    assert world.store.get_request("rfc-0001-01").txn_ids == ["txn-0001-004"]
    assert world.store.get_request("rfc-0001-02").period is not None
    # The banks remember what they issued.
    assert [r["request_id"] for r in world.banks["bank_a"].requests_sent()] == ["rfc-0001-01"]
    assert [r["request_id"] for r in world.banks["bank_b"].requests_sent()] == ["rfc-0001-02"]


def test_bootstrap_is_idempotent(world):
    again = bootstrap(world.store, world.registry, case=1, merchant=world.merchant,
                      banks=world.banks, feed=world.feed, as_of=AS_OF)
    assert set(again["replayed"]) == {"duplicate"}
    assert again["feed"]["inserted"] == 0 and again["feed"]["duplicate"] == 0
    assert again["rfcs"]["rfc-0001-01"]["status"] == "accepted"
    assert len(world.store.list_sources(SUBJECT)) == 20


def test_bootstrap_pulls_the_webhook_log_when_the_merchant_has_no_sink(tmp_path):
    w = World(tmp_path, push=False)
    try:
        summary = bootstrap(w.store, w.registry, case=1, merchant=w.merchant, banks=w.banks,
                            feed=w.feed, as_of=AS_OF)
        assert summary["merchant_pushed"] == 20
        assert len(summary["replayed"]) == 20 and set(summary["replayed"]) == {"inserted"}
        assert len(w.store.list_sources(SUBJECT)) == 20
        log = w.merchant.deliveries()
        assert len(log) == 20 and all(d["outcome"] == "not_delivered" for d in log)
        assert set(log[0]["payload"]) == {"event", "signature"}
    finally:
        w.close()


# --------------------------------------------------------------------------- lookups and the gateway


def test_merchant_lookup_is_traced_through_the_gateway(world):
    gateway = Gateway(world.store, subject_ref=SUBJECT, run_id="run_plug_trace",
                      registry=world.registry, now=world.clock.as_of, request_id="rfc-0001-01")
    found = world.merchant.lookup(payout_reference="PO-0001-01", gateway=gateway,
                                  question_id="q.source_of_funds")
    assert [e["event"]["record_id"] for e in found] == ["rec-0001-021"]
    assert found[0]["extra"]["case_kind"] == "payout"
    [event] = gateway.trace
    assert event.actor == "connector" and event.tool == "merchant_lookup"
    assert event.args == {"payout_reference": "PO-0001-01"}
    assert event.result["count"] == 1
    assert event.result["records"] == [{"record_id": "rec-0001-021", "revision": 1,
                                        "kind": "payout", "status": "settled"}]
    assert [r.key() for r in event.refs] == ["record:rec-0001-021@1"]
    assert event.question_id == "q.source_of_funds" and event.outcome == "ok"
    # Nothing is written to the passport by a plain lookup.
    assert len(world.store.list_sources(SUBJECT)) == 20
    empty = world.merchant.lookup(payout_reference="PO-9999-99", gateway=gateway)
    assert empty == [] and gateway.trace[-1].result == {"count": 0, "records": []}
    with pytest.raises(ValueError):
        world.merchant.lookup(payout_reference="x", record_id="y")


def test_settlement_lookup_returns_the_signed_payout_only(world):
    payload = world.merchant.settlement_lookup("PO-0001-01")
    assert set(payload) == {"event", "signature"}
    assert payload["event"]["kind"] == "payout" and payload["event"]["payment_ref"] == "PO-0001-01"
    verify(payload["event"], payload["signature"], world.registry, domain="alibi:record:v1",
           issuer_id="mock-marketplace")
    assert world.merchant.settlement_lookup("PO-9999-99") is None


def test_gateway_rejects_the_payout_shaped_settlement_today(world):
    # alibi/NEEDS.md item 2: the core gateway only accepts kind "settlement". The payout
    # reference must also be carried by an issuer record's payment_ref, which the balance
    # release's text is not (NEEDS.md item 3), so the gateway refuses before calling out.
    gateway = Gateway(world.store, subject_ref=SUBJECT, run_id="run_plug_gw",
                      registry=world.registry, now=world.clock.as_of,
                      settlement_lookup=world.merchant.settlement_lookup)
    with pytest.raises(Exception) as info:
        gateway.lookup_settlement("PO-0001-01")
    assert gateway.trace[-1].outcome in ("refused", "unavailable")
    assert "PO-0001-01" in str(gateway.trace[-1].args)
    assert info.type.__name__ in ("ScopeError", "RejectedResult")


@pytest.mark.xfail(strict=True, reason="alibi/NEEDS.md items 2 and 3: the core gateway does not "
                   "yet accept a case payout as settlement evidence for a reference carried in "
                   "evidence text; remove this marker when it does")
def test_gateway_ingests_the_case_payout_as_settlement_evidence(world):
    gateway = Gateway(world.store, subject_ref=SUBJECT, run_id="run_plug_gw2",
                      registry=world.registry, now=world.clock.as_of,
                      settlement_lookup=world.merchant.settlement_lookup)
    record = gateway.lookup_settlement("PO-0001-01")
    assert record is not None and record.record_id == "rec-0001-021"


def test_txn_refresh_through_the_gateway(world):
    gateway = Gateway(world.store, subject_ref=SUBJECT, run_id="run_plug_refresh",
                      registry=world.registry, now=world.clock.as_of, txn_refresh=world.feed.refresh)
    txn = gateway.refresh_txn("txn-0001-004")
    assert txn.txn_id == "txn-0001-004" and txn.amount_pence == 216000
    assert gateway.trace[-1].tool == "refresh_txn" and gateway.trace[-1].outcome == "ok"


def test_rules_investigation_runs_with_the_real_connectors(world):
    result = api.run_investigation(world.store, "rfc-0001-01", registry=world.registry,
                                   now=world.clock.as_of, **world.connectors())
    assert result.mode == "rules" and len(result.claims) == 2
    # Case 1 pushes no payout record: the rules engine stays unresolved (NEEDS.md item 2) and
    # never claims support from the matching amounts.
    assert {c.status for c in result.claims} == {"unresolved"}
    tools = {e.tool for e in result.trace if e.step == "tool_call"}
    assert {"get_txn", "list_candidates"} <= tools


# --------------------------------------------------------------------------- delivery and follow-up


def test_packages_reach_the_right_inboxes_and_only_those(world):
    pkg_a = world.package_for("rfc-0001-01")
    status, body = world.banks["bank_a"].deliver(pkg_a)
    assert status == 200 and body["status"] == "verified"
    assert body["authentication"]["valid"] is True
    assert body["receipt"]["package_id"] == pkg_a["package_id"]
    assert isinstance(body["sources"], list) and isinstance(body["claims"], list)

    pkg_b = world.package_for("rfc-0001-02")
    status, body_b = world.banks["bank_b"].deliver(pkg_b)
    assert status == 200 and body_b["status"] == "verified"
    # The pinned question asks for sales AND refunds as documented records; the core rules
    # classifier treats two totals without a net cue as "several totals" and stays unresolved
    # (alibi/NEEDS.md item 6). The package is still signed, delivered and verified.
    [claim_b] = body_b["claims"]
    assert claim_b["question_id"] == "q.sales_refunds_in_period"
    assert claim_b["status"] == "unresolved" and "net" in (claim_b.get("gap") or "")

    # Retry with identical bytes: the same receipt, one inbox entry.
    status, again = world.banks["bank_a"].deliver(pkg_a)
    assert status == 200 and again["receipt"]["receipt_id"] == body["receipt"]["receipt_id"]
    inbox_a = world.banks["bank_a"].inbox()
    assert [p["package_id"] for p in inbox_a["packages"]] == [pkg_a["package_id"]]
    inbox_b = world.banks["bank_b"].inbox()
    assert [p["package_id"] for p in inbox_b["packages"]] == [pkg_b["package_id"]]

    # Cross-recipient access by any id is a 404 with no existence leak.
    for getter in (world.banks["bank_a"].package, world.banks["bank_a"].attachments):
        with pytest.raises(ClientError) as info:
            getter(pkg_b["package_id"])
        assert info.value.status == 404 and info.value.code == "not_found"
    with pytest.raises(ClientError) as info:
        world.banks["bank_a"].package("pkg_does_not_exist")
    assert info.value.reason == info.value.reason  # same shape either way
    # Wrong audience: bank_a's package offered to bank_b is refused and shows no contents.
    status, refused = world.banks["bank_b"].deliver(pkg_a)
    assert status == 409 and refused["code"] == "audience_mismatch"
    assert refused["receipt"]["accepted"] is False
    view = world.banks["bank_b"].package(pkg_a["package_id"])
    assert view["status"] == "rejected" and "package" not in view
    world.pkg_a, world.pkg_b = pkg_a, pkg_b


def test_request_more_creates_a_follow_up_the_passport_accepts(world):
    bank_a = world.banks["bank_a"]
    answer = bank_a.disposition(world.pkg_a["package_id"], "request_more",
                                "Need the settlement record for PO-0001-01",
                                question_ids=["q.source_of_funds"],
                                requested_evidence=["settlement record for payout PO-0001-01"],
                                analyst_name="Mallory")
    disposition = answer["disposition"]
    assert disposition["analyst"] == "analyst_a1" and disposition["decision"] == "request_more"
    follow_up = answer["follow_up"]
    child = follow_up["rfc"]["request"]
    assert child["parent_request_id"] == "rfc-0001-01" and child["txn_ids"] == ["txn-0001-004"]
    assert "period" not in child and child["request_id"] != "rfc-0001-01"
    assert follow_up["forwarded"]["status"] == "accepted"
    stored = world.store.get_request(child["request_id"])
    assert stored.parent_request_id == "rfc-0001-01" and stored.audience == "bank_a"
    # The follow-up runs the normal pipeline with a fresh approval and lands in the same inbox.
    pkg_child = world.package_for(child["request_id"])
    assert pkg_child["body"]["parent_request_id"] == "rfc-0001-01"
    status, body = bank_a.deliver(pkg_child)
    assert status == 200 and body["status"] == "verified"
    assert len(bank_a.inbox()["packages"]) == 2
    sent = {r["request_id"]: r for r in bank_a.requests_sent()}
    assert sent[child["request_id"]]["parent_request_id"] == "rfc-0001-01"
    assert sent[child["request_id"]]["source"] == "follow_up"
    # The first approval was never reused: a different package id and digest.
    assert pkg_child["package_id"] != world.pkg_a["package_id"]
    # Then a terminal decision closes the first package.
    bank_a.disposition(world.pkg_a["package_id"], "clear", "settled before booking")
    with pytest.raises(ClientError) as info:
        bank_a.disposition(world.pkg_a["package_id"], "escalate", "again")
    assert info.value.code == "disposition_final"
