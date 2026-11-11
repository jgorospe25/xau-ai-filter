"""Chronos day table: decision/entry/exit times, skips, previous-exit price (synthetic bars)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from xauai.chronos_day import day_table


def _bars(start: str, end: str, freq: str, drop: list[str] | None = None) -> pd.DataFrame:
    t = pd.date_range(start, end, freq=freq, inclusive="left")
    if drop:
        t = t.drop(pd.to_datetime(drop))
    n = len(t)
    c = 2000.0 + np.sin(np.arange(n) / 7.0) * 5 + np.arange(n) * 0.01
    return pd.DataFrame({"time": t, "open": c, "high": c + 1.0, "low": c - 1.0, "close": c, "spread": 20})


def _run(day: str, drop_h1: list[str] | None = None):
    d = pd.Timestamp(day)
    s, e = d - pd.Timedelta(days=6), d + pd.Timedelta(days=2)
    h1 = _bars(s, e, "1h", drop_h1)
    m1 = _bars(s, e, "1min")
    return day_table(h1, m1, day, day)


def test_day_times_winter():
    days, _ = _run("2024-01-10")
    r = days.iloc[0]
    assert r["decision_time"] == pd.Timestamp("2024-01-10 04:00")
    assert r["exit_time"] == pd.Timestamp("2024-01-10 14:00")
    assert r["horizon"] == 10


def test_day_times_summer():
    days, _ = _run("2024-07-10")
    r = days.iloc[0]
    assert r["decision_time"] == pd.Timestamp("2024-07-10 05:00")
    assert r["exit_time"] == pd.Timestamp("2024-07-10 14:00")
    assert r["horizon"] == 9


def test_day_times_uk_us_dst_gap():
    for day in ("2024-03-13", "2024-10-30"):
        days, _ = _run(day)
        r = days.iloc[0]
        assert r["decision_time"] == pd.Timestamp(f"{day} 05:00")
        assert r["exit_time"] == pd.Timestamp(f"{day} 15:00")
        assert r["horizon"] == 10
    days, _ = _run("2024-03-13")
    assert days.iloc[0]["h1_last_open"] == pd.Timestamp("2024-03-13 04:00")


def test_day_table_skips_missing_bars():
    days, skipped = _run("2024-01-10", drop_h1=["2024-01-10 03:00"])
    assert days.empty
    assert skipped.iloc[0]["reason"] == "no_decision_bar"
    assert skipped.iloc[0]["day"] == pd.Timestamp("2024-01-10").date()


def test_prev_exit_price_monday_uses_friday():
    d = pd.Timestamp("2024-01-15")  # Monday
    h1 = _bars(d - pd.Timedelta(days=6), d + pd.Timedelta(days=2), "1h")
    m1 = _bars(d - pd.Timedelta(days=6), d + pd.Timedelta(days=2), "1min")
    days, _ = day_table(h1, m1, "2024-01-15", "2024-01-15")
    fri_exit = pd.Timestamp("2024-01-12 14:00")
    expected = m1.loc[m1["time"] < fri_exit, "close"].iloc[-1]
    assert days.iloc[0]["prev_exit_price"] == expected


# --- simulator (Task 2) ------------------------------------------------------------------------
from xauai.chronos_day import simulate_days, simulate_trail_trade  # noqa: E402
from xauai.labels import CostParams, SwapParams  # noqa: E402

_ZERO = CostParams(spread_floor=0.0, slippage=0.0)
_NOSWAP = SwapParams(long_points=0.0, short_points=0.0, point=0.01, triple_weekday=2)


def _m1(prices: list[tuple[float, float, float, float]], start: str = "2024-01-10 04:00") -> pd.DataFrame:
    """M1 bars from (open, high, low, close) tuples; zero spread."""
    t = pd.date_range(start, periods=len(prices), freq="1min")
    a = np.array(prices, float)
    return pd.DataFrame({"time": t, "open": a[:, 0], "high": a[:, 1], "low": a[:, 2], "close": a[:, 3], "spread": 0})


def _days(m1: pd.DataFrame, d: int, exit_idx: int, atr: float = 2.0) -> pd.DataFrame:
    t = m1["time"]
    return pd.DataFrame({
        "day": [t[0].date()], "decision_time": [t[0]], "entry_time": [t[0]], "exit_time": [t[exit_idx]],
        "h1_last_open": [t[0] - pd.Timedelta(hours=1)], "last_close": [100.0], "atr": [atr], "horizon": [1],
        "prev_exit_price": [np.nan], "direction": [d],
    })


def _flat(n: int, px: float = 100.0) -> list[tuple[float, float, float, float]]:
    return [(px, px + 0.1, px - 0.1, px)] * n


def _one(prices, d, style, m=1.0, exit_idx=None, cost=_ZERO, swap=_NOSWAP, atr=2.0):
    m1 = _m1(prices)
    days = _days(m1, d, len(prices) - 1 if exit_idx is None else exit_idx, atr)
    trades, skipped = simulate_days(days, m1, cost, swap, m, style)
    assert skipped.empty, skipped
    return trades.iloc[0]


def test_fixed_tp_hit():
    # atr 2, m 1 -> dist 2; tp1.5 = 103
    r = _one(_flat(3) + [(100, 103.5, 99.9, 103)] + _flat(3, 103), 1, "tp1.5")
    assert r["exit_reason"] == "tp"
    assert r["r"] == pytest.approx(1.5)
    assert r["sl_dist"] == pytest.approx(2.0)
    assert bool(r["taken"]) is True


def test_sl_and_tp_same_bar_is_loss():
    r = _one(_flat(3) + [(100, 105, 97, 100)] + _flat(2), 1, "tp2.0")
    assert r["r"] == pytest.approx(-1.0)


def test_time_exit_at_london_noon():
    prices = _flat(3) + [(100, 100.5, 99.5, 100.2)] + [(100.7, 100.8, 100.6, 100.7)] + _flat(3, 100.7)
    r = _one(prices, 1, "tp2.0", exit_idx=4)
    assert r["exit_reason"] == "session_end"
    assert r["exit_price"] == pytest.approx(100.7)
    assert r["exit_time"] == pd.Timestamp("2024-01-10 04:04")


def test_trail_moves_to_breakeven():
    # +1R = 102; bar 2 reaches it, bar 4 returns to fill 100
    prices = _flat(2) + [(100, 102.0, 99.9, 102)] + [(102, 102.0, 101.5, 101.8)] + [(101.5, 101.6, 99.5, 100)] + _flat(2)
    r = _one(prices, 1, "be_trail")
    assert r["exit_reason"] == "be_sl"
    assert r["exit_price"] == pytest.approx(100.0)
    assert r["r_price"] == pytest.approx(0.0)


def test_trail_follows_best():
    # atr 1: best 103 (fill + 3 atr) -> stop 102; a later dip to 101 exits there
    prices = _flat(2) + [(100, 103.0, 99.9, 103)] + [(103, 103.0, 102.5, 103)] + [(103, 103.1, 101.0, 102)] + _flat(2, 102)
    r = _one(prices, 1, "be_trail", atr=1.0)
    assert r["exit_reason"] == "trail_sl"
    assert r["exit_price"] == pytest.approx(102.0)
    assert r["r_price"] == pytest.approx(2.0)


def test_trail_same_bar_plus1r_and_sl_is_loss():
    prices = _flat(2) + [(100, 103, 97.5, 100)] + _flat(3)
    r = _one(prices, 1, "be_trail")
    assert r["exit_reason"] == "sl"
    assert r["r"] == pytest.approx(-1.0)


def test_trail_stop_out_on_bar_that_makes_new_best():
    # bar after arming: new best 106 but low touches the prior stop (102) -> out at 102
    prices = _flat(2) + [(100, 104, 99.9, 104)] + [(104, 106, 101.9, 105)] + _flat(2, 105)
    r = _one(prices, 1, "be_trail", atr=1.0)
    assert r["exit_reason"] == "trail_sl"
    # armed on bar 2 with best 104 -> stop 103; bar 3 low 101.9 touches it
    assert r["exit_price"] == pytest.approx(103.0)


def test_sell_mirror_trail():
    prices = _flat(2) + [(100, 100.1, 97.0, 97)] + [(97, 97.5, 97.0, 97)] + [(97, 99.0, 96.9, 98)] + _flat(2, 98)
    r = _one(prices, -1, "be_trail", atr=1.0)
    assert r["exit_reason"] == "trail_sl"
    assert r["exit_price"] == pytest.approx(98.0)
    assert r["r_price"] == pytest.approx(2.0)


def test_cost_shifts_fill_against_trade():
    cost = CostParams(spread_floor=0.30, slippage=0.05)
    prices = [(100, 100.1, 99.9, 100)] * 4
    buy = _one(prices, 1, "tp2.0", cost=cost)
    sell = _one(prices, -1, "tp2.0", cost=cost)
    assert buy["entry"] == pytest.approx(100.35)
    assert sell["entry"] == pytest.approx(99.65)
    assert buy["cost"] == pytest.approx(0.35)


def test_simulate_days_skips_sl_behind_fill():
    m1 = _m1(_flat(4))
    days = _days(m1, 1, 3, atr=0.0)
    trades, skipped = simulate_days(days, m1, _ZERO, _NOSWAP, 1.0, "tp2.0")
    assert trades.empty
    assert skipped.iloc[0]["reason"] == "sl_behind_fill"
