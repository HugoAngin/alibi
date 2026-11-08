"""Bank-verifier probes SP-25 to SP-32: plan and local reference run.

    uv run --frozen python -m agents.execution.verifier_probes          # write the plan
    uv run --frozen python -m agents.execution.verifier_probes --run    # local reference run

The plan (out/verifier_probes.json) lists each attempt as one step in the format of
agents.eval.api.score_probe_steps: {case_number, request_id, step_id, probe_id}, with
`expected_outcome` where a step of a refusal probe must be accepted (SP-31's identical retry).

The reference run builds real signed bytes with eval's helpers and delivers them to eval's local
BankInbox (agents.eval.api). It is a check of the reference verifier, NOT of the live HTTP bank
service: its observations go to out/verifier_reference_run.json, separate from any live run.
The last stdout line is one JSON object for tools/results.py.

A step that crashes, or raises without a stable refusal code, is recorded as "error", never as
"refused". All keys are synthetic demo keys derived from fixed bytes.
"""
from __future__ import annotations

import copy
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone

import rfc8785
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agents.execution.api import OUT, load_probes

CASE = 1
REQ = "rfc-0001-01"
BASE = "valid bank_a package for rfc-0001-01: canonical case 1 payout record signed by the mock merchant key, package signed by the Alibi demo key"

# Execution order matters: sp32-valid must be delivered before the SP-31 steps.
ATTEMPTS = [
    {"step_id": "sp32-valid", "probe_id": "SP-32", "deliver_to": "bank_a",
     "build": "BASE unchanged", "expect": "accepted", "codes": [],
     "assert": "receipt binds package_id and sha256 digest of the received bytes; trust and claim statuses reported separately and unchanged"},
    {"step_id": "sp31-identical-retry", "probe_id": "SP-31", "deliver_to": "bank_a",
     "build": "the exact bytes of sp32-valid again", "expect": "accepted", "codes": [],
     "assert": "original receipt returned (created false); inbox lists the package once"},
    {"step_id": "sp31-reuse-id", "probe_id": "SP-31", "deliver_to": "bank_a",
     "build": "correctly signed package with the SAME package_id as sp32-valid and a different body (issued_at + 1s)",
     "expect": "refused", "codes": ["package_id_conflict"]},
    {"step_id": "sp25-body-edit", "probe_id": "SP-25", "deliver_to": "bank_a",
     "build": "BASE with a fresh package_id; after signing, change one character of claims[0].rationale and re-serialise with JCS",
     "expect": "refused", "codes": ["bad_signature"]},
    {"step_id": "sp25-raw-flip", "probe_id": "SP-25", "deliver_to": "bank_a",
     "build": "BASE with a fresh package_id; change one character of the package signature value in the raw bytes",
     "expect": "refused", "codes": ["bad_signature", "bad_encoding", "malformed", "non_canonical"]},
    {"step_id": "sp26-wrong-inbox", "probe_id": "SP-26", "deliver_to": "bank_b",
     "build": "BASE with a fresh package_id (audience bank_a), delivered into bank_b's inbox",
     "expect": "refused", "codes": ["wrong_recipient"]},
    {"step_id": "sp27-expired", "probe_id": "SP-27", "deliver_to": "bank_a",
     "build": "BASE with a fresh package_id and expires_at 1 minute after issued_at; delivered at expires_at + 1s",
     "expect": "refused", "codes": ["expired"]},
    {"step_id": "sp28-unknown-key", "probe_id": "SP-28", "deliver_to": "bank_a",
     "build": "BASE signed by a fresh Ed25519 key with key_id alibi-rogue-1, not in the pinned registry",
     "expect": "refused", "codes": ["unknown_key"]},
    {"step_id": "sp28-merchant-key", "probe_id": "SP-28", "deliver_to": "bank_a",
     "build": "BASE signed in the package domain by the mock merchant key under its pinned issuer-record key_id",
     "expect": "refused", "codes": ["wrong_key_purpose"]},
    {"step_id": "sp29-bad-issuer-sig", "probe_id": "SP-29", "deliver_to": "bank_a",
     "build": "BASE whose issuer_verified payout record has amount_minor changed after the merchant signed it; package signed correctly",
     "expect": "refused", "codes": ["source_signature_invalid"]},
    {"step_id": "sp29-missing-issuer-sig", "probe_id": "SP-29", "deliver_to": "bank_a",
     "build": "BASE whose issuer_verified payout record carries no signature",
     "expect": "refused", "codes": ["source_signature_missing"]},
    {"step_id": "sp30-self-supplied", "probe_id": "SP-30", "deliver_to": "bank_a",
     "build": "BASE with a fresh package_id plus one self_supplied attachment that has no issuer signature",
     "expect": "accepted", "codes": [],
     "assert": "that attachment reports trust self_supplied, attested by the package signer; the issuer record stays issuer_verified"},
]

