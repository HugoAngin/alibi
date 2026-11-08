"""Bank-verifier probe plan, SP-25 to SP-32, for eval's probe block.

    uv run --frozen python -m agents.execution.verifier_probes

Writes out/verifier_probes.json. Each attempt is one step in the plan format of
agents.eval.api.score_probe_steps: {case_number, request_id, step_id, probe_id}, plus how to
build the bytes, which inbox receives them, the expected outcome and the refusal codes from
eval's reference verifier (agents/eval/sharing_delivery.py) that count as a correct refusal.

`setup` steps are preconditions and receipt-evidence checks, not probe steps: they are listed
under `checks` so a refusal probe never scores an expected acceptance as a failure.

The HTTP runner is not written yet: it waits for the S2 bank routes and the S5 attempt contract.
A route that does not exist is recorded as outcome "error", never as "refused".
"""
from __future__ import annotations

import json

from agents.execution.api import OUT, load_probes

CASE = 1
REQ = "rfc-0001-01"
BASE = "valid bank_a package for rfc-0001-01: canonical case 1 body, signed by the Alibi package key"

ATTEMPTS = [
    {"step_id": "sp25-body-edit", "probe_id": "SP-25", "deliver_to": "bank_a",
     "build": "BASE; after signing, change one character of claims[0].rationale and re-serialise the envelope with JCS",
     "expect": "refused", "codes": ["bad_signature"]},
    {"step_id": "sp25-raw-flip", "probe_id": "SP-25", "deliver_to": "bank_a",
     "build": "BASE; flip one bit of one byte inside the signature value of the raw envelope bytes",
     "expect": "refused", "codes": ["bad_signature", "bad_encoding", "malformed", "non_canonical"]},
    {"step_id": "sp26-wrong-inbox", "probe_id": "SP-26", "deliver_to": "bank_b",
     "build": "BASE unchanged (audience bank_a), delivered into bank_b's inbox",
     "expect": "refused", "codes": ["wrong_recipient"]},
    {"step_id": "sp27-expired", "probe_id": "SP-27", "deliver_to": "bank_a",
     "build": "BASE with a fresh package_id and expires_at 1 minute after issued_at; deliver at expires_at + 1s (never delivered before, so no exact-retry shortcut)",
     "expect": "refused", "codes": ["expired"]},
    {"step_id": "sp28-unknown-key", "probe_id": "SP-28", "deliver_to": "bank_a",
     "build": "BASE signed by a fresh Ed25519 key with key_id 'alibi-rogue-1' that is not in the pinned registry",
     "expect": "refused", "codes": ["unknown_key"]},
    {"step_id": "sp28-merchant-key", "probe_id": "SP-28", "deliver_to": "bank_a",
     "build": "BASE signed in the package domain by the mock merchant key, using the merchant's pinned key_id (issuer-record purpose)",
     "expect": "refused", "codes": ["wrong_key_purpose"]},
    {"step_id": "sp29-bad-issuer-sig", "probe_id": "SP-29", "deliver_to": "bank_a",
     "build": "BASE whose issuer_verified attachment carries a record signature that does not verify (one field changed after the merchant signed it); then sign the package correctly",
     "expect": "refused", "codes": ["source_signature_invalid"]},
    {"step_id": "sp29-missing-issuer-sig", "probe_id": "SP-29", "deliver_to": "bank_a",
     "build": "BASE whose attachment claims issuer_verified with no record signature or issuer proof",
     "expect": "refused", "codes": ["source_signature_missing"]},
    {"step_id": "sp30-self-supplied", "probe_id": "SP-30", "deliver_to": "bank_a",
     "build": "BASE with a fresh package_id plus one self_supplied attachment that has no issuer signature",
     "expect": "accepted", "codes": [],
     "assert": "that attachment reports trust self_supplied, source_signature absent, attested_by package_signer; no attachment is upgraded"},
    {"step_id": "sp31-reuse-id", "probe_id": "SP-31", "deliver_to": "bank_a",
     "build": "after check sp32-valid, a correctly signed package with the SAME package_id and a different body (e.g. issued_at + 1s)",
     "expect": "refused", "codes": ["package_id_conflict"]},
    {"step_id": "sp32-valid", "probe_id": "SP-32", "deliver_to": "bank_a",
     "build": "BASE unchanged",
     "expect": "accepted", "codes": [],
     "assert": "receipt binds package_id and sha256 digest of the received bytes; package authentication shown separately from per-attachment trust and per-claim status; claim statuses equal the body's"},
]

CHECKS = [
    {"check_id": "replay-identical", "after": "sp32-valid", "deliver_to": "bank_a",
     "build": "the exact bytes of sp32-valid again",
     "expect": "accepted with the original receipt (created false); inbox lists the package once; one receipt audit entry",
     "covers": "SP-31 deduplication half"},
    {"check_id": "receipt-evidence", "after": "sp32-valid", "deliver_to": None,
     "build": "read the stored receipt and the bank audit log",
     "expect": "receipt {package_id, recipient, request_id, digest, received_at}; every refused step left an audit entry with its code and no stored package",
     "covers": "SP-25 to SP-31 audit column"},
    {"check_id": "isolation-after", "after": "all", "deliver_to": None,
     "build": "bank_b lists its inbox",
     "expect": "empty: the sp26 attempt stored nothing in bank_b",
     "covers": "SP-26"},
]


def plan() -> dict:
    kinds = {p["id"]: p["kind"] for p in load_probes()}
    steps = []
    for a in ATTEMPTS:
        want = "accepted" if kinds[a["probe_id"]] == "control" else "refused"
        assert a["expect"] == want, a["step_id"]
        steps.append({"case_number": CASE, "request_id": REQ, **a})
    covered = sorted({a["probe_id"] for a in ATTEMPTS})
    assert covered == [f"SP-{n}" for n in range(25, 33)], covered
    return {
        "label": "SYNTHETIC - bank-verifier probe plan, simulated services only",
        "base": BASE,
        "reference_verifier": "agents/eval/sharing_delivery.py verify_package and BankInbox.deliver",
        "outcomes": "accepted | refused | error; a missing route or a crash is error, never refused",
        "steps": steps,
        "checks": CHECKS,
    }


def score_plan() -> list[dict]:
    """The rows eval's score_probe_steps expects as `plan`."""
    return [{k: s[k] for k in ("case_number", "request_id", "step_id", "probe_id")} for s in plan()["steps"]]


def main() -> None:
    (OUT / "verifier_probes.json").write_text(json.dumps(plan(), indent=2) + "\n", encoding="utf-8")
    print(f"wrote {len(ATTEMPTS)} verifier attempts and {len(CHECKS)} checks")


if __name__ == "__main__":
    main()
