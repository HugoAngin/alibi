"""The wired app against team cases 1 and 2 with TestClient. No network, no Gemini, temp dirs.

Every holder and analyst route renders as HTML and as JSON; the approval, dispatch, recipient
isolation and verification rules hold end to end.
"""

from __future__ import annotations

import copy
import json
import re
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from agents.data.api import load_case
from alibi import api
from alibi.app import cases
from alibi.app.config import settings_from_env
from alibi.app.main import create_app
from alibi.core import canon

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
JSON = {"accept": "application/json"}
TOKEN_A = {"authorization": "Bearer demo-token-bank-a"}
TOKEN_B = {"authorization": "Bearer demo-token-bank-b"}


def make_client(tmp_path, case_number: int, **overrides) -> TestClient:
    values = {"case_number": case_number, "data_dir": tmp_path / "app", "key_dir": tmp_path / "keys",
              "fixed_now": NOW, **overrides}
    return TestClient(create_app(settings_from_env({}, **values)))


@pytest.fixture(params=[1, 2], ids=["case1", "case2"])
def client(request, tmp_path):
    cl = make_client(tmp_path, request.param)
    cl.case_number = request.param
    cl.rid = f"rfc-000{request.param}-01"
    cl.rid_b = f"rfc-000{request.param}-02"
    cl.txn = f"txn-000{request.param}-004"
    yield cl
    cl.app.state.runtime.close()


def run_approve(cl: TestClient, rid: str) -> dict:
    assert cl.post(f"/requests/{rid}/run", headers=JSON).status_code == 200
    detail = cl.get(f"/requests/{rid}", headers=JSON).json()
    ap = detail["review"]["approval"]
    r = cl.post(f"/requests/{rid}/approve", headers=JSON,
                data={"digest": ap["digest"], "audience": ap["audience"], "policy_version": ap["policy_version"]})
    assert r.status_code == 200, r.text
    return detail


def dispatch_ok(cl: TestClient, rid: str) -> dict:
    r = cl.post(f"/requests/{rid}/dispatch", headers=JSON)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["outcome"] == "success" and body["receipt"]["status"] == "accepted"
    return body


# --------------------------------------------------------------------------- rendering


HOLDER_ROUTES = ["/passport", "/alerts", "/rules", "/audit", "/requests/{rid}", "/requests/{rid}/dispatch",
                 "/txn/{txn}", "/requests/{rid}/export"]


def test_health_and_root(client):
    health = client.get("/health").json()
    assert health["status"] == "ok" and health["mode"] == "rules-only" and health["case"] == client.case_number
    assert client.get("/", follow_redirects=False).status_code == 303


def test_every_holder_route_renders_html_and_json(client):
    rid, txn = client.rid, client.txn
    run_approve(client, rid)
    dispatch_ok(client, rid)
    for route in HOLDER_ROUTES:
        path = route.format(rid=rid, txn=txn)
        html = client.get(path)
        assert html.status_code == 200, (path, html.text[:300])
        assert html.text.lstrip().lower().startswith("<!doctype html>"), path
        assert "None" not in re.sub(r"<[^>]+>", " ", html.text).split(), path
        data = client.get(path, headers=JSON)
        assert data.status_code == 200, path
        body = data.json()
        assert body["version"] == "alibi-ui/1" and body["template"].endswith(".html"), path
    requests_json = client.get("/requests", headers=JSON).json()
    assert {r["request_id"] for r in requests_json["requests"]} >= {rid, client.rid_b}


def test_every_analyst_route_renders_html_and_json(client):
    rid = client.rid
    run_approve(client, rid)
    pid = dispatch_ok(client, rid)["package_id"]
    for path in ["/bank/bank_a/inbox", f"/bank/bank_a/packages/{pid}"]:
        html = client.get(path, headers=TOKEN_A)
        assert html.status_code == 200, (path, html.text[:300])
        assert "panel--auth" in html.text or path.endswith("inbox")
        data = client.get(path, headers={**JSON, **TOKEN_A})
        assert data.status_code == 200 and data.json()["version"] == "alibi-ui/1"
    sent = client.get("/bank/bank_a/requests", headers={**JSON, **TOKEN_A}).json()
    assert sent["recipient"] == "bank_a" and sent["requests"][0]["request_id"] == rid


def test_passport_shows_history_before_any_request_and_hooks(client):
    page = client.get("/passport")
    html = page.text
    for hook in ('data-label="Booked"', 'class="when"', "table stack", 'id="transactions"'):
        assert hook in html, hook
    data = client.get("/passport", headers=JSON).json()
    assert data["evidence_status"]["total"] == 4
    assert any(c["kind"] == "merchant" and c["trust"] == "issuer_verified" for c in data["connections"])
    assert any(c["kind"] == "bank" and c["trust"] == "bank_observed" for c in data["connections"])
    statuses = {r["request_id"]: r["status"] for r in data["requests"]}
    assert statuses[client.rid] == "new" and statuses[client.rid_b] == "new"
    assert data["sharing_history"] == []


# --------------------------------------------------------------------------- investigation and review


def test_investigation_verdict_trace_and_lookup_marker(client):
    rid = client.rid
    run = client.post(f"/requests/{rid}/run", headers=JSON).json()
    expected = "supported" if client.case_number == 1 else "contradicted"
    assert run["claims"]["q.source_of_funds"] == expected and run["mode"] == "rules"
    detail = client.get(f"/requests/{rid}", headers=JSON).json()
    assert detail["investigation"]["mode_label"] == "rules-only"
    step = next(s for s in detail["trace"] if s["question_id"] == "q.source_of_funds")
    assert step["status"] == expected
    lookups = [lk["tool"] for lk in step["lookups"]]
    assert "lookup_settlement" in lookups
    settlement = next(s for s in step["sources"] if s["record_id"].endswith("-021"))
    assert settlement["via_lookup"] is not None
    assert step["lookups"][settlement["via_lookup"]]["tool"] == "lookup_settlement"
    assert step["calculation"]["expression"] == "240000 - 18000 - 6000 = 216000"
    assert [t["label"] for t in step["calculation"]["terms"]] == ["Sales", "Refunds", "Fees"]
    html = client.get(f"/requests/{rid}").text
    for hook in ("trace__step--question", "trace__step--lookups", "trace__step--calculation", "trace__step--status",
                 "review-group--included", "review-group--excluded", "review-group--generalised", "review-group--gaps",
                 "via lookup"):
        assert hook in html, hook
    assert "transaction-scoped" in html and client.txn in html


