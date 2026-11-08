"""The Alibi web app: holder passport, request review and dispatch, bank verification console.

Every holder and analyst route renders its template context as HTML, or as JSON when the request
is in JSON mode: ``application/json`` first in ``Accept``, or a ``Content-Type: application/json``
request body. Every action takes its fields as a form post or as a JSON object with the same names
and the same validation, and in JSON mode answers ``{"version", "ok": true, ...result}`` instead of
a redirect. Refusals answer with a 4xx status and a body ``{"version", "code", "reason"}``, the code
and sentence coming from the backend verbatim; the HTML path shows the same sentence.

The connect routes are how the simulated services fill the passport (``alibi/connectors/SERVICES.md``):
``POST /connect/merchant/webhook`` takes the merchant's signed pushes (loopback only; the issuer
signature is the authentication), ``POST /connect/bank/rfc`` takes the bank service's forwarded
requests with the recipient's bearer, and ``POST /connect/bank/sync`` pulls the holder's feed from
``ALIBI_BANK_URL``. Each successful call re-runs the monitor, so alerts appear without a restart.

Run: ``uv run --frozen python -m alibi.app [--host H] [--port P] [--timeout SECONDS]``.
"""

from __future__ import annotations

import html as htmlmod
import json
import secrets

import httpx
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from alibi.app import actions, bank, monitor, views
from alibi.app.actions import Refusal
from alibi.app.config import Settings, settings_from_env
from alibi.app.runtime import Runtime
from alibi.connectors.bank_feed import FeedClient, FeedError, HttpFeed
from alibi.connectors.http import is_loopback_client
from alibi.connectors.plug import rfc_sink, webhook_sink
from alibi.core.ingest import IngestError
from alibi.core.store import NotFound

APP_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = APP_DIR / "templates"
STATIC_DIR = APP_DIR / "static"
UI_VERSION = "alibi-ui/1"
FLASH_COOKIE = "alibi_flash"
BANK_COOKIE = "alibi_bank"
HOLDER_COOKIE = "alibi_holder"
# Paths that never ask for the holder credential: the health check, static files, the bank
# service (its own credentials), the root redirect and the two service-facing connect routes
# (the merchant's signature and the recipient's bearer are their credentials).
PUBLIC_PREFIXES = ("/health", "/static/", "/bank/", "/connect/merchant/webhook", "/connect/bank/rfc")
WEBHOOK_STATUS = {"inserted": 200, "duplicate": 200, "corrected": 200, "stale": 200,
                  "refused": 409, "error": 503}


def make_env() -> Environment:
    return Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)), undefined=StrictUndefined,
                       autoescape=select_autoescape(["html"]), trim_blocks=False, lstrip_blocks=False)


def content_type(request: Request) -> str:
    return request.headers.get("content-type", "").split(";", 1)[0].strip().lower()


def has_json_body(request: Request) -> bool:
    return request.method not in ("GET", "HEAD") and content_type(request) == "application/json"


