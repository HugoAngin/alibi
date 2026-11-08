"""Black-box probe transport tests using only synthetic HTTP exchanges."""

from urllib.parse import quote, quote_plus

import httpx
import pytest

from agents.eval import http_probe_runtime as runtime


@pytest.fixture
def attempt(monkeypatch):
    client = httpx.Client

    def run(handler, respond, *, base="https://probe.invalid", tokens=None, routes=None):
        def mock_client(**kwargs):
            return client(transport=httpx.MockTransport(respond), **kwargs)

        monkeypatch.setattr(runtime.httpx, "Client", mock_client)
        monkeypatch.setattr(runtime, "_handler_for", lambda _: handler)
        return runtime.attempt("SP-01", {"app": base, "routes": routes or {}}, tokens or {})

    return run


def unexpected_request(request):
    pytest.fail(f"Unexpected mock request: {request.url}")


def test_transport_failure_cannot_supply_evidence_for_a_definite_outcome(attempt):
    def offline(request):
        raise httpx.ConnectError("synthetic failure", request=request)

    def handler(ctx):
        try:
            ctx.http("GET", "app", "/probe")
        except runtime.ProbeUnavailable:
            return runtime.BLOCKED
        pytest.fail("Expected transport failure")

    result = attempt(handler, offline)
    assert result["outcome"] == runtime.NOT_RUN
    assert result["exchanges"][0]["error"] == "ConnectError"
    assert "status" not in result["exchanges"][0]


@pytest.mark.parametrize(
    ("body", "outcome"),
    [
        ({"detail": "Not Found"}, runtime.NOT_RUN),
        ({"detail": {"code": "UNAPPROVED"}}, runtime.BLOCKED),
    ],
)
def test_denial_requires_a_specific_error_code(attempt, body, outcome):
    def handler(ctx):
        return ctx.denial(ctx.http("GET", "app", "/probe"), {"UNAPPROVED"})

    result = attempt(handler, lambda _: httpx.Response(404, json=body))
    assert result["outcome"] == outcome
    assert result["exchanges"][0]["status"] == 404


@pytest.mark.parametrize(
    "base",
    [
        "https://probe.invalid:abc",
        "https://probe.invalid:65536",
        "https://[malformed",
        "https://@probe.invalid",
        "https://probe.invalid?x=1",
        "https://probe.invalid?",
        "https://probe.invalid#stub",
    ],
)
def test_bad_base_does_not_send_a_request(attempt, base):
    def handler(ctx):
        ctx.http("GET", "app", "/probe", "holder")
        return runtime.BLOCKED

    result = attempt(handler, unexpected_request, base=base, tokens={"holder": "synthetic"})
    assert result["outcome"] == runtime.NOT_RUN
    assert result["exchanges"] == []


@pytest.mark.parametrize("routes", [{}, {"decisive": "/probe/{missing}"}])
def test_unavailable_route_is_not_run(attempt, routes):
    def handler(ctx):
        ctx.http("GET", "app", ctx.route("decisive"))
        return runtime.BLOCKED

    result = attempt(handler, unexpected_request, routes=routes)
    assert result["outcome"] == runtime.NOT_RUN
    assert result["exchanges"] == []


def test_actor_authorization_is_unique_and_escaped_credentials_are_redacted(attempt):
    token = 'synthetic +"token'

    def handler(ctx):
        path = ctx.route("decisive", "/probe/{value}", value=token)
        response = ctx.http("POST", "app", path, "holder", json={"token": token},
                            headers={"authorization": "Bearer other"})
        return ctx.denial(response, {"UNAPPROVED"})

    def respond(request):
        assert request.headers.get_list("Authorization") == [f"Bearer {token}"]
        assert request.url.raw_path == f"/api/probe/{quote(token, safe='')}".encode()
        return httpx.Response(403, json={"code": "UNAPPROVED", "literal": token,
                                         "form": quote_plus(token, safe="").lower()})

    result = attempt(handler, respond, base="https://probe.invalid/api",
                     tokens={"short": "synthetic", "holder": token})
    assert result["outcome"] == runtime.BLOCKED
    [exchange] = result["exchanges"]
    assert exchange["url"] == "https://probe.invalid/api/probe/[REDACTED]"
    assert exchange["request_body"] == '{"token":"[REDACTED]"}'
    assert exchange["response_body"] == (
        '{"code":"UNAPPROVED","literal":"[REDACTED]","form":"[REDACTED]"}'
    )


def test_failure_reason_redacts_the_token(attempt):
    def handler(ctx):
        raise runtime.ProbeUnavailable("setup echoed synthetic-secret")

    result = attempt(handler, unexpected_request, tokens={"holder": "synthetic-secret"})
    assert result["reason"] == "setup echoed [REDACTED]"