def test_transaction_page_reconciles_in_code(client):
    client.post(f"/requests/{client.rid}/run", headers=JSON)
    data = client.get(f"/txn/{client.txn}", headers=JSON).json()
    assert data["txn"]["link_state"] == "linked"
    recon = data["reconciliation"]
    assert recon["expression"] == "240000 - 18000 - 6000 = 216000" and recon["matches"] is True
    kinds = {s["kind"] for s in data["sources"]}
    assert {"payout", "settlement", "order", "refund", "fee"} <= kinds
    release = next(s for s in data["sources"] if s["record_id"].endswith("-020"))
    settlement = next(s for s in data["sources"] if s["record_id"].endswith("-021"))
    assert release["status"] == "released_to_balance" and settlement["via_lookup"] is True
    assert release["record_id"] != settlement["record_id"]
    html = client.get(f"/txn/{client.txn}").text
    assert "£2,160.00" in html and "216000 GBP" in html


def test_what_changed_shows_a_correction_as_a_newer_revision(client):
    rt = client.app.state.runtime
    case = load_case(client.case_number)
    client.post(f"/requests/{client.rid}/run", headers=JSON)
    refund = next(d["event"] for d in case["merchant_deliveries"] if d["event"]["kind"] == "refund")
    corrected = copy.deepcopy(refund)
    corrected.update({"event_id": refund["event_id"] + "-r2", "revision": 2, "amount_minor": 9000,
                      "correction_reason": "refund amount restated"})
    put = api.ingest_merchant_event(rt.store, cases.sign_event(cases.to_merchant_event(corrected), rt.keystore),
                                    registry=rt.registry, subject_ref=rt.subject_ref, now=NOW)
    assert put.status == "corrected"
    data = client.get(f"/txn/{client.txn}", headers=JSON).json()
    change = next(ch for ch in data["changes"] if ch["record_id"] == refund["record_id"])
    assert change["older"]["revision"] == 1 and change["newer"]["revision"] == 2
    assert any(d["field"] == "amount_pence" and d["after"] == "9000" for d in change["diff"])
    assert "restated" in change["effect"]
    ids = [s["record_id"] + "-r" + str(s["revision"]) for s in data["sources"]]
    assert f"{refund['record_id']}-r1" in ids and f"{refund['record_id']}-r2" in ids
    detail = client.get(f"/requests/{client.rid}", headers=JSON).json()
    assert detail["review"]["approval"]["status"] in ("invalidated", "pending")


# --------------------------------------------------------------------------- approval and dispatch


def test_dispatch_needs_an_approval(client):
    client.post(f"/requests/{client.rid}/run", headers=JSON)
    r = client.post(f"/requests/{client.rid}/dispatch", headers=JSON)
    assert r.status_code == 409 and r.json()["code"] == "approval_missing"
    html = client.post(f"/requests/{client.rid}/dispatch")
    assert html.status_code == 409 and "approval_missing" in html.text.replace("approval missing", "approval_missing")
    assert r.json()["reason"] in html.text


def test_approving_then_editing_the_body_makes_dispatch_refuse(client):
    rid = client.rid
    detail = run_approve(client, rid)
    item = next(i for i in detail["review"]["included"] if i["kind"] == "fee")
    removed = client.post(item["remove_action"], headers=JSON).json()
    assert removed["digest"] != detail["review"]["approval"]["digest"]
    r = client.post(f"/requests/{rid}/dispatch", headers=JSON)
    assert r.status_code == 409, r.text
    body = r.json()
    assert body["code"] == "approval_digest_mismatch"
    assert "changed after it was approved" in body["reason"]
    assert body["expected_digest"] == detail["review"]["approval"]["digest"] and body["actual_digest"] == removed["digest"]
    page = client.get(f"/requests/{rid}/dispatch")
    assert "result--refused" in page.text and body["reason"] in page.text.replace("&#39;", "'")
    assert client.get("/bank/bank_a/inbox", headers={**JSON, **TOKEN_A}).json()["packages"] == []
    fresh = client.get(f"/requests/{rid}", headers=JSON).json()["review"]["approval"]
    assert fresh["status"] == "pending" and fresh["digest"] == removed["digest"]
    r = client.post(f"/requests/{rid}/approve", headers=JSON,
                    data={"digest": fresh["digest"], "audience": fresh["audience"], "policy_version": fresh["policy_version"]})
    assert r.status_code == 200
    dispatch_ok(client, rid)


def test_stale_digest_and_wrong_audience_are_refused_at_approval(client):
    rid = client.rid
    client.post(f"/requests/{rid}/run", headers=JSON)
    ap = client.get(f"/requests/{rid}", headers=JSON).json()["review"]["approval"]
    stale = client.post(f"/requests/{rid}/approve", headers=JSON,
                        data={"digest": "sha256:" + "0" * 64, "audience": ap["audience"], "policy_version": ap["policy_version"]})
    assert stale.status_code == 409 and stale.json()["code"] == "approval_digest_not_current"
    wrong = client.post(f"/requests/{rid}/approve", headers=JSON,
                        data={"digest": ap["digest"], "audience": "bank_b", "policy_version": ap["policy_version"]})
    assert wrong.status_code == 409 and wrong.json()["code"] == "approval_audience_mismatch"