CHECKS = [
    {"check_id": "receipt-evidence", "after": "sp32-valid",
     "expect": "receipt {package_id, recipient, request_id, digest, received_at} with digest = sha256 of the delivered bytes",
     "covers": "SP-32, SP-31"},
    {"check_id": "refusals-stored-nothing", "after": "all",
     "expect": "bank_a lists exactly the packages from accepted steps; bank_b lists none (the SP-26 attempt stored nothing)",
     "covers": "SP-25 to SP-29"},
]


def plan() -> dict:
    kinds = {p["id"]: p["kind"] for p in load_probes()}
    steps = []
    for a in ATTEMPTS:
        step = {"case_number": CASE, "request_id": REQ, **a}
        default = "accepted" if kinds[a["probe_id"]] == "control" else "refused"
        if a["expect"] != default:
            step["expected_outcome"] = a["expect"]
        steps.append(step)
    covered = sorted({a["probe_id"] for a in ATTEMPTS})
    assert covered == [f"SP-{n}" for n in range(25, 33)], covered
    return {
        "label": "SYNTHETIC - bank-verifier probe plan, simulated services only",
        "base": BASE,
        "reference_verifier": "agents.eval.api verify_package and BankInbox.deliver",
        "outcomes": "accepted | refused | error; a missing route or a crash is error, never refused",
        "steps": steps,
        "checks": CHECKS,
    }


def score_plan() -> list[dict]:
    """Rows for agents.eval.api.score_probe_steps `plan`."""
    keys = ("case_number", "request_id", "step_id", "probe_id", "expected_outcome")
    return [{k: s[k] for k in keys if k in s} for s in plan()["steps"]]


# --------------------------------------------------------------------------- reference run

NOW = datetime(2026, 11, 8, 1, 0, tzinfo=timezone.utc)
ALIBI = Ed25519PrivateKey.from_private_bytes(bytes(range(100, 132)))
MERCHANT = Ed25519PrivateKey.from_private_bytes(bytes(range(132, 164)))
ROGUE = Ed25519PrivateKey.from_private_bytes(bytes(range(164, 196)))
ALIBI_KID, MERCHANT_KID = "alibi-demo-ref-1", "mock-marketplace-ref-1"


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _payout_record() -> dict:
    from agents.data.api import canonical_case

    lookups = canonical_case()["merchant_lookup"]
    return copy.deepcopy(next(x["event"] for x in lookups if x["event"]["kind"] == "payout"))


def _body(package_id: str, payout_attachment: dict, *, extra=(), issued=NOW - timedelta(minutes=5),
          expires=NOW + timedelta(days=7)) -> dict:
    rec = payout_attachment["record"]["body"]
    return {
        "schema_version": "alibi.context_package.v1",
        "package_id": package_id,
        "request_id": REQ,
        "audience": "bank_a",
        "case_id": "case-0001",
        "scope": {"txn_ids": ["txn-0001-004"]},
        "issued_at": _iso(issued),
        "expires_at": _iso(expires),
        "claims": [
            {"question_id": "q.source_of_funds", "status": "supported",
             "rationale": f"Credit txn-0001-004 is payout {rec['payout_reference']}, settled {rec['settled_at']} before booking.",
             "evidence_refs": ["att-payout"]},
            {"question_id": "q.breakdown", "status": "supported",
             "rationale": "240000 - 18000 - 6000 = 216000 GBP pence.",
             "evidence_refs": ["att-payout"],
             "calculation": {"sales": 240000, "refunds": -18000, "fees": -6000, "total": 216000}},
        ],
        "attachments": [payout_attachment, *extra],
    }


