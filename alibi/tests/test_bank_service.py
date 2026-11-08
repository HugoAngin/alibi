"""The mock bank: two recipient identities issue signed requests, verify delivered packages,
keep their inboxes apart, and record dispositions whose follow-ups stay inside the parent's
scope. Real packages are built from case 1 through the passport's own API."""

import copy
import json
import os
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from alibi import api
from alibi.bank.service import create_bank_app, load_tokens
from alibi.bank.state import BankState
from alibi.connectors import keys
from alibi.connectors.case_source import (
    CaseClock,
    load_case_without_gold,
    map_bank_row,
    map_merchant_event,
)
from alibi.connectors.plug import webhook_sink
from alibi.core import canon
from alibi.core.schemas import format_utc, parse_utc
from alibi.core.signing import verify
from alibi.core.store import Store

# ``admin`` is the shared demo control credential; ``create_bank_app`` takes it from the dict.
TOKENS = {"bank_a": "tok-a", "bank_b": "tok-b", "feed": "tok-feed", "admin": "tok-admin"}
DEFAULT_AS_OF = "2026-11-02T09:00:00Z"      # the clock's default: the first RFC's issued_at
RFC_B_ISSUED_AT = "2026-11-04T14:00:00Z"    # bank_b's case RFC becomes visible here
AS_OF = RFC_B_ISSUED_AT                     # tests seed here so both case RFCs are issuable
EXPIRED_AT = "2026-11-09T09:00:00Z"
RFC_A, RFC_B = "rfc-0001-01", "rfc-0001-02"
SUBJECT, ACCOUNT, CONNECTION = "maya-001", "acct-maya-001", "conn-maya-001"
OCTOBER = {"from": "2026-10-01T00:00:00Z", "to": "2026-11-01T00:00:00Z"}


def auth(identity: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKENS[identity]}"}


class SinkStub:
    """Records every forwarded request like ``plug.RfcSink`` would."""

    def __init__(self):
        self.calls = []

    def __call__(self, signed, recipient, account_refs, now):
        self.calls.append({"signed": copy.deepcopy(signed), "recipient": recipient,
                           "account_refs": list(account_refs or []), "now": now})
        return {"status": "accepted", "request_id": signed["request"]["request_id"]}


# --------------------------------------------------------------------------- fixtures


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    """Keystore, registry and two real signed packages for case 1 (P1 to bank_a for
    rfc-0001-01, P3 to bank_b for rfc-0001-02), made through the passport API."""
    root = tmp_path_factory.mktemp("bank-build")
    keystore = keys.connector_keystore(root / "keys", seed="bank-tests")
    registry = keystore.registry()
    case = load_case_without_gold(1)
    clock = CaseClock(case)
    assert clock.as_of_text == DEFAULT_AS_OF
    app = create_bank_app(keystore=keystore, registry=registry, tokens=TOKENS)
    client = TestClient(app)
    assert client.post("/admin/seed", headers=auth("admin"), params={"case": 1, "as_of": AS_OF}).status_code == 200
    packages = {}
    with Store(root / "passport.db") as store:
        sink = webhook_sink(store, registry, subject_ref=SUBJECT)
        for d in clock.visible_merchant_deliveries():
            payload = keys.sign_merchant_event(map_merchant_event(d["event"]).body, keystore)
            assert sink(payload, d["received_at"])["status"] == "inserted"
        rows = [map_bank_row(d["row"], subject_ref=SUBJECT, connection_id=CONNECTION,
                             retrieved_at=d["received_at"]).body for d in clock.visible_bank_rows()]
        api.ingest_bank_txns(store, rows, subject_ref=SUBJECT, connection_id=CONNECTION,
                             now=clock.as_of)
        for recipient, request_id, accounts in (("bank_a", RFC_A, [ACCOUNT]), ("bank_b", RFC_B, None)):
            issued = client.post("/rfc", json={"request_id": request_id}, headers=auth(recipient))
            assert issued.status_code == 201, issued.json()
            rfc = issued.json()["rfc"]
            api.receive_rfc(store, rfc, authenticated_issuer=recipient, registry=registry,
                            account_refs=accounts, now=clock.as_of)
            api.run_investigation(store, request_id, registry=registry, now=clock.as_of)
            draft = api.build_review(store, request_id, now=clock.as_of)
            approval = api.approve(store, draft.draft_id, holder_id="holder_maya", now=clock.as_of)
            package = api.sign_and_package(store, approval.approval_id, keystore=keystore,
                                           now=clock.as_of)
            packages[recipient] = package.to_json_dict()
    return SimpleNamespace(keystore=keystore, registry=registry, clock=clock,
                           p1=packages["bank_a"], p3=packages["bank_b"])


def make_app(built, *, state_path=None, sink=None, seed=True):
    app = create_bank_app(keystore=built.keystore, registry=built.registry, tokens=TOKENS,
                          state_path=state_path, rfc_sink=sink)
    client = TestClient(app)
    if seed:
        seeded = client.post("/admin/seed", headers=auth("admin"), params={"case": 1, "as_of": AS_OF})
        assert seeded.status_code == 200, seeded.json()
        assert seeded.json() == {"case_number": 1, "subject_ref": SUBJECT, "account_ref": ACCOUNT,
                                 "as_of": AS_OF, "recipients": ["bank_a", "bank_b"]}
    return client


def issue_both(client):
    a = client.post("/rfc", json={"request_id": RFC_A}, headers=auth("bank_a"))
    b = client.post("/rfc", json={}, headers=auth("bank_b"))
    assert a.status_code == 201 and b.status_code == 201
    assert b.json()["request_id"] == RFC_B
    return a.json(), b.json()


@pytest.fixture
def client(built):
    return make_app(built)


# --------------------------------------------------------------------------- authentication


def test_recipient_routes_need_a_valid_token_and_admin_routes_the_admin_token(client, built):
    for method, path, body in (("get", "/inbox", None), ("get", "/rfc", None),
                               ("post", "/rfc", {}), ("post", "/deliver", built.p1)):
        for headers in ({}, {"Authorization": "Bearer nope"}, {"Authorization": "Basic tok-a"},
                        auth("feed"), auth("admin")):
            response = client.request(method, path, json=body, headers=headers)
            assert response.status_code == 401, (path, headers)
            assert response.json()["code"] == "unauthenticated"
            assert "tok-" not in json.dumps(response.json())
    for headers in ({}, {"Authorization": "Bearer nope"}, auth("bank_a"), auth("feed")):
        refused = client.post("/admin/advance", params={"as_of": AS_OF}, headers=headers)
        assert refused.status_code == 401 and refused.json()["code"] == "unauthenticated", headers
        assert "tok-" not in json.dumps(refused.json())
    assert client.post("/admin/advance", headers=auth("admin"), params={"as_of": AS_OF}).status_code == 200
    assert client.app.state.admin_token == TOKENS["admin"]
    assert client.get("/").json()["service"] == "mock-bank"


def test_whoami_names_the_recipient_and_analyst_from_the_token(client):
    assert client.get("/whoami", headers=auth("bank_a")).json() == {"recipient": "bank_a",
                                                                   "analyst": "analyst_a1"}
    assert client.get("/whoami", headers=auth("bank_b")).json() == {"recipient": "bank_b",
                                                                   "analyst": "analyst_b1"}
    assert client.get("/whoami", headers=auth("feed")).status_code == 401


# --------------------------------------------------------------------------- issuing requests


