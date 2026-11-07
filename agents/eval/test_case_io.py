from pathlib import Path

import pytest

from agents.data.api import canonical_case, load_case
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


@pytest.mark.parametrize("number", [42, 43])
def test_hidden_reference_lookup_uses_data_contract_and_keeps_subject_scope(number):
    case = load_case(number)
    expected = set(case["gold"]["links"][0]["payout_record_ids"])
    path = case["gold"]["retrieval_path"]
    _, lookup = request_context(case, case["rfcs"][0]["request_id"])
    assert expected <= {event["record_id"] for query in path for event in lookup(**query)}

    delivery = next(item for item in case["merchant_lookup"] if item["event"]["record_id"] in expected)
    delivery["event"]["subject_ref"] = "different-subject"
    _, scoped_lookup = request_context(case, case["rfcs"][0]["request_id"])
    assert not expected & {event["record_id"] for query in path for event in scoped_lookup(**query)}


def test_sealed_loader_refuses_before_opening_a_file():
    with pytest.raises(PermissionError):
        read_sealed_inputs(Path("a-file-that-does-not-exist.jsonl"))
