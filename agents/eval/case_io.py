"""Prepare request-scoped inputs for Alibi evaluation."""

import json
from pathlib import Path

from agents.data.api import known_at, lookup
from tools.lockbox import is_open, lockbox_from


def read_sealed_inputs(path: Path) -> list[dict]:
    """Read committed-to inputs only in an authorised lockbox run."""
    boundary = lockbox_from()
    if type(boundary) is not int or not is_open():
        raise PermissionError("sealed inputs require an authorised case-number lockbox run")

    records = []
    seen = set()
    with path.open(encoding="utf-8") as source:
        for line in source:
            record = json.loads(line)
            if not isinstance(record, dict):
                raise ValueError("each line must contain one case object")
            number = record.get("case_number")
            if type(number) is not int or number < boundary or number in seen or "gold" in record:
                raise ValueError("expected unique sealed case numbers and separate gold")
            records.append(record)
            seen.add(number)
    if not records:
        raise ValueError("sealed input file is empty")
    return records


def request_context(case: dict, request_id: str):
    """Return one request's visible input and an evaluator-mediated lookup function."""
    requests = [rfc for rfc in case["rfcs"] if rfc["request_id"] == request_id]
    if len(requests) != 1:
        raise ValueError(f"expected exactly one RFC for {request_id}")
    request = requests[0]
    decision_time = request["issued_at"]
    subject = request["subject_ref"]
    if subject != case["subject_ref"]:
        raise ValueError("RFC subject differs from the case subject")

    view = known_at(case, decision_time)
    inputs = {
        key: view[key] for key in (
            "subject_ref", "merchant_deliveries", "merchant_lookup", "imports", "bank_deliveries",
            "holder_claim",
        )
    }
    inputs["rfcs"] = [rfc for rfc in view["rfcs"] if rfc["request_id"] == request_id]
    if "privacy_policy" in view:
        inputs["privacy_policy"] = view["privacy_policy"]

    def find_evidence(*, payout_reference=None, record_id=None):
        if bool(payout_reference) == bool(record_id):
            raise ValueError("give exactly one payout reference or record ID")
        return [event for event in lookup(case, decision_time, payout_reference=payout_reference,
                                          record_id=record_id)
                if event["subject_ref"] == subject]

    return inputs, find_evidence
