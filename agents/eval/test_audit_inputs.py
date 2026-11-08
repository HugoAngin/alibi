"""Exercise the visibility auditor without loading the sealed answer key."""

from agents.data.api import canonical_case
from agents.eval.audit_inputs import audit_cases, audit_source


def test_audit_checks_gold_metadata_and_future_rows():
    case = canonical_case()
    case["merchant_deliveries"].append({"received_at": "2100-01-01T00:00:00Z",
                                       "event": {"record_id": "not-yet-available"}})
    result = audit_cases([case])
    assert result["gold_labels_removed"] == 1
    assert result["adapter_labels_removed"] == 4
    assert result["requests"] == len(case["rfcs"])
    assert result["hidden_future_rows"] >= len(case["rfcs"])
    assert result["lookup_checks"] > 0


def test_source_scan_flags_runtime_access_separately_from_tests(tmp_path):
    package = tmp_path / "alibi"
    (package / "tests").mkdir(parents=True)
    (package / "tests" / "example.py").write_text("fixture['gold']\n")
    (package / "adapter.py").write_text(
        "from agents.data.api import load_case as read\n"
        "def run_case(case):\n"
        "    return read(case['gold']), 'agents/data/out/cases'\n"
    )
    report = audit_source(tmp_path)
    assert report["adapter_present"] and report["evaluator_and_test_references"] == 1
    assert {row["kind"] for row in report["runtime_references"]} == {
        "gold key", "development case path", "development case loader import",
    }
