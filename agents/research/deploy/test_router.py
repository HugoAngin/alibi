"""Checks the public router with fake upstreams; no network."""

import httpx
from starlette.testclient import TestClient

from agents.research.deploy.router import Upstream, build_app, has_credential, is_blocked, pick

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


def recording_client():
    calls: list[str] = []

    def backend(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json={})

    return TestClient(build_app(UPS, httpx.AsyncClient(transport=httpx.MockTransport(backend)))), calls


INTERNAL = [
    "/reset",
    "/bank/reset",
    "/merchant/admin/reset",
    # eval's reported cases (fc86c59): merchant seed and admin state, bank admin keys, encoded admin.
    "/merchant/seed",
    "/merchant/admin/state",
    "/bank/admin/keys",
    "/bank/%61dmin/keys",
    # Double encoding, case, and dot segments.
    "/bank/%2561dmin/keys",
    "/bank/ADMIN/keys",
    "/bank/inbox/../admin/keys",
    "/bank/inbox/%2e%2e/admin/keys",
    "/admin/seed",
    "/seed",
    "/__debug",
]


def test_internal_routes_are_not_public_and_make_no_upstream_call():
    c, calls = recording_client()
    for path in INTERNAL:
        for method in ("GET", "POST"):
            assert c.request(method, path).status_code == 404, path
    assert calls == []


def test_public_routes_still_reach_their_service():
    c, calls = recording_client()
    for path in ["/bank/rfc", "/bank/inbox", "/bank/packages/pkg-1", "/merchant/events", "/requests/rfc-0001-01"]:
        assert c.get(path).status_code == 200, path
    assert len(calls) == 5
    assert not is_blocked("/requests/rfc-0001-01")


def test_unreachable_service_is_reported_not_invented():
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    c = TestClient(build_app(UPS, httpx.AsyncClient(transport=httpx.MockTransport(down))))
    r = c.get("/bank/inbox")
    assert r.status_code == 503
    assert r.json()["code"] == "service_unavailable"


def test_credentials_in_the_url_are_refused_before_any_upstream_call():
    c, calls = recording_client()
    for path in [
        "/passport?token=abc",
        "/bank/inbox?token=abc",
        "/bank/bank_a/inbox?TOKEN=abc",
        "/passport?access_token=abc",
        "/passport?x=1&api_key=abc",
        "/passport?%74oken=abc",
        "/passport?%2574oken=abc",
    ]:
        r = c.get(path)
        assert r.status_code == 400, path
        assert r.json()["code"] == "credential_in_url"
    assert calls == []


def test_ordinary_queries_still_pass():
    assert not has_credential("since=1&limit=20")
    assert not has_credential("")
    c, calls = recording_client()
    assert c.get("/merchant/events?since=1").status_code == 200
    assert len(calls) == 1