def test_issue_case_requests_signed_by_the_recipient(built):
    sink = SinkStub()
    client = make_app(built, sink=sink)
    first = client.post("/rfc", json={"request_id": RFC_A}, headers=auth("bank_a"))
    assert first.status_code == 201
    out = first.json()
    assert out["recipient"] == "bank_a" and out["request_id"] == RFC_A
    request = out["rfc"]["request"]
    assert request["issuer"] == request["audience"] == "bank_a"
    assert request["txn_ids"] == ["txn-0001-004"] and "period" not in request
    assert out["rfc"]["signature"]["kid"] == "bank_a"
    verify(request, out["rfc"]["signature"], built.registry, domain="alibi:request:v1")
    assert out["account_refs"] == [ACCOUNT]
    assert out["forwarded"]["status"] == "accepted"
    assert sink.calls[0]["signed"] == out["rfc"] and sink.calls[0]["recipient"] == "bank_a"
    assert sink.calls[0]["account_refs"] == [ACCOUNT] and sink.calls[0]["now"] == AS_OF

    again = client.post("/rfc", json={"request_id": RFC_A}, headers=auth("bank_a"))
    assert again.status_code == 200 and again.json()["rfc"] == out["rfc"]
    assert len(sink.calls) == 1

    wrong = client.post("/rfc", json={"request_id": RFC_A}, headers=auth("bank_b"))
    assert wrong.status_code == 404 and wrong.json()["code"] == "unknown_request"
    missing = client.post("/rfc", json={"request_id": "rfc-0001-99"}, headers=auth("bank_b"))
    assert missing.status_code == 404 and missing.json()["reason"] != wrong.json()["reason"]
    assert missing.json()["reason"].replace("rfc-0001-99", RFC_A) == wrong.json()["reason"]

    second = client.post("/rfc", json={}, headers=auth("bank_b"))
    assert second.status_code == 201 and second.json()["request_id"] == RFC_B
    assert second.json()["rfc"]["request"]["period"] == OCTOBER
    assert "txn_ids" not in second.json()["rfc"]["request"]
    assert second.json()["rfc"]["signature"]["kid"] == "bank_b"
    none_left = client.post("/rfc", json={}, headers=auth("bank_b"))
    assert none_left.status_code == 404 and none_left.json()["code"] == "unknown_request"

    custom = {"request_id": "rfc-custom-01", "subject_ref": SUBJECT, "issuer": "bank_b",
              "audience": "bank_b", "txn_ids": ["txn-0001-004"],
              "questions": [{"question_id": "q.custom", "text": "Where did this credit come from?"}],
              "issued_at": AS_OF, "expires_at": "2026-11-12T09:00:00Z"}
    forced = client.post("/rfc", json={"request": custom}, headers=auth("bank_a"))
    assert forced.status_code == 201, forced.json()
    forced_request = forced.json()["rfc"]["request"]
    assert forced_request["issuer"] == forced_request["audience"] == "bank_a"
    assert forced.json()["rfc"]["signature"]["kid"] == "bank_a"
    other_subject = client.post("/rfc", json={"request": {**custom, "subject_ref": "someone-else"}},
                                headers=auth("bank_a"))
    assert other_subject.status_code == 400 and other_subject.json()["code"] == "invalid_request"
    both_scopes = client.post("/rfc", json={"request": {**custom, "request_id": "rfc-custom-02",
                                                        "period": OCTOBER}}, headers=auth("bank_a"))
    assert both_scopes.status_code == 400 and both_scopes.json()["code"] == "invalid_request"

    listed = client.get("/rfc", headers=auth("bank_a")).json()["requests"]
    assert [r["request_id"] for r in listed] == [RFC_A, "rfc-custom-01"]
    assert listed[0]["scope"] == {"txn_ids": ["txn-0001-004"]} and listed[0]["source"] == "case"
    assert listed[0]["question_ids"] == ["q.source_of_funds", "q.breakdown"]
    assert listed[0]["answered_by"] == [] and listed[1]["source"] == "custom"
    assert [r["request_id"] for r in client.get("/rfc", headers=auth("bank_b")).json()["requests"]] == [RFC_B]
    one = client.get(f"/rfc/{RFC_A}", headers=auth("bank_a"))
    assert one.status_code == 200 and one.json()["rfc"] == out["rfc"]
    assert client.get(f"/rfc/{RFC_B}", headers=auth("bank_a")).json()["code"] == "not_found"
    assert len(sink.calls) == 3


def test_case_requests_are_issuable_only_once_their_issued_at_has_passed(built):
    sink = SinkStub()
    client = make_app(built, sink=sink, seed=False)
    early = client.post("/admin/seed", headers=auth("admin"), params={"case": 1, "as_of": "2026-10-01T00:00:00Z"})
    assert early.status_code == 200 and early.json()["as_of"] == "2026-10-01T00:00:00Z"
    for recipient in ("bank_a", "bank_b"):
        nothing = client.post("/rfc", json={}, headers=auth(recipient))
        assert nothing.status_code == 404 and nothing.json()["code"] == "unknown_request"
    by_id = client.post("/rfc", json={"request_id": RFC_A}, headers=auth("bank_a"))
    assert by_id.status_code == 404 and by_id.json()["code"] == "unknown_request"
    missing = client.post("/rfc", json={"request_id": "rfc-0001-99"}, headers=auth("bank_a"))
    assert by_id.json()["reason"] == missing.json()["reason"].replace("rfc-0001-99", RFC_A)
    assert client.get("/rfc", headers=auth("bank_a")).json()["requests"] == []

    assert client.post("/admin/seed", headers=auth("admin"), params={"case": 1}).json()["as_of"] == DEFAULT_AS_OF
    first = client.post("/rfc", json={}, headers=auth("bank_a"))
    assert first.status_code == 201 and first.json()["request_id"] == RFC_A
    assert first.json()["rfc"]["request"]["issued_at"] == DEFAULT_AS_OF
    not_yet = client.post("/rfc", json={}, headers=auth("bank_b"))
    assert not_yet.status_code == 404 and not_yet.json()["code"] == "unknown_request"
    not_yet_by_id = client.post("/rfc", json={"request_id": RFC_B}, headers=auth("bank_b"))
    assert not_yet_by_id.status_code == 404 and not_yet_by_id.json()["code"] == "unknown_request"
    future = {"request_id": "rfc-custom-future", "subject_ref": SUBJECT, "txn_ids": ["txn-0001-004"],
              "questions": [{"question_id": "q.x", "text": "later"}],
              "issued_at": "2027-01-01T00:00:00Z", "expires_at": "2027-02-01T00:00:00Z"}
    ahead = client.post("/rfc", json={"request": future}, headers=auth("bank_a"))
    assert ahead.status_code == 400 and ahead.json()["code"] == "invalid_request"
    assert "issued_at" in ahead.json()["reason"]
    assert client.get("/rfc", headers=auth("bank_b")).json()["requests"] == []
    assert [c["signed"]["request"]["request_id"] for c in sink.calls] == [RFC_A]
    for call in sink.calls:
        assert call["signed"]["request"]["issued_at"] <= call["now"]

    assert client.post("/admin/advance", headers=auth("admin"), params={"as_of": RFC_B_ISSUED_AT}).status_code == 200
    now_visible = client.post("/rfc", json={}, headers=auth("bank_b"))
    assert now_visible.status_code == 201 and now_visible.json()["request_id"] == RFC_B
    assert now_visible.json()["rfc"]["request"]["issued_at"] == RFC_B_ISSUED_AT
    assert sink.calls[-1]["now"] == RFC_B_ISSUED_AT
    listed = client.get("/rfc", headers=auth("bank_b")).json()["requests"]
    assert [r["issued_at"] for r in listed] == [RFC_B_ISSUED_AT]


