"""Shared HTTP conventions for the simulated services (mock merchant, mock bank).

Every refusal is a 4xx with the body ``{"code": <stable snake_case code>, "reason": <sentence>}``
(``shared/CONTRACTS.md``, Refusals row). Extra keys may follow when the contract names them
(``receipt``, ``rejection``, ``expected_digest``). Request validation failures are reported the
same way, as ``invalid_request``, so a front end never sees FastAPI's own error shape.

``Refusal`` is raised anywhere in a route; ``install_refusals(app)`` turns it, request
validation errors, plain ``ValueError``/``TypeError``/``MappingError`` (a request value of the
wrong shape that reached a route) and Starlette's own 404/405 (unknown path, wrong method) into
that body. Reasons name ids, fields and codes only, never payload
contents, paths or credentials.

The services also share their credentials here. ``load_tokens`` reads the one tokens file
(``data/alibi/bank_tokens.json``) that holds the two recipient tokens, the feed token and the
``admin`` token; ``admin_guard(app)`` is the dependency every ``/admin/*`` route of both services
takes: a bearer equal to ``app.state.admin_token`` (constant-time comparison) from a loopback
client, else ``401 unauthenticated`` or ``403 forbidden``. Token values are never logged.
"""

from __future__ import annotations

import ipaddress
import json
import os
import secrets
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from alibi.connectors.case_source import MappingError

__all__ = [
    "ADMIN", "CODES", "DEFAULT_TOKENS_PATH", "LOOPBACK_HOSTS", "TOKEN_ENV", "Refusal",
    "admin_guard", "bearer_token", "install_refusals", "is_loopback_client", "load_tokens",
    "new_token", "refusal_body", "resolve_admin_token",
]

# Stable refusal codes used by the S2 services (new codes are a ``need`` to lead; these are
# listed in alibi/NEEDS.md for pinning).
CODES = {
    "unauthenticated": 401,
    "forbidden": 403,
    "not_found": 404,
    "unknown_case": 404,
    "unknown_event": 404,
    "unknown_record": 404,
    "unknown_request": 409,
    "invalid_request": 400,
    "invalid_time": 400,
    "invalid_query": 400,
    "invalid_correction": 400,
    "invalid_disposition": 400,
    "invalid_package": 400,
    "method_not_allowed": 405,
    "clock_backwards": 409,
    "request_expired": 409,
    "audience_mismatch": 409,
    "package_id_reused": 409,
    "package_rejected": 422,
    "disposition_final": 409,
    "scope_wider_than_parent": 409,
    "sink_unavailable": 503,
}


class Refusal(Exception):
    """A refused request: ``code`` from ``CODES``, a safe ``reason`` sentence and optional
    extra body fields."""

    def __init__(self, code: str, reason: str, *, status: int | None = None, **extra: Any):
        if code not in CODES and status is None:
            raise ValueError(f"unknown refusal code {code!r}; add it to CODES")
        super().__init__(reason)
        self.code = code
        self.reason = reason
        self.status = status if status is not None else CODES[code]
        self.extra = extra

    def body(self) -> dict[str, Any]:
        return refusal_body(self.code, self.reason, **self.extra)


def refusal_body(code: str, reason: str, **extra: Any) -> dict[str, Any]:
    return {"code": code, "reason": " ".join(str(reason).split())[:300], **extra}


def _clip(text: Any, limit: int = 300) -> str:
    text = " ".join(str(text).split()) or "-"
    return text if len(text) <= limit else text[: limit - 3] + "..."


def install_refusals(app: FastAPI) -> FastAPI:
    """Register the exception handlers that give every refusal the contract body."""

    @app.exception_handler(Refusal)
    async def _refusal(request: Request, exc: Refusal) -> JSONResponse:  # noqa: ARG001
        return JSONResponse(status_code=exc.status, content=exc.body())

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:  # noqa: ARG001
        parts = []
        for err in exc.errors()[:3]:
            loc = ".".join(str(p) for p in err.get("loc", ()) if p != "body")
            parts.append(f"{loc or 'request'}: {err.get('msg', 'invalid')}")
        return JSONResponse(status_code=400,
                            content=refusal_body("invalid_request", _clip("; ".join(parts))))

    @app.exception_handler(MappingError)
    async def _mapping(request: Request, exc: MappingError) -> JSONResponse:  # noqa: ARG001
        return JSONResponse(status_code=400, content=refusal_body("invalid_request", _clip(exc)))

    @app.exception_handler(ValueError)
    @app.exception_handler(TypeError)
    async def _value(request: Request, exc: Exception) -> JSONResponse:  # noqa: ARG001
        # A plain ValueError or TypeError from a route is a request value of the wrong shape
        # (a scalar where a list was expected, say), never a server fault: the demo services
        # take loosely typed JSON bodies. Only the exception type is echoed: its text may
        # quote request values.
        return JSONResponse(status_code=400, content=refusal_body(
            "invalid_request", f"the request could not be processed ({type(exc).__name__})"))

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:  # noqa: ARG001
        # Unknown paths and wrong methods keep the contract shape; the path is not echoed.
        if exc.status_code == 404:
            body = refusal_body("not_found", "no such route")
        elif exc.status_code == 405:
            body = refusal_body("method_not_allowed", "the method is not allowed on this route")
        else:
            body = refusal_body("invalid_request", "the request could not be served")
        return JSONResponse(status_code=exc.status_code, content=body, headers=exc.headers)

    return app


