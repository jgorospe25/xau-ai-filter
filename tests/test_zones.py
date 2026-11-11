from __future__ import annotations

import pandas as pd
from conftest import make_bars

from xauai import config
from xauai.structure import swing_points
from xauai.zones import first_touch, fvgs, order_blocks, valid_at

START = "2024-01-15 01:00"
# swing high 15 at idx2 (known at close of idx4 = 06:00); idx3 is the last bearish candle;
# idx6 closes above 15 (breaks); idx7 opens/after known_at.
BULL = [
    (10, 11, 9, 10), (10, 12, 9, 11), (11, 15, 10, 12), (12, 13, 11, 11), (11, 13, 10, 12),
    (12, 13, 11, 12.5), (12.5, 17, 12, 16), (16, 16, 12, 12),
]


def _mirror(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["open"], out["close"] = 1000 - df["open"], 1000 - df["close"]
    out["high"], out["low"] = 1000 - df["low"], 1000 - df["high"]
    return out


def _obs(rows, mirror=False):
    h1 = make_bars(rows, START, "h")
    if mirror:
        h1 = _mirror(h1)
    return h1, order_blocks(h1, swing_points(h1, "H1", n=2))


def test_bullish_ob_is_last_bearish_candle_before_break():
    h1, ob = _obs(BULL)
    assert len(ob) == 1
    r = ob.iloc[0]
    assert r.direction == 1 and r.top == 13 and r.bottom == 11
    assert r.ob_time == h1.time[3]
    assert pd.isna(r.invalid_at)
    assert list(ob.columns) == ["ob_id", "direction", "top", "bottom", "ob_time", "known_at", "invalid_at"]


def test_ob_not_known_before_breaking_bar_closes():
    h1, ob = _obs(BULL)
    assert ob.iloc[0].known_at == h1.time[6] + pd.Timedelta("1h")
    assert valid_at(ob, ob.iloc[0].known_at - pd.Timedelta("1min"), "invalid_at").empty
    assert len(valid_at(ob, ob.iloc[0].known_at, "invalid_at")) == 1


def test_ob_invalidated_by_close_below_bottom():
    rows = BULL[:-1] + [(16, 16, 10, 10.5)]
    h1, ob = _obs(rows)
    assert ob.iloc[0].invalid_at == h1.time[7] + pd.Timedelta("1h")
    assert valid_at(ob, ob.iloc[0].invalid_at, "invalid_at").empty


def test_bearish_ob_mirror():
    h1, ob = _obs(BULL, mirror=True)
    r = ob.iloc[0]
    assert r.direction == -1 and r.top == 989 and r.bottom == 987 and r.ob_time == h1.time[3]
    assert r.known_at == h1.time[6] + pd.Timedelta("1h") and pd.isna(r.invalid_at)
    h1, ob = _obs(BULL[:-1] + [(16, 16, 10, 10.5)], mirror=True)
    assert ob.iloc[0].invalid_at == h1.time[7] + pd.Timedelta("1h")


def test_swing_makes_only_one_ob():
    rows = BULL + [(12, 18, 11, 17.5)]  # second close above the same swing high
    _, ob = _obs(rows)
    assert len(ob) == 1


def _fvg_inputs(k: float, mirror: bool = False):
    body = k * 26 / (14 - k)  # makes middle body == k x ATR14 (its own TR is part of the ATR)
    rows = [(100, 101, 99, 100)] * 15 + [(100, 101, 99, 100), (100, 100 + body, 100, 100 + body),
                                          (100 + body, 101 + body, 102, 100.5 + body)]
    h1 = make_bars(rows, "2024-01-15 01:00", "h")
    known = h1.time[17] + pd.Timedelta("1h")
    m1 = make_bars([(110, 111, 109, 110)] * 5 + [(110, 110, 101, 105)] + [(105, 106, 104, 105)] * 4, str(known), "min")
    if mirror:
        h1, m1 = _mirror(h1), _mirror(m1)
    return h1, m1, known


def test_fvg_requires_huge_middle_body():
    h1, m1, _ = _fvg_inputs(config.FVG_BODY_ATR - 0.1)
    assert fvgs(h1, m1).empty
    h1, m1, known = _fvg_inputs(config.FVG_BODY_ATR + 0.1)
    f = fvgs(h1, m1)
    assert len(f) == 1
    r = f.iloc[0]
    assert r.direction == 1 and r.top == 102 and r.bottom == 101 and r.mid == (r.top + r.bottom) / 2
    assert r.known_at == known
    assert list(f.columns) == ["direction", "top", "bottom", "mid", "known_at", "filled_at"]


def test_fvg_not_valid_after_midpoint_touched():
    h1, m1, known = _fvg_inputs(1.6)
    f = fvgs(h1, m1)
    assert f.iloc[0].filled_at == known + pd.Timedelta("5min")
    assert len(valid_at(f, known + pd.Timedelta("4min"), "filled_at")) == 1
    assert valid_at(f, known + pd.Timedelta("6min"), "filled_at").empty


def test_bearish_fvg_mirror():
    h1, m1, known = _fvg_inputs(1.4, mirror=True)
    assert fvgs(h1, m1).empty
    h1, m1, known = _fvg_inputs(1.6, mirror=True)
    f = fvgs(h1, m1)
    r = f.iloc[0]
    assert r.direction == -1 and r.top == 899 and r.bottom == 898 and r.mid == 898.5
    assert r.filled_at == known + pd.Timedelta("5min")


def test_first_touch():
    m1 = make_bars([(10, 11, 9, 10)] * 3 + [(10, 20, 9, 10)], "2024-01-15 01:00", "min")
    assert first_touch(m1, m1.time[0], 20, 1) == m1.time[3]
    assert first_touch(m1, m1.time[4 - 1] + pd.Timedelta("1min"), 20, 1) is None
    assert first_touch(m1, m1.time[1], 9, -1) == m1.time[1]
    assert first_touch(m1, m1.time[0], 5, -1) is None
