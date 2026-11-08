"""Track 1f: the app as a JSON backend for the Astro front end (``web/docs/backend-integration.md``).

Case 1 is driven end to end in JSON mode only: ``Accept: application/json`` on every call and
every action's fields sent as a JSON object. No network, no Gemini, temp dirs.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from alibi import api
from alibi.app.config import DEFAULT_API_ORIGIN, settings_from_env
from alibi.app.main import create_app

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
JSON = {"accept": "application/json"}
TOKEN_A = {"authorization": "Bearer demo-token-bank-a"}
TOKEN_B = {"authorization": "Bearer demo-token-bank-b"}
RID, RID_B, TXN = "rfc-0001-01", "rfc-0001-02", "txn-0001-004"
ORIGIN = "https://api.alibi.test"


def make_client(tmp_path, **overrides) -> TestClient:
    values = {"case_number": 1, "data_dir": tmp_path / "app", "key_dir": tmp_path / "keys",
              "fixed_now": NOW, "api_origin": ORIGIN, **overrides}
    return TestClient(create_app(settings_from_env({}, **values)))


@pytest.fixture
def client(tmp_path):
    cl = make_client(tmp_path)
    yield cl
    cl.app.state.runtime.close()


def refusal(response, status: int, code: str) -> dict:
    body = response.json()
    assert response.status_code == status, response.text
    assert body["code"] == code and body["reason"] and body["version"] == "alibi-ui/1", body
    return body


# --------------------------------------------------------------------------- health


def test_health_reports_api_origin(tmp_path):
    with make_client(tmp_path) as cl:
        health = cl.get("/health").json()
        assert health["api_origin"] == ORIGIN and health["version"] == "alibi-ui/1"
        assert health["holder_auth"] == "demo"
        cl.app.state.runtime.close()
    assert settings_from_env({}).api_origin == DEFAULT_API_ORIGIN == "http://127.0.0.1:8100"
    assert settings_from_env({"ALIBI_API_ORIGIN": "https://backend.example/"}).api_origin == "https://backend.example"
    assert settings_from_env({"ALIBI_HOLDER_TOKEN": " "}).holder_token is None


# --------------------------------------------------------------------------- case 1, JSON only


def test_case_one_end_to_end_in_json_mode(client):
    # run the investigation (an empty JSON object is a valid body for a field-less action)
    run = client.post(f"/requests/{RID}/run", headers=JSON, json={})
    assert run.status_code == 200, run.text
    assert run.json()["ok"] is True and run.json()["version"] == "alibi-ui/1" and run.json()["claims"]

    detail = client.get(f"/requests/{RID}", headers=JSON).json()
    assert detail["version"] == "alibi-ui/1" and detail["template"] == "requests/detail.html"
    stale = detail["review"]["approval"]
    victim = next(i for i in detail["review"]["included"] if i["kind"] == "order")

    # remove an item: the body changes, so the digest the holder saw is no longer current
    removed = client.post(victim["remove_action"], headers=JSON, json={})
    assert removed.status_code == 200 and removed.json()["ok"] is True
    assert removed.json()["digest"] != stale["digest"]

    stale_approval = client.post(f"/requests/{RID}/approve", headers=JSON,
                                 json={"digest": stale["digest"], "audience": stale["audience"],
                                       "policy_version": stale["policy_version"]})
    body = refusal(stale_approval, 409, "approval_digest_not_current")
    assert body["expected_digest"] == removed.json()["digest"] and body["actual_digest"] == stale["digest"]
    assert body["reason"].startswith("approval refused:")

    fresh = client.get(f"/requests/{RID}", headers=JSON).json()["review"]["approval"]
    approved = client.post(f"/requests/{RID}/approve", headers=JSON,
                           json={"digest": fresh["digest"], "audience": fresh["audience"],
                                 "policy_version": fresh["policy_version"]})
    assert approved.status_code == 200, approved.text
    assert approved.json()["ok"] is True and approved.json()["audience"] == "bank_a"

    # dispatch: the result carries absolute links on ALIBI_API_ORIGIN
    sent = client.post(f"/requests/{RID}/dispatch", headers=JSON, json={})
    assert sent.status_code == 200, sent.text
    result = sent.json()
    pid = result["package_id"]
    assert result["ok"] is True and result["outcome"] == "success" and result["receipt"]["status"] == "accepted"
    assert result["package_url"] == f"{ORIGIN}/bank/bank_a/packages/{pid}"
    assert result["export_url"] == f"{ORIGIN}/requests/{RID}/export.json"
    page = client.get(f"/requests/{RID}/dispatch", headers=JSON).json()
    assert page["dispatch"]["package_url"] == result["package_url"]

    # bank_a reads its inbox and the package with its bearer token
    inbox = client.get("/bank/bank_a/inbox", headers={**JSON, **TOKEN_A}).json()
    assert inbox["version"] == "alibi-ui/1" and [p["package_id"] for p in inbox["packages"]] == [pid]
    package = client.get(f"/bank/bank_a/packages/{pid}", headers={**JSON, **TOKEN_A}).json()
    assert package["template"] == "analyst/package.html" and package["package"]["package_id"] == pid

    # disposition and follow-up as JSON objects (lists stay lists)
    disp = client.post(f"/bank/bank_a/packages/{pid}/disposition", headers={**JSON, **TOKEN_A},
                       json={"decision": "request_more", "reason": "Need the settlement record",
                             "question_ids": ["q.breakdown"],
                             "requested_evidence": ["settlement record for the payout", "bank reference"]})
    assert disp.status_code == 200, disp.text
    assert disp.json()["ok"] is True and disp.json()["decision"] == "request_more"
    assert disp.json()["question_ids"] == ["q.breakdown"]
    assert disp.json()["requested_evidence"] == ["settlement record for the payout", "bank reference"]
    follow = client.post("/bank/bank_a/requests", headers={**JSON, **TOKEN_A},
                         json={"parent_request_id": RID, "scope": "txn_ids", "txn_ids": [TXN],
                               "question": "Please provide the settlement record for the payout.",
                               "disposition_id": disp.json()["disposition_id"]})
    assert follow.status_code == 201, follow.text
    assert follow.json()["ok"] is True and follow.json()["request_id"] == "rfc-0001-03"
    assert follow.json()["txn_ids"] == [TXN]
    wider = client.post("/bank/bank_a/requests", headers={**JSON, **TOKEN_A},
                        json={"parent_request_id": RID, "scope": "period",
                              "period_from": "2026-01-01T00:00:00Z", "period_to": "2026-11-01T00:00:00Z",
                              "question": "x"})
    refusal(wider, 409, "scope_wider_than_parent")

    # bank_b is refused on bank_a's package: the URL never grants access
    refusal(client.get(f"/bank/bank_a/packages/{pid}", headers={**JSON, **TOKEN_B}), 403, "recipient_mismatch")
    refusal(client.get(f"/bank/bank_b/packages/{pid}", headers={**JSON, **TOKEN_B}), 404, "not_found")
    refusal(client.post(f"/bank/bank_a/packages/{pid}/disposition", headers={**JSON, **TOKEN_B},
                        json={"decision": "clear", "reason": "x"}), 403, "recipient_mismatch")

    # the export verifies and the export page links the file on the published origin
    raw = client.get(f"/requests/{RID}/export.json")
    assert raw.status_code == 200
    rt = client.app.state.runtime
    verified = api.verify_package(raw.content, expected_audience="bank_a", registry=rt.registry, now=NOW,
                                  expected_subject=rt.subject_ref, expected_request_id=RID)
    assert verified.accepted, verified.errors
    assert victim["record_id"] not in raw.text
    export = client.get(f"/requests/{RID}/export", headers=JSON).json()
    assert export["export"]["json_url"] == f"{ORIGIN}/requests/{RID}/export.json"
    assert export["export"]["json_href"] == f"/requests/{RID}/export.json"


def test_json_and_form_refusals_are_identical(client):
    client.post(f"/requests/{RID}/run", headers=JSON, json={})
    bad = {"digest": "sha256:" + "0" * 64, "audience": "bank_a", "policy_version": "x"}
    as_json = client.post(f"/requests/{RID}/approve", headers=JSON, json=bad).json()
    as_form = client.post(f"/requests/{RID}/approve", headers=JSON, data=bad).json()
    assert as_json == as_form and as_json["code"] == "approval_digest_not_current"
    fresh = client.get(f"/requests/{RID}", headers=JSON).json()["review"]["approval"]
    client.post(f"/requests/{RID}/approve", headers=JSON, json={"digest": fresh["digest"], "audience": "bank_a",
                                                                "policy_version": fresh["policy_version"]})
    pid = client.post(f"/requests/{RID}/dispatch", headers=JSON, json={}).json()["package_id"]
    j = client.post(f"/bank/bank_a/packages/{pid}/disposition", headers={**JSON, **TOKEN_A},
                    json={"decision": "clear", "reason": ""})
    f = client.post(f"/bank/bank_a/packages/{pid}/disposition", headers={**JSON, **TOKEN_A},
                    data={"decision": "clear", "reason": ""})
    assert j.status_code == f.status_code == 400 and j.json() == f.json()
    assert j.json()["code"] == "reason_required"
    # a form field posted as another JSON type is read as text, with the same validation
    odd = client.post(f"/bank/bank_a/packages/{pid}/disposition", headers={**JSON, **TOKEN_A},
                      json={"decision": "clear", "reason": None})
    refusal(odd, 400, "reason_required")


def test_malformed_json_bodies_are_bad_requests(client):
    headers = {**JSON, "content-type": "application/json"}
    refusal(client.post(f"/requests/{RID}/approve", headers=headers, content=b"[1, 2]"), 400, "bad_request")
    refusal(client.post(f"/requests/{RID}/approve", headers=headers, content=b"{not json"), 400, "bad_request")
    refusal(client.post("/rules/interpret", headers=headers, content=b'"text"'), 400, "bad_request")
    held = client.post("/rules/interpret", headers=JSON, json={"text": ""})
    assert held.status_code == 200 and held.json()["status"] == "held"


def test_rules_and_alerts_actions_take_json(client):
    interp = client.post("/rules/interpret", headers=JSON, json={"text": "Never share item descriptions."})
    assert interp.status_code == 200, interp.text
    body = interp.json()
    assert body["ok"] is True
    if body["status"] == "resolved":
        confirmed = client.post(f"/rules/{body['rule_id']}/confirm", headers=JSON, json={})
        assert confirmed.status_code == 200 and confirmed.json()["policy_version"].endswith("@v1")
        retired = client.post(f"/rules/{body['rule_id']}/retire", headers=JSON, json={})
        assert retired.status_code == 200 and retired.json()["rules"] == 0
        refusal(client.post(f"/rules/{body['rule_id']}/confirm", headers=JSON, json={}), 404, "rule_not_pending")
    else:
        clarify = client.post(f"/rules/held/{body['rule_id']}/clarify", headers=JSON, json={})
        assert clarify.status_code == 200 and clarify.json()["text"] == "Never share item descriptions."
    lookup = client.post("/alerts/lookup", headers=JSON, json={"query": "Threadline"})
    assert lookup.status_code == 200
    assert lookup.json()["ok"] is True and lookup.json()["lookup"]["query"] == "Threadline"
    assert lookup.json()["version"] == "alibi-ui/1" and lookup.json()["template"] == "passport/alerts.html"
    tick = client.post("/tick", headers=JSON, json={})
    assert tick.status_code == 200 and tick.json()["ok"] is True and "raised" in tick.json()


def test_every_get_route_answers_json_with_a_version(client):
    client.post(f"/requests/{RID}/run", headers=JSON, json={})
    detail = client.get(f"/requests/{RID}", headers=JSON).json()
    source = next(i for i in detail["review"]["included"] if i["kind"] == "order")
    paths = ["/passport", "/alerts", "/rules", "/audit", "/requests", f"/requests/{RID}", f"/requests/{RID}/dispatch",
             f"/requests/{RID}/export", f"/txn/{TXN}", f"/sources/{source['record_id']}/{source['revision']}"]
    for path in paths:
        r = client.get(path, headers=JSON)
        assert r.status_code == 200 and r.json()["version"] == "alibi-ui/1" and "template" in r.json(), path
    for path in ["/bank/bank_a/inbox", "/bank/bank_a/requests", "/bank/bank_a/audit"]:
        r = client.get(path, headers={**JSON, **TOKEN_A})
        assert r.status_code == 200 and r.json()["version"] == "alibi-ui/1", path
    assert client.get("/requests", headers=JSON).json()["requests"][0]["request_id"] == RID


# --------------------------------------------------------------------------- same-origin rule


def test_cross_site_rule_blocks_browser_forms_not_api_clients(client):
    evil = {"origin": "https://evil.example"}
    # a cross-site browser form post (cookie session, no bearer, no JSON body) is refused
    refusal(client.post(f"/requests/{RID}/run", headers={**JSON, **evil}, data={}), 403, "cross_site_post")
    refusal(client.post(f"/requests/{RID}/run", headers={"sec-fetch-site": "cross-site"}, data={}),
            403, "cross_site_post")
    # a JSON body or a bearer token marks an API client: the credential, not the origin, decides
    run = client.post(f"/requests/{RID}/run", headers={**JSON, **evil}, json={})
    assert run.status_code == 200 and run.json()["ok"] is True
    with_bearer = client.post("/bank/bank_a/packages/nope/disposition", headers={**JSON, **TOKEN_A, **evil},
                              data={"decision": "clear", "reason": "x"})
    refusal(with_bearer, 404, "package_not_found")
    # the same-origin browser form still works
    same = client.post(f"/requests/{RID}/restore", headers={**JSON, "origin": "http://testserver"}, data={})
    assert same.status_code == 200


# --------------------------------------------------------------------------- holder credential


def test_holder_bearer_token_when_configured(tmp_path):
    cl = make_client(tmp_path, holder_token="holder-secret")
    try:
        assert cl.get("/health").json()["holder_auth"] == "bearer"
        refusal(cl.get("/passport", headers=JSON), 401, "not_authenticated")
        refusal(cl.post(f"/requests/{RID}/run", headers=JSON, json={}), 401, "not_authenticated")
        assert cl.get("/passport").status_code == 401 and "not_authenticated" in cl.get("/passport").text
        holder = {**JSON, "authorization": "Bearer holder-secret"}
        assert cl.get("/passport", headers=holder).status_code == 200
        run = cl.post(f"/requests/{RID}/run", headers=holder, json={})
        assert run.status_code == 200 and run.json()["ok"] is True
        refusal(cl.get("/passport", headers={**JSON, "authorization": "Bearer wrong"}), 401, "not_authenticated")
        # the bank's own credentials are untouched by the holder token
        assert cl.get("/bank/bank_a/inbox", headers={**JSON, **TOKEN_A}).status_code == 200
        refusal(cl.get("/bank/bank_a/inbox", headers=holder), 401, "not_authenticated")
        # the demo login sets the holder cookie once and drops the credential from the URL
        refusal(cl.get("/passport?token=wrong", headers=JSON), 401, "not_authenticated")
        session = cl.get("/passport?token=holder-secret", follow_redirects=False)
        assert session.status_code == 303 and session.headers["location"] == "/passport"
        assert "alibi_holder" in session.headers.get("set-cookie", "")
        assert cl.get("/passport", headers=JSON).status_code == 200      # cookie session
        assert cl.get("/health").status_code == 200
    finally:
        cl.app.state.runtime.close()


# --------------------------------------------------------------------------- reviewer fixes


def test_non_ascii_holder_credential_is_refused_not_an_error(tmp_path):
    """``secrets.compare_digest`` raises on non-ASCII strings; a stray accent in a presented
    credential must be a plain 401, never a 500."""
    cl = make_client(tmp_path, holder_token="holder-secret")
    try:
        refusal(cl.get("/passport?token=%C3%A9", headers=JSON), 401, "not_authenticated")
        page = cl.get("/passport?token=%C3%A9")
        assert page.status_code == 401 and "not_authenticated" in page.text
        # raw latin-1 header values decode to non-ASCII text on the server side
        refusal(cl.get("/passport", headers={b"accept": b"application/json",
                                             b"authorization": "Bearer caf\xe9".encode("latin-1")}),
                401, "not_authenticated")
        refusal(cl.get("/passport", headers={b"accept": b"application/json",
                                             b"cookie": "alibi_holder=\xe9".encode("latin-1")}),
                401, "not_authenticated")
        refusal(cl.post(f"/requests/{RID}/run", headers={b"authorization": "Bearer \xe9".encode("latin-1")},
                        json={}), 401, "not_authenticated")
        # the right credential still works after the refusals
        assert cl.get("/passport", headers={**JSON, "authorization": "Bearer holder-secret"}).status_code == 200
    finally:
        cl.app.state.runtime.close()


def test_field_less_actions_refuse_malformed_json(client):
    """UI.md: a malformed JSON body is 400 bad_request on every action, including those that take
    no fields; a valid (ignored) object still works."""
    client.post(f"/requests/{RID}/run", headers=JSON, json={})
    held = client.post("/rules/interpret", headers=JSON, json={"text": "Share nothing with anyone ever please"})
    rule_id = held.json()["rule_id"]
    detail = client.get(f"/requests/{RID}", headers=JSON).json()
    item = next(i for i in detail["review"]["included"] if i["kind"] == "order")
    headers = {**JSON, "content-type": "application/json"}
    for path in ["/tick", f"/requests/{RID}/run", f"/requests/{RID}/restore", f"/requests/{RID}/dispatch",
                 item["remove_action"], f"/rules/{rule_id}/confirm", f"/rules/{rule_id}/reject",
                 f"/rules/{rule_id}/retire", f"/rules/held/{rule_id}/clarify", f"/rules/held/{rule_id}/discard"]:
        body = refusal(client.post(path, headers=headers, content=b"{nope"), 400, "bad_request")
        assert "not valid JSON" in body["reason"], path
        refusal(client.post(path, headers=headers, content=b"[1]"), 400, "bad_request")
    # nothing above changed state: the item is still in the draft and the held rule still exists
    assert item["record_id"] in [i.get("record_id") for i in
                                 client.get(f"/requests/{RID}", headers=JSON).json()["review"]["included"]]
    assert any(r["rule_id"] == rule_id for r in client.get("/rules", headers=JSON).json()["held_rules"])
    tick = client.post("/tick", headers=JSON, json={"ignored": "field"})
    assert tick.status_code == 200 and tick.json()["ok"] is True
    # a form post or an empty body to a field-less action is unchanged
    assert client.post("/tick", headers=JSON, data={}).status_code == 200
    assert client.post("/tick", headers=JSON).status_code == 200


def test_json_field_types_are_checked_not_coerced(client):
    """A list for a single-valued field, an object anywhere, or a nested list is 400 bad_request;
    a single string for a list field is still accepted."""
    client.post(f"/requests/{RID}/run", headers=JSON, json={})
    fresh = client.get(f"/requests/{RID}", headers=JSON).json()["review"]["approval"]
    good = {"digest": fresh["digest"], "audience": "bank_a", "policy_version": fresh["policy_version"]}
    listed = client.post(f"/requests/{RID}/approve", headers=JSON, json={**good, "digest": [good["digest"]]})
    assert "digest" in refusal(listed, 400, "bad_request")["reason"]
    refusal(client.post(f"/requests/{RID}/approve", headers=JSON, json={**good, "digest": {"sha": 1}}),
            400, "bad_request")
    refusal(client.post("/rules/interpret", headers=JSON, json={"text": ["Never share item names."]}),
            400, "bad_request")
    # none of those approved anything
    assert client.get(f"/requests/{RID}", headers=JSON).json()["review"]["approval"]["status"] != "approved"
    approved = client.post(f"/requests/{RID}/approve", headers=JSON, json=good)
    assert approved.status_code == 200, approved.text
    pid = client.post(f"/requests/{RID}/dispatch", headers=JSON, json={}).json()["package_id"]
    auth = {**JSON, **TOKEN_A}
    base = {"decision": "request_more", "reason": "need the bank statement"}
    refusal(client.post(f"/bank/bank_a/packages/{pid}/disposition", headers=auth,
                        json={**base, "reason": {"text": "x"}}), 400, "bad_request")
    refusal(client.post(f"/bank/bank_a/packages/{pid}/disposition", headers=auth,
                        json={**base, "question_ids": [["q1"]]}), 400, "bad_request")
    refusal(client.post(f"/bank/bank_a/packages/{pid}/disposition", headers=auth,
                        json={**base, "requested_evidence": [{"kind": "statement"}]}), 400, "bad_request")
    refusal(client.post("/bank/bank_a/requests", headers=auth,
                        json={"parent_request_id": RID, "question": "x", "txn_ids": [[TXN]]}), 400, "bad_request")
    # a single string for a list field is read as a one-item list (documented)
    one = client.post(f"/bank/bank_a/packages/{pid}/disposition", headers=auth,
                      json={**base, "question_ids": "q.purpose", "requested_evidence": "statement\nnotice"})
    assert one.status_code == 200, one.text
    assert one.json()["question_ids"] == ["q.purpose"] and one.json()["requested_evidence"] == ["statement", "notice"]
