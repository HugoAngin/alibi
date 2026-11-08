"""The Alibi web app: holder passport, request review and dispatch, bank verification console.

Every holder and analyst route renders its template context as HTML, or as JSON when the request
asks for ``application/json`` first in ``Accept``. Refusals answer with a 4xx status and a body
``{"code", "reason"}``; the HTML path shows the same sentence verbatim.

Run: ``uv run --frozen python -m alibi.app [--host H] [--port P] [--timeout SECONDS]``.
"""

from __future__ import annotations

import html as htmlmod
import json
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, Form, Request
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
from alibi.core.store import NotFound

APP_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = APP_DIR / "templates"
STATIC_DIR = APP_DIR / "static"
UI_VERSION = "alibi-ui/1"
FLASH_COOKIE = "alibi_flash"
BANK_COOKIE = "alibi_bank"


def make_env() -> Environment:
    return Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)), undefined=StrictUndefined,
                       autoescape=select_autoescape(["html"]), trim_blocks=False, lstrip_blocks=False)


def wants_json(request: Request) -> bool:
    accept = request.headers.get("accept", "")
    first = accept.split(",")[0].strip().lower() if accept else ""
    return first.startswith("application/json")


def create_app(settings: Settings | None = None, *, runtime: Runtime | None = None) -> FastAPI:
    settings = settings or settings_from_env()
    rt = runtime or Runtime.open(settings)
    env = make_env()
    app = FastAPI(title="Alibi", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.runtime = rt
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> Response:
        """Unknown routes and wrong methods answer in the refusal shape too."""
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
        reason = str(exc.detail) if exc.detail else code.replace("_", " ")
        if wants_json(request):
            return JSONResponse({"version": UI_VERSION, "code": code, "reason": reason}, status_code=exc.status_code)
        return HTMLResponse(f"<!doctype html><title>{htmlmod.escape(code)} · Alibi</title><h1>{htmlmod.escape(code)}</h1>"
                            f"<p>{htmlmod.escape(reason)}</p>", status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def bad_request(request: Request, exc: RequestValidationError) -> Response:
        reason = "; ".join(f"{'.'.join(str(p) for p in e.get('loc', []))}: {e.get('msg')}" for e in exc.errors())
        return JSONResponse({"version": UI_VERSION, "code": "bad_request", "reason": reason[:300]}, status_code=400)

    @app.middleware("http")
    async def same_origin_posts(request: Request, call_next):
        """A page on another site must not drive the holder's or the analyst's actions. Browsers
        send ``Sec-Fetch-Site`` and ``Origin`` on cross-site form posts; same-origin forms and
        plain API clients (no such headers) pass. The bank delivery endpoint is exempt."""
        if request.method == "POST" and not request.url.path.endswith("/receive"):
            site = request.headers.get("sec-fetch-site", "").lower()
            origin = request.headers.get("origin")
            host = request.headers.get("host", "").lower()
            origin_host = origin.split("//", 1)[-1].split("/", 1)[0].lower() if origin else None
            cross = site in ("cross-site", "same-site") or (origin is not None and origin_host != host)
            if cross:
                return JSONResponse({"version": UI_VERSION, "code": "cross_site_post",
                                     "reason": "state-changing posts are accepted from this site only"},
                                    status_code=403)
        return await call_next(request)
    deliver = (actions.deliver_over_http(settings.bank_url) if settings.bank_url
               else actions.deliver_in_process(rt))

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

    def page(request: Request, template: str, ctx: dict[str, Any], status: int = 200) -> Response:
        ctx = {**ctx, "version": UI_VERSION, "template": template}
        if wants_json(request):
            response = JSONResponse(jsonable_encoder(ctx), status_code=status)
        else:
            html = env.get_template(template).render(**ctx)
            response = HTMLResponse(html, status_code=status)
        if FLASH_COOKIE in request.cookies:
            response.delete_cookie(FLASH_COOKIE)
        return response

    def redirect(request: Request, url: str, *, flash: tuple[str, str] | None = None,
                 payload: dict | None = None, status: int = 303) -> Response:
        if wants_json(request):
            return JSONResponse(jsonable_encoder({"version": UI_VERSION, "ok": True, "location": url,
                                                  **(payload or {})}))
        response = RedirectResponse(url, status_code=status)
        if flash:
            response.set_cookie(FLASH_COOKIE, json.dumps([{"kind": flash[0], "text": flash[1]}]),
                                max_age=120, httponly=True, samesite="lax")
        return response

    def refused(request: Request, exc: Refusal, back: str) -> Response:
        if wants_json(request):
            return JSONResponse(jsonable_encoder({"version": UI_VERSION, **exc.body()}), status_code=exc.status)
        return redirect(request, back, flash=("error", f"{exc.code}: {exc.reason}"))

    def not_found(request: Request, what: str) -> Response:
        if wants_json(request):
            return JSONResponse({"version": UI_VERSION, "code": "not_found", "reason": what}, status_code=404)
        return HTMLResponse(f"<!doctype html><title>Not found · Alibi</title><h1>Not found</h1>"
                            f"<p>{htmlmod.escape(what)}</p>", status_code=404)

    # ------------------------------------------------------------------- health, root

    @app.get("/health")
    def health() -> JSONResponse:
        return JSONResponse({"status": "ok", "case": settings.case_number, "subject_ref": rt.subject_ref,
                             "mode": "rules-only", "replay": settings.replay, "now": rt.now_text(),
                             "db": str(settings.db_path), "bank_url": settings.bank_url or "in-process"})

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
            return JSONResponse(jsonable_encoder(ctx))
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
    def alerts_lookup(request: Request, query: str = Form("")) -> Response:
        from agents.data.api import merchant_lookup
        result = merchant_lookup(query, rt.now_text()) if query.strip() else None
        rt.audit(action="merchant_lookup", outcome="ok", target_id=rt.subject_ref,
                 detail=f"registry check: {result['status'] if result else 'empty query'}")
        return page(request, "passport/alerts.html", views.alerts_page(rt, query=query, result=result))

    @app.post("/tick")
    def tick(request: Request) -> Response:
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
    def rules_interpret(request: Request, text: str = Form("")) -> Response:
        result = actions.interpret_rule(rt, text)
        if result["status"] == "resolved":
            return redirect(request, f"/rules?interp={result['rule_id']}#interpretation", payload=result)
        return redirect(request, "/rules#held-rules", payload=result,
                        flash=("info", f"Held for clarification: {result['reason']}"))

    @app.post("/rules/{rule_id}/confirm")
    def rules_confirm(request: Request, rule_id: str) -> Response:
        try:
            result = actions.confirm_rule(rt, rule_id)
        except Refusal as exc:
            return refused(request, exc, "/rules")
        return redirect(request, "/rules#active-rules", payload=result,
                        flash=("success", f"Rule confirmed. Policy version {result['policy_version']}."))

    @app.post("/rules/{rule_id}/reject")
    def rules_reject(request: Request, rule_id: str) -> Response:
        actions.reject_rule(rt, rule_id)
        return redirect(request, "/rules", flash=("info", "Interpretation rejected; nothing was saved."))

    @app.post("/rules/{rule_id}/retire")
    def rules_retire(request: Request, rule_id: str) -> Response:
        try:
            result = actions.retire_rule(rt, rule_id)
        except Refusal as exc:
            return refused(request, exc, "/rules")
        return redirect(request, "/rules#active-rules", payload=result,
                        flash=("success", f"Rule retired. Policy version {result['policy_version']}."))

    @app.post("/rules/held/{rule_id}/clarify")
    def rules_clarify(request: Request, rule_id: str) -> Response:
        text = actions.held_text(rt, rule_id)
        actions.discard_held(rt, rule_id)
        return redirect(request, f"/rules?prefill={quote(text, safe='')}#add-rule", payload={"text": text})

    @app.post("/rules/held/{rule_id}/discard")
    def rules_discard(request: Request, rule_id: str) -> Response:
        actions.discard_held(rt, rule_id)
        return redirect(request, "/rules#held-rules", flash=("info", "Held rule discarded."))

    # ------------------------------------------------------------------- holder: requests

    @app.get("/requests")
    def requests_index(request: Request) -> Response:
        ctx = views.requests_index(rt)
        if wants_json(request):
            return JSONResponse(jsonable_encoder({**ctx, "version": UI_VERSION}))
        return RedirectResponse("/passport#requests", status_code=303)

    @app.get("/requests/{request_id}")
    def request_detail(request: Request, request_id: str) -> Response:
        ctx = views.request_detail(rt, request_id, flashes_from(request))
        if ctx is None:
            return not_found(request, f"no request {request_id}")
        return page(request, "requests/detail.html", ctx)

    @app.post("/requests/{request_id}/run")
    def request_run(request: Request, request_id: str) -> Response:
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
    def request_remove(request: Request, request_id: str, ref_type: str, ref_id: str) -> Response:
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
    def request_restore(request: Request, request_id: str) -> Response:
        back = f"/requests/{request_id}#review"
        try:
            result = actions.restore_items(rt, request_id)
        except (NotFound, Refusal) as exc:
            return not_found(request, str(exc)) if isinstance(exc, NotFound) else refused(request, exc, back)
        return redirect(request, back, payload=result, flash=("info", "Removed items restored; review the new draft."))

    @app.post("/requests/{request_id}/approve")
    def request_approve(request: Request, request_id: str, digest: str = Form(""), audience: str = Form(""),
                        policy_version: str = Form("")) -> Response:
        back = f"/requests/{request_id}#approval"
        try:
            result = actions.approve(rt, request_id, digest=digest, audience=audience, policy_version=policy_version)
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
    def dispatch(request: Request, request_id: str) -> Response:
        try:
            rt.store.get_request(request_id)
            result = actions.dispatch(rt, request_id, deliver)
        except NotFound:
            return not_found(request, f"no request {request_id}")
        except Refusal as exc:
            if wants_json(request):
                return JSONResponse(jsonable_encoder({"version": UI_VERSION, **exc.body()}), status_code=exc.status)
            ctx = views.dispatch_page(rt, request_id)
            return page(request, "requests/dispatch.html", ctx, status=exc.status)
        if wants_json(request):
            return JSONResponse(jsonable_encoder({"version": UI_VERSION, **result}))
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
        header = request.headers.get("authorization", "")
        bearer = header[7:].strip() if header.lower().startswith("bearer ") else None
        query_token = request.query_params.get("token") if allow_query else None
        return bank.authenticate(rt, recipient, bearer=bearer, cookie=request.cookies.get(BANK_COOKIE),
                                 query_token=query_token)

    def bank_refused(request: Request, exc: Refusal) -> Response:
        """Credential refusals: JSON for API clients, a plain escaped page for browsers."""
        if wants_json(request):
            return JSONResponse(jsonable_encoder({"version": UI_VERSION, **exc.body()}), status_code=exc.status)
        body = (f"<!doctype html><html lang='en'><head><meta charset='utf-8'><title>Refused · Alibi</title>"
                f"<link rel='stylesheet' href='{htmlmod.escape(settings.static_prefix)}/alibi.css'></head>"
                f"<body class='area-analyst'><main class='page'><div class='result result--refused'>"
                f"<h2>Refused</h2><p>{htmlmod.escape(exc.code)}</p><pre class='result__reason'>"
                f"{htmlmod.escape(exc.reason)}</pre></div></main></body></html>")
        return HTMLResponse(body, status_code=exc.status)

    @app.get("/bank/{recipient}/audit")
    def bank_audit(request: Request, recipient: str) -> Response:
        """The recipient's own audit entries (receipts, dispositions, refused access), JSON only."""
        try:
            who, _ = bank_auth(request, recipient)
        except Refusal as exc:
            return bank_refused(request, exc)
        return JSONResponse(jsonable_encoder({"version": UI_VERSION, "recipient": who,
                                              "entries": rt.bank_audit_entries(who)}))

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
        return bank_page(request, "analyst/inbox.html", bank.inbox(rt, who, flashes_from(request)), None)

    @app.get("/bank/{recipient}/requests")
    def bank_requests(request: Request, recipient: str) -> Response:
        try:
            who, _ = bank_auth(request, recipient)
        except Refusal as exc:
            return bank_refused(request, exc)
        return JSONResponse(jsonable_encoder({"version": UI_VERSION, "recipient": who,
                                              "requests": bank.issued_requests(rt, who)}))

    @app.get("/bank/{recipient}/packages/{package_id}")
    def bank_package(request: Request, recipient: str, package_id: str, disposition: str = "") -> Response:
        try:
            who, token = bank_auth(request, recipient)
        except Refusal as exc:
            return bank_refused(request, exc)
        ctx = bank.package_view(rt, who, package_id, disposition_id=disposition or None, flashes=flashes_from(request))
        if ctx is None:
            return not_found(request, f"no package {package_id} for {who}")
        return bank_page(request, "analyst/package.html", ctx, token)

    @app.post("/bank/{recipient}/packages/{package_id}/disposition")
    async def bank_disposition(request: Request, recipient: str, package_id: str) -> Response:
        try:
            who, _ = bank_auth(request, recipient)
        except Refusal as exc:
            return bank_refused(request, exc)
        form = await request.form()
        back = f"/bank/{who}/packages/{package_id}#disposition"
        try:
            disp = bank.record_disposition(
                rt, who, package_id, decision=str(form.get("decision", "")), reason=str(form.get("reason", "")),
                question_ids=[str(q) for q in form.getlist("question_ids")],
                requested_evidence=[line.strip() for line in str(form.get("requested_evidence", "")).splitlines()
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
        form = await request.form()
        parent = str(form.get("parent_request_id", ""))
        back = f"/bank/{who}/inbox#requests-sent"
        try:
            result = bank.create_followup(
                rt, who, parent_request_id=parent, scope=str(form.get("scope", "")) or None,
                txn_ids=[t for t in str(form.get("txn_ids", "")).replace(";", ",").split(",")],
                period_from=str(form.get("period_from", "")), period_to=str(form.get("period_to", "")),
                question=str(form.get("question", "")), disposition_id=str(form.get("disposition_id", "")) or None)
        except Refusal as exc:
            return refused(request, exc, back)
        if wants_json(request):
            return JSONResponse(jsonable_encoder({"version": UI_VERSION, **result}), status_code=201)
        return redirect(request, back, payload=result,
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
