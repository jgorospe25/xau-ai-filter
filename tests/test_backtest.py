import numpy as np
import pandas as pd
import pytest

from xauai.backtest import (
    assign_split,
    bootstrap_ci,
    longest_losing_streak,
    max_drawdown,
    one_position_max,
    profit_factor,
)


def test_max_drawdown_from_peak():
    assert max_drawdown(np.array([2.0, -1.0, -1.0, 2.0, -1.0])) == 2.0
    assert max_drawdown(np.array([-1.0, -1.0])) == 2.0  # measured from the starting 0
    assert max_drawdown(np.array([1.0, 2.0])) == 0.0


def test_longest_losing_streak():
    assert longest_losing_streak(np.array([2, -1, -1, 2, -1, -1, -1, 0.5])) == 3


def test_profit_factor():
    assert profit_factor(np.array([2.0, -1.0, -1.0])) == pytest.approx(1.0)


def test_bootstrap_ci_brackets_mean():
    r = np.random.default_rng(1).normal(0.1, 1, 300)
    lo, hi = bootstrap_ci(r, n=2000)
    assert lo < r.mean() < hi


def test_assign_split_inclusive_end_dates():
    t = pd.Series(pd.to_datetime(["2024-12-31 23:00", "2025-01-01 01:00", "2025-10-23 23:00", "2025-10-24 01:00"]))
    assert assign_split(t).tolist() == ["train", "val", "val", "test"]


def test_one_position_max_skips_overlaps():
    tr = pd.DataFrame({
        "entry_time": pd.to_datetime(["2024-01-01 01:00", "2024-01-01 03:00", "2024-01-01 06:00"]),
        "exit_time": pd.to_datetime(["2024-01-01 05:00", "2024-01-01 04:00", "2024-01-01 07:00"]),
    })
    assert one_position_max(tr).index.tolist() == [0, 2]
