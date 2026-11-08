"""Check the consent probes against synthetic HTTP responses and state transitions."""

from threading import Event

import httpx
import pytest

from agents.eval import api, http_probe_runtime as runtime
from agents.eval.http_probe_consent import HANDLERS, _check_delivery
from alibi.core import canon


DIGEST = "a" * 64
EDITED_DIGEST = "b" * 64
ROW = {"package_id": "package-a", "request_id": "rfc-a"}
PACKAGE = {**ROW, "authentication": {"status": "valid"}}


@pytest.fixture
def run(monkeypatch):
    client = httpx.Client

    def attempt(probe_id, respond, *, routes=None, fixtures=None):
        monkeypatch.setattr(runtime.httpx, "Client", lambda **kw: client(
            transport=httpx.MockTransport(respond), **kw,
        ))
        case = {"rfc_bank_a": "rfc-a", "recipient_bank_a": "bank_a", "policy_version": "v3"}
        case.update(fixtures or {})
        return api.attempt(probe_id, {"app": "https://alibi.invalid", "routes": routes or {}, "fixtures": case},
                           {actor: f"test-{actor}" for actor in ("holder", "bank_a", "bank_b", "merchant")})

    return attempt


def ok(body=None):
    return httpx.Response(200, json={} if body is None else body)


def refusal(code):
    return httpx.Response(409, json={"code": code})


@pytest.mark.parametrize("probe_id", HANDLERS)
def test_unconfigured_consent_probe_cannot_pass(probe_id):
    result = api.attempt(probe_id, {}, {})
    assert result["outcome"] == "not_run"
    assert result["exchanges"] == []
    assert result["reason"].startswith("missing ")


@pytest.mark.parametrize(("leak", "outcome"), [(False, "blocked"), (True, "not_blocked")])
def test_dispatch_refusal_requires_inbox_to_remain_empty(run, leak, outcome):
    sent = False

    def respond(request):
        nonlocal sent
        if request.url.path.endswith("/inbox"):
            return ok({"packages": [ROW] if sent and leak else []})
        if request.url.path == "/requests/rfc-a":
            return ok({"review": {"approval": {"digest": DIGEST}}})
        if request.url.path.endswith("/dispatch"):
            sent = True
            return refusal("approval_missing")
        return ok()

    result = run("SP-01", respond)
    assert result["outcome"] == outcome
    assert len(result["exchanges"]) == 5


@pytest.mark.parametrize(("receipt_digest", "outcome"), [(EDITED_DIGEST, "blocked"), (None, "not_run")])
def test_changed_body_needs_fresh_approval_and_matching_delivery_digest(run, receipt_digest, outcome):
    edited = False
    dispatches = 0
    approvals = []

    def respond(request):
        nonlocal edited, dispatches
        path = request.url.path
        if path.endswith("/inbox"):
            return ok({"packages": []})
        if path.endswith("/rfc-a"):
            return ok({"review": {"approval": {"digest": EDITED_DIGEST if edited else DIGEST},
                                  "included": [{"removable": True, "remove_action": "/remove"}]}})
        if path.endswith("/approve"):
            approvals.append(request.content)
        if path == "/remove":
            edited = True
        if path.endswith("/dispatch"):
            dispatches += 1
            if dispatches == 1:
                return refusal("approval_digest_mismatch")
            return ok({**ROW, "package_digest": receipt_digest})
        if path.endswith("/packages/package-a"):
            return ok(PACKAGE)
        return ok()

    result = run("SP-02", respond)
    assert result["outcome"] == outcome
    assert len(approvals) == 2 and dispatches == 2
    assert DIGEST.encode() in approvals[0] and EDITED_DIGEST.encode() in approvals[1]


@pytest.mark.parametrize("swapped", [False, True])
def test_native_dispatch_checks_the_approved_body_and_bank_receipt_separately(monkeypatch, swapped):
    body = {"audience": "bank_a", "claims": []}
    package = {"package_id": "package-a", "body": body, "body_sha256": canon.digest(body)}
    receipt = {"accepted": True, "receipt_id": "receipt-a", "package_id": "package-a",
               "package_sha256": canon.digest(package), "recipient": "bank_a"}
    holder = ok({"package_id": "package-a", "receipt": {
        "receipt_id": "receipt-a", "status": "accepted",
        "package_digest": canon.digest(body if swapped else package),
    }})

    def respond(request):
        assert request.url.host == "bank.invalid"
        assert request.headers["Authorization"] == "Bearer test-bank"
        if request.url.path == "/packages/package-a":
            return ok({"status": "verified", "authentication": {"valid": True}, "receipt": receipt})
        assert request.url.path == "/packages/package-a/raw"
        return ok(package)

    client = httpx.Client
    monkeypatch.setattr(runtime.httpx, "Client", lambda **kw: client(
        transport=httpx.MockTransport(respond), **kw,
    ))
    session = runtime.ProbeSession(
        {"bank": "http://bank.invalid", "fixtures": {"consent_bank_service": "bank"}},
        {"bank_a": "test-bank"},
    )
    try:
        if swapped:
            with pytest.raises(runtime.ProbeUnavailable, match="receipt did not agree"):
                _check_delivery(session, holder, "bank_a", canon.digest(body))
        else:
            package_id, delivered = _check_delivery(session, holder, "bank_a", canon.digest(body))
            assert package_id == "package-a" and delivered["package"] == package
    finally:
        session.close()


