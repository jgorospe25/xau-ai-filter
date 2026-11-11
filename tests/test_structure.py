from __future__ import annotations

import numpy as np
import pandas as pd
from conftest import make_bars

from xauai.structure import combined_trend, swing_points, trend_series


def test_swing_high_is_wick_and_confirmed_n_bars_later():
    b = make_bars([(10, 11, 9, 10), (10, 12, 9, 11), (11, 15, 10, 12), (12, 13, 11, 12), (12, 13, 10, 11)], "2024-01-15 01:00", "h")
    s = swing_points(b, "H1", n=2)
    hi = s[s.kind == 1].iloc[0]
    assert hi.price == 15 and hi.bar_time == pd.Timestamp("2024-01-15 03:00")
    assert hi.known_at == pd.Timestamp("2024-01-15 06:00")  # close of bar i+2
    assert list(s.columns) == ["kind", "price", "bar_time", "known_at"]


def test_swing_low_uses_low_wick():
    b = make_bars([(10, 12, 9, 10), (10, 12, 8, 10), (10, 12, 5, 10), (10, 12, 8, 10), (10, 12, 9, 10)], "2024-01-15 01:00", "h")
    s = swing_points(b, "H1", n=2)
    lo = s[s.kind == -1]
    assert len(lo) == 1 and lo.iloc[0].price == 5


def test_equal_highs_are_not_a_swing():  # strict >
    b = make_bars([(10, 11, 9, 10), (10, 12, 9, 11), (11, 15, 10, 12), (12, 15, 11, 12), (12, 13, 10, 11), (12, 12, 10, 11)], "2024-01-15 01:00", "h")
    s = swing_points(b, "H1", n=2)
    assert s[s.kind == 1].empty


def test_swing_not_emitted_at_edges_and_not_known_early():
    b = make_bars([(10, 20, 1, 10)] + [(10, 11, 9, 10)] * 4, "2024-01-15 01:00", "h")
    assert swing_points(b, "H1", n=2).empty  # first bar lacks left neighbours


def _bars(k: int, tf: str = "h") -> pd.DataFrame:
    return make_bars([(10, 11, 9, 10)] * k, "2024-01-15 00:00", tf)


def _sw(rows):  # (kind, price, known_at)
    return pd.DataFrame(
        {"kind": [r[0] for r in rows], "price": [r[1] for r in rows],
         "bar_time": [pd.Timestamp(r[2]) - pd.Timedelta(hours=3) for r in rows],
         "known_at": [pd.Timestamp(r[2]) for r in rows]}
    )


def _trend(rows):
    b = _bars(10)
    return trend_series(b, _sw(rows), "H1").to_numpy()


def test_trend_up_down_range():
    up = _trend([(1, 10, "2024-01-15 01:00"), (-1, 5, "2024-01-15 02:00"), (1, 12, "2024-01-15 03:00"), (-1, 6, "2024-01-15 04:00")])
    # bar i closes at 01:00+i h; HH+HL known at close of bar 3 (04:00) -> index 3
    assert list(up[:3]) == [0, 0, 0] and up[3] == 1 and up[-1] == 1
    down = _trend([(1, 12, "2024-01-15 01:00"), (-1, 6, "2024-01-15 02:00"), (1, 10, "2024-01-15 03:00"), (-1, 5, "2024-01-15 04:00")])
    assert list(down[:3]) == [0, 0, 0] and down[3] == -1 and down[-1] == -1
    rng = _trend([(1, 10, "2024-01-15 01:00"), (-1, 6, "2024-01-15 02:00"), (1, 12, "2024-01-15 03:00"), (-1, 5, "2024-01-15 04:00")])
    assert rng[-1] == 0


def test_trend_uses_only_swings_known_by_bar_close():
    t = _trend([(1, 10, "2024-01-15 01:00"), (-1, 5, "2024-01-15 02:00"), (1, 12, "2024-01-15 03:00"), (-1, 6, "2024-01-15 05:00")])
    assert t[3] == 0 and t[4] == 1  # last low known at 05:00 = close of bar 4


def test_combined_trend_requires_agreement():
    # H1: down-trending prices; M30: up-trending -> 0; both up -> +1
    def zigzag(prices):  # build bars whose swing highs/lows follow given sequence of (high, low) pairs
        rows = []
        for hi, lo in prices:
            rows += [(5, 6, 4, 5), (5, 6, 4, 5), (5, hi, lo, 5), (5, 6, 4, 5), (5, 6, 4, 5)]
        return rows
    # each block of 5 bars: middle bar is a swing high (hi) AND swing low (lo) when hi>6, lo<4.
    up = zigzag([(10, 3), (12, 3.5), (14, 3.8), (16, 3.9)])
    dn = zigzag([(16, 3.9), (14, 3.8), (12, 3.5), (10, 3)])
    m30_up = make_bars(up, "2024-01-15 01:00", "30min")
    h1_up = make_bars(up, "2024-01-15 01:00", "h")
    h1_dn = make_bars(dn, "2024-01-15 01:00", "h")
    at = pd.Series(pd.date_range("2024-01-15 00:00", periods=40, freq="h"))
    mixed = combined_trend(at, m30_up, h1_dn)
    both = combined_trend(at, m30_up, h1_up)
    assert mixed.shape == (40,) and (mixed == 0).all()
    assert both[0] == 0  # nothing closed yet
    assert both[-1] == 1
    assert set(np.unique(both)) <= {0, 1}
