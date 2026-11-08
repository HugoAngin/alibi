"""One public address for the three backend services (web/docs/hosting.md).

On Modal the app (8100), mock merchant (8101) and mock bank (8102) run in one
container on loopback. This ASGI router is the only thing published. It sends
``/merchant/...`` to the merchant, ``/bank/...`` to the bank and everything
else to the app, with the prefix stripped, and passes the answer back
unchanged, so a backend refusal reaches the front end word for word.

It decides nothing: no auth, no rewriting of bodies. Routes the services keep
loopback-only (admin, seed, reset, debug) are refused here, so they stay internal.

Run locally: ``uv run --frozen python -m agents.eval.deploy.router``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from posixpath import normpath
from urllib.parse import unquote

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route


@dataclass(frozen=True)
class Upstream:
    name: str
    prefix: str  # public path prefix, "" for the default
    origin: str  # loopback origin, e.g. http://127.0.0.1:8101


def upstreams_from_env(env: dict[str, str] | None = None) -> list[Upstream]:
    """Upstreams in match order: longest prefix first, the app last."""
    env = dict(os.environ if env is None else env)
    return [
        Upstream("merchant", "/merchant", env.get("ALIBI_MERCHANT_ORIGIN", "http://127.0.0.1:8101")),
        Upstream("bank", "/bank", env.get("ALIBI_BANK_ORIGIN", "http://127.0.0.1:8102")),
        Upstream("app", "", env.get("ALIBI_APP_ORIGIN", "http://127.0.0.1:8100")),
    ]


# Reserved upstream roots. The admin segment is reserved at every depth.
BLOCKED_ROOTS = frozenset({"seed", "reset", "__debug"})

#: Headers that belong to one hop and must not be forwarded.
HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}


def pick(path: str, upstreams: list[Upstream]) -> tuple[Upstream, str]:
    """The upstream for a public path, and the path to send it."""
    for up in upstreams:
        if up.prefix and (path == up.prefix or path.startswith(up.prefix + "/")):
            return up, path[len(up.prefix):] or "/"
    return upstreams[-1], path


def is_blocked(path: str, *, roots: frozenset[str] = BLOCKED_ROOTS) -> bool:
    """Refuse internal paths even if a further decode exposes a reserved segment."""
    for _ in range(6):
        decoded = unquote(path)
        if decoded == path:
            parts = path.casefold().split("/")
            root = normpath(path).lstrip("/").partition("/")[0].casefold()
            return "admin" in parts or root in roots
        path = decoded
    return True


def build_app(upstreams: list[Upstream] | None = None, client: httpx.AsyncClient | None = None,
              *, hosted: bool = False) -> Starlette:
    ups = upstreams or upstreams_from_env()
    http = client or httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=5.0), trust_env=False)
    blocked_roots = BLOCKED_ROOTS | {"connect", "deliver", "feed"} if hosted else BLOCKED_ROOTS

    async def healthz(_: Request) -> Response:
        status = {}
        for up in ups:
            try:
                r = await http.get(up.origin + "/", timeout=2.0)
                status[up.name] = r.status_code < 500
            except httpx.HTTPError:
                status[up.name] = False
        ready = not hosted or all(status.values())
        return JSONResponse({"ok": ready, "services": status}, status_code=200 if ready else 503)

    async def forward(request: Request) -> Response:
        up, upstream_path = pick(request.url.path, ups)
        try:
            url = httpx.URL(up.origin + upstream_path + (f"?{request.url.query}" if request.url.query else ""))
        except httpx.InvalidURL:
            return JSONResponse({"code": "invalid_path", "reason": "The request path is invalid."}, 400)
        # httpx normalizes dot segments and url.path applies the decoding the
        # upstream router sees. Check that path before making an upstream call.
        query_login = hosted and any(key.casefold() == "token" for key in request.query_params)
        if is_blocked(url.path, roots=blocked_roots) or query_login:
            return JSONResponse({"code": "not_public", "reason": "This route is internal to the backend."}, 404)
        headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP}
        try:
            r = await http.request(request.method, url, headers=headers, content=await request.body())
        except httpx.HTTPError:
            return JSONResponse(
                {"code": "service_unavailable", "reason": f"The {up.name} service is not running."}, 503
            )
        out = {k: v for k, v in r.headers.items() if k.lower() not in HOP_BY_HOP | {"content-encoding"}}
        return Response(r.content, status_code=r.status_code, headers=out)

    methods = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]
    return Starlette(
        routes=[
            Route("/healthz", healthz, methods=["GET"]),
            Route("/{path:path}", forward, methods=methods),
        ]
    )


app = build_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", "8000")))