def test_retry_dispatch_does_not_create_a_second_delivery(client):
    run_approve(client, client.rid)
    first = dispatch_ok(client, client.rid)
    second = dispatch_ok(client, client.rid)
    assert second["package_id"] == first["package_id"] and second["receipt"]["receipt_id"] == first["receipt"]["receipt_id"]
    inbox = client.get("/bank/bank_a/inbox", headers={**JSON, **TOKEN_A}).json()
    assert len(inbox["packages"]) == 1
    history = client.get("/passport", headers=JSON).json()["sharing_history"]
    assert [h["package_id"] for h in history] == [first["package_id"]]


def test_correction_after_approval_invalidates_it_and_dispatch_refuses(client):
    rt = client.app.state.runtime
    case = load_case(client.case_number)
    run_approve(client, client.rid)
    sale = next(d["event"] for d in case["merchant_deliveries"] if d["event"]["kind"] == "sale")
    corrected = copy.deepcopy(sale)
    corrected.update({"event_id": sale["event_id"] + "-r2", "revision": 2, "amount_minor": sale["amount_minor"] + 100,
                      "correction_reason": "price adjusted"})
    api.ingest_merchant_event(rt.store, cases.sign_event(cases.to_merchant_event(corrected), rt.keystore),
                              registry=rt.registry, subject_ref=rt.subject_ref, now=NOW)
    r = client.post(f"/requests/{client.rid}/dispatch", headers=JSON)
    assert r.status_code == 409 and r.json()["code"] == "source_revision_changed", r.text


# --------------------------------------------------------------------------- export


def test_export_json_verifies_and_print_view_hides_excluded_items(client):
    rid = client.rid
    client.post(f"/requests/{rid}/run", headers=JSON)
    detail = client.get(f"/requests/{rid}", headers=JSON).json()
    victim = next(i for i in detail["review"]["included"] if i["kind"] == "order")
    client.post(victim["remove_action"], headers=JSON)
    fresh = client.get(f"/requests/{rid}", headers=JSON).json()
    assert any(e["record_id"] == victim["record_id"] for e in fresh["review"]["excluded"])
    ap = fresh["review"]["approval"]
    assert client.post(f"/requests/{rid}/approve", headers=JSON, data={"digest": ap["digest"], "audience": ap["audience"],
                                                                     "policy_version": ap["policy_version"]}).status_code == 200
    dispatch_ok(client, rid)
    raw = client.get(f"/requests/{rid}/export.json")
    assert raw.status_code == 200
    rt = client.app.state.runtime
    result = api.verify_package(raw.content, expected_audience="bank_a", registry=rt.registry, now=NOW,
                                expected_subject=rt.subject_ref, expected_request_id=rid)
    assert result.accepted, result.errors
    assert victim["record_id"] not in raw.text
    page = client.get(f"/requests/{rid}/export")
    assert page.status_code == 200
    assert victim["record_id"] not in page.text
    assert "print-footer" in page.text and "Redacted extract, attested by Alibi" in page.text
    data = client.get(f"/requests/{rid}/export", headers=JSON).json()
    assert data["package"]["signed"] is True and data["package"]["txn_ids"] == [client.txn]
    assert data["package"]["period"] is None
    assert all(a["record_id"] != victim["record_id"] for a in data["package"]["attachments"])


def test_export_before_dispatch_is_an_unsigned_draft(client):
    client.post(f"/requests/{client.rid}/run", headers=JSON)
    data = client.get(f"/requests/{client.rid}/export", headers=JSON).json()
    assert data["package"]["signed"] is False and data["package"]["signature"]["alg"] == "none"
    assert client.get(f"/requests/{client.rid}/export.json").status_code == 404


# --------------------------------------------------------------------------- bank side


def test_recipient_isolation(client):
    rid = client.rid
    run_approve(client, rid)
    pid = dispatch_ok(client, rid)["package_id"]
    assert client.get("/bank/bank_a/inbox", headers=JSON).status_code == 401
    mismatch = client.get("/bank/bank_a/inbox", headers={**JSON, **TOKEN_B})
    assert mismatch.status_code == 403 and mismatch.json()["code"] == "recipient_mismatch"
    assert client.get(f"/bank/bank_a/packages/{pid}", headers={**JSON, **TOKEN_B}).status_code == 403
    assert client.get(f"/bank/bank_b/packages/{pid}", headers={**JSON, **TOKEN_B}).status_code == 404
    assert client.get("/bank/bank_b/inbox", headers={**JSON, **TOKEN_B}).json()["packages"] == []
    assert client.get("/bank/bank_a/packages/x?token=demo-token-bank-a", headers=JSON).status_code == 401
    session = client.get("/bank/bank_a/inbox?token=demo-token-bank-a", follow_redirects=False)
    assert session.status_code == 303 and "alibi_bank" in session.headers.get("set-cookie", "")
    assert session.headers["location"] == "/bank/bank_a/inbox"      # the credential leaves the URL
    cross = client.post("/requests/x/dispatch", headers={**JSON, "origin": "http://evil.example"})
    assert cross.status_code == 403 and cross.json()["code"] == "cross_site_post"
    assert client.get("/bank/bank_a/inbox").status_code == 200   # cookie session
    assert client.get("/bank/bank_b/inbox").status_code == 403


