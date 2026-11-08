"""Native S4 wiring for SP-33..SP-37 (source readback) and SP-39 (follow-up to package)."""

import json
from urllib.parse import parse_qs

import httpx
import pytest

from agents.eval import api, http_probe_runtime as runtime
from agents.eval.ingest_probe_profile import readback_control, sp39_fixtures
from alibi.connectors.case_source import load_case_without_gold

APP = "http://127.0.0.1:9"
ACTORS = {actor: f"synthetic-{actor}" for actor in ("bank_a", "bank_b", "holder", "merchant", "attacker")}


def _attempt(monkeypatch, probe_id, respond, routes, fixtures, origin=APP):
    client = httpx.Client
    monkeypatch.setattr(runtime.httpx, "Client", lambda **kw: client(transport=httpx.MockTransport(respond), **kw))
    urls = {"app": origin, "bank": origin, "merchant": origin, "routes": routes, "fixtures": fixtures}
    return api.attempt(probe_id, urls, ACTORS)


# --- SP-33..SP-37 over S4's /sources/{record_id}/{revision} -------------------

SOURCE = {"merchant_events": "/events", "import_ingest": "/imports",
          "active_record": "/sources/{record_id}/{revision}"}
CONTROL = {"known_active_record_id": "rec-c", "known_active_record_revision": 1}


def _sources(records):
    """records: {(record_id, revision): (fields, trust)}; superseded unless latest."""
    def respond(request):
        if request.method == "POST":
            return records.pop("_post")(request)
        _, _, rid, rev = request.url.path.split("/")
        found = records.get((rid, int(rev)))
        if found is None:
            return httpx.Response(404, json={"code": "not_found", "reason": f"no record {rid} revision {rev}"})
        history = sorted(key[1] for key in records if isinstance(key, tuple) and key[0] == rid)
        fields, trust = found
        return httpx.Response(200, json={
            "record": {"record_id": rid, "revision": int(rev), "trust": trust, "connector": "c",
                       "superseded": int(rev) < history[-1]},
            "fields": {"record_id": rid, "revision": int(rev), **fields}, "revisions": history})
    return respond


@pytest.mark.parametrize(("stale_wins", "outcome"), [(False, "blocked"), (True, "not_blocked")])
def test_stale_revision_is_decided_by_s4_history(monkeypatch, stale_wins, outcome):
    records = {("rec-c", 1): ({}, "issuer_verified"), ("rec-r", 1): ({"amount": 9}, "issuer_verified"),
               ("rec-r", 2): ({"amount": 8}, "issuer_verified")}
    posts = []

    def post(request):
        posts.append(json.loads(request.content))
        if stale_wins and len(posts) == 2:
            del records[("rec-r", 2)]  # the stale revision replaced the active one
        records["_post"] = post
        return httpx.Response(200, json={"status": "stale"})

    records["_post"] = post
    result = _attempt(monkeypatch, "SP-35", _sources(records), SOURCE, {
        **CONTROL, "event_record_id_rev": "rec-r", "expected_active_revision": 2,
        "event_revision_new": {"event": {"record_id": "rec-r", "revision": 2}},
        "event_revision_stale": {"event": {"record_id": "rec-r", "revision": 1}}})
    assert result["outcome"] == outcome
    assert [e["url"].removeprefix(APP) for e in result["exchanges"][1:4]] == [
        "/sources/rec-c/1", "/sources/rec-r/2", "/events"]


@pytest.mark.parametrize(("trust", "outcome"), [("self_supplied", "blocked"), ("issuer_verified", "not_blocked")])
def test_import_trust_is_read_from_the_stored_record(monkeypatch, trust, outcome):
    records = {("rec-c", 1): ({}, "issuer_verified"), ("self_rec-i", 1): ({}, trust),
               "_post": lambda request: httpx.Response(200, json={"status": "inserted"})}
    result = _attempt(monkeypatch, "SP-36", _sources(records), SOURCE, {
        **CONTROL, "import_record_id": "self_rec-i",
        "import_claims_issuer_verified": {"record_id": "rec-i", "trust": "issuer_verified"}})
    assert result["outcome"] == outcome


