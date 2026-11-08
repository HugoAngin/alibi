"""The demo runner builds and seeds both services from one keystore, and serving stops on the
timeout. Ports 0 are ephemeral, so the smoke test never collides with a running demo."""

import _thread
import io
import json
import socket
import threading
import time

import pytest

from alibi import demo_services
from alibi.connectors.keys import MERCHANT_KID


def _args(tmp_path, *extra):
    return demo_services.parse_args([
        "--keys", str(tmp_path / "keys"), "--state", str(tmp_path / "bank" / "state.json"),
        "--tokens", str(tmp_path / "tokens.json"), *extra])


def test_build_apps_seeds_both_services(tmp_path, monkeypatch):
    for name in ("ALIBI_BANK_A_TOKEN", "ALIBI_BANK_B_TOKEN", "ALIBI_FEED_TOKEN", "ALIBI_ADMIN_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    built = demo_services.build_apps(_args(tmp_path, "--case", "1", "--as-of", "2026-11-04T14:00:00Z"))
    merchant_seed, bank_seed = built["seeds"]["merchant"], built["seeds"]["bank"]
    assert merchant_seed["case_number"] == 1 and merchant_seed["as_of"] == "2026-11-04T14:00:00Z"
    assert len(merchant_seed["pushed"]) == 20
    assert {p["outcome"] for p in merchant_seed["pushed"]} == {"not_delivered"}
    assert bank_seed["case_number"] == 1 and bank_seed["recipients"] == ["bank_a", "bank_b"]
    assert MERCHANT_KID in built["keystore"].registry().kids()
    assert (tmp_path / "tokens.json").exists() and built["tokens_path"] == str(tmp_path / "tokens.json")
    tokens = json.loads((tmp_path / "tokens.json").read_text())
    assert set(tokens) == {"bank_a", "bank_b", "feed", "admin"}
    # Both services were given the file's admin token, and the seeds went through it.
    assert built["merchant"].state.admin_token == tokens["admin"]
    assert built["bank"].state.admin_token == tokens["admin"]
    assert tokens["admin"] not in json.dumps(built["seeds"])


def test_default_clock_is_the_first_rfc_time(tmp_path):
    built = demo_services.build_apps(_args(tmp_path))
    assert built["seeds"]["merchant"]["as_of"] == "2026-11-02T09:00:00Z"


def test_parse_args_defaults_match_services_md():
    args = demo_services.parse_args([])
    assert (args.case, args.as_of, args.timeout) == (1, None, None)
    assert (args.merchant_port, args.bank_port) == (8101, 8102)
    assert (demo_services.DEFAULT_MERCHANT_PORT, demo_services.DEFAULT_BANK_PORT) == (8101, 8102)
    assert args.host == "127.0.0.1" and args.passport_url is None and args.no_seed is False
    assert args.keys.endswith("keys") and args.tokens.endswith("bank_tokens.json")


@pytest.mark.parametrize("value", ["yesterday", "2026-11-04T14:00:00", "2026-11-04", ""])
def test_bad_as_of_is_refused_before_anything_starts(tmp_path, value):
    with pytest.raises(SystemExit) as info:
        demo_services.build_apps(_args(tmp_path, "--as-of", value))
    assert "invalid --as-of" in str(info.value) and "invalid_time" in str(info.value)
    assert value not in str(info.value).replace("--as-of", "") or value == ""
    assert not (tmp_path / "keys").exists() and not (tmp_path / "tokens.json").exists()


def test_bad_case_is_a_clean_exit(tmp_path):
    with pytest.raises(SystemExit) as info:
        demo_services.build_apps(_args(tmp_path, "--case", "999999"))
    assert "merchant seed failed" in str(info.value) and "unknown_case" in str(info.value)


def test_serve_runs_until_the_timeout_and_exits_cleanly(tmp_path):
    out = io.StringIO()
    code = demo_services.serve(_args(tmp_path, "--timeout", "1.5", "--merchant-port", "0",
                                     "--bank-port", "0"), out=out)
    lines = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
    assert code == 0
    assert lines[0]["case"] == 1 and lines[0]["merchant_pushed"] == 20
    assert lines[0]["tokens_file"] == str(tmp_path / "tokens.json")
    # The URL line names the ports the servers actually bound (not 0).
    for name in ("merchant", "bank"):
        assert lines[0][name].startswith("http://127.0.0.1:") and not lines[0][name].endswith(":0")
    tokens = json.loads((tmp_path / "tokens.json").read_text())
    assert not any(value in out.getvalue() for value in tokens.values())   # never printed
    assert lines[-1] == {"stopped": True, "failed": []}


def test_a_port_in_use_is_reported_at_once_not_after_the_timeout(tmp_path):
    """A stale demo on the merchant port must not leave the runner printing URLs as if up and
    then hanging for the whole timeout. An ephemeral listener stands in for the stale demo."""
    held = socket.socket()
    held.bind(("127.0.0.1", 0))
    held.listen(1)
    port = held.getsockname()[1]
    out = io.StringIO()
    started = time.monotonic()
    try:
        code = demo_services.serve(_args(tmp_path, "--timeout", "60", "--merchant-port", str(port),
                                         "--bank-port", "0"), out=out)
    finally:
        held.close()
    elapsed = time.monotonic() - started
    lines = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
    assert code == 1 and elapsed < 20, out.getvalue()
    assert lines[0] == {"error": "server did not start", "servers": ["merchant"]}
    assert lines[-1] == {"stopped": True, "failed": ["merchant"]}
    assert not any("http://" in str(line) for line in lines)   # no URL line claimed success
    assert not any(t.name in ("merchant", "bank") and t.is_alive() for t in threading.enumerate())


def test_ctrl_c_stops_the_runner_promptly_even_with_a_long_timeout(tmp_path):
    """SIGINT is delivered to the main thread only between waits; on Windows a single long
    ``Event.wait`` would hold it until the timeout expired. ``interrupt_main`` raises the same
    KeyboardInterrupt a console Ctrl-C does."""
    out = io.StringIO()

    def poke():
        deadline = time.monotonic() + 30
        while '"merchant"' not in out.getvalue() and time.monotonic() < deadline:
            time.sleep(0.05)
        _thread.interrupt_main()

    threading.Thread(target=poke, daemon=True).start()
    started = time.monotonic()
    code = demo_services.serve(_args(tmp_path, "--timeout", "120", "--merchant-port", "0",
                                     "--bank-port", "0"), out=out)
    elapsed = time.monotonic() - started
    lines = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
    assert code == 0 and elapsed < 15, out.getvalue()
    assert lines[-1] == {"stopped": True, "failed": []}
    assert not any(t.name in ("merchant", "bank") and t.is_alive() for t in threading.enumerate())


def test_live_http_round_trip_through_both_services(tmp_path):
    """Real sockets: the merchant and bank served by uvicorn on ephemeral ports, driven with
    httpx clients. Bounded waits only; both servers are stopped before the test returns."""
    import threading
    import time

    import httpx

    from alibi.connectors.bank_feed import FeedClient, HttpFeed
    from alibi.connectors.clients import BankClient, MerchantClient, bootstrap
    from alibi.core.store import Store

    args = _args(tmp_path, "--as-of", "2026-11-04T14:00:00Z")
    built = demo_services.build_apps(args)
    servers = [demo_services._Server(built["merchant"], "127.0.0.1", 0, "merchant"),
               demo_services._Server(built["bank"], "127.0.0.1", 0, "bank")]
    for server in servers:
        server.start()
    deadline = time.monotonic() + 15
    while not all(s.server.started for s in servers):
        assert time.monotonic() < deadline, "servers did not start within 15s"
        time.sleep(0.05)
    try:
        ports = [s.server.servers[0].sockets[0].getsockname()[1] for s in servers]
        tokens = json.loads((tmp_path / "tokens.json").read_text())
        registry = built["keystore"].registry()
        with httpx.Client(base_url=f"http://127.0.0.1:{ports[0]}", timeout=10) as mc, \
                httpx.Client(base_url=f"http://127.0.0.1:{ports[1]}", timeout=10) as bc, \
                Store(tmp_path / "passport.db") as store:
            merchant = MerchantClient(mc, admin_token=tokens["admin"])
            assert merchant.identity()["issuer_id"] == "mock-marketplace"
            banks = {name: BankClient(bc, tokens[name], recipient=name, admin_token=tokens["admin"])
                     for name in ("bank_a", "bank_b")}
            # Over real sockets the client is 127.0.0.1: admin calls need the token, then work.
            unauth_seed = mc.post("/admin/seed", params={"case": 1})
            assert unauth_seed.status_code == 401 and unauth_seed.json()["code"] == "unauthenticated"
            assert bc.post("/admin/advance", params={"as_of": "2026-11-04T14:00:00Z"}).status_code == 401
            feed = FeedClient(HttpFeed("", tokens["feed"], client=bc), store,
                              subject_ref="maya-001", connection_id="conn-maya-001")
            summary = bootstrap(store, registry, case=1, merchant=merchant, banks=banks, feed=feed,
                                as_of="2026-11-04T14:00:00Z")
            assert summary["merchant_pushed"] == 20 and set(summary["replayed"]) == {"inserted"}
            assert summary["feed"]["inserted"] == 4
            assert summary["rfcs"]["rfc-0001-01"]["status"] == "accepted"
            assert summary["rfcs"]["rfc-0001-02"]["status"] == "accepted"
            found = merchant.lookup(payout_reference="PO-0001-01")
            assert [e["event"]["record_id"] for e in found] == ["rec-0001-021"]
            assert banks["bank_a"].whoami() == {"recipient": "bank_a", "analyst": "analyst_a1"}
            unauth = bc.get("/inbox")
            assert unauth.status_code == 401 and unauth.json()["code"] == "unauthenticated"
    finally:
        for server in servers:
            server.stop()
        for server in servers:
            server.join(timeout=10)
    assert not any(s.is_alive() for s in servers)