def test_race_sends_two_concurrent_requests_and_requires_ordered_audit(run):
    correction_started, dispatch_started = Event(), Event()

    def respond(request):
        path = request.url.path
        if path.endswith("/inbox"):
            return ok({"packages": []})
        if path == "/requests/rfc-a":
            return ok({"review": {"approval": {"digest": DIGEST}}})
        if path == "/correction":
            correction_started.set()
            assert dispatch_started.wait(2), "dispatch must overlap correction"
        if path.endswith("/dispatch"):
            dispatch_started.set()
            assert correction_started.wait(2), "correction must overlap dispatch"
            return refusal("source_revision_changed")
        if path == "/audit/rfc-a":
            return ok({"entries": [{"action": "record.corrected", "event_id": "correction"},
                                   {"action": "dispatch.refused", "request_id": "rfc-a"}]})
        return ok()

    result = run("SP-08", respond, routes={"merchant_correction": "/correction", "race_audit": "/audit/{request_id}"},
                 fixtures={"correction_event": {"event_id": "correction"}, "cited_record_id": "record",
                           "cited_approved_revision": 1})
    assert result["outcome"] == "blocked"


@pytest.mark.parametrize(("duplicate", "outcome"), [(False, "blocked"), (True, "not_blocked")])
def test_dispatch_idempotency_checks_duplicate_rows_and_conflicting_bytes(run, duplicate, outcome):
    dispatches = 0
    attempted_conflict = False

    def respond(request):
        nonlocal dispatches, attempted_conflict
        path = request.url.path
        if path.endswith("/inbox"):
            count = 2 if duplicate and dispatches >= 2 else int(dispatches > 0)
            return ok({"packages": [ROW] * count})
        if path.endswith("/rfc-a"):
            return ok({"review": {"approval": {"digest": DIGEST}}})
        if path.endswith("/dispatch"):
            dispatches += 1
            return ok({"receipt": {**ROW, "package_digest": DIGEST, "status": "accepted"}})
        if path.endswith("/packages/package-a"):
            return ok(PACKAGE)
        if path == "/passport":
            return ok({"sharing_history": [ROW]})
        if path == "/retry/package-a":
            attempted_conflict = True
            assert b'"changed":"bytes"' in request.content
            return refusal("package_id_reused")
        return ok()

    result = run("SP-09", respond, routes={"dispatch_retry": "/retry/{package_id}"},
                 fixtures={"dispatch_retry_conflict": {"changed": "bytes"}})
    assert result["outcome"] == outcome
    assert attempted_conflict is not duplicate


def test_disclosure_probe_detects_forbidden_attachment_filename(run):
    def respond(request):
        path = request.url.path
        if path.endswith("/rfc-a"):
            return ok({"review": {"approval": {"digest": DIGEST}}})
        if path.endswith("/dispatch"):
            return ok({**ROW, "package_digest": DIGEST})
        if path.endswith("/packages/package-a"):
            return ok(PACKAGE)
        if path == "/metadata":
            return httpx.Response(200, text="visible evidence", headers={
                "Content-Disposition": 'attachment; filename="private-category.json"',
            })
        return ok({"evidence": "visible evidence"})

    result = run("SP-12", respond, routes={"signed_package": "/signed", "print_view": "/print",
                                          "attachment_metadata": "/metadata"},
                 fixtures={"denied_metadata_value": "private-category", "export_allowed_marker": "visible evidence"})
    assert result["outcome"] == "not_blocked"
    assert "private-category" in result["exchanges"][-1]["content_disposition"]


def test_recipient_spoof_attempts_query_and_header_independently(run):
    bank_b = {"package_id": "package-b", "request_id": "rfc-b"}

    def respond(request):
        assert not (request.url.query and request.headers.get("X-Alibi-Recipient"))
        if request.url.path == "/bank/bank_b/inbox" or request.headers.get("X-Alibi-Recipient") == "bank_b":
            return ok({"packages": [bank_b]})
        return ok({"packages": [ROW]})

    result = run("SP-20", respond, fixtures={"recipient_bank_b": "bank_b", "bank_b_package_id": "package-b"})
    assert result["outcome"] == "not_blocked"
    assert len(result["exchanges"]) == 4
