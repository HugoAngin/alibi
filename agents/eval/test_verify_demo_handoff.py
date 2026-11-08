import httpx
import pytest

from agents.eval.http_probe_runtime import ProbeUnavailable
from agents.eval.verify_demo_handoff import CASE_ONE, _session, check
from alibi.core import canon


CONFIG = {
    "base_urls": {"app": "http://127.0.0.1:8000", "bank": "http://127.0.0.1:8000/bank"},
    "tokens": {"holder": "synthetic-holder", "bank_a": "synthetic-a", "bank_b": "synthetic-b"},
}


def routed_stack(fault=None):
    deliveries = {}
    tokens = {f"Bearer {token}": actor for actor, token in CONFIG["tokens"].items()}
    requests = {request_id: recipient for recipient, request_id in CASE_ONE}

    def reply(request):
        actor = tokens.get(request.headers.get("Authorization"))
        parts = request.url.path.split("/")[1:]
        if parts[0] == "bank":
            owned = deliveries.get(actor)
            if parts == ["bank", "inbox"]:
                rows = [{"package_id": owned["package"]["package_id"], "request_id": owned["request_id"],
                         "status": "verified"}] if owned else []
                return httpx.Response(200, json={"packages": rows})
            if not owned or parts[2] != owned["package"]["package_id"]:
                return httpx.Response(404)
            if parts[-1] == "raw":
                return httpx.Response(200, json=owned["package"])
            return httpx.Response(200, json={"status": "verified", "authentication": {"valid": True},
                                             "receipt": owned["receipt"]})
        request_id = parts[1]
        recipient = requests[request_id]
        body = {"audience": recipient, "request_id": request_id}
        digest = canon.digest(body)
        if request.method == "GET":
            return httpx.Response(200, json={"review": {"approval": {"digest": digest, "policy_version": "v1"}}},
                                  headers={"Set-Cookie": "holder-session=test; Path=/"})
        action = parts[2]
        if actor != "holder":
            assert "Cookie" not in request.headers
            status = 200 if fault == action else 401 if actor is None else 403
            return httpx.Response(status)
        if action != "dispatch":
            return httpx.Response(200)
        package = {"package_id": f"package-{recipient}", "body": body, "body_sha256": digest}
        receipt = {"package_id": package["package_id"], "receipt_id": f"receipt-{recipient}",
                   "recipient": recipient, "package_sha256": canon.digest(package), "accepted": True}
        if fault != "disconnected":
            deliveries[recipient] = {"request_id": request_id, "package": package, "receipt": receipt}
        return httpx.Response(200, json={"package_id": package["package_id"], "receipt": {
            "receipt_id": receipt["receipt_id"], "package_digest": receipt["package_sha256"],
            "status": "accepted",
        }})

    session = _session(CONFIG)
    session.client.close()
    session.client = httpx.Client(transport=httpx.MockTransport(reply))
    return session


def test_accepts_two_verified_deliveries_and_separates_holder_auth():
    session = routed_stack()
    try:
        result = check(session)
    finally:
        session.close()
    assert set(result["banks"]) == {"bank_a", "bank_b"}
    for bank in result["banks"].values():
        assert bank["verified_receipts"] == 1 and bank["foreign_status"] == 404
        assert bank["approve_refusals"] == bank["dispatch_refusals"] == {
            "anonymous": 401, "bank_a": 403, "bank_b": 403,
        }


def test_independent_verifier_rejects_bank_report_with_unsigned_envelope():
    session = routed_stack()
    try:
        with pytest.raises(ProbeUnavailable, match="bank_a independent verification refused"):
            check(session, registry={})
    finally:
        session.close()


@pytest.mark.parametrize(("fault", "diagnostic"), [
    ("approve", "approve with no credential returned HTTP 200"),
    ("dispatch", "dispatch with no credential returned HTTP 200"),
    ("disconnected", "probe setup returned HTTP 404"),
])
def test_rejects_open_actions_or_a_disconnected_public_bank(fault, diagnostic):
    session = routed_stack(fault)
    try:
        with pytest.raises(ProbeUnavailable, match=diagnostic):
            check(session)
    finally:
        session.close()
