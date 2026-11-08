"""The bank feed: paging and visibility on the server side, the two transports, and a client
sync whose cursor moves only with a committed batch."""

import json

import httpx
import pytest

from alibi.connectors.bank_feed import (
    FeedClient,
    FeedError,
    FeedServer,
    HttpFeed,
    LocalFeed,
    parse_cursor,
)
from alibi.connectors.case_source import FEED_CONNECTOR, FEED_VERSION, CaseClock, load_case_without_gold
from alibi.connectors.http import Refusal
from alibi.core.ingest import IngestError
from alibi.core.schemas import BankTxn
from alibi.core.store import Store
from alibi.gateway import Gateway

RFC_A_TIME = "2026-11-02T09:00:00Z"
EARLY = "2026-10-27T00:00:00Z"
CREDIT_ARRIVAL = "2026-10-28T07:30:00Z"
CONN = "conn-maya-bank"
ACCOUNT = "acct-maya-001"
TOKEN = "feed-secret-token-do-not-print"


@pytest.fixture
def case1():
    return load_case_without_gold(1)


@pytest.fixture
def server(case1):
    return FeedServer(CaseClock(case1), account_ref=ACCOUNT, connection_id=CONN)


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "db" / "passport.db") as s:
        yield s


def _client(store, server, **kwargs):
    return FeedClient(LocalFeed(server), store, subject_ref="maya-001", connection_id=CONN, **kwargs)


# --------------------------------------------------------------------------- server


def test_paging_serves_visible_rows_in_arrival_order(server):
    first = server.page(None, limit=2)
    assert [r["txn_id"] for r in first["transactions"]] == ["txn-0001-001", "txn-0001-002"]
    assert first["next_cursor"] == "c:2" and first["has_more"] is True
    assert first["connection_id"] == CONN and first["account_ref"] == ACCOUNT
    assert first["as_of"] == RFC_A_TIME and first["source_version"] == FEED_VERSION
    second = server.page(first["next_cursor"], limit=2)
    assert [r["txn_id"] for r in second["transactions"]] == ["txn-0001-003", "txn-0001-004"]
    assert second["next_cursor"] == "c:4" and second["has_more"] is False
    empty = server.page("c:4")
    assert empty["transactions"] == [] and empty["next_cursor"] == "c:4"
    assert empty["has_more"] is False
    whole = server.page(None)
    assert len(whole["transactions"]) == 4 and whole["has_more"] is False


def test_served_rows_are_copies(server):
    page = server.page(None)
    page["transactions"][0]["amount_minor"] = 1
    assert server.page(None)["transactions"][0]["amount_minor"] == -65000


@pytest.mark.parametrize("cursor", ["c:", "c:x", "4", "c:-1", "cursor:4", "c:1e3"])
def test_bad_cursor_is_invalid_query(server, cursor):
    with pytest.raises(Refusal) as info:
        server.page(cursor)
    assert info.value.code == "invalid_query" and info.value.status == 400
    with pytest.raises(Refusal):
        parse_cursor(cursor)


@pytest.mark.parametrize("limit", [0, 501, -1, "2", 2.0, True])
def test_bad_limit_is_invalid_query(server, limit):
    with pytest.raises(Refusal) as info:
        server.page(None, limit=limit)
    assert info.value.code == "invalid_query"


def test_rows_after_as_of_are_not_served(case1):
    early = FeedServer(CaseClock(case1, as_of=EARLY), account_ref=ACCOUNT, connection_id=CONN)
    page = early.page(None)
    assert len(page["transactions"]) == 3 and page["as_of"] == EARLY
    assert early.latest("txn-0001-004") is None
    early.clock.advance(CREDIT_ARRIVAL)
    assert len(early.page(None)["transactions"]) == 4
    assert early.latest("txn-0001-004")["amount_minor"] == 216000


def test_latest_returns_the_credit_row_or_none(server):
    row = server.latest("txn-0001-004")
    assert row["payment_reference"] == "PO-0001-01" and row["amount_minor"] == 216000
    assert row["booked_at"] == "2026-10-28T06:30:00Z"
    assert server.latest("nope") is None