def test_expired_request_is_refused_and_an_expired_package_is_rejected(client, built):
    issue_both(client)
    backwards = client.post("/admin/advance", headers=auth("admin"), params={"as_of": "2026-10-01T00:00:00Z"})
    assert backwards.status_code == 409 and backwards.json()["code"] == "clock_backwards"
    bad_time = client.post("/admin/advance", headers=auth("admin"), params={"as_of": "yesterday"})
    assert bad_time.status_code == 400 and bad_time.json()["code"] == "invalid_time"
    assert client.post("/admin/advance", headers=auth("admin"), params={"as_of": EXPIRED_AT}).json() == {"as_of": EXPIRED_AT}
    expired = client.post("/rfc", json={"request_id": RFC_A}, headers=auth("bank_a"))
    assert expired.status_code == 409 and expired.json()["code"] == "request_expired"
    late = client.post("/deliver", json=built.p1, headers=auth("bank_a"))
    assert late.status_code == 422, late.json()
    body = late.json()
    assert body["code"] == "package_rejected" and body["receipt"]["accepted"] is False
    assert any("expired" in reason for reason in body["receipt"]["reasons"])
    assert any("expired" in reason for reason in body["rejection"]["errors"])
    assert body["rejection"]["authentication"]["valid"] is False
    inbox = client.get("/inbox", headers=auth("bank_a")).json()
    assert [p["status"] for p in inbox["packages"]] == ["rejected"]
    assert inbox["as_of"] == EXPIRED_AT


# --------------------------------------------------------------------------- delivery


def test_delivery_verifies_and_is_idempotent_on_identical_bytes(client, built):
    issue_both(client)
    p1 = built.p1
    delivered = client.post("/deliver", json=p1, headers=auth("bank_a"))
    assert delivered.status_code == 200, delivered.json()
    out = delivered.json()
    assert out["status"] == "verified"
    receipt = out["receipt"]
    assert receipt["package_id"] == p1["package_id"]
    assert receipt["package_sha256"] == canon.digest(p1)
    assert receipt["accepted"] is True and receipt["recipient"] == "bank_a"
    assert receipt["received_at"] == AS_OF
    assert out["authentication"]["valid"] is True
    assert isinstance(out["sources"], list) and isinstance(out["claims"], list)
    # Without the lookup-only settlement both answers are unresolved, so P1 shares nothing.
    assert out["sources"] == [] and p1["body"]["attachments"] == []
    assert {c["question_id"] for c in out["claims"]} == {"q.source_of_funds", "q.breakdown"}
    assert all(c["status"] == "unresolved" and not c.get("evidence_refs") for c in p1["body"]["claims"])

    retry = client.post("/deliver", json=p1, headers=auth("bank_a"))
    assert retry.status_code == 200 and retry.json()["receipt"]["receipt_id"] == receipt["receipt_id"]
    inbox = client.get("/inbox", headers=auth("bank_a")).json()
    assert [p["package_id"] for p in inbox["packages"]] == [p1["package_id"]]
    assert inbox["packages"][0]["status"] == "verified"
    assert inbox["packages"][0]["request_id"] == RFC_A
    assert inbox["packages"][0]["receipt_id"] == receipt["receipt_id"]
    assert inbox["packages"][0]["disposition"] is None and "reasons" not in inbox["packages"][0]
    assert inbox["requests_sent"][0]["answered_by"] == [p1["package_id"]]

    changed = copy.deepcopy(p1)
    changed["body"]["claims"][0]["statement"] += "."
    reused = client.post("/deliver", json=changed, headers=auth("bank_a"))
    assert reused.status_code == 409 and reused.json()["code"] == "package_id_reused"
    assert client.get(f"/packages/{p1['package_id']}/raw", headers=auth("bank_a")).json() == p1
    assert len(client.get("/inbox", headers=auth("bank_a")).json()["packages"]) == 1

    p3 = built.p3
    wrong_audience = client.post("/deliver", json=p3, headers=auth("bank_a"))
    assert wrong_audience.status_code == 409 and wrong_audience.json()["code"] == "audience_mismatch"
    assert wrong_audience.json()["receipt"]["accepted"] is False
    listed = {p["package_id"]: p for p in client.get("/inbox", headers=auth("bank_a")).json()["packages"]}
    assert listed[p3["package_id"]]["status"] == "rejected" and listed[p3["package_id"]]["reasons"]
    view = client.get(f"/packages/{p3['package_id']}", headers=auth("bank_a")).json()
    assert view["status"] == "rejected" and "package" not in view and view["rejection"]["reasons"]
    attachments = client.get(f"/packages/{p3['package_id']}/attachments", headers=auth("bank_a"))
    assert attachments.status_code == 409 and attachments.json()["code"] == "package_rejected"
    replay = client.post("/deliver", json=p3, headers=auth("bank_a"))
    assert replay.status_code == 409 and replay.json()["code"] == "audience_mismatch"


