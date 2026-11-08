"""Render every Alibi template once from the real app over the team's demo cases.

No hand-written sample data: the contexts come from ``alibi.app.views`` and ``alibi.app.bank``
after the real flow has run on ``agents.data.api.canonical_case()`` (case 1, the supported
control) and case 2 (the contradicted twin): load, monitor tick, rules investigation, review,
approval, dispatch to the mock bank, verification, disposition and a refused dispatch after the
holder edits an approved body. Everything runs in a temporary directory with a fixed clock.

Run from the repo root::

    uv run --frozen python -m alibi.app.preview_render

It writes one HTML file per page to ``data/alibi/ui_preview/`` and copies the stylesheet next
to them so the files open directly in a browser. ``alibi/app/UI.md`` documents the shapes.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from agents.data.api import merchant_lookup
from alibi.app import actions, bank, monitor, views
from alibi.app.actions import Refusal
from alibi.app.config import settings_from_env
from alibi.app.runtime import Runtime

APP_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = APP_DIR / "templates"
STATIC_DIR = APP_DIR / "static"
REPO_ROOT = APP_DIR.parent.parent
DEFAULT_OUT_DIR = REPO_ROOT / "data" / "alibi" / "ui_preview"

# The canonical fixture (shared/CONTRACTS.md): sales 240000 - refunds 18000 - fees 6000 = 216000.
CURRENCY = "GBP"
SALES = 240000
REFUNDS = 18000
FEES = 6000
CREDIT = 216000
assert SALES - REFUNDS - FEES == CREDIT

PREVIEW_NOW = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
TOKEN_A = "demo-token-bank-a"
_CACHE: list[tuple[str, str, dict]] | None = None


def money(minor: int, currency: str = CURRENCY) -> dict:
    return {"minor": minor, "currency": currency}


def _runtime(case_number: int, root: Path) -> Runtime:
    settings = settings_from_env({}, case_number=case_number, data_dir=root / f"case{case_number}" / "app",
                                 key_dir=root / "keys", fixed_now=PREVIEW_NOW)
    return Runtime.open(settings)


def _approve(rt: Runtime, request_id: str) -> None:
    detail = views.request_detail(rt, request_id)
    ap = detail["review"]["approval"]
    actions.approve(rt, request_id, digest=ap["digest"], audience=ap["audience"],
                    policy_version=ap["policy_version"])


def _flow(rt: Runtime, *, refuse_after: bool) -> dict:
    """Run the demo flow on one case. Returns ids the pages need."""
    n = rt.settings.case_number
    rid, rid_b = f"rfc-000{n}-01", f"rfc-000{n}-02"
    deliver = actions.deliver_in_process(rt)
    monitor.tick(rt)
    actions.run_investigation(rt, rid)
    _approve(rt, rid)
    sent = actions.dispatch(rt, rid, deliver)
    actions.run_investigation(rt, rid_b)
    _approve(rt, rid_b)
    actions.dispatch(rt, rid_b, deliver)
    monitor.tick(rt)
    out = {"rid": rid, "rid_b": rid_b, "package_id": sent["package_id"], "txn": f"txn-000{n}-004"}
    if refuse_after:
        detail = views.request_detail(rt, rid)
        fee = next(i for i in detail["review"]["included"] if i["kind"] == "fee")
        actions.remove_item(rt, rid, "record", fee["record_id"])
        try:
            actions.dispatch(rt, rid, deliver)
        except Refusal:
            pass
    return out


def build_contexts() -> list[tuple[str, str, dict]]:
    """(page name, template, context) for every preview page, from the real app."""
    root = Path(tempfile.mkdtemp(prefix="alibi-preview-"))
    rt1 = _runtime(1, root)
    rt2 = _runtime(2, root)
    try:
        one = _flow(rt1, refuse_after=False)
        two = _flow(rt2, refuse_after=True)
        interpretation = actions.interpret_rule(rt1, "don't share item names")["interpretation"]
        actions.interpret_rule(rt1, "share everything with everyone")      # held for clarification
        bank.record_disposition(rt1, "bank_a", one["package_id"], decision="request_more",
                                reason="Please provide the settlement record for the payout.",
                                question_ids=["q.breakdown"], requested_evidence=["settlement record for PO-0001-01"])
        unverified = merchant_lookup("Payflux Holdings", rt1.now_text())
        audit = views.audit_page(rt1)
        replay = {**views.audit_page(rt1), "replay_mode": True,
                  "replay": {"label": "Recorded run", "recorded_at": rt1.now_text(),
                             "source": "case 1 replay (ALIBI_REPLAY=1)"}}
        pages = [
            ("passport_home", "passport/home.html", views.passport_home(rt1)),
            ("transaction_detail", "passport/transaction.html", views.transaction_detail(rt1, one["txn"])),
            ("alerts", "passport/alerts.html", views.alerts_page(rt1)),
            ("alerts_lookup_unverified", "passport/alerts.html",
             views.alerts_page(rt1, query="Payflux Holdings", result=unverified)),
            ("rules", "passport/rules.html", views.rules_page(rt1)),
            ("rules_interpretation", "passport/rules.html", views.rules_page(rt1, interpretation)),
            ("request_detail", "requests/detail.html", views.request_detail(rt1, one["rid"])),
            ("request_detail_contradicted", "requests/detail.html", views.request_detail(rt2, two["rid"])),
            ("export", "requests/export.html", views.export_page(rt1, one["rid"])),
            ("dispatch_success", "requests/dispatch.html", views.dispatch_page(rt1, one["rid"])),
            ("dispatch_refused", "requests/dispatch.html", views.dispatch_page(rt2, two["rid"])),
            ("analyst_inbox", "analyst/inbox.html", bank.inbox(rt1, "bank_a")),
            ("analyst_package", "analyst/package.html", bank.package_view(rt1, "bank_a", one["package_id"])),
            ("analyst_package_contradicted", "analyst/package.html",
             bank.package_view(rt2, "bank_a", two["package_id"])),
            ("audit", "audit/list.html", audit),
            ("audit_replay", "audit/list.html", replay),
        ]
    finally:
        rt1.close()
        rt2.close()
    shutil.rmtree(root, ignore_errors=True)
    return pages


def sample_contexts() -> list[tuple[str, str, dict]]:
    """Cached ``build_contexts()``; the flow runs once per process."""
    global _CACHE
    if _CACHE is None:
        _CACHE = build_contexts()
    return _CACHE


def context(name: str) -> dict:
    for page, _, ctx in sample_contexts():
        if page == name:
            return ctx
    raise KeyError(name)


def ctx_dispatch(outcome: str) -> dict:
    """Compatibility helper: the dispatch page context for ``success`` or ``refused``."""
    return context("dispatch_success" if outcome == "success" else "dispatch_refused")


def make_env() -> Environment:
    """The Jinja environment the app uses. StrictUndefined makes a missing variable an error."""
    return Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)), autoescape=select_autoescape(["html"]),
                       undefined=StrictUndefined)


def render_all(out_dir: Path | None = None) -> dict[str, str]:
    env = make_env()
    rendered = {name: env.get_template(template).render(**ctx) for name, template, ctx in sample_contexts()}
    if out_dir is not None:
        out_dir = Path(out_dir)
        (out_dir / "static").mkdir(parents=True, exist_ok=True)
        shutil.copy(STATIC_DIR / "alibi.css", out_dir / "static" / "alibi.css")
        for name, html in rendered.items():
            (out_dir / f"{name}.html").write_text(html, encoding="utf-8")
        links = "".join(f'<li><a href="{name}.html">{name}</a></li>' for name in rendered)
        (out_dir / "index.html").write_text(
            "<!doctype html><html lang='en'><head><meta charset='utf-8'><title>Alibi UI preview</title>"
            "<link rel='stylesheet' href='static/alibi.css'></head><body class='area-public'><main class='page'>"
            "<h1 class='page-title'>Alibi UI preview</h1><p class='page-lede'>Rendered from the real app over "
            f"team cases 1 and 2 at a fixed clock ({PREVIEW_NOW.isoformat()}).</p><ul class='items'>{links}</ul>"
            "</main></body></html>", encoding="utf-8")
    return rendered


def main(argv: list[str]) -> int:
    out = Path(argv[1]) if len(argv) > 1 else DEFAULT_OUT_DIR
    rendered = render_all(out)
    print(f"wrote {len(rendered)} pages to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