# --------------------------------------------------------------------------- credentials

ADMIN = "admin"
TOKEN_ENV = {"bank_a": "ALIBI_BANK_A_TOKEN", "bank_b": "ALIBI_BANK_B_TOKEN",
             "feed": "ALIBI_FEED_TOKEN", ADMIN: "ALIBI_ADMIN_TOKEN"}
DEFAULT_TOKENS_PATH = Path("data/alibi/bank_tokens.json")
# Client hosts that count as local besides loopback IP addresses: FastAPI's TestClient.
LOOPBACK_HOSTS = frozenset({"testclient"})


def new_token() -> str:
    return secrets.token_urlsafe(32)


def _read_tokens_file(path: Path) -> dict[str, str]:
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict) or not all(isinstance(v, str) for v in data.values()):
        raise ValueError(f"the tokens file {path.as_posix()} must map identities to strings")
    return data


def _write_tokens_file(path: Path, tokens: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(tokens, handle, indent=1, sort_keys=True)
        handle.write("\n")
    os.chmod(path, 0o600)


def load_tokens(path: Path | str | None = None) -> dict[str, str]:
    """The bearer tokens for ``bank_a``, ``bank_b``, ``feed`` and ``admin``, shared by the mock
    bank, the mock merchant (``admin`` only) and the demo runner.

    Environment variables (``ALIBI_BANK_A_TOKEN``, ``ALIBI_BANK_B_TOKEN``, ``ALIBI_FEED_TOKEN``,
    ``ALIBI_ADMIN_TOKEN``) win. Identities without one come from the JSON file at ``path``
    (default ``data/alibi/bank_tokens.json``), which is created with ``secrets.token_urlsafe(32)``
    per missing identity and mode 0600. Values are never logged.
    """
    tokens: dict[str, str] = {}
    from_env = {name: os.environ.get(var) for name, var in TOKEN_ENV.items()}
    missing = [name for name, value in from_env.items() if not value]
    if missing:
        file_path = DEFAULT_TOKENS_PATH if path is None else Path(path)
        stored = _read_tokens_file(file_path) if file_path.exists() else {}
        created = False
        for name in TOKEN_ENV:
            if not stored.get(name):
                stored[name] = new_token()
                created = True
        if created:
            _write_tokens_file(file_path, stored)
        tokens.update({name: stored[name] for name in TOKEN_ENV})
    for name, value in from_env.items():
        if value:
            tokens[name] = value
    return tokens


def resolve_admin_token(admin_token: str | None, tokens: dict[str, str] | None) -> str:
    """The admin credential an app factory was given: the explicit ``admin_token``, else the
    ``admin`` entry of ``tokens``, else a fresh random one (the app keeps it as
    ``app.state.admin_token``, so an in-process caller can read it; nobody else can guess it)."""
    if admin_token:
        return admin_token
    if tokens and tokens.get(ADMIN):
        return tokens[ADMIN]
    return new_token()


def bearer_token(request: Request) -> str | None:
    """The token of an ``Authorization: Bearer <token>`` header, or None."""
    header = request.headers.get("authorization") or ""
    scheme, _, token = header.partition(" ")
    return token.strip() if scheme.lower() == "bearer" and token.strip() else None


def is_loopback_client(request: Request) -> bool:
    """True when the connection comes from a loopback address (127.0.0.0/8, ``::1``, their
    IPv4-mapped forms) or from FastAPI's TestClient. A missing client is not local."""
    client = request.client
    host = client.host if client is not None else None
    if not host:
        return False
    if host in LOOPBACK_HOSTS:
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_loopback


def admin_guard(app: FastAPI) -> Callable[[Request], None]:
    """The dependency every ``/admin/*`` route takes: the bearer must equal
    ``app.state.admin_token`` (``401 unauthenticated`` otherwise) and the client must be local
    (``403 forbidden`` otherwise), so a seed through a public proxy fails even with a leaked
    token. Use as ``Depends(admin_guard(app))``."""

    def require_admin(request: Request) -> None:
        expected = getattr(app.state, "admin_token", None) or ""
        token = bearer_token(request) or ""
        if not expected or not secrets.compare_digest(token.encode("utf-8"),
                                                       expected.encode("utf-8")):
            raise Refusal("unauthenticated", "a valid admin bearer token is required")
        if not is_loopback_client(request):
            raise Refusal("forbidden", "admin routes are served to loopback clients only")

    return require_admin
