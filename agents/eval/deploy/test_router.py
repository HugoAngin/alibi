"""Checks the public router with fake upstreams; no network."""

import asyncio
from urllib.parse import unquote

import httpx
import pytest
from starlette.testclient import TestClient

from agents.eval.deploy.router import Upstream, build_app, is_blocked, pick

UPS = [
    Upstream("merchant", "/merchant", "http://merchant.test"),
    Upstream("bank", "/bank", "http://bank.test"),
    Upstream("app", "", "http://app.test"),
]

NOT_PUBLIC = {"code": "not_public", "reason": "This route is internal to the backend."}
# Spaced and in a non-default key order, so any re-serialisation would show.
REFUSAL = b'{"reason": "dispatch refused: the body changed", "code": "approval_digest_mismatch"}'


def fake_backend(request: httpx.Request) -> httpx.Response:
    host = request.url.host
    if host == "bank.test" and request.url.path == "/rfc":
        return httpx.Response(409, content=REFUSAL, headers={"content-type": "application/json"})
    return httpx.Response(200, json={"host": host, "path": request.url.path, "query": request.url.query.decode()})


class Recorder:
    """Fake services that record every request the router sends them."""

    def __init__(self, *, hosted=False) -> None:
        self.calls: list[tuple[str, str, bytes]] = []
        self.app = build_app(UPS, httpx.AsyncClient(transport=httpx.MockTransport(self.handle)), hosted=hosted)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, request.url.host, request.url.raw_path))
        return fake_backend(request)


def client() -> TestClient:
    transport = httpx.MockTransport(fake_backend)
    return TestClient(build_app(UPS, httpx.AsyncClient(transport=transport)))


def serve(app, method: str, target: str) -> tuple[int, bytes]:
    """Send one request target to ``app`` with the scope uvicorn 0.54 builds for it.

    uvicorn decodes the path once. Starlette's TestClient and httpx's
    ASGITransport decode it twice, which hides double-encoded paths.
    """
    raw_path, _, query = target.encode("ascii").partition(b"?")
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "server": ("router.test", 80),
        "client": ("203.0.113.9", 50000),
        "scheme": "http",
        "method": method,
        "root_path": "",
        "path": unquote(raw_path.decode("ascii")),
        "raw_path": raw_path,
        "query_string": query,
        "headers": [(b"host", b"router.test")],
        "state": {},
    }
    sent: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    return status, b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")


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
    assert r.content == REFUSAL
    assert r.headers["content-type"] == "application/json"
    assert r.json() == {"code": "approval_digest_mismatch", "reason": "dispatch refused: the body changed"}


def test_internal_routes_are_not_public():
    for path in ["/reset", "/merchant/admin/reset", "/bank/reset"]:
        assert client().post(path).status_code == 404
    assert not is_blocked("/requests/rfc-0001-01")


def test_blocking_matches_internal_prefixes_and_preserves_public_ids():
    for path in ["/admin/seed", "/admin/seed/", "/admin/reset//", "/__debug", "/__debug/trace", "/reset"]:
        assert is_blocked(path), path
    for path in ["/records/administrator", "/reseed", "/resets", "/records/seed", "/requests/reset", "/packages/__debug", "/"]:
        assert not is_blocked(path), path


# Request targets as they arrive on the wire. Each reaches, or is redirected
# to, an internal route on the upstream once uvicorn and httpx have parsed it.
BLOCKED_TARGETS = [
    # direct, as the app would see them
    "/reset",
    "/reset/",
    "/admin/seed",
    "/admin/reset",
    "/__debug/trace",
    # behind the merchant and bank prefixes, with Starlette's slash variants
    "/merchant/admin/advance",
    "/merchant/admin/correct",
    "/merchant/admin/state",
    "/merchant/admin/seeds",
    "/requests/admin/dispatch",
    "/bank/admin/advance",
    "/bank/admin/reset/",
    "/merchant/admin/seed",
    "/merchant/admin/seed/",
    "/merchant/admin/seed?case=1&as_of=2026-03-01T00:00:00Z",
    "/merchant/admin/reset",
    "/merchant/admin/reset/",
    "/merchant/admin/reset//",
    "/merchant/reset",
    "/bank/reset",
    "/bank/__debug",
    "/bank/__debug/",
    "/bank/__debug/trace",
    "/bank/__debug/trace/",
    # spellings the router's own server decodes to the above
    "/merchant/admin%2Fseed",
    "/merchant%2Fadmin%2Freset",
    "/merchant/admin/%73eed",
    "/merchant/admin/reset%2F",
    # double-encoded: survives the router's decode, the upstream decodes it again
    "/merchant/admin/%2573eed",
    "/merchant/admin/reset%252F",
    "/bank/%255F%255Fdebug/trace",
    "/merchant/ADMIN/seed",
    "/merchant/%252561dmin/seed",
    "/merchant/x/%252e%252e/%2573eed",
    "/merchant/%25252525252525252561dmin/seed",
    # dot segments, which httpx removes before sending
    "/merchant/events/../admin/seed",
    "/merchant/events/%2e%2e/admin/seed",
    "/merchant/%2e%2e/admin/reset",
    # tab and CR, which urlsplit drops from request.url
    "/merchant/admin/se%09ed",
    "/merchant/admin/re%09set",
    "/bank/__deb%0Dug/trace",
]


