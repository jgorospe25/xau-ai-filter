"""Chronos day table: decision/entry/exit times, skips, previous-exit price (synthetic bars)."""

from __future__ import annotations

import numpy as np
import pandas as pd

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