@pytest.mark.parametrize(("change", "outcome", "reason"), [
    (None, "blocked", None),
    ("stored", "not_blocked", None),
    ("control_missing", "not_run", "active-state control record did not read back as active"),
    ("control_superseded", "not_run", "active-state control record did not read back as active"),
])
def test_forged_webhook_absence_needs_an_active_control(monkeypatch, change, outcome, reason):
    records = {("rec-c", 1): ({}, "issuer_verified"),
               "_post": lambda request: httpx.Response(403, json={"code": "signature_invalid"})}
    if change == "stored":
        records[("rec-f", 1)] = ({}, "issuer_verified")
    if change == "control_missing":
        del records[("rec-c", 1)]
    if change == "control_superseded":
        records[("rec-c", 2)] = ({}, "issuer_verified")
    result = _attempt(monkeypatch, "SP-33", _sources(records), SOURCE, {
        **CONTROL, "event_bad_signature_record_id": "rec-f",
        "event_bad_signature": {"event": {"record_id": "rec-f", "revision": 1}, "signature": {"sig": "AA"}}})
    assert (result["outcome"], result.get("reason")) == (outcome, reason)


@pytest.mark.parametrize(("reason", "outcome"), [
    ("issuer signature rejected: signature does not verify", "blocked"),
    ("the event's subject is not this connection's subject", "not_run"),
])
def test_s5_generic_webhook_refusal_requires_the_probe_specific_reason(monkeypatch, reason, outcome):
    records = {("rec-c", 1): ({}, "issuer_verified"),
               "_post": lambda _: httpx.Response(409, json={"code": "ingest_refused", "reason": reason})}
    result = _attempt(monkeypatch, "SP-33", _sources(records), SOURCE, {
        **CONTROL, "sp33_ingest_reason": "issuer signature rejected:",
        "event_bad_signature_record_id": "rec-f",
        "event_bad_signature": {"event": {"record_id": "rec-f", "revision": 1}, "signature": {"sig": "AA"}},
    })
    assert result["outcome"] == outcome


@pytest.mark.parametrize(("probe_id", "route"), [("SP-33", "merchant_events"), ("SP-34", "merchant_events"),
                                                 ("SP-35", "merchant_events"), ("SP-36", "import_ingest"),
                                                 ("SP-37", "merchant_events")])
def test_ingest_probes_name_the_missing_intake_route(probe_id, route):
    urls = {"app": APP, "routes": {"active_record": SOURCE["active_record"]}, "fixtures": CONTROL}
    result = api.attempt(probe_id, urls, ACTORS)
    assert (result["outcome"], result["reason"], result["exchanges"]) == ("not_run", f"missing route: {route}", [])


# --- SP-39 on S4 with its in-process bank -----------------------------------

FOLLOWUP_ROUTES = {
    "followup_requests": "/bank/{recipient}/requests", "request_detail": "/requests/{request_id}",
    "followup_run": "/requests/{request_id}/run", "followup_approve": "/requests/{request_id}/approve",
    "followup_dispatch": "/requests/{request_id}/dispatch"}
FOLLOWUP_FIXTURES = {"rfc_followup_wide_text": {"parent_request_id": "rfc-1", "question": "all accounts"},
                     "parent_txn_ids": ["t-in"], "followup_forbidden_markers": ["t-out"]}