def test_latest_prefers_the_highest_revision_then_the_latest_arrival(case1):
    case = json.loads(json.dumps(case1))
    base = case["bank_deliveries"][-1]
    case["bank_deliveries"].append({"received_at": "2026-10-28T08:00:00Z",
                                    "row": {**base["row"], "revision": 2, "description": "FIXED"}})
    case["bank_deliveries"].append({"received_at": "2026-10-28T09:00:00Z",
                                    "row": {**base["row"], "revision": 1, "description": "REDELIVERED"}})
    server = FeedServer(CaseClock(case), account_ref=ACCOUNT, connection_id=CONN)
    assert server.latest("txn-0001-004")["description"] == "FIXED"
    case29 = load_case_without_gold(29)
    server29 = FeedServer(CaseClock(case29), account_ref="acct-seller-0029", connection_id=CONN)
    assert server29.latest("txn-0029-004")["revision"] == 1


# --------------------------------------------------------------------------- client sync


def test_sync_ingests_case1_and_commits_the_cursor(store, server):
    client = _client(store, server)
    report = client.sync()
    assert report == {"pages": 1, "inserted": 4, "duplicate": 0, "corrected": 0, "stale": 0,
                      "cursor": "c:4"}
    assert store.get_cursor(FEED_CONNECTOR, CONN) == "c:4"
    txns = store.list_bank_txns("maya-001")
    assert len(txns) == 4
    for txn in txns:
        assert txn.provenance.connection_id == CONN
        assert txn.provenance.retrieved_at.isoformat() == "2026-11-02T09:00:00+00:00"
        assert txn.provenance.source_version == FEED_VERSION
        assert txn.provenance.cursor == "c:4" and txn.provenance.connector == "bank_feed"
        assert txn.subject_ref == "maya-001" and txn.source_trust == "bank_observed"
    credit = store.get_bank_txn("txn-0001-004")
    assert credit.refs == ["PO-0001-01"] and credit.amount_pence == 216000
    assert store.get_bank_txn("txn-0001-001").refs == []
    again = client.sync()
    assert again["inserted"] == 0 and again["duplicate"] == 0 and again["cursor"] == "c:4"
    assert store.get_cursor(FEED_CONNECTOR, CONN) == "c:4"
    assert len(store.list_bank_txns("maya-001")) == 4


def test_sync_in_small_pages_keeps_each_pages_cursor(store, server):
    client = _client(store, server, page_size=3)
    report = client.sync()
    assert report["pages"] == 2 and report["inserted"] == 4 and report["cursor"] == "c:4"
    assert store.get_bank_txn("txn-0001-001").provenance.cursor == "c:3"
    assert store.get_bank_txn("txn-0001-004").provenance.cursor == "c:4"


def test_sync_after_advance_picks_up_only_new_rows(store, case1):
    clock = CaseClock(case1, as_of=EARLY)
    server = FeedServer(clock, account_ref=ACCOUNT, connection_id=CONN)
    client = _client(store, server)
    assert client.sync()["inserted"] == 3
    assert store.get_cursor(FEED_CONNECTOR, CONN) == "c:3"
    clock.advance(CREDIT_ARRIVAL)
    report = client.sync()
    assert report["inserted"] == 1 and report["duplicate"] == 0 and report["cursor"] == "c:4"
    credit = store.get_bank_txn("txn-0001-004")
    assert credit.provenance.retrieved_at.isoformat() == "2026-10-28T07:30:00+00:00"
    assert credit.provenance.cursor == "c:4"
    assert len(store.txn_history("txn-0001-001")) == 1


class _Corrupting:
    """A transport that returns page 1 intact and corrupts the first row of page 2."""

    def __init__(self, inner, broken=True):
        self.inner, self.broken = inner, broken

    def page(self, cursor, limit):
        page = self.inner.page(cursor, limit)
        if self.broken and cursor == "c:2" and page["transactions"]:
            page["transactions"][0]["amount_minor"] = "two thousand"
            del page["transactions"][0]["currency"]
        return page

    def txn(self, txn_id):
        return self.inner.txn(txn_id)


