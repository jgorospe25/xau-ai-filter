from __future__ import annotations

import numpy as np
import pandas as pd

from conftest import make_bars
from xauai.levelstudy import (
    draw_other,
    fill_hours,
    level_reaction,
    transplant_points,
)

START = pd.Timestamp("2024-01-02 08:00")
END = START + pd.Timedelta(hours=10)


def _m1(rows):
    return make_bars(rows, str(START), "1min")


def test_untouched():
    m1 = _m1([(95, 98, 94, 96)] * 5)
    assert level_reaction(m1, 100.0, 1, START, END, 2.0) == "untouched"


def test_reversed_above():
    # touch 100, then low <= 98 before high >= 102
    m1 = _m1([(95, 96, 94, 95), (99, 100, 98.5, 99), (99, 99.5, 97.5, 98), (98, 103, 98, 102)])
    assert level_reaction(m1, 100.0, 1, START, END, 2.0) == "reversed"


def test_broke_above():
    m1 = _m1([(95, 96, 94, 95), (99, 100, 98.5, 99), (99, 102.5, 98.5, 102)])
    assert level_reaction(m1, 100.0, 1, START, END, 2.0) == "broke"


def test_same_bar_both_is_broke():
    m1 = _m1([(99, 100, 99, 99.5), (99, 103, 97, 100)])
    assert level_reaction(m1, 100.0, 1, START, END, 2.0) == "broke"


def test_touched_no_resolution():
    m1 = _m1([(99, 100, 98.5, 99), (99, 100.5, 98.5, 99)])
    assert level_reaction(m1, 100.0, 1, START, END, 2.0) == "touched_no_resolution"


def test_below_side_mirrors():
    # level 100 below price; touched by low; reversal = high >= 102
    rev = _m1([(105, 106, 104, 105), (101, 102, 100, 101), (101, 102.5, 100.5, 102)])
    assert level_reaction(rev, 100.0, -1, START, END, 2.0) == "reversed"
    brk = _m1([(105, 106, 104, 105), (101, 101.5, 100, 101), (100, 100.5, 97.5, 98)])
    assert level_reaction(brk, 100.0, -1, START, END, 2.0) == "broke"


def test_window_is_respected():
    m1 = _m1([(95, 96, 94, 95)] * 3 + [(99, 101, 98, 100)])
    assert level_reaction(m1, 100.0, 1, START, START + pd.Timedelta(minutes=3), 2.0) == "untouched"


def test_random_levels_same_side_same_distance():
    rng = np.random.default_rng(7)
    real_price, real_open, real_atr = 2010.0, 2000.0, 5.0  # +2 ATR above
    d = (real_price - real_open) / real_atr
    opens = np.array([1900.0, 2100.0, 1800.0])
    atrs = np.array([4.0, 8.0, 2.0])
    idx = draw_other(rng, 500, 3, exclude=1)
    assert len(idx) == 500 and not (idx == 1).any()
    px = transplant_points(d, opens[idx], atrs[idx])
    assert np.all(px > opens[idx])
    assert np.allclose((px - opens[idx]) / atrs[idx], d)
    below = transplant_points(-1.5, opens[idx], atrs[idx])
    assert np.all(below < opens[idx])


def test_fill_hours_within_and_beyond_horizon():
    n = 60 * 60
    rows = [(100, 100.5, 99.5, 100)] * n
    rows[3 * 60] = (100, 100.5, 94.5, 95)   # low touches 95 at +3 h
    rows[50 * 60] = (100, 100.5, 94.5, 95)
    m1 = make_bars(rows, "2024-01-02 00:00", "1min")
    after = pd.Timestamp("2024-01-02 00:00")
    assert fill_hours(m1, after, 95.0, -1, 48) == 3.0
    rows2 = [(100, 100.5, 99.5, 100)] * n
    rows2[50 * 60] = (100, 100.5, 94.5, 95)
    m2 = make_bars(rows2, "2024-01-02 00:00", "1min")
    assert np.isnan(fill_hours(m2, after, 95.0, -1, 48))
    # side +1: reached from below
    rows3 = [(100, 100.5, 99.5, 100)] * n
    rows3[90] = (100, 106, 99.5, 105)
    m3 = make_bars(rows3, "2024-01-02 00:00", "1min")
    assert fill_hours(m3, after, 105.0, 1, 48) == 1.5


def test_fvg_random_points_same_side_same_distance():
    rng = np.random.default_rng(7)
    mid, close3, atr3 = 1995.0, 2000.0, 5.0  # bullish gap: midpoint 1 ATR below the close
    d = (mid - close3) / atr3
    closes = np.array([1900.0, 2100.0, 1800.0, 1700.0])
    atrs = np.array([4.0, 8.0, 2.0, 3.0])
    idx = draw_other(rng, 500, 4, exclude=0)
    px = transplant_points(d, closes[idx], atrs[idx])
    assert np.all(px < closes[idx])
    assert np.allclose((px - closes[idx]) / atrs[idx], d)


def test_touch_bar_reversal_above():
    m1 = _m1([(99, 100, 97.5, 98)])  # touches 100 and drops to 97.5 (<= 98) without reaching 102
    assert level_reaction(m1, 100.0, 1, START, END, 2.0) == "reversed"


def test_touch_bar_reversal_below():
    m1 = _m1([(101, 102, 100, 101.5)])  # touches 100 and rises to 102 (>= 102) without reaching 98
    assert level_reaction(m1, 100.0, -1, START, END, 2.0) == "reversed"


def test_touch_bar_with_both_is_broke():
    m1 = _m1([(99, 102.5, 97.5, 100)])
    assert level_reaction(m1, 100.0, 1, START, END, 2.0) == "broke"
