import pytest

from agents.eval.headline import _failure_row, exact_sign_test


def test_exact_sign_test_uses_both_tails_and_leaves_no_comparison_when_all_tied():
    assert exact_sign_test(4, 0) == exact_sign_test(0, 4) == 0.125
    assert exact_sign_test(3, 1) == 0.625
    assert exact_sign_test(2, 2) == 1.0
    assert exact_sign_test(0, 0) is None


def test_zero_failure_bound_uses_only_observed_trials():
    row = _failure_row(0, 10, 47)
    assert row["upper_95"] == pytest.approx(0.25886555)
    assert row["observed"] == 10 and row["eligible"] == 47
    assert _failure_row(0, 0, 47)["upper_95"] is None
    assert _failure_row(1, 10, 47)["upper_95"] is None