def test_cursor_moves_only_with_a_committed_batch(store, server):
    transport = _Corrupting(LocalFeed(server))
    client = FeedClient(transport, store, subject_ref="maya-001", connection_id=CONN, page_size=2)
    with pytest.raises(IngestError) as info:
        client.sync()
    assert "two thousand" not in info.value.reason
    assert store.get_cursor(FEED_CONNECTOR, CONN) == "c:2"
    assert len(store.list_bank_txns("maya-001")) == 2
    transport.broken = False
    report = client.sync()
    assert report["inserted"] == 2 and report["cursor"] == "c:4"
    assert store.get_cursor(FEED_CONNECTOR, CONN) == "c:4"
    assert len(store.list_bank_txns("maya-001")) == 4


def test_a_batch_the_core_refuses_leaves_the_cursor(store, server):
    class Foreign(_Corrupting):
        def page(self, cursor, limit):
            page = self.inner.page(cursor, limit)
            if self.broken and cursor == "c:2":
                page["transactions"][0]["amount_minor"] = 0     # BankTxn refuses a zero amount
            return page

    transport = Foreign(LocalFeed(server))
    client = FeedClient(transport, store, subject_ref="maya-001", connection_id=CONN, page_size=2)
    with pytest.raises(IngestError):
        client.sync()
    assert store.get_cursor(FEED_CONNECTOR, CONN) == "c:2"


def test_redelivered_row_is_a_duplicate_with_one_active_row(store):
    case29 = load_case_without_gold(29)
    server = FeedServer(CaseClock(case29), account_ref="acct-seller-0029", connection_id=CONN)
    client = FeedClient(LocalFeed(server), store, subject_ref="seller-0029", connection_id=CONN)
    report = client.sync()
    assert report["inserted"] == 4 and report["duplicate"] == 1
    assert len(server.page(None)["transactions"]) == 5
    active = store.list_bank_txns("seller-0029")
    assert sorted(t.txn_id for t in active) == [f"txn-0029-00{n}" for n in range(1, 5)]
    assert len(store.txn_history("txn-0029-004")) == 1
    assert store.get_cursor(FEED_CONNECTOR, CONN) == "c:5"


# --------------------------------------------------------------------------- refresh


def test_refresh_returns_bank_txn_json_for_the_gateway(store, server):
    client = _client(store, server)
    client.sync()
    refreshed = client.refresh("txn-0001-004")
    txn = BankTxn.model_validate(refreshed)
    assert txn.txn_id == "txn-0001-004" and txn.provenance.connection_id == CONN
    assert txn.provenance.retrieved_at.isoformat() == "2026-11-02T09:00:00+00:00"
    assert txn.provenance.cursor is None and txn.refs == ["PO-0001-01"]
    assert client.refresh("nope") is None
    gateway = Gateway(store, subject_ref="maya-001", run_id="run_test",
                      txn_refresh=client.refresh, now=server.clock.as_of)
    active = gateway.refresh_txn("txn-0001-004")
    assert isinstance(active, BankTxn)
    assert active.txn_id == "txn-0001-004" and active.revision == 1
    assert active.amount_pence == 216000
    assert gateway.trace[-1].outcome == "ok"


# --------------------------------------------------------------------------- HTTP transport


def _mock_bank(server):
    """A fake feed route pair over ``httpx.MockTransport`` with bearer auth."""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.headers.get("authorization") != f"Bearer {TOKEN}":
            return httpx.Response(401, json={"code": "unauthenticated", "reason": "bad token"})
        path = request.url.path
        if path == "/feed/transactions":
            params = request.url.params
            try:
                page = server.page(params.get("cursor"), int(params.get("limit", "100")))
            except Refusal as exc:
                return httpx.Response(exc.status, json=exc.body())
            return httpx.Response(200, json=page)
        if path.startswith("/feed/transactions/"):
            row = server.latest(path.rsplit("/", 1)[1])
            if row is None:
                return httpx.Response(404, json={"code": "not_found", "reason": "no such transaction"})
            return httpx.Response(200, json={"as_of": server.clock.as_of_text,
                                             "source_version": FEED_VERSION, "transaction": row})
        return httpx.Response(404, json={"code": "not_found", "reason": "no route"})

    return httpx.Client(transport=httpx.MockTransport(handler), base_url="http://bank"), seen