def test_analyst_package_panels_timing_and_txn_scope(client):
    rid = client.rid
    run_approve(client, rid)
    pid = dispatch_ok(client, rid)["package_id"]
    data = client.get(f"/bank/bank_a/packages/{pid}", headers={**JSON, **TOKEN_A}).json()
    assert data["rejected"] is False and data["authentication"]["status"] == "valid"
    assert all(ch["result"] == "pass" for ch in data["authentication"]["checks"])
    assert data["package"]["period"] is None and data["package"]["txn_ids"] == [client.txn]
    q = next(e for e in data["evidence_by_question"] if e["question_id"] == "q.source_of_funds")
    timing = q["timing"]
    assert timing["payout_ref"].startswith("PO-000") and timing["released_to_balance_at"] == "2026-10-26T08:00:00Z"
    if client.case_number == 1:
        assert timing["bank_settled_at"] == "2026-10-27T16:00:00Z" and timing["settled_before_booking"] is True
        assert q["claim"]["status"] == "supported" and data["reconciliation"]["matches"] is True
    else:
        assert timing["bank_settled_at"] is None and timing["settlement_status"] == "pending"
        assert q["claim"]["status"] == "contradicted" and data["contradictions"]
    assert all(s["attestation"] in ("issuer_signature", "package_signer") for s in data["source_trust"])
    txn_row = next(s for s in data["source_trust"] if s["record_id"] == client.txn)
    assert txn_row["redacted"] is True and txn_row["attestation"] == "package_signer"
    html = client.get(f"/bank/bank_a/packages/{pid}", headers=TOKEN_A).text
    for hook in ("panel--auth", "panel--trust", "panel--claims", "transaction-scoped", client.txn,
                 "Redacted extract, attested by Alibi", 'name="decision"', 'name="reason"', 'name="question_ids"',
                 'name="requested_evidence"', 'name="scope"', 'name="txn_ids"'):
        assert hook in html, hook
    assert 'name="analyst_name"' not in html
    assert ">True<" not in html and ">true<" not in html


def test_rejected_package_shows_only_the_rejection(client):
    rid = client.rid
    run_approve(client, rid)
    dispatch_ok(client, rid)
    raw = client.get(f"/requests/{rid}/export.json").json()
    tampered = copy.deepcopy(raw)
    tampered["package_id"] = "pkg_tampered000000000000000"
    att = tampered["body"]["attachments"][0]
    att["content"]["amount_pence"] = (att["content"].get("amount_pence") or 0) + 1
    r = client.post("/bank/bank_a/receive", content=canon.canonical_bytes(tampered),
                    headers={"content-type": "application/json"})
    assert r.status_code == 422 and r.json()["accepted"] is False
    inbox = client.get("/bank/bank_a/inbox", headers={**JSON, **TOKEN_A}).json()
    row = next(p for p in inbox["packages"] if p["package_id"] == "pkg_tampered000000000000000")
    assert row["authentication"] == "invalid"
    data = client.get("/bank/bank_a/packages/pkg_tampered000000000000000", headers={**JSON, **TOKEN_A}).json()
    assert data["rejected"] is True and data["rejection"]["reasons"]
    assert data["source_trust"] == [] and data["evidence_by_question"] == []
    html = client.get("/bank/bank_a/packages/pkg_tampered000000000000000", headers=TOKEN_A).text
    assert "Rejected" in html and "rec-000" not in html and 'id="evidence-' not in html
    assert "panel--trust" not in html and 'name="decision"' not in html
    disp = client.post("/bank/bank_a/packages/pkg_tampered000000000000000/disposition", headers={**JSON, **TOKEN_A},
                       data={"decision": "clear", "reason": "x"})
    assert disp.status_code == 409 and disp.json()["code"] == "package_rejected"


def test_same_package_id_with_different_bytes_is_refused(client):
    run_approve(client, client.rid)
    first = dispatch_ok(client, client.rid)
    pkg = client.get(f"/requests/{client.rid}/export.json").json()
    again = client.post("/bank/bank_a/receive", content=canon.canonical_bytes(pkg))
    assert again.status_code == 200 and again.json()["receipt_id"] == first["receipt"]["receipt_id"]
    pkg["body"]["expires_at"] = "2027-01-01T00:00:00Z"
    reused = client.post("/bank/bank_a/receive", content=canon.canonical_bytes(pkg))
    assert reused.status_code == 409 and reused.json()["code"] == "package_id_reused"
    wrong_bank = client.post("/bank/bank_b/receive", content=canon.canonical_bytes(
        client.get(f"/requests/{client.rid}/export.json").json()))
    assert wrong_bank.status_code == 422
    assert any("addressed to bank_a" in r or "did not issue" in r for r in wrong_bank.json()["reasons"])


def test_disposition_and_follow_up_flow(client):
    rid = client.rid
    run_approve(client, rid)
    pid = dispatch_ok(client, rid)["package_id"]
    missing_reason = client.post(f"/bank/bank_a/packages/{pid}/disposition", headers={**JSON, **TOKEN_A},
                                 data={"decision": "clear", "reason": ""})
    assert missing_reason.status_code == 400
    disp = client.post(f"/bank/bank_a/packages/{pid}/disposition", headers={**JSON, **TOKEN_A},
                       data={"decision": "request_more", "reason": "Need the settlement record",
                             "question_ids": ["q.breakdown"], "requested_evidence": "settlement record for the payout"})
    assert disp.status_code == 200
    body = disp.json()
    assert body["analyst"] == "Analyst, Bank A" and body["decision"] == "request_more"
    assert body["question_ids"] == ["q.breakdown"] and body["supersedes"] is None
    assert body["location"].endswith("#followup")
    page = client.get(f"/bank/bank_a/packages/{pid}?disposition={body['disposition_id']}", headers={**JSON, **TOKEN_A}).json()
    assert page["followup_form"]["scope"] == "txn_ids" and page["followup_form"]["txn_ids"] == [client.txn]
    assert "settlement record" in page["followup_form"]["question_prefill"]
    assert page["disposition_form"]["current"]["decision"] == "request_more" and page["disposition_form"]["final"] is False
    wider = client.post("/bank/bank_a/requests", headers={**JSON, **TOKEN_A},
                        data={"parent_request_id": rid, "scope": "period", "period_from": "2026-01-01T00:00:00Z",
                              "period_to": "2026-11-01T00:00:00Z", "question": "x"})
    assert wider.status_code == 409 and wider.json()["code"] == "scope_wider_than_parent"
    follow = client.post("/bank/bank_a/requests", headers={**JSON, **TOKEN_A},
                         data={"parent_request_id": rid, "scope": "txn_ids", "txn_ids": "",
                               "question": "Please provide the settlement record for the payout.",
                               "disposition_id": body["disposition_id"]})
    assert follow.status_code == 201, follow.text
    new_id = follow.json()["request_id"]
    assert new_id == f"rfc-000{client.case_number}-03" and follow.json()["txn_ids"] == [client.txn]
    passport = client.get("/passport", headers=JSON).json()
    row = next(r for r in passport["requests"] if r["request_id"] == new_id)
    assert row["parent_request_id"] == rid and row["status"] == "new"
    inbox = client.get("/bank/bank_a/inbox", headers={**JSON, **TOKEN_A}).json()
    sent = next(r for r in inbox["requests_sent"] if r["request_id"] == new_id)
    assert sent["parent_request_id"] == rid and sent["status"] == "awaiting_consent"
    # the follow-up needs its own investigation and a fresh approval
    assert client.post(f"/requests/{new_id}/dispatch", headers=JSON).json()["code"] == "approval_missing"
    run_approve(client, new_id)
    second = dispatch_ok(client, new_id)
    assert second["package_id"] != pid
    final = client.post(f"/bank/bank_a/packages/{pid}/disposition", headers={**JSON, **TOKEN_A},
                        data={"decision": "clear", "reason": "resolved by follow-up"})
    assert final.status_code == 200
    again = client.post(f"/bank/bank_a/packages/{pid}/disposition", headers={**JSON, **TOKEN_A},
                        data={"decision": "escalate", "reason": "changed my mind"})
    assert again.status_code == 409 and again.json()["code"] == "disposition_final"


