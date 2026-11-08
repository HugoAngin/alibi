"""One in-process stack: the holder app with ``ALIBI_MERCHANT_URL`` and ``ALIBI_BANK_URL`` pointing
at TestClients of the real mock merchant and mock bank. No sockets, no Gemini, temp dirs.

The merchant pushes case 1 through ``POST /connect/merchant/webhook``, the feed syncs through
``POST /connect/bank/sync``, the bank issues ``rfc-0001-01`` and forwards it through
``POST /connect/bank/rfc``, the holder runs, approves and dispatches to the bank service's
``/deliver``, the analyst console reads the service's inbox and package, records a disposition
whose ``request_more`` comes back as a new request on the passport, and a restart keeps history.
Plus SP-21 (a recipient credential on holder routes), the shared tokens file, the dispatch page
before any attempt and the unified trust registry.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from alibi.app.config import APP_KEYS, settings_from_env
from alibi.app.main import create_app
from alibi.bank.service import create_bank_app
from alibi.connectors import keys
from alibi.connectors.http import load_tokens
from alibi.connectors.merchant import create_merchant_app, http_sink
from alibi.core.schemas import SigningDomain
from alibi.core.signing import DemoKeystore, TrustRegistry, verify

NOW = datetime(2026, 11, 5, 15, 0, tzinfo=timezone.utc)      # the holder app clock: 25 h after the feed as_of, so the unexplained-credit alert is due
AS_OF = "2026-11-04T14:00:00Z"                                # the services' clock: both case RFCs visible
TOKENS = {"bank_a": "tok-a", "bank_b": "tok-b", "feed": "tok-feed", "admin": "tok-admin"}
MERCHANT_URL, BANK_URL, APP_URL = "http://merchant.test", "http://bank.test", "http://app.test"
JSON = {"accept": "application/json"}
TOKEN_A = {"authorization": "Bearer tok-a"}
TOKEN_B = {"authorization": "Bearer tok-b"}
ADMIN = {"authorization": "Bearer tok-admin"}
RID, RID_B, TXN = "rfc-0001-01", "rfc-0001-02", "txn-0001-004"


class _Forward:
    """A client that always posts through the stack's *current* app client, so the services keep
    working after the app is restarted."""

    def __init__(self, stack: "Stack"):
        self.stack = stack

    def post(self, *args, **kwargs):
        return self.stack.client.post(*args, **kwargs)

    def get(self, *args, **kwargs):
        return self.stack.client.get(*args, **kwargs)


class Stack:
    def __init__(self, root, *, fixed_now: datetime = NOW):
        self.root = root
        self.fixed_now = fixed_now
        self.client: TestClient | None = None
        self.start()
        keystore = keys.connector_keystore(root / "keys")      # the same key directory as the app
        self.keystore = keystore
        forward = _Forward(self)
        self.merchant = create_merchant_app(http_sink(f"{APP_URL}/connect/merchant/webhook", client=forward),
                                            keystore=keystore, admin_token="tok-admin")
        self.bank = create_bank_app(keystore=keystore, registry=keystore.registry(), tokens=TOKENS,
                                    rfc_url=f"{APP_URL}/connect/bank/rfc", http_client=forward,
                                    state_path=root / "bank" / "state.json")
        self.merchant_client = TestClient(self.merchant)
        self.bank_client = TestClient(self.bank)
        self.inject()

    def start(self) -> None:
        settings = settings_from_env({}, case_number=1, data_dir=self.root / "app", key_dir=self.root / "keys",
                                     fixed_now=self.fixed_now, bank_url=BANK_URL, merchant_url=MERCHANT_URL,
                                     tokens={"bank_a": "tok-a", "bank_b": "tok-b"},
                                     service_tokens={"feed": "tok-feed", "admin": "tok-admin"})
        self.app = create_app(settings)
        self.rt = self.app.state.runtime
        self.client = TestClient(self.app)

    def inject(self) -> None:
        self.rt.http_clients[MERCHANT_URL] = self.merchant_client
        self.rt.http_clients[BANK_URL] = self.bank_client

    def restart(self) -> None:
        self.rt.close()
        self.start()
        self.inject()

    def seed(self) -> dict:
        merchant = self.merchant_client.post("/admin/seed", params={"case": 1, "as_of": AS_OF}, headers=ADMIN)
        assert merchant.status_code == 200, merchant.text
        bank = self.bank_client.post("/admin/seed", params={"case": 1, "as_of": AS_OF}, headers=ADMIN)
        assert bank.status_code == 200, bank.text
        return {"merchant": merchant.json(), "bank": bank.json()}

    def sync(self) -> dict:
        r = self.client.post("/connect/bank/sync", headers=JSON, json={})
        assert r.status_code == 200, r.text
        return r.json()

    def advance_bank(self, to: datetime = NOW) -> None:
        """Move the bank service's clock to the app's: a package signed at the app clock must not
        look issued in the future to the recipient (``/deliver`` checks the validity window)."""
        r = self.bank_client.post("/admin/advance", params={"as_of": to.strftime("%Y-%m-%dT%H:%M:%SZ")}, headers=ADMIN)
        assert r.status_code == 200, r.text

    def issue(self, recipient: str, request_id: str) -> dict:
        r = self.bank_client.post("/rfc", json={"request_id": request_id},
                                  headers={"authorization": f"Bearer {TOKENS[recipient]}"})
        assert r.status_code in (200, 201), r.text
        return r.json()

    def run_approve(self, rid: str) -> dict:
        run = self.client.post(f"/requests/{rid}/run", headers=JSON, json={})
        assert run.status_code == 200, run.text
        detail = self.client.get(f"/requests/{rid}", headers=JSON).json()
        ap = detail["review"]["approval"]
        r = self.client.post(f"/requests/{rid}/approve", headers=JSON,
                             json={"digest": ap["digest"], "audience": ap["audience"], "policy_version": ap["policy_version"]})
        assert r.status_code == 200, r.text
        return detail

    def dispatch(self, rid: str) -> dict:
        r = self.client.post(f"/requests/{rid}/dispatch", headers=JSON, json={})
        assert r.status_code == 200, r.text
        return r.json()

    def close(self) -> None:
        self.rt.close()


@pytest.fixture
def stack(tmp_path):
    st = Stack(tmp_path)
    yield st
    st.close()


# --------------------------------------------------------------------------- the whole path


def test_case_one_travels_through_the_connect_routes(stack: Stack):
    health = stack.client.get("/health").json()
    assert health["seed"] == "connectors" and health["bank_url"] == BANK_URL and health["merchant_url"] == MERCHANT_URL
    empty = stack.client.get("/passport", headers=JSON).json()
    assert empty["transactions"] == [] and empty["requests"] == [] and empty["connections"] == []

    # 1. the merchant pushes case 1 through the webhook route, every push landing as inserted
    seeds = stack.seed()
    pushed = seeds["merchant"]["pushed"]
    assert len(pushed) == 20 and {p["outcome"] for p in pushed} == {"inserted"}, seeds["merchant"]["counts"]
    merchant_conn = [c for c in stack.client.get("/passport", headers=JSON).json()["connections"] if c["kind"] == "merchant"]
    assert merchant_conn and merchant_conn[0]["record_count"] == 20 and merchant_conn[0]["trust"] == "issuer_verified"

    # a retry of the exact bytes is a duplicate (200), not a second record
    retry = stack.merchant_client.post("/admin/retry", json={"event_id": pushed[0]["event_id"]}, headers=ADMIN)
    assert retry.status_code == 200 and retry.json()["outcome"] == "duplicate" and retry.json()["same_bytes"] is True

    # 2. the feed syncs from the bank service with the feed token; the monitor runs on the new rows
    synced = stack.sync()
    assert synced["inserted"] == 4 and synced["pages"] >= 1 and synced["cursor"] == "c:4"
    passport = stack.client.get("/passport", headers=JSON).json()
    assert passport["evidence_status"]["total"] == 4
    assert any(c["kind"] == "bank" and c["record_count"] == 4 for c in passport["connections"])
    assert [a["kind"] for a in passport["alerts"]] == ["unexplained_credit"]
    again = stack.sync()
    assert again["inserted"] == 0 and again["cursor"] == "c:4"

    # 3. bank_a issues its case request; the bank forwards it to POST /connect/bank/rfc
    issued = stack.issue("bank_a", RID)
    assert issued["forwarded"]["status"] == "accepted" and issued["forwarded"]["request_id"] == RID, issued["forwarded"]
    rows = {r["request_id"]: r for r in stack.client.get("/requests", headers=JSON).json()["requests"]}
    assert rows[RID]["status"] == "new" and rows[RID]["audience"] == "bank_a"
    detail = stack.client.get(f"/requests/{RID}", headers=JSON).json()
    assert detail["request"]["signature"] == {"verified": True, "key_id": "bank_a"}
    assert detail["request"]["txn_ids"] == [TXN] and detail["request"]["period"] is None
    # re-issuing the same request is idempotent on the bank and leaves one request on the passport
    assert stack.issue("bank_a", RID)["request_id"] == RID
    assert [r["request_id"] for r in stack.client.get("/requests", headers=JSON).json()["requests"]] == [RID]

    # 4. the investigation reaches the merchant over HTTP for the settlement, then approve and dispatch
    stack.run_approve(RID)
    detail = stack.client.get(f"/requests/{RID}", headers=JSON).json()
    step = next(s for s in detail["trace"] if s["question_id"] == "q.source_of_funds")
    assert step["status"] == "supported" and any(lk["tool"] == "lookup_settlement" for lk in step["lookups"])
    lookups = stack.merchant_client.get("/admin/lookups", headers=ADMIN).json()["lookups"]
    assert any(lk["route"].endswith("/settlements") for lk in lookups), lookups
    assert detail["review"]["approval"]["status"] == "approved"
    assert detail["review"]["approval"]["dispatch_action"] == f"/requests/{RID}/dispatch"
    # The review's claim statuses are whatever the core built from the connector-mapped records
    # (see the report: the S2 mapping keeps the settled payout as kind ``payout``, which the core's
    # review check does not reconcile, so the packaged claims may be downgraded). The console must
    # show exactly those statuses, never the trace's.
    review_claims = {cl["question_id"]: cl["status"] for cl in detail["review"]["claims"]}
    assert set(review_claims) == {"q.source_of_funds", "q.breakdown"}

    # the recipient's clock must have reached the app's before the first delivery: a package signed
    # "now" is otherwise refused as issued in the future, and the service keeps that verdict for the
    # package id (same bytes, same receipt), so the demo advances the services to the wall clock first
    stack.advance_bank()
    sent = stack.dispatch(RID)
    pid = sent["package_id"]
    assert sent["outcome"] == "success" and sent["receipt"]["status"] == "accepted"
    receipt_id = sent["receipt"]["receipt_id"]
    assert receipt_id.startswith("rcp_"), receipt_id                 # the bank service's receipt, not the in-process one
    inbox = stack.bank_client.get("/inbox", headers=TOKEN_A).json()
    assert [(p["package_id"], p["status"], p["receipt_id"]) for p in inbox["packages"]] == [(pid, "verified", receipt_id)]
    assert sent["receipt"]["package_digest"] == inbox["packages"][0]["package_sha256"]
    history = stack.client.get("/passport", headers=JSON).json()["sharing_history"]
    assert [(h["package_id"], h["status"]) for h in history] == [(pid, "acknowledged")]
    page = stack.client.get(f"/requests/{RID}/dispatch", headers=JSON).json()["dispatch"]
    assert page["outcome"] == "success" and page["receipt"]["receipt_id"] == receipt_id
    assert receipt_id in stack.client.get(f"/requests/{RID}/dispatch").text
    # a retry resends identical bytes and gets the same receipt; the inbox still holds one package
    retry = stack.dispatch(RID)
    assert retry["package_id"] == pid and retry["receipt"]["receipt_id"] == receipt_id
    assert len(stack.bank_client.get("/inbox", headers=TOKEN_A).json()["packages"]) == 1

    # 5. the analyst console reads the service: inbox, package panels, timing and reconciliation
    console = stack.client.get("/bank/bank_a/inbox", headers={**JSON, **TOKEN_A}).json()
    row = console["packages"][0]
    assert row["package_id"] == pid and row["authentication"] == "valid" and row["request_id"] == RID
    assert sum(row["claim_counts"].values()) == 2 and row["disposition"] is None
    assert row["claim_counts"][review_claims["q.source_of_funds"]] >= 1
    sent_rows = {r["request_id"]: r for r in console["requests_sent"]}
    assert sent_rows[RID]["status"] == "answered" and sent_rows[RID]["scope"] == "txn_ids"
    html = stack.client.get("/bank/bank_a/inbox", headers=TOKEN_A).text
    assert pid in html and "<!doctype html>" in html.lower()
    package = stack.client.get(f"/bank/bank_a/packages/{pid}", headers={**JSON, **TOKEN_A}).json()
    assert package["rejected"] is False and package["authentication"]["status"] == "valid"
    assert all(ch["result"] == "pass" for ch in package["authentication"]["checks"])
    assert package["authentication"]["signer_key_id"] == "alibi_demo"
    assert package["package"]["txn_ids"] == [TXN] and package["package"]["period"] is None
    assert package["source_trust"] and all(s["attestation"] in ("issuer_signature", "package_signer")
                                           for s in package["source_trust"])
    assert {c["question_id"]: c["status"] for c in package["claim_status"]} == review_claims
    q = next(e for e in package["evidence_by_question"] if e["question_id"] == "q.source_of_funds")
    assert q["claim"]["status"] == review_claims["q.source_of_funds"]
    assert q["timing"] is not None and q["timing"]["payout_ref"] == "PO-0001-01"
    assert package["reconciliation"]["matches"] is True and package["reconciliation"]["lines"]
    assert package["disposition_form"]["questions"] and package["disposition_form"]["current"] is None
    html = stack.client.get(f"/bank/bank_a/packages/{pid}", headers=TOKEN_A).text
    for hook in ("panel--auth", "panel--trust", "panel--claims", 'name="decision"', TXN):
        assert hook in html, hook
    assert stack.client.get(f"/bank/bank_a/packages/{pid}", headers={**JSON, **TOKEN_B}).status_code == 403
    assert stack.client.get(f"/bank/bank_b/packages/{pid}", headers={**JSON, **TOKEN_B}).status_code == 404
    assert stack.client.get("/bank/bank_a/packages/nope", headers={**JSON, **TOKEN_A}).status_code == 404

    # 6. a request_more disposition on the service issues the follow-up, which comes back to the passport
    bad = stack.client.post(f"/bank/bank_a/packages/{pid}/disposition", headers={**JSON, **TOKEN_A},
                            json={"decision": "clear", "reason": ""})
    assert bad.status_code == 400 and bad.json()["code"] == "reason_required"
    disp = stack.client.post(f"/bank/bank_a/packages/{pid}/disposition", headers={**JSON, **TOKEN_A},
                             json={"decision": "request_more", "reason": "Need the settlement record",
                                   "question_ids": ["q.breakdown"],
                                   "requested_evidence": ["settlement record for the payout"]})
    assert disp.status_code == 200, disp.text
    body = disp.json()
    assert body["decision"] == "request_more" and body["analyst"] == "analyst_a1"
    follow_id = body["followup_request_id"]
    assert follow_id == f"{RID}-f1" and body["follow_up"]["forwarded"]["status"] == "accepted", body
    rows = {r["request_id"]: r for r in stack.client.get("/requests", headers=JSON).json()["requests"]}
    assert rows[follow_id]["parent_request_id"] == RID and rows[follow_id]["status"] == "new"
    follow = stack.client.get(f"/requests/{follow_id}", headers=JSON).json()
    assert follow["request"]["signature"] == {"verified": True, "key_id": "bank_a"}
    assert follow["request"]["txn_ids"] == [TXN]
    view = stack.client.get(f"/bank/bank_a/packages/{pid}?disposition={body['disposition_id']}",
                            headers={**JSON, **TOKEN_A}).json()
    assert view["disposition_form"]["current"]["decision"] == "request_more"
    assert view["disposition_form"]["current"]["followup_request_id"] == follow_id
    assert "settlement record" in view["followup_form"]["question_prefill"]
    # the follow-up form posts a request_more with the analyst's question; wider scope is refused by the service
    wider = stack.client.post("/bank/bank_a/requests", headers={**JSON, **TOKEN_A},
                              json={"parent_request_id": RID, "scope": "period", "period_from": "2026-01-01T00:00:00Z",
                                    "period_to": "2026-11-01T00:00:00Z", "question": "x"})
    assert wider.status_code == 409 and wider.json()["code"] == "scope_wider_than_parent"
    second = stack.client.post("/bank/bank_a/requests", headers={**JSON, **TOKEN_A},
                               json={"parent_request_id": RID, "scope": "txn_ids", "txn_ids": [TXN],
                                     "question": "Please provide the settlement record for the payout."})
    assert second.status_code == 201, second.text
    assert second.json()["request_id"] == f"{RID}-f2" and second.json()["txn_ids"] == [TXN]
    assert f"{RID}-f2" in {r["request_id"] for r in stack.client.get("/requests", headers=JSON).json()["requests"]}
    console = stack.client.get("/bank/bank_a/inbox", headers={**JSON, **TOKEN_A}).json()
    assert {r["request_id"] for r in console["requests_sent"]} == {RID, f"{RID}-f1", f"{RID}-f2"}
    # the follow-up needs its own investigation and approval; it is answered through the same service
    stack.run_approve(follow_id)
    second_pkg = stack.dispatch(follow_id)
    assert second_pkg["package_id"] != pid and second_pkg["receipt"]["receipt_id"] != receipt_id
    final = stack.client.post(f"/bank/bank_a/packages/{pid}/disposition", headers={**JSON, **TOKEN_A},
                              json={"decision": "clear", "reason": "resolved"})
    assert final.status_code == 200
    again = stack.client.post(f"/bank/bank_a/packages/{pid}/disposition", headers={**JSON, **TOKEN_A},
                              json={"decision": "escalate", "reason": "changed my mind"})
    assert again.status_code == 409 and again.json()["code"] == "disposition_final"
    audit = stack.client.get("/bank/bank_a/audit", headers={**JSON, **TOKEN_A}).json()["entries"]
    assert {e["action"] for e in audit} >= {"issue_request", "deliver", "disposition"}
    # the holder's audit shows the connector events and none of the bank's
    holder_audit = {e["action"] for e in stack.client.get("/audit", headers=JSON).json()["entries"]}
    assert {"receive_rfc", "bank_sync", "dispatch", "ingest_merchant_event"} <= holder_audit
    assert not {"deliver", "disposition", "package.received"} & holder_audit


# --------------------------------------------------------------------------- connect route edges


def test_connect_routes_refuse_bad_callers(stack: Stack):
    stack.seed()
    good = stack.merchant_client.get("/admin/deliveries", headers=ADMIN).json()["deliveries"][0]["payload"]
    # the webhook needs received_at and an {event, signature} body; a tampered signature is refused 409
    assert stack.client.post("/connect/merchant/webhook", json=good).status_code == 400
    bad_shape = stack.client.post("/connect/merchant/webhook?received_at=2026-11-01T00:00:00Z", json={"event": {}})
    assert bad_shape.status_code == 400 and bad_shape.json()["code"] == "bad_request"
    tampered = json.loads(json.dumps(good))
    tampered["event"]["amount_pence"] = (tampered["event"].get("amount_pence") or 0) + 1
    refused = stack.client.post("/connect/merchant/webhook?received_at=2026-11-01T00:00:00Z", json=tampered)
    assert refused.status_code == 409 and refused.json()["code"] == "ingest_refused" and refused.json()["status"] == "refused"
    dup = stack.client.post("/connect/merchant/webhook?received_at=2026-11-01T00:00:00Z", json=good)
    assert dup.status_code == 200 and dup.json()["status"] == "duplicate"
    # the RFC route needs a recipient bearer, and the body's recipient must be the bearer's
    issued = stack.bank_client.post("/rfc", json={"request_id": RID_B}, headers=TOKEN_B).json()
    body = {"rfc": issued["rfc"], "recipient": "bank_b", "account_refs": [], "now": AS_OF}
    assert stack.client.post("/connect/bank/rfc", json=body).status_code == 401
    mismatch = stack.client.post("/connect/bank/rfc", json=body, headers=TOKEN_A)
    assert mismatch.status_code == 403 and mismatch.json()["code"] == "recipient_mismatch"
    ok = stack.client.post("/connect/bank/rfc", json=body, headers=TOKEN_B)
    assert ok.status_code == 200 and ok.json()["status"] == "accepted"      # the forwarded copy was a duplicate
    forged = json.loads(json.dumps(body))
    forged["rfc"]["request"]["expires_at"] = "2027-01-01T00:00:00Z"
    bad = stack.client.post("/connect/bank/rfc", json=forged, headers=TOKEN_B)
    assert bad.status_code == 409 and bad.json()["code"] == "rfc_refused"
    assert stack.client.post("/connect/bank/rfc", json={"rfc": "x", "recipient": "bank_b"}, headers=TOKEN_B).status_code == 400
    # sync without a bank URL is a clear refusal
    settings = settings_from_env({}, case_number=1, data_dir=stack.root / "solo", key_dir=stack.root / "keys", fixed_now=NOW)
    solo = TestClient(create_app(settings))
    try:
        r = solo.post("/connect/bank/sync", headers=JSON, json={})
        assert r.status_code == 409 and r.json()["code"] == "no_bank_url"
        assert solo.get("/health").json()["seed"] == "case_adapter"
    finally:
        solo.app.state.runtime.close()


# --------------------------------------------------------------------------- SP-21


def test_recipient_credentials_cannot_act_on_holder_routes(stack: Stack):
    stack.seed()
    stack.sync()
    stack.issue("bank_a", RID)
    for path in ("/passport", f"/requests/{RID}", "/alerts", "/rules", "/requests", "/audit", f"/txn/{TXN}"):
        assert stack.client.get(path, headers=JSON).status_code == 200, path
        r = stack.client.get(path, headers={**JSON, **TOKEN_B})
        assert r.status_code == 403 and r.json()["code"] == "recipient_credential", (path, r.text)
        html = stack.client.get(path, headers=TOKEN_B)
        assert html.status_code == 403 and "recipient_credential" in html.text
    for path in (f"/requests/{RID}/run", "/connect/bank/sync", "/tick"):
        r = stack.client.post(path, headers={**JSON, **TOKEN_A}, json={})
        assert r.status_code == 403 and r.json()["code"] == "recipient_credential", path
    cookie = stack.client.get("/passport", headers=JSON, cookies={"alibi_bank": "tok-a"})
    assert cookie.status_code == 403 and cookie.json()["code"] == "recipient_credential"
    # the refusal is written to the app's sidecar for that recipient, and the console's audit route
    # over the bank service merges those entries into the service's trail (source: passport), so
    # the SP-21 refusal is visible on GET /bank/{recipient}/audit as UI.md says
    local = stack.rt.bank_audit_entries("bank_b")
    assert any(e["action"] == "access.refused" and "holder route" in e["detail"] for e in local)
    entries = stack.client.get("/bank/bank_b/audit", headers={**JSON, **TOKEN_B}).json()["entries"]
    refused = [e for e in entries if e.get("action") == "access.refused"]
    assert refused and all(e["source"] == "passport" and e["actor"] == "bank_b" for e in refused), entries
    assert any("holder route /passport" in e["detail"] for e in refused)
    service_side = [e for e in entries if e.get("source") == "bank_service"]
    assert not any(e.get("action") == "access.refused" for e in service_side)   # the service's own trail is untouched
    assert [e["at"] for e in entries] == sorted(e["at"] for e in entries)      # one trail, in time order
    # bank_a's trail shows only its own refusals (the POSTs above carried TOKEN_A), never bank_b's
    a_entries = stack.client.get("/bank/bank_a/audit", headers={**JSON, **TOKEN_A}).json()["entries"]
    a_refused = [e for e in a_entries if e.get("action") == "access.refused"]
    assert a_refused and all(e["actor"] == "bank_a" and e["source"] == "passport" for e in a_refused)
    assert not any("/alerts" in e["detail"] for e in a_refused)        # only TOKEN_B was tried on /alerts
    assert any(e["source"] == "bank_service" and e["action"] == "issue_request" for e in a_entries), a_entries
    assert not any(e["action"] == "access.refused" for e in stack.client.get("/audit", headers=JSON).json()["entries"])
    # nothing ran: the request is still new
    assert stack.client.get(f"/requests/{RID}", headers=JSON).json()["investigation"]["status"] == "not_started"
    # the bank's own routes still take the bank's token, and the webhook/rfc routes are not holder routes
    assert stack.client.get("/bank/bank_a/inbox", headers={**JSON, **TOKEN_A}).status_code == 200


# --------------------------------------------------------------------------- dispatch page before any attempt


def test_dispatch_page_before_any_attempt_is_neutral(stack: Stack):
    stack.seed()
    stack.sync()
    stack.issue("bank_a", RID)
    before = stack.client.get(f"/requests/{RID}/dispatch", headers=JSON).json()["dispatch"]
    assert before["outcome"] == "not_attempted" and before["refusal"] is None and before["receipt"] is None
    assert before["retry_action"] is None and before["package_url"] is None
    assert "refused" not in json.dumps(before).lower()
    html = stack.client.get(f"/requests/{RID}/dispatch")
    assert html.status_code == 200 and "not_dispatched" not in html.text
    detail = stack.client.get(f"/requests/{RID}", headers=JSON).json()
    assert detail["review"]["approval"]["dispatch_action"] == f"/requests/{RID}/dispatch"
    assert detail["request"]["status"] == "new"
    stack.run_approve(RID)
    ready = stack.client.get(f"/requests/{RID}/dispatch", headers=JSON).json()["dispatch"]
    assert ready["outcome"] == "not_attempted" and ready["retry_action"] == f"/requests/{RID}/dispatch"
    detail = stack.client.get(f"/requests/{RID}", headers=JSON).json()
    assert detail["review"]["approval"]["status"] == "approved"
    assert f'action="/requests/{RID}/dispatch"' in stack.client.get(f"/requests/{RID}").text


# --------------------------------------------------------------------------- restart keeps history


def test_restart_keeps_history_without_loading_twice(stack: Stack):
    stack.seed()
    stack.sync()
    stack.issue("bank_a", RID)
    stack.run_approve(RID)
    stack.advance_bank()
    pid = stack.dispatch(RID)["package_id"]
    records_before = sum(c["record_count"] for c in stack.client.get("/passport", headers=JSON).json()["connections"])
    audit_before = len(stack.client.get("/audit", headers=JSON).json()["entries"])
    stack.restart()
    assert stack.client.get("/health").json()["seed"] == "connectors"
    passport = stack.client.get("/passport", headers=JSON).json()
    assert passport["evidence_status"]["total"] == 4
    assert sum(c["record_count"] for c in passport["connections"]) == records_before
    assert {r["request_id"]: r["status"] for r in passport["requests"]}[RID] == "sent"
    assert [h["package_id"] for h in passport["sharing_history"]] == [pid]
    assert stack.client.get(f"/txn/{TXN}", headers=JSON).json()["txn"]["link_state"] == "linked"
    # nothing was loaded twice: no ingest event was added by the restart (on the fixed clock the
    # start-up tick's audit id repeats, so the trail is unchanged)
    audit_after = stack.client.get("/audit", headers=JSON).json()["entries"]
    assert audit_before <= len(audit_after) <= audit_before + 1
    assert all(e["action"] == "tick" for e in audit_after[:len(audit_after) - audit_before])
    # the services replaying are idempotent duplicates on the passport
    reseed = stack.merchant_client.post("/admin/seed", params={"case": 1, "as_of": AS_OF}, headers=ADMIN).json()
    assert {p["outcome"] for p in reseed["pushed"]} == {"duplicate"}
    assert stack.sync()["inserted"] == 0
    assert stack.client.get("/passport", headers=JSON).json()["evidence_status"]["total"] == 4
    # the console still reads the service's inbox after the restart
    inbox = stack.client.get("/bank/bank_a/inbox", headers={**JSON, **TOKEN_A}).json()
    assert [p["package_id"] for p in inbox["packages"]] == [pid]


# --------------------------------------------------------------------------- the one tokens file


def test_tokens_come_from_the_shared_file_and_env_wins(tmp_path, monkeypatch):
    for name in ("ALIBI_BANK_A_TOKEN", "ALIBI_BANK_B_TOKEN", "ALIBI_FEED_TOKEN", "ALIBI_ADMIN_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / "tokens.json"
    settings = settings_from_env({"ALIBI_TOKENS_FILE": str(path)}, case_number=1, data_dir=tmp_path / "app",
                                 key_dir=tmp_path / "keys", fixed_now=NOW)
    assert path.exists() and settings.tokens_file == path
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert set(stored) == {"bank_a", "bank_b", "feed", "admin"}
    assert settings.tokens == {"bank_a": stored["bank_a"], "bank_b": stored["bank_b"]}
    assert settings.service_tokens == {"feed": stored["feed"], "admin": stored["admin"]}
    assert settings.feed_token == stored["feed"]
    assert load_tokens(path) == stored          # the bank service reads the very same values
    cl = TestClient(create_app(settings))
    try:
        health = cl.get("/health").json()
        assert health["tokens_file"] == str(path) and stored["bank_a"] not in json.dumps(health)
        # the ?token= the Driver copies from the file opens the inbox on this app
        session = cl.get(f"/bank/bank_a/inbox?token={stored['bank_a']}", follow_redirects=False)
        assert session.status_code == 303 and session.headers["location"] == "/bank/bank_a/inbox"
        assert cl.get("/bank/bank_a/inbox", headers={**JSON, "authorization": f"Bearer {stored['bank_a']}"}).status_code == 200
        assert cl.get("/bank/bank_a/inbox", headers={**JSON, "authorization": "Bearer demo-token-bank-a"}).status_code == 401
    finally:
        cl.app.state.runtime.close()
    # the environment still overrides one identity, and the file keeps the others
    monkeypatch.setenv("ALIBI_BANK_B_TOKEN", "env-wins")
    again = settings_from_env({"ALIBI_TOKENS_FILE": str(path), "ALIBI_BANK_B_TOKEN": "env-wins"})
    assert again.tokens == {"bank_a": stored["bank_a"], "bank_b": "env-wins"}
    assert load_tokens(path)["bank_b"] == "env-wins"
    # an explicit env without the variable keeps the demo defaults and touches no file
    plain = settings_from_env({})
    assert plain.tokens_file is None and plain.tokens["bank_a"] == "demo-token-bank-a"


# --------------------------------------------------------------------------- one trust registry


def test_app_and_connector_keystores_share_one_registry(tmp_path):
    key_dir = tmp_path / "keys"
    app_keys = DemoKeystore(key_dir, specs=APP_KEYS).ensure()
    connector = keys.connector_keystore(key_dir)
    assert set(APP_KEYS) == set(keys.CONNECTOR_KEYS)
    expected = {"alibi_demo", "bank_a", "bank_b", "merchant_demo", "merchant_mock", "mock_marketplace", "second_platform"}
    assert set(connector.registry().kids()) == expected
    written = TrustRegistry.load(key_dir / "trust_registry.json")
    assert set(written.kids()) == expected
    # a record signed by the app keystore under mock_marketplace verifies with the connector registry
    body = {"record_id": "rec-x", "issuer_id": "mock-marketplace", "amount_pence": 1}
    envelope = app_keys.sign(body, kid="mock_marketplace", domain=SigningDomain.RECORD)
    entry = verify(body, envelope, connector.registry(), domain=SigningDomain.RECORD, issuer_id="mock-marketplace")
    assert entry.kid == "mock_marketplace"
    # and a request the connector keystore signs as bank_a verifies with the app's registry
    signed = keys.sign_request({"request_id": "rfc-x", "issuer": "bank_a"}, connector, kid="bank_a")
    assert verify(signed["request"], signed["signature"], app_keys.registry(), domain=SigningDomain.REQUEST).kid == "bank_a"
    # the registry either keystore rewrites still carries every kid
    connector.ensure()
    assert set(TrustRegistry.load(key_dir / "trust_registry.json").kids()) == expected
    DemoKeystore(key_dir, specs=APP_KEYS).ensure()
    assert set(TrustRegistry.load(key_dir / "trust_registry.json").kids()) == expected


# --------------------------------------------------------------------------- webhook: loopback only


def test_merchant_webhook_is_served_to_loopback_clients_only(stack: Stack):
    """The issuer signature authenticates the push, and the route is additionally served to
    loopback clients only, like the services' admin routes: a remote client is refused 403
    ``forbidden`` before the body is read, and nothing is recorded."""
    stack.seed()
    deliveries = stack.merchant_client.get("/admin/deliveries", headers=ADMIN).json()["deliveries"]
    first = deliveries[0]
    payload, received_at = first["payload"], first["received_at"]
    assert set(payload) == {"event", "signature"}
    before = stack.client.get("/passport", headers=JSON).json()
    merchant_count = next(c["record_count"] for c in before["connections"] if c["kind"] == "merchant")

    remote = TestClient(stack.app, client=("203.0.113.9", 40000))
    r = remote.post("/connect/merchant/webhook", params={"received_at": received_at}, json=payload)
    assert r.status_code == 403, r.text
    assert r.json()["code"] == "forbidden" and "loopback" in r.json()["reason"]
    # refused before the body or the query string is read: the same answer without either
    assert remote.post("/connect/merchant/webhook").status_code == 403
    assert remote.post("/connect/merchant/webhook", params={"received_at": received_at},
                       json={"event": "x"}).json()["code"] == "forbidden"

    loopback = TestClient(stack.app, client=("127.0.0.1", 40000))
    ok = loopback.post("/connect/merchant/webhook", params={"received_at": received_at}, json=payload)
    assert ok.status_code == 200 and ok.json()["status"] == "duplicate", ok.text
    after = stack.client.get("/passport", headers=JSON).json()
    assert next(c["record_count"] for c in after["connections"] if c["kind"] == "merchant") == merchant_count
    # the other connect routes are not loopback-gated: the recipient bearer is their authentication
    assert remote.post("/connect/bank/rfc", json={"rfc": {}, "recipient": "bank_a"}).status_code == 401
    assert remote.post("/connect/bank/rfc", json={"rfc": {}, "recipient": "bank_b"}, headers=TOKEN_A).status_code == 403


# --------------------------------------------------------------------------- dispatch: the service's refusals


def test_dispatch_refusal_from_the_service_is_flattened(stack: Stack):
    """A ``/deliver`` refusal (422 ``package_rejected`` here: the bank's clock is behind the app's,
    so the package looks issued in the future) comes back as the app's dispatch refusal with the
    service's code and sentence, the dispatch page shows it, the sharing history marks the delivery
    failed, and the service keeps the verdict for the package id on an identical resend."""
    stack.seed()
    stack.sync()
    stack.issue("bank_a", RID)
    stack.run_approve(RID)
    r = stack.client.post(f"/requests/{RID}/dispatch", headers=JSON, json={})
    assert r.status_code == 409, r.text                                        # the app's dispatch refusal status
    body = r.json()
    assert body["code"] == "package_rejected"
    assert body["reason"].startswith("dispatch refused by the recipient: ") and "issued in the future" in body["reason"]
    assert body["expected_digest"].startswith("sha256:") and body["actual_digest"] == body["expected_digest"]
    page = stack.client.get(f"/requests/{RID}/dispatch", headers=JSON).json()["dispatch"]
    assert page["outcome"] == "refused" and page["receipt"] is None and page["package_url"] is None
    assert page["refusal"]["code"] == "package_rejected" and "issued in the future" in page["refusal"]["reason"]
    assert page["retry_action"] == f"/requests/{RID}/dispatch"
    html = stack.client.get(f"/requests/{RID}/dispatch")
    assert html.status_code == 200 and "package_rejected" in html.text
    history = stack.client.get("/passport", headers=JSON).json()["sharing_history"]
    assert [h["status"] for h in history] == ["failed"]
    pid = history[0]["package_id"]
    # the service filed the rejection with its reasons; the console shows it as rejected
    inbox = stack.bank_client.get("/inbox", headers=TOKEN_A).json()["packages"]
    assert [(p["package_id"], p["status"]) for p in inbox] == [(pid, "rejected")]
    service_view = stack.bank_client.get(f"/packages/{pid}", headers=TOKEN_A)
    assert service_view.status_code == 200
    assert any("future" in reason for reason in service_view.json()["receipt"]["reasons"]), service_view.json()["receipt"]
    console = stack.client.get("/bank/bank_a/inbox", headers={**JSON, **TOKEN_A}).json()
    assert [(p["package_id"], p["authentication"]) for p in console["packages"]] == [(pid, "invalid")]
    # the trail on the service carries the refused deliver; the merged console trail shows it too
    trail = stack.client.get("/bank/bank_a/audit", headers={**JSON, **TOKEN_A}).json()["entries"]
    assert any(e["source"] == "bank_service" and e["action"] == "deliver" and e["outcome"] == "refused"
               and "package_rejected" in e["detail"] for e in trail), trail
    # advancing the clock does not change the verdict: the resend is identical bytes, and the
    # service answers the stored rejection for that package id
    stack.advance_bank()
    again = stack.client.post(f"/requests/{RID}/dispatch", headers=JSON, json={})
    assert again.status_code == 409 and again.json()["code"] == "package_rejected", again.text
    assert stack.client.get(f"/requests/{RID}/dispatch", headers=JSON).json()["dispatch"]["package_id"] == pid
    assert len(stack.bank_client.get("/inbox", headers=TOKEN_A).json()["packages"]) == 1


# --------------------------------------------------------------------------- connect/bank/rfc refusals on the console trail


def test_forwarded_rfc_refusals_land_on_the_recipients_console_trail(stack: Stack):
    """``POST /connect/bank/rfc`` with a bearer that does not match the body's recipient is
    refused 403 ``recipient_mismatch`` and audited for the bearer's recipient; over the bank
    service the console's audit route shows that entry from the app's sidecar."""
    stack.seed()
    stack.sync()
    issued = stack.issue("bank_a", RID)
    assert issued["forwarded"]["status"] == "accepted", issued["forwarded"]
    forwarded = stack.bank_client.get(f"/rfc/{RID}", headers=TOKEN_A).json()
    body = {"rfc": forwarded["rfc"], "recipient": "bank_a", "account_refs": None, "now": issued.get("issued_at")}
    wrong = stack.client.post("/connect/bank/rfc", json=body, headers=TOKEN_B)
    assert wrong.status_code == 403 and wrong.json()["code"] == "recipient_mismatch"
    trail = stack.client.get("/bank/bank_b/audit", headers={**JSON, **TOKEN_B}).json()["entries"]
    hit = [e for e in trail if e["action"] == "access.refused" and "another recipient" in e["detail"]]
    assert hit and hit[0]["source"] == "passport" and hit[0]["actor"] == "bank_b", trail
    assert not any(e["action"] == "access.refused" for e in
                   stack.client.get("/bank/bank_a/audit", headers={**JSON, **TOKEN_A}).json()["entries"])
    # the passport still holds exactly the one request the bank forwarded
    assert [r["request_id"] for r in stack.client.get("/requests", headers=JSON).json()["requests"]] == [RID]
