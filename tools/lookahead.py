"""Catch strategies that read the future.

A decision made at time t may only depend on data up to t. So if you cut off, or scramble,
everything after t, the decision at t must not change. This module checks exactly that,
without reading the strategy's code.

    from tools.lookahead import assert_no_lookahead

    def decide(frame, t):            # adapt the real interface to this shape
        return strategy.decision(features(frame), t)

    assert_no_lookahead(decide, prices, at=30, column="timestamp")

`decide(frame, t)` must build everything from `frame`, raw data included, so that leaks in
feature code are caught too. `at` is a list of times to test, or a number of times to pick
from the middle of the data.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd


def times_to_test(times: pd.Series, at, skip: float = 0.1) -> list:
    """The times to test: the ones given, or `at` evenly spaced ones away from both ends."""
    if not isinstance(at, int):
        return list(at)
    ordered = times.dropna().sort_values().unique()
    lo, hi = int(len(ordered) * skip), int(len(ordered) * (1 - skip)) - 1
    if hi <= lo:
        return list(ordered)
    picks = np.linspace(lo, hi, num=min(at, hi - lo + 1)).round().astype(int)
    return [ordered[i] for i in dict.fromkeys(picks)]


def perturbed(frame: pd.DataFrame, future, column: str | None, rng) -> pd.DataFrame:
    """Copy of frame where every row after t has different values. Time itself is kept."""
    changed = frame.copy()
    for name in changed.columns:
        if name == column:
            continue
        values = changed.loc[future, name].to_numpy()
        if len(values) == 0:
            continue
        dtype = changed[name].dtype
        try:
            if pd.api.types.is_bool_dtype(dtype):
                new = ~rng.permutation(values).astype(bool)
            elif pd.api.types.is_integer_dtype(dtype):
                spread = int(np.std(values)) + 1
                new = (rng.permutation(values) * int(rng.integers(2, 4))
                       + rng.integers(-spread, spread + 1, len(values)))
            elif pd.api.types.is_numeric_dtype(dtype):
                spread = float(np.nanstd(values)) if len(values) > 1 else 1.0
                new = (rng.permutation(values) * rng.uniform(1.5, 3.0)
                       + rng.normal(0, spread + 1, len(values)))
            else:
                new = rng.permutation(values)
            changed.loc[future, name] = new
        except (TypeError, ValueError):
            changed.loc[future, name] = rng.permutation(values)
    return changed


def same(a, b, rtol: float, atol: float) -> bool:
    """Equality for decisions: numbers, arrays, pandas objects, dicts and lists."""
    if isinstance(a, pd.DataFrame) or isinstance(a, pd.Series):
        try:
            check = pd.testing.assert_frame_equal if isinstance(a, pd.DataFrame) else pd.testing.assert_series_equal
            check(a, b, check_exact=False, rtol=rtol, atol=atol)
            return True
        except (AssertionError, TypeError):
            return False
    if isinstance(a, dict):
        return isinstance(b, dict) and a.keys() == b.keys() and all(same(a[k], b[k], rtol, atol) for k in a)
    if isinstance(a, (list, tuple)):
        return isinstance(b, (list, tuple)) and len(a) == len(b) and all(same(x, y, rtol, atol) for x, y in zip(a, b))
    if isinstance(a, np.ndarray) or isinstance(b, np.ndarray):
        try:
            return np.allclose(a, b, rtol=rtol, atol=atol, equal_nan=True)
        except TypeError:
            return bool(np.array_equal(a, b))
    if isinstance(a, (int, float, np.number)) and isinstance(b, (int, float, np.number)) \
            and not isinstance(a, bool) and not isinstance(b, bool):
        if math.isnan(a) and math.isnan(b):
            return True
        return math.isclose(a, b, rel_tol=rtol, abs_tol=atol)
    return a == b


def find_lookahead(decide, frame: pd.DataFrame, at=20, *, column: str | None = None,
                   seed: int = 0, rtol: float = 1e-9, atol: float = 1e-12) -> list[str]:
    """Return one line per time where the decision changed when the future changed."""
    times = frame.index.to_series(index=frame.index) if column is None else frame[column]
    rng = np.random.default_rng(seed)
    problems = []
    for t in times_to_test(times, at):
        future = (times > t).to_numpy()
        if not future.any():
            continue
        base = decide(frame, t)
        trials = {"cut off": lambda: frame[~future],
                  "scrambled": lambda: perturbed(frame, future, column, rng)}
        for label, make in trials.items():
            try:
                other = decide(make(), t)
            except Exception as error:
                problems.append(f"at {t}: decide failed when the future was {label}: {error!r}")
                continue
            if not same(base, other, rtol, atol):
                problems.append(f"at {t}: decision changed when the future was {label} "
                                f"({base!r} became {other!r})")
    return problems


def assert_no_lookahead(decide, frame: pd.DataFrame, at=20, **options) -> None:
    """Raise AssertionError listing every time the strategy read the future."""
    problems = find_lookahead(decide, frame, at, **options)
    if problems:
        shown = "\n".join(problems[:20])
        more = f"\n...and {len(problems) - 20} more" if len(problems) > 20 else ""
        raise AssertionError(f"lookahead found at {len(problems)} point(s):\n{shown}{more}")