def test_second_recipient_reuses_the_passport_with_a_fresh_approval(client):
    rid_a, rid_b = client.rid, client.rid_b
    run_approve(client, rid_a)
    pid_a = dispatch_ok(client, rid_a)["package_id"]
    client.post(f"/requests/{rid_b}/run", headers=JSON)
    detail_b = client.get(f"/requests/{rid_b}", headers=JSON).json()
    assert detail_b["request"]["period"] is not None and detail_b["request"]["txn_ids"] == []
    assert client.post(f"/requests/{rid_b}/dispatch", headers=JSON).json()["code"] == "approval_missing"
    ap = detail_b["review"]["approval"]
    r = client.post(f"/requests/{rid_b}/approve", headers=JSON,
                    data={"digest": ap["digest"], "audience": ap["audience"], "policy_version": ap["policy_version"]})
    assert r.status_code == 200
    pid_b = dispatch_ok(client, rid_b)["package_id"]
    assert pid_b != pid_a
    inbox_b = client.get("/bank/bank_b/inbox", headers={**JSON, **TOKEN_B}).json()
    assert [p["package_id"] for p in inbox_b["packages"]] == [pid_b]
    inbox_a = client.get("/bank/bank_a/inbox", headers={**JSON, **TOKEN_A}).json()
    assert [p["package_id"] for p in inbox_a["packages"]] == [pid_a]
    history = client.get("/passport", headers=JSON).json()["sharing_history"]
    assert {(h["package_id"], h["audience"]) for h in history} == {(pid_a, "bank_a"), (pid_b, "bank_b")}
    data = client.get(f"/bank/bank_b/packages/{pid_b}", headers={**JSON, **TOKEN_B}).json()
    assert data["package"]["period"]["from"] == "2026-10-01T00:00:00Z"
    connections_before = client.get("/passport", headers=JSON).json()["connections"]
    assert sum(c["record_count"] for c in connections_before if c["kind"] == "merchant") == 21  # 20 pushed + 1 lookup


# --------------------------------------------------------------------------- alerts, rules, audit, replay


def test_alerts_tick_and_merchant_lookup(client):
    r = client.post("/tick", headers=JSON).json()
    assert r["raised"] == [] and len(r["kept"]) == 1       # the start-up tick already raised it
    alerts = client.get("/alerts", headers=JSON).json()["alerts"]
    alert = alerts[0]
    assert alert["kind"] == "unexplained_credit" and alert["status"] == "open" and alert["created_at"] == "2026-10-29T07:30:00Z"
    assert any(f["source_id"] == client.txn for f in alert["facts"])
    assert any("PO-000" in check for check in alert["suggested_checks"])
    assert client.post("/tick", headers=JSON).json()["raised"] == []     # idempotent
    client.post(f"/requests/{client.rid}/run", headers=JSON)
    after = client.post("/tick", headers=JSON).json()
    alerts = client.get("/alerts", headers=JSON).json()["alerts"]
    if client.case_number == 1:
        assert after["resolved"] == [alert["alert_id"]] and alerts[0]["status"] == "resolved"
    else:
        assert after["resolved"] == [] and alerts[0]["status"] == "open"
    lookup = client.post("/alerts/lookup", data={"query": "Payflux Holdings"}, headers=JSON).json()
    assert lookup["merchant_lookup"]["result"]["status"] == "unverified"
    html = client.post("/alerts/lookup", data={"query": "Payflux Holdings"}).text
    assert "Unverified" in html and "not fraudulent" in html
    known = client.post("/alerts/lookup", data={"query": "Synthetic Grocer"}, headers=JSON).json()
    assert known["merchant_lookup"]["result"]["status"] == "verified"


