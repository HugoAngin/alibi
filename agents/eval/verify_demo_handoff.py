"""Check the holder and bank release gates through the disposable Alibi router."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from urllib.parse import urlsplit

from agents.eval import http_probe_consent as consent
from agents.eval.http_probe_runtime import ProbeSession, ProbeUnavailable


CASE_ONE = (("bank_a", "rfc-0001-01"), ("bank_b", "rfc-0001-02"))


def _session(config: dict) -> ProbeSession:
    base, tokens = config["base_urls"], config["tokens"]
    origins = [base[name] for name in ("app", "bank")]
    if any(not isinstance(value, str) for value in origins):
        raise ValueError("app and bank origins must be strings")
    app, bank = map(urlsplit, origins)
    if (app.scheme not in {"http", "https"} or app.hostname not in {"127.0.0.1", "::1"}
            or app.username is not None or app.password is not None or app.path.rstrip("/")
            or app.query or app.fragment or (bank.scheme, bank.netloc) != (app.scheme, app.netloc)
            or bank.path.rstrip("/") != "/bank" or bank.query or bank.fragment):
        raise ValueError("use one disposable loopback router, with the bank at its /bank prefix")
    credentials = [tokens[name] for name in ("holder", "bank_a", "bank_b")]
    if any(not isinstance(value, str) or not value for value in credentials) or len(set(credentials)) != 3:
        raise ValueError("provide three distinct synthetic holder and bank tokens")
    return ProbeSession({**base, "fixtures": {**base.get("fixtures", {}),
                                             "consent_bank_service": "bank"}}, tokens)


def _check_auth(session: ProbeSession, path: str, phase: str, fields: dict | None = None) -> dict:
    statuses = {}
    for actor in (None, "bank_a", "bank_b"):
        session.client.cookies.clear()
        response = session.http("POST", "app", path, actor, data=fields,
                                headers=consent.JSON_HEADERS, capture_response=False)
        status = response.status_code
        if status not in (401, 403) or session.code(response) == "cross_site_post":
            raise ProbeUnavailable(f"{phase} with {actor or 'no credential'} returned HTTP {status}")
        statuses[actor or "anonymous"] = status
    return statuses


def check(session: ProbeSession) -> dict:
    """Send Case 1 to both banks and prove the public inboxes see the actual receipts."""
    facts = {}
    for recipient, request_id in CASE_ONE:
        other = "bank_b" if recipient == "bank_a" else "bank_a"
        for bank in (recipient, other):
            consent._fresh_inbox(session, bank, request_id)
        consent._run_investigation(session, request_id)
        review, digest = consent._current_digest(session, request_id)
        approval = consent._review_section(review).get("approval") or {}
        policy = session.fixtures.get("policy_version") or approval.get("policy_version")
        if not isinstance(policy, str) or not policy:
            raise ProbeUnavailable("holder review did not expose the current policy version")
        approve = session.route("approve", consent.APPROVE, request_id=request_id)
        dispatch = session.route("dispatch", consent.DISPATCH, request_id=request_id)
        auth_approve = _check_auth(session, approve, "approve",
                                  {"digest": digest, "audience": recipient, "policy_version": policy})
        session.require_success(consent._approve(session, request_id, digest, recipient, policy))
        auth_dispatch = _check_auth(session, dispatch, "dispatch")
        for bank in (recipient, other):
            consent._fresh_inbox(session, bank, request_id)
        package_id, delivered = consent._check_delivery(
            session, consent._dispatch(session, request_id), recipient, digest)
        if delivered["package"]["body"].get("request_id") != request_id:
            raise ProbeUnavailable(f"{recipient} signed package did not bind the expected request")
        rows = consent._inbox_rows(consent._bank_inbox(session, recipient))
        matching = [row for row in rows if row["request_id"] == request_id]
        if len(matching) != 1 or matching[0]["package_id"] != package_id or matching[0].get("status") != "verified":
            raise ProbeUnavailable(f"{recipient} public inbox did not list exactly one verified receipt")
        consent._fresh_inbox(session, other, request_id)
        path = session.route("package", "/packages/{package_id}", recipient=recipient, package_id=package_id)
        session.client.cookies.clear()
        foreign = session.http("GET", "bank", path, other, headers=consent.JSON_HEADERS, capture_response=False)
        if foreign.status_code != 404:
            raise ProbeUnavailable(f"foreign package read returned HTTP {foreign.status_code} instead of 404")
        facts[recipient] = {"approve_refusals": auth_approve, "dispatch_refusals": auth_dispatch,
                            "verified_receipts": 1, "foreign_status": foreign.status_code}
    return {"case": 1, "banks": facts}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True,
                        help="private profile for a disposable Case 1 router and bank")
    args = parser.parse_args(argv)
    try:
        session = _session(json.loads(args.config.read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError, TypeError):
        parser.error("config needs one loopback router, its /bank prefix and three distinct synthetic tokens")
    try:
        result = check(session)
    except ProbeUnavailable as exc:
        print(json.dumps({"passed": False, "reason": str(exc)}))
        return 1
    finally:
        session.close()
    print(json.dumps({"passed": True, **result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