@pytest.mark.parametrize("target", BLOCKED_TARGETS)
def test_internal_route_is_refused_without_an_upstream_call(target):
    ups = Recorder()
    status, body = serve(ups.app, "POST", target)
    assert (status, body, ups.calls) == (404, httpx.Response(404, json=NOT_PUBLIC).content, [])


@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
def test_internal_route_is_refused_for_every_method(method):
    ups = Recorder()
    status, _ = serve(ups.app, method, "/merchant/admin/seed")
    assert (status, ups.calls) == (404, [])


def test_newline_never_reaches_forward():
    # Starlette's {path:path} does not match "\n", so its own plain 404 answers.
    ups = Recorder()
    assert serve(ups.app, "POST", "/merchant/admin/se%0Aed") == (404, b"Not Found")
    assert ups.calls == []


def test_unsendable_path_fails_closed():
    ups = Recorder()
    status, body = serve(ups.app, "POST", "/merchant/admin/seed%00")
    assert status == 400
    assert b'"code":"invalid_path"' in body
    assert ups.calls == []


@pytest.mark.parametrize(
    ("method", "target", "sent"),
    [
        ("GET", "/passport", ("app.test", b"/passport")),
        ("GET", "/requests/rfc-0001-01", ("app.test", b"/requests/rfc-0001-01")),
        ("GET", "/requests/seed", ("app.test", b"/requests/seed")),
        ("GET", "/bank/packages/__debug", ("bank.test", b"/packages/__debug")),
        ("POST", "/requests/rfc-0001-01/approve", ("app.test", b"/requests/rfc-0001-01/approve")),
        ("GET", "/requests/rfc-0001-01?reset=1", ("app.test", b"/requests/rfc-0001-01?reset=1")),
        ("POST", "/bank/rfc", ("bank.test", b"/rfc")),
        ("GET", "/bank/inbox", ("bank.test", b"/inbox")),
        ("POST", "/bank/deliver", ("bank.test", b"/deliver")),
        ("GET", "/bank/packages/pkg-0001-01/attachments", ("bank.test", b"/packages/pkg-0001-01/attachments")),
        ("POST", "/bank/packages/pkg-0001-01/disposition", ("bank.test", b"/packages/pkg-0001-01/disposition")),
        ("GET", "/merchant/records/rec-0001-021", ("merchant.test", b"/records/rec-0001-021")),
        (
            "GET",
            "/merchant/settlements?payout_reference=PO-0001-01",
            ("merchant.test", b"/settlements?payout_reference=PO-0001-01"),
        ),
        # judged by the route it reaches: httpx sends /records/..., not /seed
        ("GET", "/merchant/seed/../records/rec-0001-021", ("merchant.test", b"/records/rec-0001-021")),
    ],
)
def test_public_route_is_forwarded_once_as_sent(method, target, sent):
    ups = Recorder()
    serve(ups.app, method, target)
    assert ups.calls == [(method, *sent)]


def test_health_reports_each_service_and_forwards_nothing_else():
    ups = Recorder()
    r = TestClient(ups.app).get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "services": {"merchant": True, "bank": True, "app": True}}
    assert ups.calls == [("GET", "merchant.test", b"/"), ("GET", "bank.test", b"/"), ("GET", "app.test", b"/")]


def test_unreachable_service_is_reported_not_invented():
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    c = TestClient(build_app(UPS, httpx.AsyncClient(transport=httpx.MockTransport(down))))
    r = c.get("/bank/inbox")
    assert r.status_code == 503
    assert r.json()["code"] == "service_unavailable"
    h = c.get("/healthz")
    assert h.status_code == 200
    assert h.json() == {"ok": True, "services": {"merchant": False, "bank": False, "app": False}}


def test_hosted_health_fails_closed():
    transport = httpx.MockTransport(lambda _: httpx.Response(503))
    with TestClient(build_app(UPS, httpx.AsyncClient(transport=transport), hosted=True)) as hosted:
        health = hosted.get("/healthz")
        assert health.status_code == 503 and health.json()["ok"] is False


@pytest.mark.parametrize("path", [
    "/connect/merchant/webhook", "/connect/bank/rfc", "/bank/%2564eliver", "/bank/feed/transactions",
    "/passport?%74oken=synthetic",
])
def test_hosted_router_never_forwards_internal_connectors_or_query_login(path):
    upstream = Recorder(hosted=True)
    assert serve(upstream.app, "POST", path)[0] == 404
    assert not upstream.calls