def _s4(change=None):
    issued = [{"request_id": "rfc-1", "txn_ids": ["t-in"]}]
    state, calls = {}, []

    def respond(request):
        path, who = request.url.path, request.headers["authorization"].removeprefix("Bearer synthetic-")
        calls.append((request.method, path, who))
        wide = ["t-in", "t-out"]
        if request.method == "POST" and path == "/bank/bank_a/requests":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            assert form == {"parent_request_id": "rfc-1", "question": "all accounts"}
            issued.append({"request_id": "rfc-2", "txn_ids": wide if change == "bank_stored_wide" else ["t-in"]})
            return httpx.Response(201, json={"request_id": "rfc-2", "txn_ids": ["t-in"], "period": None})
        if path == "/bank/bank_a/requests":
            return httpx.Response(200, json={"requests": issued})
        if path == "/requests/rfc-2" and request.method == "GET":
            approval = {"digest": "sha256:ab", "audience": "bank_a", "policy_version": "p1"}
            return httpx.Response(200, json={
                "request": {"txn_ids": wide if change == "passport_wide" else ["t-in"], "period": None,
                            "questions": [{"question_id": "q.follow_up_1"}]},
                "review": {"approval": approval if state.get("ran") else None}})
        if path == "/requests/rfc-2/run":
            state["ran"] = True
            return httpx.Response(200, json={"ok": True})
        if path == "/requests/rfc-2/approve":
            assert parse_qs(request.content.decode())["digest"] == ["sha256:ab"]
            return httpx.Response(200, json={"ok": True})
        if path == "/requests/rfc-2/dispatch":
            return httpx.Response(200, json={"outcome": "success", "package_id": "pkg-2"})
        if path == "/bank/bank_a/inbox":
            auth = "invalid" if change == "unverified" else "valid"
            return httpx.Response(200, json={"packages": [{"package_id": "pkg-2", "request_id": "rfc-2",
                                                           "authentication": auth}]})
        if path == "/bank/bank_a/packages/pkg-2":
            return httpx.Response(200, json={
                "package": {"package_id": "pkg-2", "request_id": "rfc-2", "period": None,
                            "txn_ids": wide if change == "package_wide" else ["t-in"]},
                "authentication": {"status": "valid"}, "claim_status": [{"question_id": "q.follow_up_1"}],
                "source_trust": [{"record_id": "t-out"}] if change == "leak" else []})
        return httpx.Response(404, json={"code": "not_found"})

    return respond, calls


@pytest.mark.parametrize(("change", "outcome"), [
    (None, "blocked"), ("leak", "not_blocked"), ("package_wide", "not_blocked"),
    ("passport_wide", "not_blocked"), ("bank_stored_wide", "not_blocked"), ("unverified", "not_run"),
])
def test_native_followup_is_decided_by_the_delivered_package(monkeypatch, change, outcome):
    respond, calls = _s4(change)
    result = _attempt(monkeypatch, "SP-39", respond, FOLLOWUP_ROUTES, FOLLOWUP_FIXTURES)
    assert result["outcome"] == outcome
    assert calls[:3] == [("GET", "/bank/bank_a/requests", "bank_a"), ("POST", "/bank/bank_a/requests", "bank_a"),
                         ("GET", "/bank/bank_a/requests", "bank_a")]
    if change is None:
        assert [c[1] for c in calls[3:]] == [
            "/requests/rfc-2", "/requests/rfc-2/run", "/requests/rfc-2", "/requests/rfc-2/approve",
            "/requests/rfc-2/dispatch", "/bank/bank_a/inbox", "/bank/bank_a/packages/pkg-2"]
        assert {who for method, _, who in calls if method == "POST" and "/requests/rfc-2" in _} == {"holder"}


@pytest.mark.parametrize(("change", "reason"), [
    ("remote", "native SP-39 approves and dispatches; it needs a loopback stack"),
    ("no_run_route", "missing route: followup_run"),
    ("parent_mismatch", "parent request's stored scope differs from the parent fixtures"),
])
def test_native_followup_refuses_to_write_without_its_preconditions(monkeypatch, change, reason):
    respond, calls = _s4()
    routes = {k: v for k, v in FOLLOWUP_ROUTES.items() if not (change == "no_run_route" and k == "followup_run")}
    fixtures = {**FOLLOWUP_FIXTURES, **({"parent_txn_ids": ["t-other"]} if change == "parent_mismatch" else {})}
    origin = "https://app.invalid" if change == "remote" else APP
    result = _attempt(monkeypatch, "SP-39", respond, routes, fixtures, origin=origin)
    assert (result["outcome"], result["reason"]) == ("not_run", reason)
    assert not [c for c in calls if c[0] == "POST"]


# --- fixtures from public, gold-free case inputs ------------------------------

def test_public_case_fixtures_follow_the_parent_scope():
    case = load_case_without_gold(1)
    assert "gold" not in case
    fixtures = sp39_fixtures(case)
    assert fixtures["rfc_followup_wide_text"]["parent_request_id"] == "rfc-0001-01"
    assert (fixtures["parent_txn_ids"], fixtures["parent_period"]) == (["txn-0001-004"], None)
    assert fixtures["followup_forbidden_markers"] == ["txn-0001-001", "txn-0001-002", "txn-0001-003",
                                                      "rec-0001-019"]
    assert readback_control(case) == {"known_active_record_id": "rec-0001-001", "known_active_record_revision": 1}
