"""Exercise catalogue verdicts with synthetic HTTP exchanges."""

import json

import httpx
import pytest

from agents.eval import api, http_probe_runtime as runtime
from agents.eval.http_probe_routes import HANDLERS


@pytest.fixture
def run(monkeypatch):
    client = httpx.Client

    def attempt(probe_id, respond, *, routes=None, fixtures=None):
        monkeypatch.setattr(runtime.httpx, "Client", lambda **kw: client(
            transport=httpx.MockTransport(respond), **kw
        ))
        origins = {service: f"https://{service}.invalid" for service in ("bank", "merchant", "app")}
        actors = {actor: f"synthetic-{actor}" for actor in ("bank_a", "bank_b", "holder", "merchant")}
        return api.attempt(probe_id, {**origins, "routes": routes or {}, "fixtures": fixtures or {}}, actors)

    return attempt


@pytest.mark.parametrize("probe_id", HANDLERS)
def test_unconfigured_route_probe_cannot_pass(probe_id):
    result = api.attempt(probe_id, {}, {})
    assert result["outcome"] == "not_run"
    assert result["exchanges"] == []
    assert result["reason"].startswith("missing ")


@pytest.mark.parametrize(("leak", "outcome"), [(False, "blocked"), (True, "not_blocked")])
def test_package_404_must_not_distinguish_known_and_unknown_ids(run, leak, outcome):
    def respond(request):
        package_id = request.url.path.split("/")[-1]
        if request.headers["authorization"] == "Bearer synthetic-bank_b":
            return httpx.Response(200, json={"package_id": package_id})
        reason = "exists but denied" if leak and package_id == "private-package" else "not found"
        return httpx.Response(404, json={"reason": reason, "package_id": package_id})

    result = run("SP-18", respond, fixtures={"bank_b_package_id": "private-package",
                                              "bank_b_absent_package_id": "unknown-package"})
    assert result["outcome"] == outcome
    assert len(result["exchanges"]) == 3


@pytest.mark.parametrize(("partial", "leak", "outcome"), [
    (False, False, "blocked"), (True, False, "not_run"), (True, True, "not_blocked"),
])
def test_download_probe_needs_every_endpoint_but_a_leak_still_fails(run, partial, leak, outcome):
    def respond(request):
        owner = request.headers["authorization"] == "Bearer synthetic-bank_b"
        return httpx.Response(200 if owner or leak else 403, text="synthetic evidence")

    routes = {"attachment_download": "/download/{attachment_id}", "package_pdf": "/pdf/{package_id}"}
    if not partial:
        routes["receipt_download"] = "/receipt/{receipt_id}"
    fixtures = {"bank_b_attachment_id": "attachment", "bank_b_package_id": "package",
                "bank_b_receipt_id": "receipt"}
    result = run("SP-19", respond, routes=routes, fixtures=fixtures)
    assert result["outcome"] == outcome
    assert len(result["exchanges"]) == (4 if partial else 6)
    if outcome == "not_run":
        assert result["reason"] == "missing route: receipt_download"


def test_holder_isolation_does_not_pass_when_source_route_is_missing(run):
    def respond(request):
        return httpx.Response(200 if request.headers["authorization"] == "Bearer synthetic-holder" else 403)

    result = run("SP-21", respond, fixtures={"holder_request_id": "request", "source_record_id": "source"})
    assert result["outcome"] == "not_run"
    assert result["reason"] == "missing route: source_record"
    assert len(result["exchanges"]) == 4


def test_rfc_issuer_spoof_requires_rejection_even_when_the_server_corrects_audience(run):
    result = run("SP-24", lambda _: httpx.Response(200, json={"audience": "bank_b"}),
                 routes={"rfc_intake": "/intake"}, fixtures={"rfc_claims_bank_a": {"audience": "bank_a"}})
    assert result["outcome"] == "not_blocked"


def test_ingest_replay_checks_same_bytes_and_conflicting_bytes(run):
    posts = []

    def respond(request):
        if request.method == "POST":
            posts.append(request.content)
            if len(posts) == 3:
                return httpx.Response(409, json={"code": "event_id_reused"})
            return httpx.Response(200, json={"record_id": "record"})
        return httpx.Response(200, json={"record_id": request.url.path.split("/")[-1], "revision": 1})

    result = run("SP-34", respond, routes={"merchant_events": "/events", "active_record": "/active/{record_id}"},
                 fixtures={"event_original": {"event_id": "event", "amount": 500},
                           "event_same_id_conflicting": {"event_id": "event", "amount": 600},
                           "event_record_id": "record", "known_active_record_id": "control"})
    assert result["outcome"] == "blocked"
    original, retry, conflicting = posts
    assert original == retry and conflicting != original


