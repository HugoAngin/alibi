"""Every Alibi template must render with the sample contexts from preview_render.

These tests never call Gemini or any network. They render in memory; the
on-disk preview is exercised once into a temporary directory.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from alibi.app import preview_render as pr

EXPECTED_PAGES = {
    "passport_home",
    "transaction_detail",
    "alerts",
    "alerts_lookup_unverified",
    "rules",
    "rules_interpretation",
    "request_detail",
    "export",
    "dispatch_success",
    "dispatch_refused",
    "analyst_inbox",
    "analyst_package",
    "audit",
    "audit_replay",
}


def test_canonical_amounts():
    assert pr.SALES == 240000
    assert pr.REFUNDS == 18000
    assert pr.FEES == 6000
    assert pr.CREDIT == 216000
    assert pr.SALES - pr.REFUNDS - pr.FEES == pr.CREDIT


def test_every_template_renders_without_raising():
    rendered = pr.render_all()
    assert set(rendered) == EXPECTED_PAGES
    for name, html in rendered.items():
        assert html.strip().lower().startswith("<!doctype html>"), name
        assert "</html>" in html, name


def test_every_template_file_is_rendered_by_a_preview():
    """A template nobody renders is a template nobody has compiled."""
    used = {t for _, t, _ in pr.sample_contexts()}
    on_disk = {
        p.relative_to(pr.TEMPLATE_DIR).as_posix()
        for p in pr.TEMPLATE_DIR.rglob("*.html")
        if p.name != "base.html" and not p.relative_to(pr.TEMPLATE_DIR).as_posix().startswith("partials/")
    }
    assert on_disk == used


@pytest.mark.parametrize("name,template,ctx", pr.sample_contexts(), ids=[n for n, _, _ in pr.sample_contexts()])
def test_no_python_none_leaks_into_html(name, template, ctx):
    html = pr.make_env().get_template(template).render(**ctx)
    text = re.sub(r"<[^>]+>", " ", html)
    assert not re.search(r"\bNone\b", text), f"{name}: a None value leaked into visible text"
    assert "undefined" not in text.lower(), name


def test_reconciliation_and_badges_appear():
    rendered = pr.render_all()
    txn = rendered["transaction_detail"]
    assert "240000 - 18000 - 6000 = 216000" in txn
    assert "£2,160.00" in txn
    assert "216000 GBP" in txn
    assert "badge--trust-self_supplied" in txn
    pkg = rendered["analyst_package"]
    for cls in ("badge--trust-issuer_verified", "badge--trust-self_supplied",
                "badge--claim-supported", "badge--claim-contradicted", "badge--claim-unresolved"):
        assert cls in pkg, cls
    home = rendered["passport_home"]
    for cls in ("badge--link-linked", "badge--link-ambiguous", "badge--link-unmatched"):
        assert cls in home, cls


def test_refusal_reason_is_verbatim():
    ctx = pr.ctx_dispatch("refused")
    html = pr.make_env().get_template("requests/dispatch.html").render(**ctx)
    assert ctx["dispatch"]["refusal"]["reason"] in html.replace("&#39;", "'").replace("&quot;", '"')


def test_replay_banner_only_in_replay_mode():
    rendered = pr.render_all()
    assert "banner--replay" in rendered["audit_replay"]
    assert "banner--replay" not in rendered["audit"]


def test_templates_extend_base_and_use_no_script_except_print():
    for p in pr.TEMPLATE_DIR.rglob("*.html"):
        rel = p.relative_to(pr.TEMPLATE_DIR).as_posix()
        src = p.read_text(encoding="utf-8")
        if rel != "base.html" and not rel.startswith("partials/"):
            assert '{% extends "base.html" %}' in src, rel
        assert "<script" not in src.lower(), rel
        if rel != "requests/export.html":
            assert "onclick" not in src.lower(), rel


def test_render_all_writes_files(tmp_path: Path):
    out = tmp_path / "preview"
    rendered = pr.render_all(out)
    assert (out / "index.html").exists()
    assert (out / "static" / "alibi.css").exists()
    for name in rendered:
        assert (out / f"{name}.html").exists()
