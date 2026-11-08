"""Rules release diagnostics share the strict semantic check with one-pass."""
import pytest

from alibi.adapter import run_case
from alibi.tests.test_release_diagnostic_safety import RELEASE_GAP, release_case, correction_invalidates


@pytest.mark.parametrize("change", [None, "unrelated", "negative", "conditional", "currency", "status", "untrusted"])
def test_rules_only_affirmative_issuer_release_changes_gap(tmp_path, change):
    case, _ = release_case(change)
    rows = run_case(case, "rules", root=tmp_path / "run")
    for row in rows:
        assert row["failure"] is None
        assert all(c["status"] == "unresolved" and (c["gap"] == RELEASE_GAP) == (change is None)
                   for c in row["claims"].values())


@pytest.mark.parametrize("initially_release", [True, False])
def test_rules_release_correction_invalidates_minimal_review(tmp_path, initially_release):
    correction_invalidates(tmp_path, "rules", initially_release)