def test_http_feed_sends_the_bearer_and_pages(store, server):
    http, seen = _mock_bank(server)
    feed = HttpFeed("http://bank", TOKEN, client=http)
    page = feed.page(None, 2)
    assert [r["txn_id"] for r in page["transactions"]] == ["txn-0001-001", "txn-0001-002"]
    assert seen[-1].headers["authorization"] == f"Bearer {TOKEN}"
    assert seen[-1].url.params["limit"] == "2" and "cursor" not in seen[-1].url.params
    feed.page("c:2", 2)
    assert seen[-1].url.params["cursor"] == "c:2"
    answer = feed.txn("txn-0001-004")
    assert answer["transaction"]["txn_id"] == "txn-0001-004" and answer["as_of"] == RFC_A_TIME
    assert feed.txn("nope") is None
    client = FeedClient(feed, store, subject_ref="maya-001", connection_id=CONN, page_size=3)
    report = client.sync()
    assert report["inserted"] == 4 and report["pages"] == 2 and report["cursor"] == "c:4"
    assert BankTxn.model_validate(client.refresh("txn-0001-004")).provenance.connection_id == CONN


def test_http_feed_errors_name_the_status_and_never_the_token(server):
    http, _ = _mock_bank(server)
    feed = HttpFeed("http://bank", "wrong-token-value", client=http)
    with pytest.raises(FeedError) as info:
        feed.page(None)
    assert "401" in str(info.value) and "wrong-token-value" not in str(info.value)
    with pytest.raises(FeedError) as info:
        feed.txn("txn-0001-004")
    assert "401" in str(info.value) and "wrong-token-value" not in str(info.value)
    good = HttpFeed("http://bank", TOKEN, client=http)
    with pytest.raises(FeedError) as info:
        good.page("c:x")
    assert "400" in str(info.value) and TOKEN not in str(info.value)


def test_http_feed_works_through_a_testclient_like_object(server):
    """A client whose ``.get`` takes a path (FastAPI's TestClient style) and an empty base url."""
    http, _ = _mock_bank(server)

    class Relative:
        def get(self, url, params=None, headers=None):
            return http.get(url, params=params, headers=headers)

    feed = HttpFeed("", TOKEN, client=Relative())
    assert len(feed.page(None)["transactions"]) == 4


# --------------------------------------------------------------------------- reviewed findings


def test_feed_server_takes_its_arguments_positionally_as_the_contract_shows(case1):
    """SERVICES.md: ``FeedServer(clock, account_ref, connection_id)``; keywords keep working."""
    clock = CaseClock(case1)
    positional = FeedServer(clock, ACCOUNT, CONN)
    keyword = FeedServer(clock, account_ref=ACCOUNT, connection_id=CONN)
    assert positional.page(None) == keyword.page(None)
    assert positional.account_ref == ACCOUNT and positional.connection_id == CONN


@pytest.mark.parametrize("cursor", ["c:\u00b2", "c:\u2460", "c:\u0663", "c:1\u0663", "c:\uff11"])
def test_non_ascii_digits_in_a_cursor_are_invalid_query_not_a_crash(server, cursor):
    """``str.isdigit`` accepts superscripts, circled and Arabic-Indic digits; the feed issues
    ASCII only, so these were never issued (and ``int()`` raises on some of them)."""
    with pytest.raises(Refusal) as info:
        parse_cursor(cursor)
    assert info.value.code == "invalid_query"
    with pytest.raises(Refusal) as info:
        server.page(cursor)
    assert info.value.code == "invalid_query" and info.value.status == 400


def test_a_cursor_past_the_rows_ever_served_was_not_issued_here(server):
    assert server.page("c:4")["transactions"] == []        # exactly at the end is fine
    with pytest.raises(Refusal) as info:
        server.page("c:5")
    assert info.value.code == "invalid_query"


