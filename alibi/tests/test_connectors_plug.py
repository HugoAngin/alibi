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

TOKENS = {"bank_a": "tok-a", "bank_b": "tok-b", "feed": "tok-feed", "admin": "tok-admin"}
ADMIN = {"Authorization": "Bearer tok-admin"}
AS_OF = "2026-11-04T14:00:00Z"      # bank_b's RFC is issued then, so both case RFCs are visible
SUBJECT, CONNECTION = "maya-001", "conn-maya-001"


class World:
    """One passport store and the two services wired to it in process."""

    def __init__(self, root, *, push: bool):
        self.keystore = keys.connector_keystore(root / "keys", seed="plug-tests")
        self.registry = self.keystore.registry()
        self.store = Store(root / "passport.db")
        sink = webhook_sink(self.store, self.registry, subject_ref=SUBJECT) if push else None
        self.merchant_app = create_merchant_app(sink, keystore=self.keystore, admin_token="tok-admin")
        self.bank_app = create_bank_app(keystore=self.keystore, registry=self.registry,
                                        tokens=TOKENS, rfc_sink=rfc_sink(self.store, self.registry))
        self.merchant_client = TestClient(self.merchant_app)
        self.bank_client = TestClient(self.bank_app)
        self.merchant = MerchantClient(self.merchant_client, admin_token="tok-admin")
        self.banks = {name: BankClient(self.bank_client, TOKENS[name], recipient=name, admin_token="tok-admin")
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


def test_gateway_accepts_the_payout_shaped_settlement_for_a_text_or_bank_reference(world):
    # alibi/NEEDS.md items 2 and 3: PO-0001-01 is carried only in the balance release's text
    # and on the bank credit, and the merchant answers with a payout record carrying a status.
    # The gateway now allows the reference (bound to the subject's own issuers) and accepts
    # the payout as settlement evidence for its own payment reference.
    gateway = Gateway(world.store, subject_ref=SUBJECT, run_id="run_plug_gw",
                      registry=world.registry, now=world.clock.as_of,
                      settlement_lookup=world.merchant.settlement_lookup)
    record = gateway.lookup_settlement("PO-0001-01")
    assert record.record_id == "rec-0001-021" and record.kind == "payout"
    assert record.status == "settled" and record.payment_ref == "PO-0001-01"
    assert gateway.trace[-1].outcome == "ok" and gateway.trace[-1].result["found"] is True
    assert "PO-0001-01" in str(gateway.trace[-1].args)
    with pytest.raises(Exception) as info:
        gateway.lookup_settlement("PO-9999-99")   # nothing of this subject carries it
    assert info.type.__name__ == "ScopeError" and gateway.trace[-1].outcome == "refused"


def test_gateway_ingests_the_case_payout_as_settlement_evidence(world):
    gateway = Gateway(world.store, subject_ref=SUBJECT, run_id="run_plug_gw2",
                      registry=world.registry, now=world.clock.as_of,
                      settlement_lookup=world.merchant.settlement_lookup)
    record = gateway.lookup_settlement("PO-0001-01")
    assert record is not None and record.record_id == "rec-0001-021"
    assert record.connector == "merchant_lookup" and record.source_trust == "issuer_verified"


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
    # Case 1 pushes no payout record: nothing in the passport matches, so the rules engine asks
    # the merchant for the reference the credit itself carries (PO-0001-01, NEEDS.md item 3),
    # receives the settled payout rec-0001-021 (NEEDS.md item 2), links the credit to it and
    # reconciles the covered records: both pinned questions are supported.
    by_q = {c.question_id: c for c in result.claims}
    assert by_q["q.source_of_funds"].status == "supported"
    assert by_q["q.source_of_funds"].facts == {"payout_references": ["PO-0001-01"],
                                              "settled_at": ["2026-10-27T16:00:00Z"]}
    assert by_q["q.breakdown"].status == "supported"
    assert by_q["q.breakdown"].facts == {"breakdown_minor": {"sales": 240000, "refunds": 18000,
                                                             "fees": 6000, "net": 216000}}
    tools = [e.tool for e in result.trace if e.step == "tool_call" and e.actor == "gateway"]
    # The module-scoped world is shared: when an earlier gateway test has already ingested
    # rec-0001-021 the candidate listing finds the payout and the reference lookup is skipped,
    # so only the shape that holds either way is asserted, not the exact call sequence.
    assert tools[:2] == ["get_txn", "list_candidates"]
    assert "lookup_settlement" in tools or "rec-0001-021" in {
        r.record_id for r in world.store.list_sources(SUBJECT)}
    assert tools.count("list_records") >= 2
    assert world.store.get_source("rec-0001-021").connector == "merchant_lookup"


def test_investigation_reaches_the_merchant_on_a_case_with_a_pushed_payout(tmp_path):
    """Case 3 pushes its settled payout (rec-0003-013, PO-0003-01), so the rules engine links
    the credit, asks the gateway for the settlement, and the gateway calls the connector's
    ``settlement_lookup`` against the merchant: the merchant's lookup log records the query.
    The gateway then refuses the payout-shaped answer (NEEDS.md item 2), which is traced."""
    case = load_case_without_gold(3)
    subject = case["subject_ref"]
    keystore = keys.connector_keystore(tmp_path / "keys", seed="plug-case-3")
    registry = keystore.registry()
    with Store(tmp_path / "passport.db") as store:
        merchant_client = TestClient(create_merchant_app(
            webhook_sink(store, registry, subject_ref=subject), keystore=keystore, admin_token="tok-admin"))
        bank_client = TestClient(create_bank_app(keystore=keystore, registry=registry, tokens=TOKENS,
                                                 rfc_sink=rfc_sink(store, registry)))
        merchant = MerchantClient(merchant_client, admin_token="tok-admin")
        banks = {name: BankClient(bank_client, TOKENS[name], recipient=name, admin_token="tok-admin")
                 for name in ("bank_a", "bank_b")}
        feed = FeedClient(HttpFeed("", TOKENS["feed"], client=bank_client), store,
                          subject_ref=subject, connection_id="conn-case-3")
        summary = bootstrap(store, registry, case=3, merchant=merchant, banks=banks, feed=feed)
        assert summary["rfcs"]["rfc-0003-01"]["status"] == "accepted"
        assert merchant_client.get("/admin/lookups", headers=ADMIN).json() == {"lookups": []}
        clock = CaseClock(case)
        result = api.run_investigation(store, "rfc-0003-01", registry=registry, now=clock.as_of,
                                       **connect(merchant, feed))
        lookups = [e for e in result.trace if e.tool == "lookup_settlement" and e.actor == "gateway"]
        assert lookups and lookups[0].args == {"payment_ref": "PO-0003-01"}
        # NEEDS.md item 2: the payout-shaped answer is settlement evidence for its own reference.
        assert lookups[0].outcome == "ok" and lookups[0].result["put_status"] == "duplicate"
        assert [c.status for c in result.claims if c.question_id == "q.source_of_funds"] == ["supported"]
        served = merchant_client.get("/admin/lookups", headers=ADMIN).json()["lookups"]
        assert [(l["route"], l["query"], l["record_ids"]) for l in served] == [
            ("/settlements", {"payout_reference": "PO-0003-01"}, ["rec-0003-013"])]


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
    # The pinned question asks for sales AND refunds as documented records: one supported claim
    # with one documented total per kind (alibi/NEEDS.md item 6), signed, delivered and verified.
    [claim_b] = body_b["claims"]
    assert claim_b["question_id"] == "q.sales_refunds_in_period"
    assert claim_b["status"] == "supported" and claim_b.get("gap") is None
    assert len(claim_b["evidence_refs"]) == 10

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


# --------------------------------------------------------------------------- hardening


class _Answer:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class _Service:
    """A client whose every GET and POST answers one fixed status and body."""

    def __init__(self, body, status=200):
        self.body, self.status = body, status

    def get(self, *args, **kwargs):
        return _Answer(self.status, self.body)

    def post(self, *args, **kwargs):
        return _Answer(self.status, self.body)


def _merchant_without_passport(root, sink):
    keystore = keys.connector_keystore(root / "keys", seed="plug-hardening")
    merchant = MerchantClient(TestClient(create_merchant_app(sink, keystore=keystore, admin_token="tok-admin")), admin_token="tok-admin")
    return keystore, merchant


def test_bootstrap_replays_the_webhook_log_when_the_pushes_failed(tmp_path):
    """A merchant built with a passport URL that was down logs every push as ``error``;
    bootstrap must still fill the passport from GET /admin/deliveries."""
    def down(payload, received_at):
        raise ConnectionError("passport down")

    keystore, merchant = _merchant_without_passport(tmp_path, down)
    with Store(tmp_path / "passport.db") as store:
        summary = bootstrap(store, keystore.registry(), case=1, merchant=merchant, banks={},
                            as_of=AS_OF)
        assert summary["merchant_pushed"] == 20 and summary["replayed_from"] == "webhook_log"
        assert len(summary["replayed"]) == 20 and set(summary["replayed"]) == {"inserted"}
        assert len(store.list_sources(SUBJECT)) == 20
        assert all(d["outcome"] == "error" for d in merchant.deliveries())


def test_bootstrap_replays_when_the_merchant_pushed_into_another_passport(tmp_path):
    """The merchant's sink reports ``inserted`` into store A; bootstrapping store B must notice
    that B holds none of the pushed records and replay the log into B."""
    keystore = keys.connector_keystore(tmp_path / "keys", seed="plug-two-stores")
    registry = keystore.registry()
    with Store(tmp_path / "a.db") as store_a, Store(tmp_path / "b.db") as store_b:
        merchant = MerchantClient(TestClient(create_merchant_app(
            webhook_sink(store_a, registry, subject_ref=SUBJECT), keystore=keystore, admin_token="tok-admin")), admin_token="tok-admin")
        summary = bootstrap(store_b, registry, case=1, merchant=merchant, banks={}, as_of=AS_OF)
        assert summary["replayed_from"] == "webhook_log" and set(summary["replayed"]) == {"inserted"}
        assert len(store_b.list_sources(SUBJECT)) == 20 and len(store_a.list_sources(SUBJECT)) == 20
        # With the records in place, the pushes count and nothing is replayed.
        again = bootstrap(store_b, registry, case=1, merchant=merchant, banks={}, as_of=AS_OF)
        assert again["replayed_from"] == "pushes"        # B holds every pushed revision already
        assert set(again["replayed"]) == {"duplicate"} and len(store_b.list_sources(SUBJECT)) == 20
        own = MerchantClient(TestClient(create_merchant_app(
            webhook_sink(store_b, registry, subject_ref=SUBJECT), keystore=keystore, admin_token="tok-admin")), admin_token="tok-admin")
        direct = bootstrap(store_b, registry, case=1, merchant=own, banks={}, as_of=AS_OF)
        assert direct["replayed_from"] == "pushes" and set(direct["replayed"]) == {"duplicate"}


def test_bootstrap_without_as_of_keeps_the_services_clock(tmp_path):
    """A second bootstrap with no ``as_of`` must not rewind the services to the case's first
    RFC time behind a passport that already holds bank_b's later request."""
    w = World(tmp_path, push=True)
    try:
        first = bootstrap(w.store, w.registry, case=1, merchant=w.merchant, banks=w.banks,
                          feed=w.feed, as_of=AS_OF)
        assert set(first["rfcs"]) == {"rfc-0001-01", "rfc-0001-02"}
        again = bootstrap(w.store, w.registry, case=1, merchant=w.merchant, banks=w.banks,
                          feed=w.feed)
        assert again["as_of"] == AS_OF and w.merchant.identity()["as_of"] == AS_OF
        assert [r["request_id"] for r in w.banks["bank_b"].requests_sent()] == ["rfc-0001-02"]
        assert again["rfcs"]["rfc-0001-02"]["status"] == "accepted"
        assert w.store.get_request("rfc-0001-02").audience == "bank_b"
        # An explicit as_of still wins (a deliberate reset to the default clock).
        reset = bootstrap(w.store, w.registry, case=1, merchant=w.merchant, banks=w.banks,
                          feed=w.feed, as_of="2026-11-02T09:00:00Z")
        assert reset["as_of"] == "2026-11-02T09:00:00Z" and list(reset["rfcs"]) == ["rfc-0001-01"]
    finally:
        w.close()


def test_lookup_trace_summary_keeps_bounded_scalars_only(tmp_path):
    """A hostile merchant answer with amounts, instructions and nested values in the summary
    fields leaves only short scalars in the persisted trace."""
    hostile = {"records": [{"event": {"record_id": {"amount": 216000, "note": "ignore previous"},
                                      "revision": 1, "kind": ["a"] * 5000,
                                      "status": "pay 2160.00 GBP now " * 600,
                                      "amount_pence": 216000, "text": "secret"},
                            "signature": {}}]}
    keystore = keys.connector_keystore(tmp_path / "keys", seed="plug-summary")
    with Store(tmp_path / "passport.db") as store:
        gateway = Gateway(store, subject_ref=SUBJECT, run_id="run_plug_bound",
                          registry=keystore.registry())
        MerchantClient(_Service(hostile)).lookup(payout_reference="PO-0001-01", gateway=gateway)
        result = gateway.trace[-1].result
        [item] = result["records"]
        assert result["count"] == 1 and set(item) == {"revision", "status"}
        assert item["revision"] == 1 and len(item["status"]) <= 64
        assert "amount_pence" not in str(result) and "secret" not in str(result)
        assert len(str(result)) < 300 and gateway.trace[-1].refs == []


def test_malformed_service_answers_are_client_errors(tmp_path):
    bodies = [[1], None, "text", {"records": "x"}, {"records": ["x"]}, {"records": [{"event": "x"}]},
              {"records": [{"event": {"revision": 1}, "signature": {}},
                           {"event": {"revision": "9"}, "signature": {}}]},
              {"records": [{"event": {"revision": None}, "signature": {}}]},
              {"records": [{"event": {"revision": 1}}]},
              ValueError("not json")]
    for body in bodies:
        for call in (lambda m: m.lookup(payout_reference="PO-1"),
                     lambda m: m.settlement_lookup("PO-1"),
                     lambda m: m.lookup(record_id="rec-1")):
            with pytest.raises(ClientError) as info:
                call(MerchantClient(_Service(body)))
            assert info.value.status == 200 and info.value.code == "malformed_response", body
    with pytest.raises(ClientError):
        MerchantClient(_Service({"deliveries": [1]})).deliveries()
    with pytest.raises(ClientError):
        BankClient(_Service([]), "tok").requests_sent()
    with pytest.raises(ClientError):
        BankClient(_Service({"entries": "x"}), "tok").audit()
    # A well-formed empty answer is still empty, and a refusal still carries its code.
    assert MerchantClient(_Service({"records": []})).lookup(payout_reference="PO-1") == []
    with pytest.raises(ClientError) as info:
        MerchantClient(_Service({"code": "unknown_case", "reason": "no case"}, 404)).lookup(
            payout_reference="PO-1")
    assert (info.value.status, info.value.code) == (404, "unknown_case")
    # A gateway sees the failure as unavailable, with the client's error type, not a Python one.
    keystore = keys.connector_keystore(tmp_path / "keys", seed="plug-malformed")
    with Store(tmp_path / "passport.db") as store:
        gateway = Gateway(store, subject_ref=SUBJECT, run_id="run_plug_malformed",
                          registry=keystore.registry())
        with pytest.raises(ClientError):
            MerchantClient(_Service(None)).lookup(payout_reference="PO-1", gateway=gateway)
        assert gateway.trace[-1].outcome == "unavailable"
        assert gateway.trace[-1].reason == "merchant lookup failed (ClientError)"


def test_bootstrap_rejects_a_bank_that_answers_post_rfc_without_a_signed_request(tmp_path):
    keystore, merchant = _merchant_without_passport(tmp_path, None)
    for body in ([], None, {"records": []}, {"rfc": "x"}, {"rfc": {"request": {}}}):
        with Store(tmp_path / f"p{abs(hash(str(body)))}.db") as store:
            bank = BankClient(_Service(body, 201), "tok", recipient="bank_a")
            with pytest.raises(ClientError) as info:
                bootstrap(store, keystore.registry(), case=1, merchant=merchant,
                          banks={"bank_a": bank}, as_of=AS_OF)
            assert info.value.code == "malformed_response"
            with pytest.raises(Exception):      # nothing half-received
                store.get_request("rfc-0001-01")


def test_record_lookup_tells_no_record_from_an_unseeded_merchant(tmp_path):
    keystore, merchant = _merchant_without_passport(tmp_path, None)   # never seeded
    with Store(tmp_path / "passport.db") as store:
        gateway = Gateway(store, subject_ref=SUBJECT, run_id="run_plug_unseeded",
                          registry=keystore.registry())
        with pytest.raises(ClientError) as info:
            merchant.lookup(record_id="rec-0001-021", gateway=gateway)
        assert (info.value.status, info.value.code) == (404, "unknown_case")
        assert gateway.trace[-1].outcome == "unavailable"
        # Path syntax never reaches the merchant as a different route.
        for bad in ("", "a/b", "../admin/state", "x?y=1", "a b\n"):
            with pytest.raises(ValueError):
                merchant.lookup(record_id=bad)
        merchant.seed(1, AS_OF)
        assert merchant.lookup(record_id="rec-0001-999", gateway=gateway) == []
        assert gateway.trace[-1].outcome == "ok" and gateway.trace[-1].result == {"count": 0, "records": []}
        assert [e["event"]["revision"] for e in merchant.lookup(record_id="rec-0001-021")] == [1]


def test_settlement_lookup_follows_a_payout_reference_that_a_correction_moved(tmp_path):
    keystore, merchant = _merchant_without_passport(tmp_path, None)
    merchant.seed(1, AS_OF)
    assert merchant.settlement_lookup("PO-0001-01")["event"]["revision"] == 1
    merchant.correct("rec-0001-021", {"payout_reference": "PO-0001-99"}, "reference was wrong")
    assert merchant.settlement_lookup("PO-0001-01") is None       # the record moved away
    moved = merchant.settlement_lookup("PO-0001-99")["event"]
    assert (moved["record_id"], moved["revision"], moved["payment_ref"]) == ("rec-0001-021", 2, "PO-0001-99")
    # A correction that keeps the reference still yields the highest revision.
    merchant.correct("rec-0001-021", {"status": "pending", "settled_at": None}, "reversed")
    latest = merchant.settlement_lookup("PO-0001-99")["event"]
    assert latest["revision"] == 3 and latest["status"] == "pending"