def test_rules_interpret_confirm_hold_and_retire(client):
    r = client.post("/rules/interpret", data={"text": "don't share item names"}, headers=JSON).json()
    assert r["status"] == "resolved"
    page = client.get(f"/rules?interp={r['rule_id']}", headers=JSON).json()
    assert page["interpretation"]["policy"]["kind"] == "deny"
    assert "Item names will not be shared" in page["interpretation"]["plain_lines"][0]
    html = client.get(f"/rules?interp={r['rule_id']}")
    assert "Confirm this interpretation" in html.text
    confirmed = client.post(f"/rules/{r['rule_id']}/confirm", headers=JSON).json()
    assert confirmed["rules"] == 1
    active = client.get("/rules", headers=JSON).json()["active_rules"]
    assert active[0]["kind"] == "deny" and active[0]["status"] == "active"
    held = client.post("/rules/interpret", data={"text": "share everything with everyone"}, headers=JSON).json()
    assert held["status"] == "held"
    page = client.get("/rules", headers=JSON).json()
    assert page["held_rules"][0]["reason"].startswith("Policy rules can only restrict")
    assert any(row["status"] == "held" for row in client.get("/passport", headers=JSON).json()["rules"])
    # the policy applies to the next review: item descriptions are withheld
    client.post(f"/requests/{client.rid}/run", headers=JSON)
    detail = client.get(f"/requests/{client.rid}", headers=JSON).json()
    assert any(w["field"] == "items.description" for w in detail["review"]["withheld_fields"])
    assert detail["review"]["approval"]["policy_version"].endswith("@v1")
    retired = client.post(f"/rules/{r['rule_id']}/retire", headers=JSON).json()
    assert retired["rules"] == 0 and retired["policy_version"].endswith("@v2")
    client.post(f"/rules/held/{held['rule_id']}/discard", headers=JSON)
    assert client.get("/rules", headers=JSON).json()["held_rules"] == []


def test_audit_lists_refusals_and_bank_entries_stay_with_the_bank(client):
    run_approve(client, client.rid)
    client.post(f"/requests/{client.rid}/dispatch", headers=JSON)
    client.get("/bank/bank_a/inbox", headers=TOKEN_B)
    entries = client.get("/audit", headers=JSON).json()["entries"]
    actions_seen = {e["action"] for e in entries}
    assert {"receive_rfc", "run_investigation", "approve", "sign_and_package", "dispatch"} <= actions_seen
    # bank-side entries (receipts, dispositions, refused access) are the bank's, never the holder's
    assert not {"package.received", "access.refused", "disposition.recorded"} & actions_seen
    bank_audit = client.get("/bank/bank_a/audit", headers={**JSON, **TOKEN_A}).json()["entries"]
    assert {"package.received"} <= {e["action"] for e in bank_audit}
    bank_b_audit = client.get("/bank/bank_b/audit", headers={**JSON, **TOKEN_B}).json()["entries"]
    assert any(e["action"] == "access.refused" and e["outcome"] == "refused" for e in bank_b_audit)
    assert client.get("/bank/bank_a/audit", headers=JSON).status_code == 401
    filtered = client.get("/audit?actor=alibi", headers=JSON).json()
    assert filtered["entries"] and all(e["actor"] == "alibi" for e in filtered["entries"])
    html = client.get("/audit?q=dispatch")
    assert html.status_code == 200 and "dispatch" in html.text
    refused_page = client.get("/bank/bank_b/inbox", headers={**TOKEN_A, "accept": "text/html"})
    assert refused_page.status_code == 403 and "recipient_mismatch" in refused_page.text


def test_replay_banner_when_enabled(tmp_path):
    cl = make_client(tmp_path, 1, replay=True)
    try:
        assert "banner--replay" in cl.get("/passport").text
        assert "banner--replay" in cl.get("/audit").text
        assert cl.get("/passport", headers=JSON).json()["replay_mode"] is True
    finally:
        cl.app.state.runtime.close()


def test_restart_keeps_history(tmp_path):
    cl = make_client(tmp_path, 1)
    run_approve(cl, "rfc-0001-01")
    pid = dispatch_ok(cl, "rfc-0001-01")["package_id"]
    cl.app.state.runtime.close()
    again = make_client(tmp_path, 1)
    try:
        passport = again.get("/passport", headers=JSON).json()
        assert [h["package_id"] for h in passport["sharing_history"]] == [pid]
        assert {r["request_id"]: r["status"] for r in passport["requests"]}["rfc-0001-01"] == "sent"
        inbox = again.get("/bank/bank_a/inbox", headers={**JSON, **TOKEN_A}).json()
        assert [p["package_id"] for p in inbox["packages"]] == [pid]
        assert again.get(f"/txn/txn-0001-004", headers=JSON).json()["txn"]["link_state"] == "linked"
    finally:
        again.app.state.runtime.close()


def test_refusal_bodies_are_code_and_reason(client):
    r = client.post(f"/requests/{client.rid}/dispatch", headers=JSON)
    assert set(r.json()) >= {"code", "reason"} and r.status_code == 409
    assert client.get("/requests/nope", headers=JSON).status_code == 404
    assert client.get("/txn/nope").status_code == 404


def test_dispatch_over_http_to_the_configured_bank_url(tmp_path):
    """With ALIBI_BANK_URL set, dispatch POSTs the signed bytes to {url}/bank/{audience}/receive.
    The TestClient's post stands in for httpx.post so the request still reaches a bank service."""
    from alibi.app import actions

    # One pinned demo trust registry for both parties, as in the demo (data/alibi/keys).
    holder = make_client(tmp_path / "holder", 1, bank_url="http://bank.test", key_dir=tmp_path / "keys")
    bank = make_client(tmp_path / "bank", 1, key_dir=tmp_path / "keys")
    try:
        rt = holder.app.state.runtime
        run_approve(holder, "rfc-0001-01")
        calls = []

        def post(url, **kwargs):
            calls.append(url)
            return bank.post(url, content=kwargs["content"], headers=kwargs["headers"])

        deliver = actions.deliver_over_http("http://bank.test", post=post)
        result = actions.dispatch(rt, "rfc-0001-01", deliver)
        assert result["outcome"] == "success" and calls == ["http://bank.test/bank/bank_a/receive"]
        inbox = bank.get("/bank/bank_a/inbox", headers={**JSON, **TOKEN_A}).json()
        assert [p["package_id"] for p in inbox["packages"]] == [result["package_id"]]
        assert holder.get("/health").json()["bank_url"] == "http://bank.test"
        down = actions.deliver_over_http("http://127.0.0.1:9", timeout=0.5)
        status, body = down("bank_a", {"x": 1})
        assert status == 503 and body["code"] == "bank_unreachable"
    finally:
        holder.app.state.runtime.close()
        bank.app.state.runtime.close()