def _signed_payout(api) -> dict:
    return {"attachment_id": "att-payout", "trust": "issuer_verified",
            "record": api.sign_record(_payout_record(), MERCHANT, MERCHANT_KID)}


def _seal(api, body, key=ALIBI, kid=ALIBI_KID) -> bytes:
    return api.sign_package(body, key, kid, approved_body_digest=api.body_digest(body))


def _build(api, step_id: str, valid_raw: bytes) -> tuple[bytes, datetime]:
    pid = f"pkg-ref-{step_id}"
    good = _signed_payout(api)
    if step_id == "sp32-valid":
        return _seal(api, _body("pkg-ref-valid", good)), NOW
    if step_id == "sp31-identical-retry":
        return valid_raw, NOW
    if step_id == "sp31-reuse-id":
        return _seal(api, _body("pkg-ref-valid", good, issued=NOW - timedelta(minutes=5) + timedelta(seconds=1))), NOW
    if step_id == "sp25-body-edit":
        env = api.parse_envelope(_seal(api, _body(pid, good)))
        env["body"]["claims"][0]["rationale"] = env["body"]["claims"][0]["rationale"].replace("before", "befare", 1)
        return rfc8785.dumps(env), NOW
    if step_id == "sp25-raw-flip":
        env = api.parse_envelope(_seal(api, _body(pid, good)))
        sig = env["signature"]["signature"]
        env["signature"]["signature"] = ("B" if sig[0] != "B" else "C") + sig[1:]
        return rfc8785.dumps(env), NOW
    if step_id == "sp26-wrong-inbox":
        return _seal(api, _body(pid, good)), NOW
    if step_id == "sp27-expired":
        issued = NOW - timedelta(minutes=5)
        expires = issued + timedelta(minutes=1)
        return _seal(api, _body(pid, good, issued=issued, expires=expires)), expires + timedelta(seconds=1)
    if step_id == "sp28-unknown-key":
        return _seal(api, _body(pid, good), key=ROGUE, kid="alibi-rogue-1"), NOW
    if step_id == "sp28-merchant-key":
        return _seal(api, _body(pid, good), key=MERCHANT, kid=MERCHANT_KID), NOW
    if step_id == "sp29-bad-issuer-sig":
        bad = copy.deepcopy(good)
        bad["record"]["body"]["amount_minor"] += 100
        return _seal(api, _body(pid, bad)), NOW
    if step_id == "sp29-missing-issuer-sig":
        bare = {"attachment_id": "att-payout", "trust": "issuer_verified", "record": {"body": _payout_record()}}
        return _seal(api, _body(pid, bare)), NOW
    if step_id == "sp30-self-supplied":
        own = {"attachment_id": "att-seller-export", "trust": "self_supplied",
               "record": {"body": {"kind": "receipt", "label": "SYNTHETIC seller export", "amount_minor": 240000, "currency": "GBP"}},
               "provenance": {"connector": "import", "retrieved_at": "2026-11-06T08:00:00Z"}}
        return _seal(api, _body(pid, good, extra=[own])), NOW
    raise KeyError(step_id)


def _check_accepted(step_id: str, delivery, inbox, raw: bytes, valid_raw: bytes) -> str:
    """Return '' when the accepted delivery shows the evidence the plan asks for, else why not."""
    import hashlib

    r = delivery.receipt
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    if r.digest.removeprefix("sha256:") != digest.removeprefix("sha256:"):
        return "receipt digest does not match delivered bytes"
    if step_id == "sp32-valid" and not delivery.created:
        return "first delivery not created"
    if step_id == "sp31-identical-retry" and (delivery.created or raw != valid_raw):
        return "identical retry created a second delivery"
    stored = inbox.get("cred-a", r.package_id)
    atts = {a["attachment_id"]: a for a in stored.verification["attachments"]}
    if atts["att-payout"]["trust"] != "issuer_verified":
        return "issuer record trust changed"
    if step_id == "sp30-self-supplied":
        own = atts.get("att-seller-export")
        if not own or own["trust"] != "self_supplied" or own.get("attested_by") != "package_signer":
            return f"self_supplied attachment reported as {own}"
    return ""


