"""Shared HTTP conventions for the simulated services (mock merchant, mock bank).

Every refusal is a 4xx with the body ``{"code": <stable snake_case code>, "reason": <sentence>}``
(``shared/CONTRACTS.md``, Refusals row). Extra keys may follow when the contract names them
(``receipt``, ``rejection``, ``expected_digest``). Request validation failures are reported the
same way, as ``invalid_request``, so a front end never sees FastAPI's own error shape.

``Refusal`` is raised anywhere in a route; ``install_refusals(app)`` turns it, request
validation errors and plain ``ValueError``/``MappingError`` into that body. Reasons name ids,
fields and codes only, never payload contents or credentials.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from alibi.connectors.case_source import MappingError

__all__ = ["CODES", "Refusal", "install_refusals", "refusal_body"]

# Stable refusal codes used by the S2 services (new codes are a ``need`` to lead; these are
# listed in alibi/NEEDS.md for pinning).
CODES = {
    "unauthenticated": 401,
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

    return app