def bearer_of(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    return header[7:].strip() if header.lower().startswith("bearer ") else None


def same_secret(presented: str, expected: str) -> bool:
    """Constant-time equality over UTF-8 bytes. ``secrets.compare_digest`` raises ``TypeError`` on
    non-ASCII *strings*, and a presented credential is attacker-supplied text, so a stray accent
    must be a plain 401, never a 500."""
    return secrets.compare_digest(presented.encode("utf-8"), expected.encode("utf-8"))


def wants_json(request: Request) -> bool:
    """JSON mode: the first ``Accept`` media type is ``application/json``, or the request carries
    a JSON body (an API client that sends JSON gets JSON back)."""
    accept = request.headers.get("accept", "")
    first = accept.split(",")[0].strip().lower() if accept else ""
    return first.startswith("application/json") or has_json_body(request)


def is_api_client(request: Request) -> bool:
    """A bearer token or a JSON body: a browser cannot attach either to a cross-site form post
    without a CORS preflight this app never grants, so such a request is an API client's."""
    return bearer_of(request) is not None or has_json_body(request)


def is_cross_site(request: Request) -> bool:
    site = request.headers.get("sec-fetch-site", "").lower()
    origin = request.headers.get("origin")
    host = request.headers.get("host", "").lower()
    origin_host = origin.split("//", 1)[-1].split("/", 1)[0].lower() if origin else None
    return site in ("cross-site", "same-site") or (origin is not None and origin_host != host)


def holder_path(path: str) -> bool:
    return path != "/" and not any(path == p.rstrip("/") or path.startswith(p) for p in PUBLIC_PREFIXES)


class Params:
    """The fields of an action, from a form post or a JSON object, read the same way.

    ``text(key)`` is the first value as a string (``""`` when absent or null); ``many(key)`` is
    every value as strings: a JSON list, or a repeated form field. Validation happens in the
    action functions, so both encodings are refused with the same codes and sentences.

    Field types are checked at the edge: a JSON object anywhere, a nested list inside a list field,
    or a list sent for a single-valued field (``text``) is 400 ``bad_request``, never silently
    coerced. Numbers and booleans read as their text, ``null`` as the empty string.
    """

    def __init__(self, values: dict[str, list[Any]], list_fields: frozenset[str] = frozenset()):
        self.values = values
        self.list_fields = list_fields   # JSON fields that arrived as a list

    @classmethod
    async def read(cls, request: Request) -> "Params":
        if has_json_body(request):
            raw = await request.body()
            if not raw.strip():
                return cls({})
            try:
                obj = json.loads(raw)
            except ValueError as exc:
                raise Refusal("bad_request", "the request body is not valid JSON", status=400) from exc
            if not isinstance(obj, dict):
                raise Refusal("bad_request", "the JSON body must be an object of field names to values",
                              status=400)
            fields: dict[str, list[Any]] = {}
            lists: set[str] = set()
            for key, value in obj.items():
                if isinstance(value, dict):
                    raise Refusal("bad_request", f"field {key!r} must be a string or a list of strings, "
                                                 "not an object", status=400)
                if isinstance(value, list):
                    if any(isinstance(v, (dict, list)) for v in value):
                        raise Refusal("bad_request", f"field {key!r} must be a list of strings; nested lists "
                                                     "and objects are not accepted", status=400)
                    lists.add(key)
                    fields[key] = value
                else:
                    fields[key] = [value]
            return cls(fields, frozenset(lists))
        values: dict[str, list[Any]] = {}
        for key, value in (await request.form()).multi_items():
            values.setdefault(key, []).append(value)
        return cls(values)

    @staticmethod
    def _text(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value)

    def text(self, key: str, default: str = "") -> str:
        if key in self.list_fields:
            raise Refusal("bad_request", f"field {key!r} takes one string, not a list", status=400)
        items = self.values.get(key) or []
        return self._text(items[0]) if items else default

    def many(self, key: str) -> list[str]:
        return [self._text(v) for v in (self.values.get(key) or []) if v is not None]


def create_app(settings: Settings | None = None, *, runtime: Runtime | None = None) -> FastAPI:
    settings = settings or settings_from_env()
    rt = runtime or Runtime.open(settings)
    env = make_env()
    app = FastAPI(title="Alibi", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.runtime = rt
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    def refusal_json(exc: Refusal) -> JSONResponse:
        return JSONResponse(jsonable_encoder({"version": UI_VERSION, **exc.body()}), status_code=exc.status)

    def refusal_page(exc: Refusal, area: str) -> HTMLResponse:
        """A plain escaped page for a browser refused before any template context exists."""
        body = (f"<!doctype html><html lang='en'><head><meta charset='utf-8'><title>Refused · Alibi</title>"
                f"<link rel='stylesheet' href='{htmlmod.escape(settings.static_prefix)}/alibi.css'></head>"
                f"<body class='area-{htmlmod.escape(area)}'><main class='page'><div class='result result--refused'>"
                f"<h2>Refused</h2><p>{htmlmod.escape(exc.code)}</p><pre class='result__reason'>"
                f"{htmlmod.escape(exc.reason)}</pre></div></main></body></html>")
        return HTMLResponse(body, status_code=exc.status)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> Response:
        """Unknown routes and wrong methods answer in the refusal shape too."""
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
        reason = str(exc.detail) if exc.detail else code.replace("_", " ")
        if wants_json(request):
            return JSONResponse({"version": UI_VERSION, "code": code, "reason": reason}, status_code=exc.status_code)
        return HTMLResponse(f"<!doctype html><title>{htmlmod.escape(code)} · Alibi</title><h1>{htmlmod.escape(code)}</h1>"
                            f"<p>{htmlmod.escape(reason)}</p>", status_code=exc.status_code)

    @app.exception_handler(Refusal)
    async def refusal_error(request: Request, exc: Refusal) -> Response:
        """A ``Refusal`` raised before the route body runs (the ``body_ok`` dependency)."""
        return refusal_json(exc) if wants_json(request) else refusal_page(exc, "holder")

    async def body_ok(request: Request) -> None:
        """Field-less actions depend on this: a JSON body is still parsed (and a malformed one
        refused 400 ``bad_request`` like everywhere else); its fields are then ignored."""
        if has_json_body(request):
            await Params.read(request)

    @app.exception_handler(RequestValidationError)
    async def bad_request(request: Request, exc: RequestValidationError) -> Response:
        reason = "; ".join(f"{'.'.join(str(p) for p in e.get('loc', []))}: {e.get('msg')}" for e in exc.errors())
        return JSONResponse({"version": UI_VERSION, "code": "bad_request", "reason": reason[:300]}, status_code=400)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        """Two checks before any route.

        Same-origin posts: a page on another site must not drive the holder's or the analyst's
        actions through their cookie session. A POST is refused ``cross_site_post`` when the
        browser marks it cross-site (``Sec-Fetch-Site: cross-site`` or ``same-site``) or sends an
        ``Origin`` whose host differs from ``Host``, unless it carries a bearer token or a JSON
        body: browsers attach neither to a cross-site form post without a CORS preflight this app
        never answers, so those requests come from API clients (the Astro server) and the
        credential, not the origin, authorises them. The bank delivery endpoint is exempt.

        Holder credential: with ``ALIBI_HOLDER_TOKEN`` set, every holder route needs
        ``Authorization: Bearer <token>`` or the ``alibi_holder`` cookie that
        ``GET /passport?token=<token>`` sets once (303 back to ``/passport`` so the credential
        leaves the address bar). Unset, the holder is the demo holder and nothing is asked.

        Role gate (eval SP-21): a recipient credential (a bank bearer or the ``alibi_bank``
        cookie) on any holder route is refused 403 ``recipient_credential`` and audited on that
        recipient's trail as ``access.refused``. The holder's credential is never a recipient
        credential, so on a bank route it is refused 401 ``not_authenticated`` by ``bank.authenticate``
        like any unknown token. Banks only ever receive approved packages.
        """
        path = request.url.path
        if request.method == "POST" and not path.endswith("/receive"):
            if is_cross_site(request) and not is_api_client(request):
                return JSONResponse({"version": UI_VERSION, "code": "cross_site_post",
                                     "reason": "state-changing posts are accepted from this site only"},
                                    status_code=403)
        if holder_path(path):
            bearer = bearer_of(request)
            who = settings.recipient_for_token(bearer) or settings.recipient_for_token(request.cookies.get(BANK_COOKIE))
            if who is not None:
                rt.bank_audit(who, action="access.refused", outcome="refused", target_id=None,
                              detail=f"recipient credential presented on holder route {path[:80]}")
                exc = Refusal("recipient_credential", "a recipient credential cannot act on holder routes",
                              status=403)
                return refusal_json(exc) if wants_json(request) else refusal_page(exc, "analyst")
        if settings.holder_token and holder_path(path):
            expected = settings.holder_token
            if path == "/passport" and request.method == "GET" and "token" in request.query_params:
                if same_secret(request.query_params["token"], expected):
                    response = RedirectResponse("/passport", status_code=303)
                    response.set_cookie(HOLDER_COOKIE, expected, httponly=True, samesite="lax")
                    return response
                exc = Refusal("not_authenticated", "the holder credential is not recognised", status=401)
                return refusal_json(exc) if wants_json(request) else refusal_page(exc, "holder")
            presented = bearer_of(request) or request.cookies.get(HOLDER_COOKIE) or ""
            if not same_secret(presented, expected):
                exc = Refusal("not_authenticated", "the holder credential is required: send it as a bearer "
                                                   "token, or open /passport?token=<credential> once to start "
                                                   "a session", status=401)
                return refusal_json(exc) if wants_json(request) else refusal_page(exc, "holder")
        return await call_next(request)
    if settings.bank_url:
        def _post(url: str, **kwargs):
            client = rt.http_client(settings.bank_url)
            if not isinstance(client, httpx.Client):
                kwargs.pop("timeout", None)        # an injected TestClient takes no per-call timeout
            return client.post(url, **kwargs)
        deliver = actions.deliver_over_http(settings.bank_url, tokens=settings.tokens, post=_post)
    else:
        deliver = actions.deliver_in_process(rt)
    merchant_sink = webhook_sink(rt.store, rt.registry, subject_ref=rt.subject_ref)
    bank_rfc_sink = rfc_sink(rt.store, rt.registry)

    # ------------------------------------------------------------------- helpers

    def flashes_from(request: Request) -> list[dict[str, str]]:
        raw = request.cookies.get(FLASH_COOKIE)
        if not raw:
            return []
        try:
            data = json.loads(raw)
            return data if isinstance(data, list) else []
        except ValueError:
            return []

    def page(request: Request, template: str, ctx: dict[str, Any], status: int = 200,
             extra: dict[str, Any] | None = None) -> Response:
        """The template context as HTML, or as JSON (plus ``version``, ``template`` and
        ``extra``, which an action uses for ``ok`` and its result)."""
        ctx = {**ctx, "version": UI_VERSION, "template": template}
        if wants_json(request):
            response = JSONResponse(jsonable_encoder({**ctx, **(extra or {})}), status_code=status)
        else:
            html = env.get_template(template).render(**ctx)
            response = HTMLResponse(html, status_code=status)
        if FLASH_COOKIE in request.cookies:
            response.delete_cookie(FLASH_COOKIE)
        return response

    def redirect(request: Request, url: str, *, flash: tuple[str, str] | None = None,
                 payload: dict | None = None, status: int = 303, json_status: int = 200) -> Response:
        if wants_json(request):
            return JSONResponse(jsonable_encoder({"version": UI_VERSION, "ok": True, "location": url,
                                                  **(payload or {})}), status_code=json_status)
        response = RedirectResponse(url, status_code=status)
        if flash:
            response.set_cookie(FLASH_COOKIE, json.dumps([{"kind": flash[0], "text": flash[1]}]),
                                max_age=120, httponly=True, samesite="lax")
        return response

    def refused(request: Request, exc: Refusal, back: str) -> Response:
        if wants_json(request):
            return refusal_json(exc)
        return redirect(request, back, flash=("error", f"{exc.code}: {exc.reason}"))

    def not_found(request: Request, what: str) -> Response:
        if wants_json(request):
            return JSONResponse({"version": UI_VERSION, "code": "not_found", "reason": what}, status_code=404)
        return HTMLResponse(f"<!doctype html><title>Not found · Alibi</title><h1>Not found</h1>"
                            f"<p>{htmlmod.escape(what)}</p>", status_code=404)

    # ------------------------------------------------------------------- health, root

    @app.get("/health")
    def health() -> JSONResponse:
        return JSONResponse({"status": "ok", "version": UI_VERSION, "case": settings.case_number,
                             "subject_ref": rt.subject_ref, "mode": ("rules-only" if settings.mode == "rules" else settings.mode), "replay": settings.replay,
                             "now": rt.now_text(), "db": str(settings.db_path),
                             "bank_url": settings.bank_url or "in-process",
                             "merchant_url": settings.merchant_url or "case_adapter",
                             "seed": rt.seed, "api_origin": settings.api_origin,
                             "tokens_file": str(settings.tokens_file) if settings.tokens_file else None,
                             "holder_auth": "bearer" if settings.holder_token else "demo"})

    @app.get("/")
    def root() -> Response:
        return RedirectResponse("/passport", status_code=303)

    # ------------------------------------------------------------------- holder: passport

    @app.get("/passport")
    def passport(request: Request) -> Response:
        return page(request, "passport/home.html", views.passport_home(rt, flashes_from(request)))

    @app.get("/txn/{txn_id}")
    def txn(request: Request, txn_id: str) -> Response:
        ctx = views.transaction_detail(rt, txn_id, flashes_from(request))
        if ctx is None:
            return not_found(request, f"no transaction {txn_id}")
        return page(request, "passport/transaction.html", ctx)

    @app.get("/sources/{record_id}/{revision}")
    def source(request: Request, record_id: str, revision: int) -> Response:
        ctx = views.source_detail(rt, record_id, revision)
        if ctx is None:
            return not_found(request, f"no record {record_id} revision {revision}")
        if wants_json(request):
            return JSONResponse(jsonable_encoder({**ctx, "version": UI_VERSION, "template": None}))
        item = ctx["record"]
        esc = htmlmod.escape   # record text is merchant-supplied data: escape every value
        rows = "".join(f"<tr><td>{esc(str(k))}</td><td><code>{esc(json.dumps(v))}</code></td></tr>"
                       for k, v in ctx["fields"].items())
        title = esc(f"{record_id} r{revision}")
        html = (f"<!doctype html><html lang='en'><head><meta charset='utf-8'><title>{title} · Alibi</title>"
                f"<link rel='stylesheet' href='{esc(settings.static_prefix)}/alibi.css'></head><body class='area-holder'>"
                f"<main class='page'><h1 class='page-title'>Record {title}</h1>"
                f"<p class='page-lede'>{esc(item['summary'])}</p><p class='muted small'>Trust {esc(item['trust'])} · "
                f"connector {esc(item['connector'])} · revisions {esc(str(ctx['revisions']))}"
                f"{' · superseded' if item['superseded'] else ''}</p>"
                f"<div class='table-wrap'><table class='table table--compact'><tbody>{rows}</tbody></table></div>"
                f"<p><a href='/passport'>Back to the passport</a></p></main></body></html>")
        return HTMLResponse(html)

    # ------------------------------------------------------------------- holder: alerts

    @app.get("/alerts")
    def alerts(request: Request) -> Response:
        return page(request, "passport/alerts.html", views.alerts_page(rt, flashes=flashes_from(request)))

    @app.post("/alerts/lookup")
    async def alerts_lookup(request: Request) -> Response:
        from agents.data.api import merchant_lookup
        try:
            query = (await Params.read(request)).text("query")
        except Refusal as exc:
            return refused(request, exc, "/alerts")
        result = merchant_lookup(query, rt.now_text()) if query.strip() else None
        rt.audit(action="merchant_lookup", outcome="ok", target_id=rt.subject_ref,
                 detail=f"registry check: {result['status'] if result else 'empty query'}")
        return page(request, "passport/alerts.html", views.alerts_page(rt, query=query, result=result),
                    extra={"ok": True, "lookup": {"query": query, "result": result}})

    @app.post("/tick")
    def tick(request: Request, _body: None = Depends(body_ok)) -> Response:
        summary = monitor.tick(rt)
        return redirect(request, "/alerts", payload=summary,
                        flash=("info", f"Checks re-run: {len(summary['raised'])} alert(s) raised, "
                                       f"{len(summary['resolved'])} resolved."))

    # ------------------------------------------------------------------- holder: rules

    @app.get("/rules")
    def rules(request: Request, interp: str = "", prefill: str = "") -> Response:
        interpretation = actions.pending_interpretation(rt, interp) if interp else None
        return page(request, "passport/rules.html",
                    views.rules_page(rt, interpretation, prefill, flashes_from(request)))

    @app.post("/rules/interpret")
    async def rules_interpret(request: Request) -> Response:
        try:
            text = (await Params.read(request)).text("text")
        except Refusal as exc:
            return refused(request, exc, "/rules")
        result = actions.interpret_rule(rt, text)
        if result["status"] == "resolved":
            return redirect(request, f"/rules?interp={result['rule_id']}#interpretation", payload=result)
        return redirect(request, "/rules#held-rules", payload=result,
                        flash=("info", f"Held for clarification: {result['reason']}"))

    @app.post("/rules/{rule_id}/confirm")
    def rules_confirm(request: Request, rule_id: str, _body: None = Depends(body_ok)) -> Response:
        try:
            result = actions.confirm_rule(rt, rule_id)
        except Refusal as exc:
            return refused(request, exc, "/rules")
        return redirect(request, "/rules#active-rules", payload=result,
                        flash=("success", f"Rule confirmed. Policy version {result['policy_version']}."))

    @app.post("/rules/{rule_id}/reject")
    def rules_reject(request: Request, rule_id: str, _body: None = Depends(body_ok)) -> Response:
        actions.reject_rule(rt, rule_id)
        return redirect(request, "/rules", payload={"rule_id": rule_id, "status": "rejected"},
                        flash=("info", "Interpretation rejected; nothing was saved."))

    @app.post("/rules/{rule_id}/retire")
    def rules_retire(request: Request, rule_id: str, _body: None = Depends(body_ok)) -> Response:
        try:
            result = actions.retire_rule(rt, rule_id)
        except Refusal as exc:
            return refused(request, exc, "/rules")
        return redirect(request, "/rules#active-rules", payload=result,
                        flash=("success", f"Rule retired. Policy version {result['policy_version']}."))

    @app.post("/rules/held/{rule_id}/clarify")
    def rules_clarify(request: Request, rule_id: str, _body: None = Depends(body_ok)) -> Response:
        text = actions.held_text(rt, rule_id)
        actions.discard_held(rt, rule_id)
        return redirect(request, f"/rules?prefill={quote(text, safe='')}#add-rule",
                        payload={"rule_id": rule_id, "text": text})

    @app.post("/rules/held/{rule_id}/discard")
    def rules_discard(request: Request, rule_id: str, _body: None = Depends(body_ok)) -> Response:
        actions.discard_held(rt, rule_id)
        return redirect(request, "/rules#held-rules", payload={"rule_id": rule_id, "status": "discarded"},
                        flash=("info", "Held rule discarded."))

    # ------------------------------------------------------------------- holder: requests

    @app.get("/requests")
    def requests_index(request: Request) -> Response:
        ctx = views.requests_index(rt)
        if wants_json(request):
            return JSONResponse(jsonable_encoder({**ctx, "version": UI_VERSION, "template": None}))
        return RedirectResponse("/passport#requests", status_code=303)

    @app.get("/requests/{request_id}")
    def request_detail(request: Request, request_id: str) -> Response:
        ctx = views.request_detail(rt, request_id, flashes_from(request))
        if ctx is None:
            return not_found(request, f"no request {request_id}")
        return page(request, "requests/detail.html", ctx)

    @app.post("/requests/{request_id}/run")
    def request_run(request: Request, request_id: str, _body: None = Depends(body_ok)) -> Response:
        back = f"/requests/{request_id}"
        try:
            rt.store.get_request(request_id)
            result = actions.run_investigation(rt, request_id)
        except NotFound:
            return not_found(request, f"no request {request_id}")
        except Refusal as exc:
            return refused(request, exc, back)
        statuses = ", ".join(f"{q} {s}" for q, s in result["claims"].items())
        return redirect(request, f"{back}#trace", payload=result,
                        flash=("success", f"Investigation complete (rules-only): {statuses}. Review the draft below."))

    @app.post("/requests/{request_id}/remove/{ref_type}/{ref_id}")
    def request_remove(request: Request, request_id: str, ref_type: str, ref_id: str,
                       _body: None = Depends(body_ok)) -> Response:
        back = f"/requests/{request_id}#review"
        if ref_type not in ("record", "txn"):
            return refused(request, Refusal("bad_ref", "ref_type must be record or txn", status=400), back)
        try:
            result = actions.remove_item(rt, request_id, ref_type, ref_id)
        except NotFound:
            return not_found(request, f"no request {request_id}")
        except Refusal as exc:
            return refused(request, exc, back)
        return redirect(request, back, payload=result,
                        flash=("info", f"Removed {ref_type} {ref_id}. The body changed, so any earlier approval "
                                       "no longer matches; approve the revised preview before dispatch."))

    @app.post("/requests/{request_id}/restore")
    def request_restore(request: Request, request_id: str, _body: None = Depends(body_ok)) -> Response:
        back = f"/requests/{request_id}#review"
        try:
            result = actions.restore_items(rt, request_id)
        except (NotFound, Refusal) as exc:
            return not_found(request, str(exc)) if isinstance(exc, NotFound) else refused(request, exc, back)
        return redirect(request, back, payload=result, flash=("info", "Removed items restored; review the new draft."))

    @app.post("/requests/{request_id}/approve")
    async def request_approve(request: Request, request_id: str) -> Response:
        back = f"/requests/{request_id}#approval"
        try:
            p = await Params.read(request)
            result = actions.approve(rt, request_id, digest=p.text("digest"), audience=p.text("audience"),
                                     policy_version=p.text("policy_version"))
        except NotFound:
            return not_found(request, f"no request {request_id}")
        except Refusal as exc:
            return refused(request, exc, back)
        return redirect(request, back, payload=result,
                        flash=("success", f"Approved for {result['audience']} until {result['expires_at']}. "
                                          "Dispatch sends exactly this body."))

    @app.get("/requests/{request_id}/dispatch")
    def dispatch_result(request: Request, request_id: str) -> Response:
        ctx = views.dispatch_page(rt, request_id, flashes_from(request))
        if ctx is None:
            return not_found(request, f"no request {request_id}")
        return page(request, "requests/dispatch.html", ctx)

    @app.post("/requests/{request_id}/dispatch")
    def dispatch(request: Request, request_id: str, _body: None = Depends(body_ok)) -> Response:
        try:
            rfc = rt.store.get_request(request_id)
            result = actions.dispatch(rt, request_id, deliver)
        except NotFound:
            return not_found(request, f"no request {request_id}")
        except Refusal as exc:
            if wants_json(request):
                return refusal_json(exc)
            ctx = views.dispatch_page(rt, request_id)
            return page(request, "requests/dispatch.html", ctx, status=exc.status)
        if wants_json(request):
            return JSONResponse(jsonable_encoder({"version": UI_VERSION, "ok": True, **result,
                                                  **views.dispatch_urls(rt, request_id, rfc.audience, result)}))
        return page(request, "requests/dispatch.html", views.dispatch_page(rt, request_id))

    @app.get("/requests/{request_id}/export")
    def export(request: Request, request_id: str) -> Response:
        ctx = views.export_page(rt, request_id, flashes_from(request))
        if ctx is None:
            return not_found(request, f"request {request_id} has no draft or package yet")
        return page(request, "requests/export.html", ctx)

    @app.get("/requests/{request_id}/export.json")
    def export_json(request: Request, request_id: str) -> Response:
        found = views.export_json(rt, request_id)
        if found is None:
            return not_found(request, f"request {request_id} has no signed package yet")
        raw, filename = found
        return Response(raw, media_type="application/json",
                        headers={"content-disposition": f'attachment; filename="{filename}"'})

    # ------------------------------------------------------------------- connect: the services

    async def json_object(request: Request) -> dict[str, Any]:
        raw = await request.body()
        try:
            obj = json.loads(raw) if raw.strip() else None
        except ValueError:
            obj = None
        if not isinstance(obj, dict):
            raise Refusal("bad_request", "the request body must be a JSON object", status=400)
        return obj

    @app.post("/connect/merchant/webhook")
    async def connect_merchant_webhook(request: Request) -> Response:
        """The merchant's push: ``{event, signature}`` at ``?received_at=``. The issuer signature
        inside the payload is the authentication; the route is served to loopback clients only,
        like the services' admin routes. The sink's outcome is the answer: 200 for inserted,
        duplicate, corrected and stale; 409 ``ingest_refused``; 503 ``store_error``."""
        if not is_loopback_client(request):
            return refusal_json(Refusal("forbidden", "the merchant webhook is served to loopback clients only",
                                        status=403))
        received_at = request.query_params.get("received_at", "").strip()
        if not received_at:
            return refusal_json(Refusal("bad_request", "received_at is required: ?received_at=<utc>", status=400))
        try:
            payload = await json_object(request)
        except Refusal as exc:
            return refusal_json(exc)
        if not isinstance(payload.get("event"), dict) or not isinstance(payload.get("signature"), dict):
            return refusal_json(Refusal("bad_request", "the body must be {event, signature}", status=400))
        with rt.lock:
            result = merchant_sink({"event": payload["event"], "signature": payload["signature"]}, received_at)
            if result.get("status") in WEBHOOK_STATUS and WEBHOOK_STATUS[result["status"]] == 200:
                monitor.tick(rt)
        status = WEBHOOK_STATUS.get(result.get("status"), 503)
        body = dict(result)
        if status >= 400:
            body = {"code": "ingest_refused" if status == 409 else "store_error",
                    "reason": result.get("reason") or result.get("status"), **result}
        return JSONResponse(jsonable_encoder({"version": UI_VERSION, **body}), status_code=status)

    @app.post("/connect/bank/rfc")
    async def connect_bank_rfc(request: Request) -> Response:
        """The bank service's forwarded request: ``{rfc: {request, signature}, recipient,
        account_refs, now}`` with the recipient's bearer. The recipient in the body must be the
        one the bearer names; ``receive_rfc`` then verifies the bank's signature and scope."""
        who = settings.recipient_for_token(bearer_of(request))
        if who is None:
            return refusal_json(Refusal("not_authenticated", "a recipient bearer token is required", status=401))
        try:
            body = await json_object(request)
        except Refusal as exc:
            return refusal_json(exc)
        if body.get("recipient") != who:
            rt.bank_audit(who, action="access.refused", outcome="refused", target_id=None,
                          detail="forwarded request names another recipient")
            return refusal_json(Refusal("recipient_mismatch", f"the credential is for {who}, not the recipient named",
                                        status=403))
        signed = body.get("rfc")
        if not isinstance(signed, dict) or not isinstance(signed.get("request"), dict):
            return refusal_json(Refusal("bad_request", "rfc must be the signed {request, signature} object", status=400))
        account_refs = body.get("account_refs")
        if account_refs is not None and (not isinstance(account_refs, list)
                                         or not all(isinstance(a, str) for a in account_refs)):
            return refusal_json(Refusal("bad_request", "account_refs must be a list of account ids", status=400))
        now = body.get("now") if isinstance(body.get("now"), str) and body.get("now") else rt.now_text()
        with rt.lock:
            result = bank_rfc_sink(signed, who, account_refs, now)
            if result.get("status") == "accepted":
                rt.record_rfc_signature(result["request_id"], who)
                monitor.tick(rt)
        if result.get("status") == "accepted":
            payload = {**result, "href": f"/requests/{result['request_id']}"}
            return JSONResponse(jsonable_encoder({"version": UI_VERSION, **payload}), status_code=200)
        if result.get("status") == "refused":
            rt.bank_audit(who, action="rfc.refused", outcome="refused",
                          target_id=signed["request"].get("request_id") if _ident(signed["request"].get("request_id")) else None,
                          detail=result.get("reason"))
            return JSONResponse({"version": UI_VERSION, "code": "rfc_refused", **result}, status_code=409)
        return JSONResponse({"version": UI_VERSION, "code": "store_error", **result}, status_code=503)

    @app.post("/connect/bank/sync")
    def connect_bank_sync(request: Request, _body: None = Depends(body_ok)) -> Response:
        """Pull the holder's bank feed from ``ALIBI_BANK_URL`` with the feed token
        (``FeedClient.sync`` over ``HttpFeed``), then re-run the monitor."""
        back = "/passport#transactions"
        if not settings.bank_url:
            return refused(request, Refusal("no_bank_url", "ALIBI_BANK_URL is not set: the passport's bank feed is "
                                                           "loaded by the case adapter, there is nothing to sync"), back)
        feed = FeedClient(HttpFeed(settings.bank_url, settings.feed_token, client=rt.http_client(settings.bank_url)),
                          rt.store, subject_ref=rt.subject_ref, connection_id=rt.connection_id)
        try:
            with rt.lock:
                summary = feed.sync(now=rt.clock())
                tick = monitor.tick(rt)
        except FeedError as exc:
            return refused(request, Refusal("feed_unavailable", f"the bank feed could not be read: {exc}", status=502), back)
        except IngestError as exc:
            return refused(request, Refusal("feed_refused", f"the passport refused the feed page: {exc.reason}"), back)
        rt.audit(action="bank_sync", outcome="ok", target_id=rt.connection_id,
                 detail=f"feed sync: {summary['inserted']} inserted, {summary['duplicate']} duplicate, "
                        f"{summary['corrected']} corrected, {summary['stale']} stale; cursor {summary['cursor']}")
        return redirect(request, back, payload={**summary, "tick": tick},
                        flash=("info", f"Bank feed synced: {summary['inserted']} new, {summary['duplicate']} already "
                                       f"known, {summary['corrected']} corrected."))

    # ------------------------------------------------------------------- holder: audit

    @app.get("/audit")
    def audit(request: Request, q: str = "", actor: str = "", page_number: int = 1) -> Response:
        try:
            number = int(request.query_params.get("page", page_number))
        except ValueError:
            number = 1
        return page(request, "audit/list.html", views.audit_page(rt, q, actor, number, flashes=flashes_from(request)))

    # ------------------------------------------------------------------- bank service

    def bank_auth(request: Request, recipient: str, *, allow_query: bool = False) -> tuple[str, str | None]:
        query_token = request.query_params.get("token") if allow_query else None
        return bank.authenticate(rt, recipient, bearer=bearer_of(request), cookie=request.cookies.get(BANK_COOKIE),
                                 query_token=query_token)

    def bank_refused(request: Request, exc: Refusal) -> Response:
        """Credential refusals: JSON for API clients, a plain escaped page for browsers."""
        return refusal_json(exc) if wants_json(request) else refusal_page(exc, "analyst")

    @app.get("/bank/{recipient}/audit")
    def bank_audit(request: Request, recipient: str) -> Response:
        """The recipient's own audit entries (receipts, dispositions, refused access), JSON only."""
        try:
            who, _ = bank_auth(request, recipient)
        except Refusal as exc:
            return bank_refused(request, exc)
        try:
            entries = bank.audit_entries(rt, who)
        except Refusal as exc:
            return bank_refused(request, exc)
        return JSONResponse(jsonable_encoder({"version": UI_VERSION, "recipient": who, "entries": entries}))

    def bank_page(request: Request, template: str, ctx: dict, token: str | None, status: int = 200) -> Response:
        response = page(request, template, ctx, status)
        if token:
            response.set_cookie(BANK_COOKIE, token, httponly=True, samesite="lax")
        return response

    @app.post("/bank/{recipient}/receive")
    async def bank_receive(request: Request, recipient: str) -> Response:
        raw = await request.body()
        status, body = bank.receive_package(rt, recipient, raw)
        return JSONResponse(jsonable_encoder(body), status_code=status)

    @app.get("/bank/{recipient}/inbox")
    def bank_inbox(request: Request, recipient: str) -> Response:
        try:
            who, token = bank_auth(request, recipient, allow_query=True)
        except Refusal as exc:
            return bank_refused(request, exc)
        if token:
            # Start the session, then drop the credential from the address bar, history and referers.
            response = RedirectResponse(f"/bank/{who}/inbox", status_code=303)
            response.set_cookie(BANK_COOKIE, token, httponly=True, samesite="lax")
            return response
        try:
            ctx = bank.inbox(rt, who, flashes_from(request))
        except Refusal as exc:
            return bank_refused(request, exc)
        return bank_page(request, "analyst/inbox.html", ctx, None)

    @app.get("/bank/{recipient}/requests")
    def bank_requests(request: Request, recipient: str) -> Response:
        try:
            who, _ = bank_auth(request, recipient)
        except Refusal as exc:
            return bank_refused(request, exc)
        try:
            issued = bank.issued_requests(rt, who)
        except Refusal as exc:
            return bank_refused(request, exc)
        return JSONResponse(jsonable_encoder({"version": UI_VERSION, "recipient": who, "requests": issued}))

    @app.get("/bank/{recipient}/packages/{package_id}")
    def bank_package(request: Request, recipient: str, package_id: str, disposition: str = "") -> Response:
        try:
            who, token = bank_auth(request, recipient)
        except Refusal as exc:
            return bank_refused(request, exc)
        try:
            ctx = bank.package_view(rt, who, package_id, disposition_id=disposition or None,
                                    flashes=flashes_from(request))
        except Refusal as exc:
            return bank_refused(request, exc)
        if ctx is None:
            return not_found(request, f"no package {package_id} for {who}")
        return bank_page(request, "analyst/package.html", ctx, token)

    @app.post("/bank/{recipient}/packages/{package_id}/disposition")
    async def bank_disposition(request: Request, recipient: str, package_id: str) -> Response:
        try:
            who, _ = bank_auth(request, recipient)
        except Refusal as exc:
            return bank_refused(request, exc)
        back = f"/bank/{who}/packages/{package_id}#disposition"
        try:
            p = await Params.read(request)
            disp = bank.record_disposition(
                rt, who, package_id, decision=p.text("decision"), reason=p.text("reason"),
                question_ids=p.many("question_ids"),
                requested_evidence=[line.strip() for line in "\n".join(p.many("requested_evidence")).splitlines()
                                    if line.strip()])
        except Refusal as exc:
            return refused(request, exc, back)
        if disp["decision"] == "request_more":
            target = f"/bank/{who}/packages/{package_id}?disposition={disp['disposition_id']}#followup"
            flash = ("info", "Disposition recorded: request more. Send the follow-up request below; the holder "
                             "approves any new disclosure separately.")
        else:
            target, flash = back, ("success", f"Disposition recorded: {disp['decision']}. No account action was taken.")
        return redirect(request, target, payload=disp, flash=flash)

    @app.post("/bank/{recipient}/requests")
    async def bank_followup(request: Request, recipient: str) -> Response:
        try:
            who, _ = bank_auth(request, recipient)
        except Refusal as exc:
            return bank_refused(request, exc)
        back = f"/bank/{who}/inbox#requests-sent"
        try:
            p = await Params.read(request)
            result = bank.create_followup(
                rt, who, parent_request_id=p.text("parent_request_id"), scope=p.text("scope") or None,
                txn_ids=",".join(p.many("txn_ids")).replace(";", ",").split(","),
                period_from=p.text("period_from"), period_to=p.text("period_to"),
                question=p.text("question"), disposition_id=p.text("disposition_id") or None)
        except Refusal as exc:
            return refused(request, exc, back)
        return redirect(request, back, payload=result, json_status=201,
                        flash=("success", f"Follow-up {result['request_id']} sent through the bank connection; "
                                          "it appears in the holder's passport as a new request."))

    return app


app: FastAPI | None = None


def get_app() -> FastAPI:
    """Lazily built module-level app for ``uvicorn alibi.app.main:get_app --factory``."""
    global app
    if app is None:
        app = create_app()
    return app


def _ident(value: Any) -> bool:
    import re
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}", value) is not None
