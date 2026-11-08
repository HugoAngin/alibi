"""Exercise the process boundary using public fixtures and a synthetic gold file."""

import shutil

import pytest

from agents.data.api import canonical_case
from agents.eval.sealed_worker import case_payload, run_case_worker, verify_file_isolation
from alibi.eval.scorer import build_system


def test_adapter_receives_an_allowlisted_case_and_rejects_nested_gold():
    case = canonical_case()
    payload = case_payload(case)
    assert not {"gold", "title", "variant", "family", "label"} & payload.keys()
    assert payload["merchant_lookup"] == case["merchant_lookup"]
    case["holder_claim"]["gold"] = {"test": "private"}
    with pytest.raises(ValueError, match="nested gold"):
        case_payload(case)


@pytest.mark.parametrize("mode", ["rules", "one_pass"])
def test_worker_rehearses_public_baselines_with_official_predictions(mode):
    case = canonical_case()
    rows = run_case_worker(case, mode, dry_run=True, timeout=15)
    assert [row["request_id"] for row in rows] == [rfc["request_id"] for rfc in case["rfcs"]]
    assert all(row["proposed_package"]["claims"] for row in rows)
    assert all(row["cost"]["model_calls"] == int(mode == "one_pass") for row in rows)
    assert all(row["package"] is None and row["failure"]["type"] == "unavailable" for row in rows)
    assert not build_system(mode, rows).invalid


def test_live_worker_requires_an_explicit_isolation_boundary():
    with pytest.raises(ValueError, match="denied paths"):
        run_case_worker(canonical_case(), "agents", dry_run=False, timeout=15)


@pytest.mark.skipif(not shutil.which("sandbox-exec"), reason="the verified worker sandbox is macOS-specific")
def test_sandbox_proves_gold_path_is_unreadable(tmp_path):
    private = tmp_path / "private"
    private.mkdir()
    gold = private / "gold.jsonl"
    gold.write_text("synthetic marker")
    verify_file_isolation((private,), gold)
    with pytest.raises(RuntimeError, match="could not prove"):
        verify_file_isolation((tmp_path / "unrelated",), gold)
    rows = run_case_worker(canonical_case(), "rules", dry_run=True, timeout=15, blocked=(private,))
    assert all(row.get("proposed_package") for row in rows)