NATIVE_FIXTURES = {
    "bank_b_package_id": "pkg-b", "bank_b_attachment_id": "att-b1", "bank_b_receipt_id": "rcp-b",
    "holder_request_id": "rfc-1", "source_record_id": "rec-1", "source_record_revision": 2,
    "rfc_foreign_subject": {"request": {"request_id": "rfc-x2", "subject_ref": "subj-other", "txn_ids": ["t1"]}},
    "rfc_foreign_subject_control": {"request": {"request_id": "rfc-x1", "subject_ref": "subj-1", "txn_ids": ["t1"]}},
}
NATIVE_ROUTES = {"attachment_download": "/packages/{package_id}/attachments",
                 "receipt_download": "/packages/{package_id}", "source_record": "/sources/{record_id}/{revision}",
                 "rfc_intake": "/rfc"}


@pytest.mark.parametrize(("probe_id", "change", "outcome", "reason"), [
    ("SP-19", None, "not_run", "missing route: package_pdf"),
    ("SP-19", "leak", "not_blocked", None),
    ("SP-19", "wrong_attachment", "not_run", "owner control did not show the bound resource; missing route: package_pdf"),
    ("SP-21", None, "blocked", None),
    ("SP-21", "leak", "not_blocked", None),
    ("SP-21", "wrong_revision", "not_run", "holder control did not return the requested source revision"),
    ("SP-22", None, "blocked", None),
    ("SP-22", "leak", "not_blocked", None),
    ("SP-22", "remote", "not_run", "RFC intake controls need a disposable loopback bank"),
])
def test_native_bank_and_holder_shapes(monkeypatch, probe_id, change, outcome, reason):
    """Mock bank package-bound reads, S4 source revisions and a differential RFC control."""
    client, seen = httpx.Client, []

    def respond(request):
        who = request.headers["authorization"].removeprefix("Bearer synthetic-")
        path = request.url.path
        owner = who == ("bank_b" if path.startswith("/packages") else "holder") or change == "leak"
        seen.append((request.method, path, who, request.headers.get("accept")))
        if request.method == "POST":
            sent = json.loads(request.content)["request"]
            if sent["subject_ref"] != "subj-1" and change != "leak":
                return httpx.Response(400, json={"code": "invalid_request",
                                                 "reason": "request.subject_ref must be the case subject"})
            return httpx.Response(201, json={"request_id": sent["request_id"], "recipient": who})
        if not owner:
            return httpx.Response(404 if path.startswith("/packages") else 403, json={"code": "not_found"})
        if path == "/packages/pkg-b/attachments":
            attachment = "att-other" if change == "wrong_attachment" else "att-b1"
            return httpx.Response(200, json={"package_id": "pkg-b", "attachments": [{"attachment_id": attachment}]})
        if path == "/packages/pkg-b":
            return httpx.Response(200, json={"status": "verified", "receipt": {"receipt_id": "rcp-b"}})
        if path == "/sources/rec-1/2":
            revision = 1 if change == "wrong_revision" else 2
            return httpx.Response(200, json={"record": {"record_id": "rec-1", "revision": revision}})
        return httpx.Response(200, json={"request": {"request_id": "rfc-1"}})

    monkeypatch.setattr(runtime.httpx, "Client", lambda **kw: client(transport=httpx.MockTransport(respond), **kw))
    loopback = "https://bank.invalid" if change == "remote" else "http://127.0.0.1:9"
    origins = {"bank": loopback, "merchant": "http://127.0.0.1:9", "app": "http://127.0.0.1:9"}
    actors = {actor: f"synthetic-{actor}" for actor in ("bank_a", "bank_b", "holder")}
    result = api.attempt(probe_id, {**origins, "routes": NATIVE_ROUTES, "fixtures": NATIVE_FIXTURES}, actors)
    assert (result["outcome"], result.get("reason")) == (outcome, reason)
    if probe_id == "SP-19" and change is None:
        assert [(p, w, a) for _, p, w, a in seen] == [
            ("/packages/pkg-b/attachments", "bank_b", "application/json"),
            ("/packages/pkg-b/attachments", "bank_a", "application/json"),
            ("/packages/pkg-b", "bank_b", "application/json"), ("/packages/pkg-b", "bank_a", "application/json")]
    if probe_id == "SP-21" and change is None:
        assert ("GET", "/sources/rec-1/2", "bank_b", "application/json") in seen
        assert all(accept == "application/json" for *_, accept in seen)
    if probe_id == "SP-22" and change is None:
        assert [(m, w) for m, _, w, _ in seen] == [("POST", "bank_a"), ("POST", "bank_a")]


def test_followup_intake_success_alone_does_not_prove_disclosure_scope(run):
    result = run("SP-39", lambda _: httpx.Response(200, json={
        "request_id": "followup", "txn_ids": ["in-scope"], "period": None,
    }), fixtures={"rfc_followup_wide_text": {"question": "all accounts"}, "parent_txn_ids": ["in-scope"]})
    assert result["outcome"] == "not_run"
    assert result["reason"] == "missing route: followup_package"
