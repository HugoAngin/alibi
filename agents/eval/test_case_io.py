from pathlib import Path

import pytest

from agents.data.api import canonical_case
from agents.eval.case_io import read_sealed_inputs, request_context


def test_demo_only_exposes_the_current_request_and_scoped_lookup():
    case = canonical_case()
    bank_a, bank_b = case["rfcs"]
    inputs, lookup = request_context(case, bank_a["request_id"])

    assert inputs["rfcs"] == [bank_a]
    assert not {"gold", "title", "family", "variant"} & inputs.keys()
    assert inputs["merchant_lookup"] == []
    assert lookup(payout_reference="PO-0001-01")[0]["status"] == "settled"
    assert lookup(record_id="unavailable") == []
    with pytest.raises(ValueError):
        lookup()

    other, _ = request_context(case, bank_b["request_id"])
    assert other["rfcs"] == [bank_b]
    assert other["subject_ref"] == inputs["subject_ref"]


def test_a_late_settlement_lookup_is_hidden():
    case = canonical_case()
    case["merchant_lookup"][0]["available_from"] = "2099-01-01T00:00:00Z"
    _, lookup = request_context(case, case["rfcs"][0]["request_id"])
    assert lookup(payout_reference="PO-0001-01") == []


def test_sealed_loader_refuses_before_opening_a_file():
    with pytest.raises(PermissionError):
        read_sealed_inputs(Path("a-file-that-does-not-exist.jsonl"))