# --------------------------------------------------------------------------- regressions from the adversarial review


def test_rerun_keeps_the_holders_removals(client):
    rid = client.rid
    client.post(f"/requests/{rid}/run", headers=JSON)
    detail = client.get(f"/requests/{rid}", headers=JSON).json()
    victim = next(i for i in detail["review"]["included"] if i["kind"] == "fee")
    client.post(victim["remove_action"], headers=JSON)
    client.post(f"/requests/{rid}/run", headers=JSON)           # a rerun must not re-include it
    after = client.get(f"/requests/{rid}", headers=JSON).json()
    assert all(i["record_id"] != victim["record_id"] for i in after["review"]["included"])
    assert any(e["record_id"] == victim["record_id"] for e in after["review"]["excluded"])
    restored = client.post(f"/requests/{rid}/restore", headers=JSON).json()
    page = client.get(f"/requests/{rid}", headers=JSON).json()
    assert page["review"]["approval"]["digest"] == restored["digest"]
    assert any(i["record_id"] == victim["record_id"] for i in page["review"]["included"])


def test_failed_delivery_is_not_shown_as_sent(tmp_path):
    from alibi.app import actions
    cl = make_client(tmp_path, 1)
    try:
        rt = cl.app.state.runtime
        run_approve(cl, "rfc-0001-01")
        down = actions.deliver_over_http("http://127.0.0.1:9", timeout=0.3)
        with pytest.raises(actions.Refusal) as info:
            actions.dispatch(rt, "rfc-0001-01", down)
        assert info.value.code == "bank_unreachable"
        passport = cl.get("/passport", headers=JSON).json()
        assert {r["request_id"]: r["status"] for r in passport["requests"]}["rfc-0001-01"] == "refused"
        assert [h["status"] for h in passport["sharing_history"]] == ["failed"]
        page = cl.get("/requests/rfc-0001-01/dispatch", headers=JSON).json()
        assert page["dispatch"]["outcome"] == "refused" and "could not be reached" in page["dispatch"]["refusal"]["reason"]
        # the retry with a working bank reuses the same signed package
        result = actions.dispatch(rt, "rfc-0001-01", actions.deliver_in_process(rt))
        assert result["outcome"] == "success"
        assert [h["status"] for h in cl.get("/passport", headers=JSON).json()["sharing_history"]] == ["acknowledged"]
    finally:
        cl.app.state.runtime.close()


def test_correction_marks_the_investigation_out_of_date_and_restore_refuses_cleanly(client):
    rt = client.app.state.runtime
    case = load_case(client.case_number)
    client.post(f"/requests/{client.rid}/run", headers=JSON)
    fee = next(d["event"] for d in case["merchant_deliveries"] if d["event"]["kind"] == "fee")
    corrected = copy.deepcopy(fee)
    corrected.update({"event_id": fee["event_id"] + "-r2", "revision": 2, "amount_minor": fee["amount_minor"] + 1,
                      "correction_reason": "fee restated"})
    api.ingest_merchant_event(rt.store, cases.sign_event(cases.to_merchant_event(corrected), rt.keystore),
                              registry=rt.registry, subject_ref=rt.subject_ref, now=NOW)
    detail = client.get(f"/requests/{client.rid}", headers=JSON).json()
    assert detail["investigation"]["status"] == "out_of_date"
    assert any("out of date" in f["text"] for f in detail["flashes"])
    step = next(s for s in detail["trace"] if s["question_id"] == "q.source_of_funds")
    assert step["state"] == "invalidated" and step["status_reason"].startswith("INVALIDATED")
    r = client.post(f"/requests/{client.rid}/restore", headers=JSON)
    assert r.status_code == 409 and r.json()["code"] == "review_failed"
    client.post(f"/requests/{client.rid}/run", headers=JSON)
    assert client.get(f"/requests/{client.rid}", headers=JSON).json()["investigation"]["status"] == "complete"


def test_period_package_reconciles_against_the_claimed_total(client):
    rid_b = client.rid_b
    client.post(f"/requests/{rid_b}/run", headers=JSON)
    ap = client.get(f"/requests/{rid_b}", headers=JSON).json()["review"]["approval"]
    client.post(f"/requests/{rid_b}/approve", headers=JSON,
                data={"digest": ap["digest"], "audience": ap["audience"], "policy_version": ap["policy_version"]})
    pid = dispatch_ok(client, rid_b)["package_id"]
    data = client.get(f"/bank/bank_b/packages/{pid}", headers={**JSON, **TOKEN_B}).json()
    recon = data["reconciliation"]
    # Bank B's combined question is unresolved under the rules engine, so nothing is attached:
    # the panel must say so instead of showing a red mismatch against a made-up target.
    if not recon["lines"]:
        assert recon["expression"].startswith("nothing to reconcile")
        html = client.get(f"/bank/bank_b/packages/{pid}", headers=TOKEN_B).text
        assert "nothing to reconcile" in html and "does not match" not in html
    else:
        assert recon["compared_to"]["label"].startswith("Claimed total") and recon["matches"] is True


def test_reapproving_a_sent_body_is_refused_not_celebrated(client):
    run_approve(client, client.rid)
    dispatch_ok(client, client.rid)
    ap = client.get(f"/requests/{client.rid}", headers=JSON).json()["review"]["approval"]
    r = client.post(f"/requests/{client.rid}/approve", headers=JSON,
                    data={"digest": ap["digest"], "audience": ap["audience"], "policy_version": ap["policy_version"]})
    assert r.status_code == 409 and r.json()["code"] == "approval_consumed"