def _package_texts(package):
    """Free-text strings the package body carries (claim statements, gaps, attachment notes).
    The echoed request questions are skipped: the recipient's own sent request holds them."""
    found = []

    def walk(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "questions":
                    continue
                if key in ("statement", "gap", "note", "text") and isinstance(item, str) \
                        and len(item) >= 12:
                    found.append(item)
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(package["body"])
    return found


def test_tampered_package_is_rejected_and_keeps_no_contents(built, tmp_path):
    state_path = tmp_path / "state.json"
    client = make_app(built, state_path=state_path)
    issue_both(client)
    tampered = copy.deepcopy(built.p1)   # P1 shares nothing, so tamper with its claim text
    changed = "TAMPERED-CLAIM-TEXT"
    tampered["body"]["claims"][0]["statement"] += f" {changed}"
    rejected = client.post("/deliver", json=tampered, headers=auth("bank_a"))
    assert rejected.status_code == 422 and rejected.json()["code"] == "package_rejected"
    assert rejected.json()["receipt"]["package_sha256"] == canon.digest(tampered)
    assert set(rejected.json()["rejection"]) == {"authentication", "errors"}
    pid = tampered["package_id"]
    inbox = client.get("/inbox", headers=auth("bank_a")).json()["packages"]
    assert [(p["package_id"], p["status"]) for p in inbox] == [(pid, "rejected")]
    view = client.get(f"/packages/{pid}", headers=auth("bank_a")).json()
    assert view["status"] == "rejected" and "package" not in view
    assert changed not in json.dumps(view) and "amount_pence" not in json.dumps(view)
    for suffix in ("/attachments", "/raw"):
        response = client.get(f"/packages/{pid}{suffix}", headers=auth("bank_a"))
        assert response.status_code == 409 and response.json()["code"] == "package_rejected"
    assert changed not in json.dumps(client.get("/audit", headers=auth("bank_a")).json())

    # The replay of a rejected delivery returns the stored receipt and the same 422.
    replay = client.post("/deliver", json=tampered, headers=auth("bank_a"))
    assert replay.status_code == 422 and replay.json()["code"] == "package_rejected"
    assert replay.json()["receipt"] == rejected.json()["receipt"]
    assert replay.json()["rejection"] == rejected.json()["rejection"]
    assert len(client.get("/inbox", headers=auth("bank_a")).json()["packages"]) == 1

    # The state file keeps reasons and digests only: no package, no sources, no claims.
    text = state_path.read_text(encoding="utf-8")
    entries = json.loads(text)["recipients"]["bank_a"]["inbox"]
    assert [e["package_id"] for e in entries] == [pid]
    entry = entries[0]
    assert entry["status"] == "rejected" and entry["package"] is None
    assert set(entry["verification"]) == {"authentication", "errors"}
    assert entry["package_sha256"] == canon.digest(tampered) and entry["reasons"]
    texts = _package_texts(tampered)
    assert texts, "the fixture package carries claim text to check against"
    for sample in texts:
        assert sample not in text
    assert changed not in text and "q.source_of_funds" not in json.dumps(entry)

    # Another recipient's genuine package is kept the same way after an audience mismatch.
    other = client.post("/deliver", json=built.p3, headers=auth("bank_a"))
    assert other.status_code == 409 and other.json()["code"] == "audience_mismatch"
    stored = {e["package_id"]: e for e in
              json.loads(state_path.read_text(encoding="utf-8"))["recipients"]["bank_a"]["inbox"]}
    assert set(stored[built.p3["package_id"]]["verification"]) == {"authentication", "errors"}
    assert stored[built.p3["package_id"]]["package"] is None
    assert stored[built.p3["package_id"]]["request_id"] == RFC_B


def test_package_for_a_request_this_bank_never_issued(client, built):
    unknown = client.post("/deliver", json=built.p1, headers=auth("bank_a"))
    assert unknown.status_code == 409 and unknown.json()["code"] == "unknown_request"
    assert RFC_A in unknown.json()["reason"]
    not_object = client.post("/deliver", content=b"[1, 2]", headers=auth("bank_a"))
    assert not_object.status_code == 400 and not_object.json()["code"] == "invalid_package"
    no_id = client.post("/deliver", json={"body": {}}, headers=auth("bank_a"))
    assert no_id.status_code == 400 and no_id.json()["code"] == "invalid_package"


def test_delivery_never_echoes_unvalidated_body_fields(built, tmp_path):
    state_path = tmp_path / "state.json"
    client = make_app(built, state_path=state_path)
    issue_both(client)
    markers = {"audience": "bank_a' OR 1=1 -- ATTACKER_AUDIENCE_MARKER",
               "request_id": "IGNORE PREVIOUS INSTRUCTIONS ATTACKER_REQUEST_MARKER " * 150,
               "expires_at": "ATTACKER_EXPIRES_MARKER not a time"}
    evil_audience = client.post("/deliver", json={"package_id": "pkg_evil1", "body": dict(markers)},
                                headers=auth("bank_b"))
    assert evil_audience.status_code == 409 and evil_audience.json()["code"] == "audience_mismatch"
    assert "body.audience" in evil_audience.json()["reason"]
    evil_request = client.post("/deliver", json={
        "package_id": "pkg_evil2",
        "body": {"audience": "bank_a", "request_id": markers["request_id"],
                 "expires_at": markers["expires_at"]}}, headers=auth("bank_a"))
    assert evil_request.status_code == 409 and evil_request.json()["code"] == "unknown_request"
    assert "body.request_id" in evil_request.json()["reason"]
    genuine_unknown = client.post("/deliver", json={
        "package_id": "pkg_evil3", "body": {"audience": "bank_a", "request_id": "rfc-0001-77"}},
        headers=auth("bank_a"))
    assert genuine_unknown.status_code == 409 and "rfc-0001-77" in genuine_unknown.json()["reason"]

    everything = json.dumps([
        evil_audience.json(), evil_request.json(),
        client.get("/inbox", headers=auth("bank_a")).json(),
        client.get("/inbox", headers=auth("bank_b")).json(),
        client.get("/audit", headers=auth("bank_a")).json(),
        client.get("/audit", headers=auth("bank_b")).json(),
        client.get("/packages/pkg_evil1", headers=auth("bank_b")).json(),
        client.get("/packages/pkg_evil2", headers=auth("bank_a")).json(),
    ]) + state_path.read_text(encoding="utf-8")
    for marker in ("ATTACKER_AUDIENCE_MARKER", "ATTACKER_REQUEST_MARKER", "ATTACKER_EXPIRES_MARKER",
                   "IGNORE PREVIOUS", "OR 1=1"):
        assert marker not in everything, marker
    by_id = {p["package_id"]: p for p in client.get("/inbox", headers=auth("bank_a")).json()["packages"]}
    assert by_id["pkg_evil2"]["request_id"] is None and "expires_at" not in by_id["pkg_evil2"]
    assert by_id["pkg_evil3"]["request_id"] == "rfc-0001-77"
    assert all(len(json.dumps(p)) < 2000 for p in by_id.values())


def test_malformed_json_bodies_are_refusals_not_server_errors(client, built):
    issue_both(client)
    headers = {**auth("bank_a"), "content-type": "application/json"}
    for payload in (b'{"package_id":"pkg_x","n":1e400}',
                    b'{"package_id":"pkg_x","n":1180591620717411303424}'):
        response = client.post("/deliver", content=payload, headers=headers)
        assert response.status_code == 400 and response.json()["code"] == "invalid_package", payload
        assert "pkg_x" in response.json()["reason"]
    assert client.get("/inbox", headers=auth("bank_a")).json()["packages"] == []
    audit = client.get("/audit", headers=auth("bank_a")).json()["entries"]
    assert sum(1 for e in audit if e["action"] == "deliver" and "invalid_package" in e["detail"]) == 2

    deep = b'{"request":' + b"[" * 50000 + b"]" * 50000 + b"}"
    pid = built.p1["package_id"]
    for path, code in (("/deliver", "invalid_package"), ("/rfc", "invalid_request"),
                       (f"/packages/{pid}/disposition", "invalid_disposition")):
        response = client.post(path, content=deep, headers=headers)
        assert response.status_code == 400 and response.json()["code"] == code, path
        assert "nested" in response.json()["reason"]


def test_unknown_routes_and_methods_keep_the_refusal_shape(client):
    missing = client.get("/nope", headers=auth("bank_a"))
    assert missing.status_code == 404 and missing.json() == {"code": "not_found", "reason": "no such route"}
    split = client.get("/packages/..%2F..%2Fetc%2Fpasswd", headers=auth("bank_a"))
    assert split.status_code == 404 and split.json()["code"] == "not_found"
    assert "passwd" not in json.dumps(split.json())
    wrong_method = client.delete("/inbox", headers=auth("bank_a"))
    assert wrong_method.status_code == 405 and wrong_method.json()["code"] == "method_not_allowed"
    assert set(wrong_method.json()) == {"code", "reason"}


def test_custom_request_scope_of_the_wrong_shape_is_a_refusal(built):
    """``txn_ids`` as an int was a text/plain 500 (TypeError in map_rfc's list()); as a dict or
    a string it was silently coerced into its keys or characters and issued."""
    client = TestClient(create_bank_app(keystore=built.keystore, registry=built.registry,
                                        tokens=TOKENS), raise_server_exceptions=False)
    assert client.post("/admin/seed", headers=auth("admin"), params={"case": 1, "as_of": AS_OF}).status_code == 200
    base = {"subject_ref": SUBJECT, "issued_at": AS_OF, "expires_at": EXPIRED_AT,
            "questions": [{"question_id": "q.x", "text": "why"}]}
    cases = [({"txn_ids": 1}, "txn_ids"), ({"txn_ids": True}, "txn_ids"),
             ({"txn_ids": {"a": 1}}, "txn_ids"), ({"txn_ids": "txn-0001-004"}, "txn_ids"),
             ({"txn_ids": [1]}, "txn_ids"), ({"txn_ids": "abc"}, "txn_ids"),
             ({"period": [OCTOBER]}, "period"), ({"period": "october"}, "period"),
             ({"txn_ids": ["txn-0001-004"], "questions": "why"}, "questions")]
    for n, (scope, field) in enumerate(cases):
        request = {**base, "request_id": f"cust-{n}", **scope}
        response = client.post("/rfc", json={"request": request}, headers=auth("bank_a"))
        assert response.status_code == 400, (scope, response.text)
        assert response.headers["content-type"].startswith("application/json")
        assert response.json()["code"] == "invalid_request" and field in response.json()["reason"]
    assert client.get("/rfc", headers=auth("bank_a")).json()["requests"] == []
    good = client.post("/rfc", json={"request": {**base, "request_id": "cust-ok",
                                                 "txn_ids": ["txn-0001-004"]}},
                       headers=auth("bank_a"))
    assert good.status_code == 201 and good.json()["rfc"]["request"]["txn_ids"] == ["txn-0001-004"]


# --------------------------------------------------------------------------- isolation


def test_recipients_never_see_each_others_packages_or_requests(client, built):
    issue_both(client)
    p1, p3 = built.p1, built.p3
    assert client.post("/deliver", json=p1, headers=auth("bank_a")).status_code == 200
    assert client.post("/deliver", json=p3, headers=auth("bank_b")).status_code == 200
    assert [p["package_id"] for p in client.get("/inbox", headers=auth("bank_a")).json()["packages"]] == [p1["package_id"]]
    assert [p["package_id"] for p in client.get("/inbox", headers=auth("bank_b")).json()["packages"]] == [p3["package_id"]]
    made_up = client.get("/packages/pkg_made_up", headers=auth("bank_a")).json()
    for path in (f"/packages/{p3['package_id']}", f"/packages/{p3['package_id']}/attachments",
                 f"/packages/{p3['package_id']}/raw"):
        response = client.get(path, headers=auth("bank_a"))
        assert response.status_code == 404 and response.json()["code"] == "not_found"
        assert response.json()["reason"] == made_up["reason"].replace("pkg_made_up", p3["package_id"])
    disposition = client.post(f"/packages/{p3['package_id']}/disposition",
                              json={"decision": "clear", "reason": "ok"}, headers=auth("bank_a"))
    assert disposition.status_code == 404
    assert disposition.json()["reason"] == made_up["reason"].replace("pkg_made_up", p3["package_id"])
    assert [r["request_id"] for r in client.get("/rfc", headers=auth("bank_a")).json()["requests"]] == [RFC_A]
    assert [r["request_id"] for r in client.get("/rfc", headers=auth("bank_b")).json()["requests"]] == [RFC_B]


# --------------------------------------------------------------------------- views


def test_verified_package_view_attachments_and_raw(client, built):
    issue_both(client)
    p3 = built.p3   # P1's answers are unresolved and share nothing; P3 carries the records
    assert client.post("/deliver", json=p3, headers=auth("bank_b")).status_code == 200
    view = client.get(f"/packages/{p3['package_id']}", headers=auth("bank_b")).json()
    assert view["status"] == "verified" and view["receipt"]["accepted"] is True
    assert view["authentication"]["valid"] is True
    assert view["sources"] and view["claims"]
    assert all({"claimed_trust", "attestation"} <= set(s) for s in view["sources"])
    package = view["package"]
    assert package["package_id"] == p3["package_id"] and package["body_sha256"] == p3["body_sha256"]
    assert package["issued_at"] == p3["issued_at"]
    assert package["body"]["scope"] == {"period": {"from": "2026-10-01T00:00:00Z",
                                                   "to": "2026-11-01T00:00:00Z"}}
    assert package["body"]["request_id"] == RFC_B and package["body"]["audience"] == "bank_b"
    assert package["body"]["attachments"]
    for attachment in package["body"]["attachments"]:
        assert "content" not in attachment and "issuer_signature" not in attachment
        assert {"attachment_id", "ref", "form", "source_trust"} <= set(attachment)
    assert view["dispositions"] == [] and view["follow_ups"] == []
    full = client.get(f"/packages/{p3['package_id']}/attachments", headers=auth("bank_b")).json()
    assert full["package_id"] == p3["package_id"]
    assert full["attachments"] == p3["body"]["attachments"]
    assert full["attachments"][0]["content"]["amount_pence"] == 30000   # rec-0001-001, a sale
    assert client.get(f"/packages/{p3['package_id']}/raw", headers=auth("bank_b")).json() == p3


# --------------------------------------------------------------------------- dispositions


def test_dispositions_and_follow_ups(built):
    sink = SinkStub()
    client = make_app(built, sink=sink)
    issue_both(client)
    p1, p3 = built.p1, built.p3
    pid1, pid3 = p1["package_id"], p3["package_id"]
    receipt = client.post("/deliver", json=p1, headers=auth("bank_a")).json()["receipt"]
    assert client.post("/deliver", json=p3, headers=auth("bank_b")).status_code == 200
    assert client.post("/deliver", json=p3, headers=auth("bank_a")).status_code == 409   # rejected entry
    sink_calls_before = len(sink.calls)

    more = client.post(f"/packages/{pid1}/disposition", json={
        "decision": "request_more", "reason": "Need the settlement record for PO-0001-01",
        "question_ids": ["q.source_of_funds"],
        "requested_evidence": ["settlement record for payout PO-0001-01"],
        "analyst_name": "Mallory"}, headers=auth("bank_a"))
    assert more.status_code == 200, more.json()
    disposition = more.json()["disposition"]
    assert disposition["analyst"] == "analyst_a1" and "Mallory" not in json.dumps(more.json())
    assert disposition["package_digest"] == receipt["package_sha256"]
    assert disposition["recipient"] == "bank_a" and disposition["decided_at"] == AS_OF
    assert disposition["supersedes"] is None and disposition["decision"] == "request_more"
    assert disposition["package_id"] == pid1 and disposition["request_id"] == RFC_A
    assert disposition["question_ids"] == ["q.source_of_funds"]
    assert disposition["requested_evidence"] == ["settlement record for payout PO-0001-01"]
    follow_up = more.json()["follow_up"]
    request = follow_up["rfc"]["request"]
    assert request["parent_request_id"] == RFC_A and request["request_id"] != RFC_A
    assert follow_up["request_id"] == request["request_id"] == f"{RFC_A}-f1"
    assert request["txn_ids"] == ["txn-0001-004"] and "period" not in request
    assert request["issuer"] == request["audience"] == "bank_a"
    assert request["questions"] == [{"question_id": "q.followup_1",
                                     "text": "settlement record for payout PO-0001-01"}]
    assert request["issued_at"] == AS_OF
    assert request["expires_at"] == format_utc(parse_utc(AS_OF) + timedelta(days=7))
    assert follow_up["rfc"]["signature"]["kid"] == "bank_a"
    verify(request, follow_up["rfc"]["signature"], built.registry, domain="alibi:request:v1")
    assert follow_up["account_refs"] == [ACCOUNT] and follow_up["forwarded"]["status"] == "accepted"
    assert sink.calls[-1]["signed"] == follow_up["rfc"] and len(sink.calls) == sink_calls_before + 1
    listed = {r["request_id"]: r for r in client.get("/rfc", headers=auth("bank_a")).json()["requests"]}
    assert listed[f"{RFC_A}-f1"]["source"] == "follow_up"
    assert listed[f"{RFC_A}-f1"]["parent_request_id"] == RFC_A
    assert client.get(f"/packages/{pid1}", headers=auth("bank_a")).json()["follow_ups"] == [f"{RFC_A}-f1"]
    assert client.get("/inbox", headers=auth("bank_a")).json()["packages"][0]["disposition"] == "request_more"

    wider = client.post(f"/packages/{pid1}/disposition", json={
        "decision": "request_more", "reason": "more", "txn_ids": ["txn-0001-004", "txn-0001-001"],
        "requested_evidence": ["anything"]}, headers=auth("bank_a"))
    assert wider.status_code == 409 and wider.json()["code"] == "scope_wider_than_parent"
    assert len(client.get("/rfc", headers=auth("bank_a")).json()["requests"]) == 2
    assert len(client.get(f"/packages/{pid1}/dispositions", headers=auth("bank_a")).json()["dispositions"]) == 1
    assert len(sink.calls) == sink_calls_before + 1

    wider_period = client.post(f"/packages/{pid3}/disposition", json={
        "decision": "request_more", "reason": "more",
        "period": {"from": "2026-09-01T00:00:00Z", "to": "2026-11-01T00:00:00Z"},
        "requested_evidence": ["September too"]}, headers=auth("bank_b"))
    assert wider_period.status_code == 409 and wider_period.json()["code"] == "scope_wider_than_parent"
    narrower = client.post(f"/packages/{pid3}/disposition", json={
        "decision": "request_more", "reason": "narrow it down",
        "period": {"from": "2026-10-10T00:00:00Z", "to": "2026-10-20T00:00:00Z"},
        "questions": [{"question_id": "q.mid_october", "text": "Sales between 10 and 20 October?"}]},
        headers=auth("bank_b"))
    assert narrower.status_code == 200, narrower.json()
    narrowed = narrower.json()["follow_up"]["rfc"]["request"]
    assert narrowed["period"] == {"from": "2026-10-10T00:00:00Z", "to": "2026-10-20T00:00:00Z"}
    assert "txn_ids" not in narrowed and narrowed["parent_request_id"] == RFC_B
    assert narrowed["request_id"] == f"{RFC_B}-f1" and narrowed["questions"][0]["question_id"] == "q.mid_october"
    assert narrower.json()["follow_up"]["rfc"]["signature"]["kid"] == "bank_b"
    assert narrower.json()["disposition"]["analyst"] == "analyst_b1"

    cleared = client.post(f"/packages/{pid1}/disposition", json={"decision": "clear", "reason": "ok"},
                          headers=auth("bank_a"))
    assert cleared.status_code == 200
    assert cleared.json()["disposition"]["supersedes"] == disposition["disposition_id"]
    assert cleared.json()["follow_up"] is None
    final = client.post(f"/packages/{pid1}/disposition", json={"decision": "escalate", "reason": "x"},
                        headers=auth("bank_a"))
    assert final.status_code == 409 and final.json()["code"] == "disposition_final"
    escalated = client.post(f"/packages/{pid3}/disposition",
                            json={"decision": "escalate", "reason": "suspicious"}, headers=auth("bank_b"))
    assert escalated.status_code == 200
    assert client.post(f"/packages/{pid3}/disposition", json={"decision": "clear", "reason": "ok"},
                       headers=auth("bank_b")).json()["code"] == "disposition_final"

    for bad in ({"decision": "approve", "reason": "ok"}, {"decision": "clear", "reason": ""},
                {"decision": "clear"}, {"reason": "ok"}):
        response = client.post(f"/packages/{pid3}/disposition", json=bad, headers=auth("bank_b"))
        assert response.status_code == 400 and response.json()["code"] == "invalid_disposition", bad
    on_rejected = client.post(f"/packages/{pid3}/disposition", json={"decision": "clear", "reason": "ok"},
                              headers=auth("bank_a"))
    assert on_rejected.status_code == 409 and on_rejected.json()["code"] == "package_rejected"

    both = client.get(f"/packages/{pid1}/dispositions", headers=auth("bank_a")).json()["dispositions"]
    assert [d["decision"] for d in both] == ["request_more", "clear"]
    assert both == client.get(f"/packages/{pid1}", headers=auth("bank_a")).json()["dispositions"]

    audit = client.get("/audit", headers=auth("bank_a")).json()["entries"]
    assert audit and all(e["actor"] in ("bank_a", "analyst_a1") for e in audit)
    assert {e["action"] for e in audit} >= {"issue_request", "deliver", "disposition"}
    assert {e["outcome"] for e in audit} == {"ok", "refused"}
    refused = [e for e in audit if e["outcome"] == "refused"]
    assert any("scope_wider_than_parent" in e["detail"] for e in refused)
    assert any("disposition_final" in e["detail"] for e in refused)
    assert all(e["at"] == AS_OF for e in audit)
    audit_b = client.get("/audit", headers=auth("bank_b")).json()["entries"]
    assert audit_b and all(e["actor"] in ("bank_b", "analyst_b1") for e in audit_b)
    assert "Mallory" not in json.dumps(audit) and "tok-" not in json.dumps(audit)


def test_custom_follow_up_inherits_the_parent_scope_and_ids_cannot_be_squatted(built):
    sink = SinkStub()
    client = make_app(built, sink=sink)
    assert client.post("/rfc", json={"request_id": RFC_A}, headers=auth("bank_a")).status_code == 201
    base = {"subject_ref": SUBJECT, "parent_request_id": RFC_A,
            "questions": [{"question_id": "q.x", "text": "everything"}],
            "issued_at": AS_OF, "expires_at": "2026-11-12T09:00:00Z"}

    wide = client.post("/rfc", json={"request": {**base, "request_id": "rfc-wide",
                                                "period": {"from": "2026-01-01T00:00:00Z",
                                                           "to": "2026-11-01T00:00:00Z"}}},
                       headers=auth("bank_a"))
    assert wide.status_code == 409 and wide.json()["code"] == "scope_wider_than_parent"
    outside = client.post("/rfc", json={"request": {**base, "request_id": "rfc-outside",
                                                   "txn_ids": ["txn-0001-004", "txn-0001-001"]}},
                          headers=auth("bank_a"))
    assert outside.status_code == 409 and outside.json()["code"] == "scope_wider_than_parent"
    others_parent = client.post("/rfc", json={"request": {**base, "request_id": "rfc-theirs",
                                                         "parent_request_id": RFC_B,
                                                         "txn_ids": ["txn-0001-004"]}},
                                headers=auth("bank_a"))
    assert others_parent.status_code == 400 and others_parent.json()["code"] == "invalid_request"
    assert RFC_B in others_parent.json()["reason"]
    no_parent = client.post("/rfc", json={"request": {**base, "request_id": "rfc-orphan",
                                                     "parent_request_id": "rfc-0001-55",
                                                     "txn_ids": ["txn-0001-004"]}},
                            headers=auth("bank_a"))
    assert no_parent.status_code == 400 and no_parent.json()["code"] == "invalid_request"
    junk_parent = client.post("/rfc", json={"request": {**base, "request_id": "rfc-junk",
                                                       "parent_request_id": "not an id MARKER",
                                                       "txn_ids": ["txn-0001-004"]}},
                              headers=auth("bank_a"))
    assert junk_parent.status_code == 400 and "MARKER" not in junk_parent.json()["reason"]
    assert [r["request_id"] for r in client.get("/rfc", headers=auth("bank_a")).json()["requests"]] == [RFC_A]
    assert len(sink.calls) == 1

    inherited = client.post("/rfc", json={"request": {**base, "request_id": "rfc-inherit",
                                                     "txn_ids": [], "period": None}},
                            headers=auth("bank_a"))
    assert inherited.status_code == 201, inherited.json()
    request = inherited.json()["rfc"]["request"]
    assert request["parent_request_id"] == RFC_A and request["txn_ids"] == ["txn-0001-004"]
    assert "period" not in request and request["issuer"] == request["audience"] == "bank_a"
    narrowed = client.post("/rfc", json={"request": {**base, "request_id": "rfc-narrow",
                                                    "txn_ids": ["txn-0001-004"]}},
                           headers=auth("bank_a"))
    assert narrowed.status_code == 201 and narrowed.json()["rfc"]["request"]["txn_ids"] == ["txn-0001-004"]
    listed = {r["request_id"]: r for r in client.get("/rfc", headers=auth("bank_a")).json()["requests"]}
    assert listed["rfc-inherit"]["source"] == "custom" and listed["rfc-inherit"]["parent_request_id"] == RFC_A

    # Nobody may mint a request under a case RFC id or another recipient's id.
    squat = client.post("/rfc", json={"request": {**base, "request_id": RFC_B, "parent_request_id": None,
                                                 "txn_ids": ["txn-0001-004"]}}, headers=auth("bank_a"))
    assert squat.status_code == 400 and squat.json()["code"] == "invalid_request"
    assert RFC_B in squat.json()["reason"]
    own_case_id = client.post("/rfc", json={"request": {**base, "request_id": RFC_A, "parent_request_id": None,
                                                       "period": OCTOBER}}, headers=auth("bank_a"))
    assert own_case_id.status_code == 400 and own_case_id.json()["code"] == "invalid_request"
    taken = client.post("/rfc", json={"request": {**base, "request_id": "rfc-inherit", "parent_request_id": None,
                                                 "txn_ids": ["txn-0001-004"]}}, headers=auth("bank_b"))
    assert taken.status_code == 400 and taken.json()["code"] == "invalid_request"
    assert client.post("/rfc", json={}, headers=auth("bank_b")).json()["request_id"] == RFC_B
    assert client.get("/rfc", headers=auth("bank_b")).json()["requests"][0]["request_id"] == RFC_B
    assert client.post("/deliver", json=built.p1, headers=auth("bank_a")).status_code == 200
    pid = built.p1["package_id"]
    squat_follow_up = client.post(f"/packages/{pid}/disposition", json={
        "decision": "request_more", "reason": "x", "requested_evidence": ["e"], "request_id": RFC_B},
        headers=auth("bank_a"))
    assert squat_follow_up.status_code == 400 and squat_follow_up.json()["code"] == "invalid_disposition"
    assert {c["signed"]["request"]["request_id"] for c in sink.calls} == {RFC_A, RFC_B, "rfc-inherit", "rfc-narrow"}


def test_follow_up_request_id_from_the_body_and_bounded_fields(built):
    sink = SinkStub()
    client = make_app(built, sink=sink)
    issue_both(client)
    pid = built.p1["package_id"]
    assert client.post("/deliver", json=built.p1, headers=auth("bank_a")).status_code == 200
    chosen = client.post(f"/packages/{pid}/disposition", json={
        "decision": "request_more", "reason": "chosen id", "requested_evidence": ["the settlement"],
        "request_id": "rfc-0001-01-custom"}, headers=auth("bank_a"))
    assert chosen.status_code == 200, chosen.json()
    assert chosen.json()["follow_up"]["request_id"] == "rfc-0001-01-custom"
    assert chosen.json()["follow_up"]["rfc"]["request"]["parent_request_id"] == RFC_A
    again = client.post(f"/packages/{pid}/disposition", json={
        "decision": "request_more", "reason": "again", "requested_evidence": ["the settlement"],
        "request_id": "rfc-0001-01-custom"}, headers=auth("bank_a"))
    assert again.status_code == 400 and again.json()["code"] == "invalid_disposition"
    not_ident = client.post(f"/packages/{pid}/disposition", json={
        "decision": "request_more", "reason": "x", "requested_evidence": ["e"],
        "request_id": "not an identifier MARKER"}, headers=auth("bank_a"))
    assert not_ident.status_code == 400 and "MARKER" not in not_ident.json()["reason"]
    numbered = client.post(f"/packages/{pid}/disposition", json={
        "decision": "request_more", "reason": "numbered", "requested_evidence": ["more"]},
        headers=auth("bank_a"))
    assert numbered.status_code == 200 and numbered.json()["follow_up"]["request_id"] == f"{RFC_A}-f2"

    for bad in ({"decision": "clear", "reason": "R" * 301},
                {"decision": "clear", "reason": "ok", "question_ids": ["x" * 200]},
                {"decision": "clear", "reason": "ok", "question_ids": ["not an id"]},
                {"decision": "clear", "reason": "ok", "requested_evidence": ["e"] * 51},
                {"decision": "request_more", "reason": "ok", "requested_evidence": ["e" * 1001]}):
        response = client.post(f"/packages/{pid}/disposition", json=bad, headers=auth("bank_a"))
        assert response.status_code == 400 and response.json()["code"] == "invalid_disposition", bad
    assert len(client.get(f"/packages/{pid}/dispositions", headers=auth("bank_a")).json()["dispositions"]) == 2
    at_limit = client.post(f"/packages/{pid}/disposition", json={
        "decision": "clear", "reason": "R" * 300, "question_ids": ["q.source_of_funds"]},
        headers=auth("bank_a"))
    assert at_limit.status_code == 200


def test_a_failing_rfc_sink_is_reported_and_audited(built):
    def exploding_sink(signed, recipient, account_refs, now):
        raise RuntimeError("boom")

    client = make_app(built, sink=exploding_sink)
    issued = client.post("/rfc", json={"request_id": RFC_A}, headers=auth("bank_a"))
    assert issued.status_code == 201, issued.json()
    assert issued.json()["forwarded"] == {"status": "error", "reason": "the rfc_sink raised RuntimeError"}
    assert "boom" not in json.dumps(issued.json())
    assert [r["request_id"] for r in client.get("/rfc", headers=auth("bank_a")).json()["requests"]] == [RFC_A]
    audit = client.get("/audit", headers=auth("bank_a")).json()["entries"]
    assert audit[-1]["action"] == "issue_request" and audit[-1]["outcome"] == "ok"
    assert "forwarded error" in audit[-1]["detail"]


# --------------------------------------------------------------------------- persistence


def test_state_persists_across_apps_and_never_holds_tokens(built, tmp_path):
    state_path = tmp_path / "bank" / "state.json"
    first = make_app(built, state_path=state_path)
    issue_both(first)
    p1 = built.p1
    assert first.post("/deliver", json=p1, headers=auth("bank_a")).status_code == 200
    assert first.post(f"/packages/{p1['package_id']}/disposition",
                      json={"decision": "request_more", "reason": "more please",
                            "requested_evidence": ["the settlement record"]},
                      headers=auth("bank_a")).status_code == 200
    inbox = first.get("/inbox", headers=auth("bank_a")).json()
    view = first.get(f"/packages/{p1['package_id']}", headers=auth("bank_a")).json()
    dispositions = first.get(f"/packages/{p1['package_id']}/dispositions", headers=auth("bank_a")).json()
    requests = first.get("/rfc", headers=auth("bank_a")).json()
    audit = first.get("/audit", headers=auth("bank_a")).json()

    assert state_path.exists() and not state_path.with_name("state.json.tmp").exists()
    text = state_path.read_text(encoding="utf-8")
    for token in TOKENS.values():
        assert token not in text
    loaded = BankState.load(state_path)
    assert loaded.case_number == 1 and loaded.as_of == AS_OF and loaded.account_ref == ACCOUNT
    assert loaded.recipient("bank_a").inbox[p1["package_id"]].package == p1
    assert loaded.to_json() == BankState.from_json(loaded.to_json()).to_json()

    second = make_app(built, state_path=state_path, seed=False)
    assert second.get("/inbox", headers=auth("bank_a")).json() == inbox
    assert second.get(f"/packages/{p1['package_id']}", headers=auth("bank_a")).json() == view
    assert second.get(f"/packages/{p1['package_id']}/dispositions", headers=auth("bank_a")).json() == dispositions
    assert second.get("/rfc", headers=auth("bank_a")).json() == requests
    assert second.get("/audit", headers=auth("bank_a")).json() == audit
    assert second.get(f"/packages/{p1['package_id']}/raw", headers=auth("bank_a")).json() == p1
    assert second.get("/inbox", headers=auth("bank_b")).json()["packages"] == []


def test_unseeded_app_refuses_until_seeded(built):
    client = make_app(built, seed=False)
    response = client.post("/rfc", json={}, headers=auth("bank_a"))
    assert response.status_code == 404 and response.json()["code"] == "not_found"
    for path in ("/audit", "/inbox", "/rfc", "/packages/pkg_x"):
        unseeded = client.get(path, headers=auth("bank_a"))
        assert unseeded.status_code == 404 and unseeded.json()["code"] == "not_found", path
        assert unseeded.json()["reason"] == response.json()["reason"]
    unknown_case = client.post("/admin/seed", headers=auth("admin"), params={"case": 1000})
    assert unknown_case.status_code == 404 and unknown_case.json()["code"] == "unknown_case"
    bad_as_of = client.post("/admin/seed", headers=auth("admin"), params={"case": 1, "as_of": "2026-11-02T09:00:00"})
    assert bad_as_of.status_code == 400 and bad_as_of.json()["code"] == "invalid_time"
    assert client.post("/admin/seed", headers=auth("admin"), params={"case": 2}).json()["subject_ref"] == SUBJECT
    second = client.post("/rfc", json={}, headers=auth("bank_a")).json()
    assert second["request_id"] == "rfc-0002-01" and second["rfc"]["request"]["txn_ids"] == ["txn-0002-004"]


# --------------------------------------------------------------------------- tokens


def test_load_tokens_creates_a_private_file_and_env_overrides(tmp_path, monkeypatch):
    for var in ("ALIBI_BANK_A_TOKEN", "ALIBI_BANK_B_TOKEN", "ALIBI_FEED_TOKEN", "ALIBI_ADMIN_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    path = tmp_path / "tokens.json"
    created = load_tokens(path)
    assert set(created) == {"bank_a", "bank_b", "feed", "admin"}
    assert len(set(created.values())) == 4 and all(len(v) >= 32 for v in created.values())
    assert path.exists()
    if os.name != "nt":
        assert (path.stat().st_mode & 0o777) == 0o600
    assert load_tokens(path) == created
    assert json.loads(path.read_text(encoding="utf-8")) == created
    monkeypatch.setenv("ALIBI_BANK_A_TOKEN", "env-a")
    overridden = load_tokens(path)
    assert overridden["bank_a"] == "env-a"
    assert overridden["bank_b"] == created["bank_b"] and overridden["feed"] == created["feed"]
    assert overridden["admin"] == created["admin"]
    monkeypatch.setenv("ALIBI_BANK_B_TOKEN", "env-b")
    monkeypatch.setenv("ALIBI_FEED_TOKEN", "env-feed")
    monkeypatch.setenv("ALIBI_ADMIN_TOKEN", "env-admin")
    assert load_tokens(tmp_path / "never-created.json") == {"bank_a": "env-a", "bank_b": "env-b",
                                                           "feed": "env-feed", "admin": "env-admin"}
    assert not (tmp_path / "never-created.json").exists()


def test_app_refuses_incomplete_tokens(built):
    with pytest.raises(ValueError):
        create_bank_app(keystore=built.keystore, registry=built.registry, tokens={"bank_a": "x"})
    # ``admin`` is not a required identity: without it the app makes its own admin token.
    app = create_bank_app(keystore=built.keystore, registry=built.registry,
                          tokens={"bank_a": "x", "bank_b": "y", "feed": "z"})
    assert app.state.admin_token and app.state.admin_token not in ("x", "y", "z")
    assert TestClient(app).post("/admin/seed", params={"case": 1}).status_code == 401


# --------------------------------------------------------------------------- feed


def test_feed_routes_serve_the_case_rows_to_the_feed_token(client):
    pytest.importorskip("alibi.connectors.bank_feed")
    page = client.get("/feed/transactions", headers=auth("feed"))
    assert page.status_code == 200, page.json()
    out = page.json()
    assert out["connection_id"] == f"conn-{SUBJECT}" and out["account_ref"] == ACCOUNT
    assert out["as_of"] == AS_OF and out["source_version"] == "mock_bank_feed.v1"
    assert [t["txn_id"] for t in out["transactions"]] == [f"txn-0001-00{i}" for i in range(1, 5)]
    assert out["has_more"] is False and out["next_cursor"]
    small = client.get("/feed/transactions", params={"limit": 2}, headers=auth("feed")).json()
    assert len(small["transactions"]) == 2 and small["has_more"] is True
    rest = client.get("/feed/transactions", params={"cursor": small["next_cursor"]},
                      headers=auth("feed")).json()
    assert [t["txn_id"] for t in rest["transactions"]] == ["txn-0001-003", "txn-0001-004"]
    assert client.get("/feed/transactions", headers=auth("bank_a")).status_code == 401
    assert client.get("/feed/transactions").status_code == 401
    one = client.get("/feed/transactions/txn-0001-004", headers=auth("feed"))
    assert one.status_code == 200
    assert one.json()["transaction"]["txn_id"] == "txn-0001-004"
    assert one.json()["transaction"]["amount_minor"] == 216000
    assert one.json()["as_of"] == AS_OF and one.json()["source_version"] == "mock_bank_feed.v1"
    unknown = client.get("/feed/transactions/txn-9999-999", headers=auth("feed"))
    assert unknown.status_code == 404 and unknown.json()["code"] == "not_found"


def test_feed_routes_report_an_absent_feed_module(client, monkeypatch):
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "alibi.connectors.bank_feed":
            raise ImportError("not built yet")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    response = client.get("/feed/transactions", headers=auth("feed"))
    assert response.status_code == 503 and response.json()["code"] == "sink_unavailable"


# --------------------------------------------------------------------------- rfc_url forwarder (S5 app track)


def test_rfc_url_forwards_issued_requests_over_http_with_the_recipient_bearer(built):
    """``create_bank_app(rfc_url=...)`` POSTs every issued request (case RFC and follow-up) to the
    passport app's ``/connect/bank/rfc`` as ``{rfc, recipient, account_refs, now}`` with the
    recipient's own token; the answer is recorded as ``forwarded`` exactly like the sink path."""
    import httpx
    calls = []

    class Passport:
        def __init__(self):
            self.status, self.body, self.raise_error = 200, None, False

        def post(self, url, *, json, headers):
            if self.raise_error:
                raise httpx.ConnectError("down")
            calls.append({"url": url, "json": json, "headers": headers})
            body = self.body if self.body is not None else {"status": "accepted",
                                                            "request_id": json["rfc"]["request"]["request_id"]}
            return httpx.Response(self.status, json=body)

    passport = Passport()
    app = create_bank_app(keystore=built.keystore, registry=built.registry, tokens=TOKENS,
                          rfc_url="http://app.test/connect/bank/rfc", http_client=passport)
    client = TestClient(app)
    assert client.post("/admin/seed", headers=auth("admin"), params={"case": 1, "as_of": AS_OF}).status_code == 200
    issued = client.post("/rfc", json={"request_id": RFC_A}, headers=auth("bank_a"))
    assert issued.status_code == 201, issued.json()
    assert issued.json()["forwarded"] == {"status": "accepted", "request_id": RFC_A}
    assert len(calls) == 1 and calls[0]["url"] == "http://app.test/connect/bank/rfc"
    sent = calls[0]["json"]
    assert sent["recipient"] == "bank_a" and sent["account_refs"] == [ACCOUNT] and sent["now"] == AS_OF
    assert sent["rfc"]["request"]["request_id"] == RFC_A and "signature" in sent["rfc"]
    assert calls[0]["headers"]["Authorization"] == f"Bearer {TOKENS['bank_a']}"
    # the follow-up a request_more creates goes the same way, with bank_a's own token again
    delivered = client.post("/deliver", json=built.p1, headers=auth("bank_a"))
    assert delivered.status_code == 200, delivered.json()
    disp = client.post(f"/packages/{built.p1['package_id']}/disposition", headers=auth("bank_a"),
                       json={"decision": "request_more", "reason": "more", "requested_evidence": ["the settlement"]})
    assert disp.status_code == 200, disp.json()
    assert disp.json()["follow_up"]["forwarded"]["status"] == "accepted"
    assert calls[-1]["json"]["rfc"]["request"]["parent_request_id"] == RFC_A
    assert calls[-1]["headers"]["Authorization"] == f"Bearer {TOKENS['bank_a']}"
    audit = client.get("/audit", headers=auth("bank_a")).json()["entries"]
    assert any("forwarded accepted" in e["detail"] for e in audit)
    # a refusing passport is ``refused`` with its code and reason; an unreachable one is ``error``
    passport.status, passport.body = 409, {"code": "rfc_refused", "reason": "no", "status": "refused"}
    issued_b = client.post("/rfc", json={"request_id": RFC_B}, headers=auth("bank_b"))
    assert issued_b.status_code == 201
    assert issued_b.json()["forwarded"] == {"status": "refused", "reason": "no", "code": "rfc_refused"}
    assert calls[-1]["headers"]["Authorization"] == f"Bearer {TOKENS['bank_b']}"
    passport.raise_error = True
    custom = client.post("/rfc", headers=auth("bank_b"),
                         json={"request": {"request_id": "rfc-custom-1", "subject_ref": SUBJECT,
                                           "questions": [{"question_id": "q.x", "text": "x"}],
                                           "issued_at": AS_OF, "expires_at": "2026-11-20T00:00:00Z",
                                           "period": OCTOBER}})
    assert custom.status_code == 201, custom.json()
    assert custom.json()["forwarded"] == {"status": "error", "reason": "passport unreachable: ConnectError"}
    assert TOKENS["bank_a"] not in json.dumps(custom.json()) and TOKENS["bank_b"] not in json.dumps(custom.json())
