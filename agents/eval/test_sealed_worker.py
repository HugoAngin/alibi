"""Exercise the process boundary using public fixtures and a synthetic gold file."""

from pathlib import Path
import shutil

import pytest

from agents.data.api import canonical_case
from agents.eval import sealed_worker
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
def test_adapter_preflight_imports_inside_the_sandbox_without_running_a_case(tmp_path, monkeypatch):
    checkout = tmp_path / "checkout"
    worker = checkout / "agents" / "eval" / "sealed_worker.py"
    worker.parent.mkdir(parents=True)
    shutil.copyfile(Path(sealed_worker.__file__), worker)
    adapter = checkout / "alibi" / "adapter.py"
    adapter.parent.mkdir()
    private = tmp_path / "private"
    private.mkdir()
    marker = private / "marker"
    marker.touch()
    adapter.write_text(
        "from pathlib import Path\n"
        "import os\n"
        "assert Path(os.environ['ALIBI_DATA_DIR']).resolve() == Path.cwd()\n"
        f"try:\n    Path({str(marker)!r}).open().close()\n"
        "except PermissionError:\n    pass\n"
        "else:\n    raise AssertionError('sandbox denied path was readable')\n"
        "def run_case(case, *, mode):\n    raise AssertionError('preflight must not run a case')\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sealed_worker, "REPO", checkout)
    sealed_worker.verify_adapter((private,))

    adapter.write_text("def run_case(case):\n    pass\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="cannot import"):
        sealed_worker.verify_adapter((private,))

    adapter.write_text("from nonexistent_alibi_adapter_dependency import run_case\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="cannot import"):
        sealed_worker.verify_adapter((private,))


@pytest.mark.skipif(not shutil.which("sandbox-exec"), reason="the verified worker sandbox is macOS-specific")
def test_sandbox_proves_gold_path_is_unreadable(tmp_path):
    private = tmp_path / "private"
    private.mkdir()
    gold = private / "gold.jsonl"
    gold.write_text("synthetic marker")
    capture = tmp_path / "capture"
    capture.mkdir()
    predictions = capture / "agents.jsonl"
    predictions.write_text("synthetic earlier prediction")
    verify_file_isolation((private, capture), gold)
    verify_file_isolation((private, capture), predictions)
    with pytest.raises(RuntimeError, match="could not prove"):
        verify_file_isolation((tmp_path / "unrelated",), gold)
    rows = run_case_worker(canonical_case(), "rules", dry_run=True, timeout=15, blocked=(private,))
    assert all(row.get("proposed_package") for row in rows)
