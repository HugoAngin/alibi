"""Check public Case 1 for readable evidence in the standalone bank's state file."""

import argparse
import json
from pathlib import Path

from agents.eval.native_verify import verify_native
from agents.eval.verify_demo_handoff import CASE_ONE
from agents.eval.verify_package import VerificationError, _evaluation_time, _read_object
from alibi.core import canon


def inspect(state_dir: Path, registry: dict, at) -> dict:
    bank = _read_object(state_dir / "bank_state.json", "invalid_bank_state")
    passport = _read_object(state_dir / "case_1.state.json", "invalid_passport_state")
    if bank.get("case_number") != 1:
        raise ValueError("use the disposable public Case 1 stack")

    findings = []
    for recipient, request_id in CASE_ONE:
        dispatch = passport[f"dispatch:{request_id}"]
        matches = [entry for entry in bank["recipients"][recipient]["inbox"]
                   if entry["package_id"] == dispatch["package_id"]]
        if len(matches) != 1:
            continue
        entry = matches[0]
        package, receipt = entry["package"], entry["receipt"]
        if entry["status"] != "verified" or not isinstance(package, dict):
            continue
        raw = canon.canonical_bytes(package)
        digest = canon.sha256_hex(raw)
        if (entry["request_id"] != request_id or receipt["accepted"] is not True
                or receipt["recipient"] != recipient
                or digest != entry["package_sha256"] or digest != receipt["package_sha256"]
                or digest != dispatch["receipt"]["package_digest"]):
            raise ValueError("stored package and public dispatch receipts disagree")
        verify_native(package, raw, registry, recipient=recipient, now=at, request_id=request_id)
        findings.append({"recipient": recipient, "signature_and_receipts_verified": True,
                         "plaintext_attachments": len(package["body"]["attachments"])})

    # One plaintext signed evidence attachment is enough to refute the control.
    # Absence in this snapshot alone cannot establish encryption at rest.
    outcome = "not_blocked" if any(row["plaintext_attachments"] for row in findings) else "not_run"
    return {"probe_id": "SP-47", "scope": "standalone bank persistence", "outcome": outcome,
            "findings": findings}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--registry", type=Path, required=True, help="public trust registry")
    parser.add_argument("--at", required=True, help="the public stack's fixed UTC time")
    args = parser.parse_args()
    try:
        registry = _read_object(args.registry, "invalid_registry")
        result = inspect(args.state_dir, registry, _evaluation_time(args.at))
    except (OSError, ValueError, KeyError, TypeError, VerificationError):
        parser.error("could not validate the disposable Case 1 snapshot against its public receipts")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
