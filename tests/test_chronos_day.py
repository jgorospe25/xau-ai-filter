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


# ---- Task 3: forecasts -------------------------------------------------------------------------

import sys

from xauai import chronos_day as cd


class FakePredictor:
    def __init__(self, revision: str = "r1"):
        self.model_id = "fake/model"
        self.revision = revision
        self.calls = 0

    def predict_median(self, contexts: np.ndarray, horizons: np.ndarray) -> np.ndarray:
        self.calls += 1
        return contexts[:, -1] + np.arange(len(contexts))


def _fc_inputs():
    h1 = _bars("2024-01-01", "2024-03-01", "1h")  # ~1416 bars, 512 reached ~2024-01-22
    days = pd.DataFrame({
        "day": [pd.Timestamp("2024-01-10").date(), pd.Timestamp("2024-02-12").date(),
                pd.Timestamp("2024-02-13").date()],
        "h1_last_open": pd.to_datetime(["2024-01-10 04:00", "2024-02-12 04:00", "2024-02-13 04:00"]),
        "horizon": [10, 9, 10],
        "last_close": [0.0, 0.0, 0.0],
    })
    days["last_close"] = [float(h1.loc[h1["time"] == t, "close"].iloc[0]) for t in days["h1_last_open"]]
    return days, h1


def test_contexts_end_at_last_closed_bar():
    days, h1 = _fc_inputs()
    days = days.iloc[1:].reset_index(drop=True)
    ctx = cd.contexts_for(days, h1)
    assert ctx.shape == (2, 512) and ctx.dtype == np.float64
    for i, t in enumerate(days["h1_last_open"]):
        assert ctx[i, -1] == h1.loc[h1["time"] == t, "close"].iloc[0]
        # the bar opening one hour later (still open at decision) is not included
        nxt = h1.loc[h1["time"] == t + pd.Timedelta(hours=1), "close"].iloc[0]
        assert nxt not in ctx[i]
        assert ctx[i, 0] == h1.loc[h1["time"] == t - pd.Timedelta(hours=511), "close"].iloc[0]


def test_forecast_days_warmup_dropped():
    days, h1 = _fc_inputs()
    assert cd.warmup_mask(days, h1, 512).tolist() == [True, False, False]
    fc = cd.forecast_days(days, h1, FakePredictor())
    assert list(fc.columns) == ["day", "last_close", "median", "horizon", "model_id", "revision"]
    assert fc["day"].tolist() == days["day"].iloc[1:].tolist()
    assert fc["median"].tolist() == [fc["last_close"].iloc[0], fc["last_close"].iloc[1] + 1]


def test_forecast_cache_reused_and_invalidated(tmp_path):
    days, h1 = _fc_inputs()
    path = tmp_path / "f.parquet"
    made: list[FakePredictor] = []

    def factory(rev="r1"):
        p = FakePredictor(rev)
        made.append(p)
        return p

    a = cd.load_or_build_forecasts(days, h1, factory, path, model_id="fake/model", revision="r1")
    assert made[0].calls == 1 and len(made) == 1
    b = cd.load_or_build_forecasts(days, h1, factory, path, model_id="fake/model", revision="r1")
    assert len(made) == 1  # reused: factory not even called
    pd.testing.assert_frame_equal(a, b)
    cd.load_or_build_forecasts(days, h1, lambda: factory("r2"), path, model_id="fake/model", revision="r2")
    assert len(made) == 2 and made[1].calls == 1
    # revision None: reuse when model_id matches and days are covered
    c = cd.load_or_build_forecasts(days.iloc[1:2], h1, factory, path, model_id="fake/model")
    assert len(made) == 2 and len(c) == 1
    # uncovered day -> rebuild
    more = days.copy()
    more.loc[len(more)] = [pd.Timestamp("2024-02-14").date(), pd.Timestamp("2024-02-14 04:00"), 10, 0.0]
    more.loc[len(more) - 1, "last_close"] = float(h1.loc[h1["time"] == pd.Timestamp("2024-02-14 04:00"), "close"].iloc[0])
    cd.load_or_build_forecasts(more, h1, lambda: factory("r2"), path, model_id="fake/model", revision="r2")
    assert len(made) == 3


def test_unit_tests_do_not_import_chronos():
    assert "chronos" not in sys.modules and "torch" not in sys.modules
    assert hasattr(cd, "ChronosBoltPredictor")


# --- directions, rivals, grid, selection (Task 4) -------------------------------------------------


def _dir_inputs(delta: float, atr: float = 2.0):
    days = pd.DataFrame({
        "day": [pd.Timestamp("2024-01-10").date()], "last_close": [100.0], "atr": [atr],
        "prev_exit_price": [99.0],
    })
    fc = pd.DataFrame({"day": days["day"], "median": [100.0 + delta]})
    return days, fc


def test_grid_is_18_named():
    g = cd.chronos_grid()
    names = [v.name for v in g]
    assert len(g) == 18 and len(set(names)) == 18
    assert "m2.0|tp2.0|flat" in names and "m1.5|be_trail|all" in names
    assert names[0] == "m1.5|tp1.5|all" and names[1] == "m1.5|tp1.5|flat"


def test_flat_filter_drops_small_moves():
    days, fc = _dir_inputs(0.4, atr=2.0)  # 0.4 = 0.2 * atr
    assert len(cd.chronos_directions(days, fc, flat=True)) == 0
    kept = cd.chronos_directions(days, fc, flat=False)
    assert len(kept) == 1 and kept["direction"].iloc[0] == 1
    days, fc = _dir_inputs(0.0)
    assert len(cd.chronos_directions(days, fc, flat=False)) == 0
    days, fc = _dir_inputs(-3.0)
    assert cd.chronos_directions(days, fc, flat=True)["direction"].iloc[0] == -1


def test_overnight_direction_sign():
    days = pd.DataFrame({"last_close": [101.0, 99.0, 100.0, 100.0], "prev_exit_price": [100.0, 100.0, 100.0, np.nan]})
    s = cd.overnight_directions(days)
    assert s.dropna().tolist() == [1, -1] and s.index.tolist() == [0, 1]


def test_coin_flips_seeded_per_variant():
    m1 = _m1(_flat(3) + [(100, 103.5, 99.9, 103)] + _flat(3, 103))
    days = _days(m1, 1, 6)
    v = cd.ChronosVariant(1.0, "tp1.5", False)
    big = pd.concat([days.assign(day=pd.Timestamp("2024-01-10").date())] * 1)
    a = cd.coin_flip_exps(big, m1, _ZERO, _NOSWAP, v, 30, 11)
    b = cd.coin_flip_exps(big, m1, _ZERO, _NOSWAP, v, 30, 11)
    c = cd.coin_flip_exps(big, m1, _ZERO, _NOSWAP, v, 30, 12)
    assert len(a) == 30 and np.array_equal(a, b, equal_nan=True)
    assert not np.array_equal(a, c, equal_nan=True)


def test_select_order_and_best_only():
    base = dict(n=150, exp_2022=0.1, exp_2023=0.1, exp_2024=0.1, exp_2025=0.1, beat_share=0.99, overnight_exp=0.0)
    t = pd.DataFrame([
        {**base, "name": "A", "exp_2024": -0.1, "expectancy_R": 0.5},
        {**base, "name": "B", "expectancy_R": 0.2},
        {**base, "name": "C", "expectancy_R": 0.3, "overnight_exp": 0.4},
    ])
    out = cd.chronos_select(t)
    assert out["fail"].tolist() == ["years", "", "overnight"]
    assert out["selected"].tolist() == [False, True, False]