def test_confirming_a_rule_rebuilds_the_draft(client):
    client.post(f"/requests/{client.rid}/run", headers=JSON)
    before = client.get(f"/requests/{client.rid}", headers=JSON).json()["review"]
    r = client.post("/rules/interpret", data={"text": "don't share item names"}, headers=JSON).json()
    confirmed = client.post(f"/rules/{r['rule_id']}/confirm", headers=JSON).json()
    assert client.rid in confirmed["rebuilt"]
    after = client.get(f"/requests/{client.rid}", headers=JSON).json()["review"]
    assert after["draft_status"] == "pending" and after["approval"]["status"] == "pending"
    assert after["approval"]["policy_version"].endswith("@v1") and after["approval"]["digest"] != before["approval"]["digest"]
    assert any(w["field"] == "items.description" for w in after["withheld_fields"])
    ap = after["approval"]
    ok = client.post(f"/requests/{client.rid}/approve", headers=JSON,
                     data={"digest": ap["digest"], "audience": ap["audience"], "policy_version": ap["policy_version"]})
    assert ok.status_code == 200


def test_rejected_package_exposes_nothing_in_json_or_inbox(client):
    run_approve(client, client.rid)
    dispatch_ok(client, client.rid)
    raw = client.get(f"/requests/{client.rid}/export.json").json()
    raw["package_id"] = "pkg_tampered000000000000001"
    raw["body"]["expires_at"] = "2027-01-01T00:00:00Z"
    assert client.post("/bank/bank_a/receive", content=canon.canonical_bytes(raw)).status_code == 422
    data = client.get("/bank/bank_a/packages/pkg_tampered000000000000001", headers={**JSON, **TOKEN_A}).json()
    assert data["package"]["txn_ids"] == [] and data["package"]["period"] is None
    assert data["package"]["issued_at"] == "—" and data["package"]["expires_at"] == "—"
    row = next(p for p in client.get("/bank/bank_a/inbox", headers={**JSON, **TOKEN_A}).json()["packages"]
               if p["package_id"] == "pkg_tampered000000000000001")
    assert row["claim_counts"] == {"supported": 0, "contradicted": 0, "unresolved": 0} and row["expires_at"] == "—"
    assert client.post("/bank/bank_a/receive", content=b"not json").status_code == 400
    assert client.post("/bank/bank_a/receive", content=b'{"package_id": "x"}').status_code == 400


def test_source_page_and_404_escape_markup(client):
    client.post(f"/requests/{client.rid}/run", headers=JSON)
    page = client.get(f"/sources/rec-000{client.case_number}-020/1")
    assert page.status_code == 200 and "<script" not in page.text and "Good news!" in page.text
    evil = client.get("/txn/%3Cimg%20src%3Dx%20onerror%3Dalert(1)%3E")
    assert evil.status_code == 404 and "<img" not in evil.text and "&lt;img" in evil.text
    held = client.post("/rules/interpret", data={"text": "share everything & more with everyone"}, headers=JSON).json()
    clarify = client.post(f"/rules/held/{held['rule_id']}/clarify", follow_redirects=False)
    assert clarify.status_code == 303 and "%26" in clarify.headers["location"]


def test_alerts_exist_at_start_and_the_page_has_a_tick_form(client):
    data = client.get("/alerts", headers=JSON).json()
    assert data["alerts"] and data["alerts"][0]["kind"] == "unexplained_credit"
    assert client.get("/passport", headers=JSON).json()["alerts"]
    html = client.get("/alerts").text
    assert 'action="/tick"' in html and "Re-run checks now" in html


def test_sent_request_cannot_be_rerun_or_reapproved_without_a_correction(client):
    run_approve(client, client.rid)
    dispatch_ok(client, client.rid)
    detail = client.get(f"/requests/{client.rid}", headers=JSON).json()
    assert detail["investigation"]["can_run"] is False and detail["request"]["status"] == "sent"
    r = client.post(f"/requests/{client.rid}/run", headers=JSON)
    assert r.status_code == 409 and r.json()["code"] == "request_already_answered"
    assert len(client.get("/passport", headers=JSON).json()["sharing_history"]) == 1


def test_expired_request_is_refused_server_side(tmp_path):
    late = datetime(2026, 11, 12, 0, 0, tzinfo=timezone.utc)
    cl = make_client(tmp_path, 1, fixed_now=late)
    try:
        passport = cl.get("/passport", headers=JSON).json()
        assert {r["request_id"]: r["status"] for r in passport["requests"]}["rfc-0001-01"] == "expired"
        r = cl.post("/requests/rfc-0001-01/run", headers=JSON)
        assert r.status_code == 409 and r.json()["code"] == "request_expired"
        a = cl.post("/requests/rfc-0001-01/approve", headers=JSON,
                    data={"digest": "sha256:" + "0" * 64, "audience": "bank_a", "policy_version": "x"})
        assert a.status_code == 409 and a.json()["code"] == "request_expired"
        assert cl.get("/requests/rfc-0001-01").status_code == 200
    finally:
        cl.app.state.runtime.close()


def test_remove_unknown_item_and_bad_inputs_answer_with_code_and_reason(client):
    client.post(f"/requests/{client.rid}/run", headers=JSON)
    r = client.post(f"/requests/{client.rid}/remove/record/nope", headers=JSON)
    assert r.status_code == 404 and r.json()["code"] == "item_not_in_draft"
    bad = client.get("/sources/rec-0001-001/abc", headers=JSON)
    assert bad.status_code == 400 and bad.json()["code"] == "bad_request"
    run_approve(client, client.rid)
    dispatch_ok(client, client.rid)
    period = client.post("/bank/bank_a/requests", headers={**JSON, **TOKEN_A},
                         data={"parent_request_id": client.rid_b, "scope": "period", "period_from": "2026-10-10",
                               "period_to": "2026-10-20", "question": "x"})
    assert period.status_code in (400, 404, 409)
    if period.status_code == 400:
        assert "validation error" not in period.json()["reason"]


def test_date_only_alibi_now_is_accepted_and_bad_values_fail_clearly():
    from alibi.app.config import settings_from_env
    assert settings_from_env({"ALIBI_NOW": "2026-11-05"}).fixed_now == datetime(2026, 11, 5, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="ALIBI_NOW must be"):
        settings_from_env({"ALIBI_NOW": "yesterday"})