class _Stuck:
    """A transport that always reports more rows without moving its cursor, and counts calls.
    ``limit_calls`` makes it fail loudly if a client keeps asking."""

    def __init__(self, inner, next_cursor="same", limit_calls=10):
        self.inner, self.mode, self.calls, self.limit_calls = inner, next_cursor, 0, limit_calls

    def page(self, cursor, limit):
        self.calls += 1
        assert self.calls <= self.limit_calls, "the client is looping on a stuck feed"
        page = self.inner.page(None, limit)
        page["has_more"] = True
        page["next_cursor"] = cursor if self.mode == "same" else None
        return page

    def txn(self, txn_id):
        return self.inner.txn(txn_id)


def test_sync_stops_with_feed_error_when_rows_come_back_but_the_cursor_does_not_advance(store, server):
    """Rows + has_more + a next_cursor equal to the request cursor: FeedError, not a loop, and
    the repeated page is not re-ingested."""
    store.put_bank_txns([], connector=FEED_CONNECTOR, cursor="c:2", connection_id=CONN)
    transport = _Stuck(LocalFeed(server), next_cursor="same")
    client = FeedClient(transport, store, subject_ref="maya-001", connection_id=CONN, page_size=2)
    with pytest.raises(FeedError) as info:
        client.sync()
    assert "did not advance" in str(info.value)
    assert transport.calls == 1
    assert store.get_cursor(FEED_CONNECTOR, CONN) == "c:2"
    assert store.list_bank_txns("maya-001") == [] and store.list_audit() == []


def test_sync_refuses_a_page_without_a_next_cursor_before_writing(store, server):
    """``next_cursor: null`` with has_more can neither commit the batch with its cursor nor
    terminate; it is FeedError before anything is written."""
    transport = _Stuck(LocalFeed(server), next_cursor=None)
    client = FeedClient(transport, store, subject_ref="maya-001", connection_id=CONN, page_size=2)
    with pytest.raises(FeedError) as info:
        client.sync()
    assert "next_cursor" in str(info.value)
    assert transport.calls == 1
    assert store.list_bank_txns("maya-001") == [] and store.get_cursor(FEED_CONNECTOR, CONN) is None
    assert store.list_audit() == []


def _with_rows(case, *rows):
    case = json.loads(json.dumps(case))
    for received_at, row in rows:
        case["bank_deliveries"].append({"received_at": received_at, "row": row})
    return case


def test_sync_reports_corrected_and_stale_rows(store, case1):
    base = case1["bank_deliveries"][-1]["row"]
    new = {**base, "txn_id": "txn-0001-005", "payment_reference": None, "description": "NEW"}
    case = _with_rows(case1,
                      ("2026-10-28T08:00:00Z", {**base, "revision": 2, "description": "FIXED"}),
                      ("2026-10-28T09:00:00Z", {**new, "revision": 2}),
                      ("2026-10-28T10:00:00Z", {**new, "revision": 1}))
    server = FeedServer(CaseClock(case), ACCOUNT, CONN)
    report = FeedClient(LocalFeed(server), store, subject_ref="maya-001", connection_id=CONN,
                        page_size=3).sync()
    assert report == {"pages": 3, "inserted": 5, "duplicate": 0, "corrected": 1, "stale": 1,
                      "cursor": "c:7"}
    assert len(store.txn_history("txn-0001-004")) == 2
    assert store.get_bank_txn("txn-0001-004").revision == 2
    assert store.get_bank_txn("txn-0001-004").description == "FIXED"
    assert store.get_bank_txn("txn-0001-005").revision == 2
    assert len(store.txn_history("txn-0001-005")) == 2
    assert store.get_cursor(FEED_CONNECTOR, CONN) == "c:7"