def run() -> dict:
    from agents.eval import api

    registry = api.TrustRegistry()
    registry.add(ALIBI_KID, api.PURPOSE_PACKAGE, "alibi", ALIBI)
    registry.add(MERCHANT_KID, api.PURPOSE_ISSUER_RECORD, "mock-marketplace", MERCHANT)
    observations, valid_raw = [], b""
    with tempfile.TemporaryDirectory() as root:
        inbox = api.BankInbox(root, registry, {"cred-a": "bank_a", "cred-b": "bank_b"})
        for a in ATTEMPTS:
            row = {"case_number": CASE, "request_id": REQ, "step_id": a["step_id"], "probe_id": a["probe_id"]}
            try:
                raw, at = _build(api, a["step_id"], valid_raw)
                if a["step_id"] == "sp32-valid":
                    valid_raw = raw
            except Exception as exc:  # a broken fixture is an error, never a refusal
                observations.append({**row, "outcome": "error", "code": None, "detail": f"build: {exc!r}"})
                continue
            try:
                delivery = inbox.deliver(a["deliver_to"], raw, now=at)
            except Exception as exc:
                code = getattr(exc, "code", None)
                if code is None:
                    observations.append({**row, "outcome": "error", "code": None, "detail": repr(exc)})
                else:
                    ok = code in a["codes"]
                    observations.append({**row, "outcome": "refused", "code": code,
                                         "detail": "" if ok else f"unexpected code, plan lists {a['codes']}"})
                continue
            problem = _check_accepted(a["step_id"], delivery, inbox, raw, valid_raw)
            observations.append({**row, "outcome": "accepted", "code": None,
                                 "created": delivery.created, "digest": delivery.receipt.digest, "detail": problem})
        listed_a = sorted(r.package_id for r in inbox.list_receipts("cred-a"))
        listed_b = sorted(r.package_id for r in inbox.list_receipts("cred-b"))

    score = api.score_probe_steps(score_plan(), observations)
    evidence_problems = [o["step_id"] for o in observations if o.get("detail")]
    checks = {
        "bank_a_lists": listed_a,
        "bank_b_lists": listed_b,
        "refusals_stored_nothing": listed_a == ["pkg-ref-sp30-self-supplied", "pkg-ref-valid"] and listed_b == [],
    }
    return {
        "label": "SYNTHETIC - local reference run against agents.eval BankInbox, not the live HTTP bank service",
        "observations": observations,
        "checks": checks,
        "score": score,
        "metrics": {
            "verifier_ref_probes": 8,
            "verifier_ref_passed": sum(1 for p, o in score["observed"].items()
                                       if o == ("accepted" if p in ("SP-30", "SP-32") else "refused")),
            "verifier_ref_steps": len(observations),
            "verifier_ref_incorrect_steps": score["incorrect_steps"],
            "verifier_ref_error_steps": score["error_steps"],
            "verifier_ref_evidence_problems": len(evidence_problems),
            "verifier_ref_checks_passed": int(checks["refusals_stored_nothing"]),
        },
    }


def main() -> None:
    if "--run" in sys.argv:
        result = run()
        (OUT / "verifier_reference_run.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        for o in result["observations"]:
            print(f"{o['step_id']:24} {o['outcome']:9} {o.get('code') or ''} {o.get('detail') or ''}")
        print(json.dumps(result["metrics"]))
        return
    (OUT / "verifier_probes.json").write_text(json.dumps(plan(), indent=2) + "\n", encoding="utf-8")
    print(f"wrote {len(ATTEMPTS)} verifier attempts and {len(CHECKS)} checks")


if __name__ == "__main__":
    main()
