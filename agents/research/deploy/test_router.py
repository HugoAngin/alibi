"""Checks the public router with fake upstreams; no network."""

import httpx
from starlette.testclient import TestClient

from agents.research.deploy.router import Upstream, build_app, is_blocked, pick

UPS = [
    Upstream("merchant", "/merchant", "http://merchant.test"),
    Upstream("bank", "/bank", "http://bank.test"),
    Upstream("app", "", "http://app.test"),
]


def fake_backend(request: httpx.Request) -> httpx.Response:
    host = request.url.host
    if host == "bank.test" and request.url.path == "/rfc":
        return httpx.Response(
            409,
            json={"code": "approval_digest_mismatch", "reason": "dispatch refused: the body changed"},
        )
    return httpx.Response(200, json={"host": host, "path": request.url.path, "query": request.url.query.decode()})


def client() -> TestClient:
    transport = httpx.MockTransport(fake_backend)
    return TestClient(build_app(UPS, httpx.AsyncClient(transport=transport)))


def test_pick_routes_by_prefix_and_strips_it():
    assert pick("/bank/rfc", UPS) == (UPS[1], "/rfc")
    assert pick("/merchant", UPS) == (UPS[0], "/")
    assert pick("/requests/rfc-0001-01", UPS) == (UPS[2], "/requests/rfc-0001-01")
    assert pick("/bankrupt", UPS)[0].name == "app"


def test_forwards_to_the_right_service_with_query():
    r = client().get("/merchant/events?since=1")
    assert r.json() == {"host": "merchant.test", "path": "/events", "query": "since=1"}
    assert client().get("/passport").json()["host"] == "app.test"


def test_passes_a_refusal_through_unchanged():
    r = client().post("/bank/rfc", json={"audience": "bank_a"})
    assert r.status_code == 409
    assert r.json() == {"code": "approval_digest_mismatch", "reason": "dispatch refused: the body changed"}


def test_internal_routes_are_not_public():
    for path in ["/reset", "/merchant/admin/reset", "/bank/reset"]:
        assert is_blocked(path)
        assert client().post(path).status_code == 404
    assert not is_blocked("/requests/rfc-0001-01")


def test_unreachable_service_is_reported_not_invented():
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    c = TestClient(build_app(UPS, httpx.AsyncClient(transport=httpx.MockTransport(down))))
    r = c.get("/bank/inbox")
    assert r.status_code == 503
    assert r.json()["code"] == "service_unavailable"