def test_same_id_and_revision_with_different_bytes_is_refused_and_leaves_the_cursor(store, case1):
    case = json.loads(json.dumps(case1))
    conflict = {**case["bank_deliveries"][1]["row"], "description": "SAME ID DIFFERENT BYTES"}
    case["bank_deliveries"].insert(2, {"received_at": "2026-10-27T12:00:00Z", "row": conflict})
    server = FeedServer(CaseClock(case), ACCOUNT, CONN)
    client = FeedClient(LocalFeed(server), store, subject_ref="maya-001", connection_id=CONN,
                        page_size=2)
    for _ in range(2):      # the refusal is stable: the cursor stays before the bad page
        with pytest.raises(IngestError) as info:
            client.sync()
        assert "txn-0001-002" in info.value.reason and "DIFFERENT BYTES" not in info.value.reason
        assert store.get_cursor(FEED_CONNECTOR, CONN) == "c:2"
        assert sorted(t.txn_id for t in store.list_bank_txns("maya-001")) == ["txn-0001-001",
                                                                             "txn-0001-002"]
    assert any(a.outcome == "refused" for a in store.list_audit())


class _Mangling:
    def __init__(self, inner, mangle):
        self.inner, self.mangle = inner, mangle

    def page(self, cursor, limit):
        return self.mangle(self.inner.page(cursor, limit))

    def txn(self, txn_id):
        return self.inner.txn(txn_id)


def _without(key):
    def mangle(page):
        del page[key]
        return page
    return mangle


@pytest.mark.parametrize("mangle", [
    _without("as_of"), _without("next_cursor"), lambda page: {**page, "as_of": "yesterday"},
    lambda page: {**page, "transactions": "rows"}, lambda page: ["not", "a", "page"],
    lambda page: None,
])
def test_a_malformed_page_is_a_feed_error_and_writes_nothing(store, server, mangle):
    client = FeedClient(_Mangling(LocalFeed(server), mangle), store, subject_ref="maya-001",
                        connection_id=CONN)
    with pytest.raises(FeedError):
        client.sync()
    assert store.list_bank_txns("maya-001") == [] and store.get_cursor(FEED_CONNECTOR, CONN) is None


def test_http_feed_non_json_answers_are_feed_errors():
    html = httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, text="<html>login</html>")), base_url="http://bank")
    feed = HttpFeed("http://bank", TOKEN, client=html)
    with pytest.raises(FeedError) as info:
        feed.page(None)
    assert "not JSON" in str(info.value) and TOKEN not in str(info.value)
    with pytest.raises(FeedError) as info:
        feed.txn("txn-0001-004")
    assert "not JSON" in str(info.value) and TOKEN not in str(info.value)


SENDER_TEXT = "ACCOUNT 12345678 SORT 00-11-22 PAYEE JOHN SMITH " * 8


def test_refused_mapping_reasons_name_ids_fields_and_positions_only_and_are_audited(store, case1):
    good = case1["bank_deliveries"][0]["row"]
    hostile = {**good, "txn_id": SENDER_TEXT, "amount_minor": "x"}
    case = _with_rows(case1, ("2026-11-01T00:00:00Z", hostile))
    server = FeedServer(CaseClock(case), ACCOUNT, CONN)
    client = FeedClient(LocalFeed(server), store, subject_ref="maya-001", connection_id=CONN)
    with pytest.raises(IngestError) as info:
        client.sync()
    reason = info.value.reason
    assert len(reason) <= 300 and "12345678" not in reason and "JOHN" not in reason
    assert "position 4" in reason and "identifier" in reason
    refused = [a for a in store.list_audit() if a.outcome == "refused"]
    assert len(refused) == 1 and refused[0].action == "ingest_bank_txns"
    assert refused[0].actor == "bank_feed" and refused[0].target_id == CONN
    assert "12345678" not in refused[0].detail
    assert store.get_cursor(FEED_CONNECTOR, CONN) is None
    # An identifier txn_id is named; the bad value still is not.
    case = _with_rows(case1, ("2026-11-01T00:00:00Z",
                              {**good, "txn_id": "txn-0001-099", "amount_minor": "two thousand"}))
    server = FeedServer(CaseClock(case), ACCOUNT, CONN)
    with pytest.raises(IngestError) as info:
        FeedClient(LocalFeed(server), store, subject_ref="maya-001", connection_id=CONN).sync()
    assert "txn txn-0001-099" in info.value.reason
    assert "two thousand" not in info.value.reason and "bank row" not in info.value.reason
