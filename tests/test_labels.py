"""Label rules. Signal on bar 0, entry at bar 1 open = 100, ATR 10 -> SL distance 15.
Buy: SL 85, TP 130. Sell: SL 115, TP 70. Costs and swap are zero unless a test sets them."""

import pandas as pd
import pytest

from xauai.labels import CostParams, SwapParams, label_signals, rollovers

NO_COST = CostParams(spread_floor=0.0, slippage=0.0, point=0.01)
NO_SWAP = SwapParams(long_points=0.0, short_points=0.0, point=0.01, triple_weekday=2)
MON = "2024-01-01 01:00"  # Monday
FLAT = (100, 101, 99, 100)


def bars(rows, start=MON, times=None, signal=1):
    """rows: list of (open, high, low, close). Bar 0 is the signal bar."""
    t = pd.DatetimeIndex(times) if times is not None else pd.date_range(start, periods=len(rows), freq="h")
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"], dtype=float)
    df.insert(0, "time", t)
    df["spread"] = 0.0
    df["atr"] = 10.0
    df["signal"] = 0
    df.loc[0, "signal"] = signal
    return df


def one(df, cost=NO_COST, swap=NO_SWAP):
    trades, skipped = label_signals(df, cost, swap)
    assert len(trades) == 1 and skipped.empty
    return trades.iloc[0]


def test_tp_hit():
    tr = one(bars([FLAT, FLAT, (100, 131, 99, 125)]))
    assert tr.exit_reason == "tp" and tr.exit_price == 130 and tr.r == pytest.approx(2.0)


def test_sl_hit():
    tr = one(bars([FLAT, FLAT, (100, 101, 84, 90)]))
    assert tr.exit_reason == "sl" and tr.r == pytest.approx(-1.0)


def test_sl_and_tp_same_bar_is_a_loss():
    tr = one(bars([FLAT, FLAT, (100, 131, 84, 120)]))
    assert tr.exit_reason == "sl_same_bar" and tr.r == pytest.approx(-1.0)


def test_sl_hit_on_entry_bar():
    tr = one(bars([FLAT, (100, 101, 80, 90)]))
    assert tr.exit_reason == "sl" and tr.bars_held == 1


def test_timeout_after_48_bars_at_close():
    rows = [FLAT] * 48 + [(103, 104, 102, 103)] + [FLAT] * 5  # entry bar 1 -> bar 48 = 48th bar held
    tr = one(bars(rows, start="2024-01-08 01:00"))  # Mon..Wed, no Friday inside
    assert tr.exit_reason == "timeout" and tr.bars_held == 48
    assert tr.exit_price == 103 and tr.r == pytest.approx(3 / 15)


def test_friday_22_forced_exit_at_open():
    # signal Fri 19:00, entry 20:00, Friday 22:00 bar opens at 104
    df = bars([FLAT, FLAT, FLAT, (104, 106, 103, 105), FLAT], start="2024-01-05 19:00")
    tr = one(df)
    assert tr.exit_reason == "friday" and tr.exit_time == pd.Timestamp("2024-01-05 22:00")
    assert tr.exit_price == 104 and tr.r == pytest.approx(4 / 15)


def test_no_entry_at_or_after_friday_22():
    df = bars([FLAT, FLAT, FLAT], start="2024-01-05 21:00")  # entry bar would be Fri 22:00
    trades, skipped = label_signals(df, NO_COST, NO_SWAP)
    assert trades.empty and skipped["reason"].tolist() == ["entry_after_friday_close"]


def test_gap_through_sl_fills_at_open_beyond_1r():
    tr = one(bars([FLAT, FLAT, (80, 82, 78, 81)]))
    assert tr.exit_reason == "gap_sl" and tr.exit_price == 80 and tr.r == pytest.approx(-20 / 15)


def test_gap_through_tp_fills_at_open():
    tr = one(bars([FLAT, FLAT, (140, 141, 139, 140)]))
    assert tr.exit_reason == "gap_tp" and tr.r == pytest.approx(40 / 15)


def test_holiday_gap_exit_at_close_before_gap():
    times = ["2024-12-24 18:00", "2024-12-24 19:00", "2024-12-24 20:00", "2024-12-26 01:00"]
    tr = one(bars([FLAT, FLAT, (100, 102, 99, 102), FLAT], times=times))
    assert tr.exit_reason == "holiday" and tr.exit_price == 102
    assert tr.exit_time < pd.Timestamp("2024-12-24 21:00")


def test_stale_signal_after_gap_is_skipped():
    times = ["2024-12-24 20:00", "2024-12-26 01:00", "2024-12-26 02:00"]
    trades, skipped = label_signals(bars([FLAT] * 3, times=times), NO_COST, NO_SWAP)
    assert trades.empty and skipped["reason"].tolist() == ["stale_after_gap"]


def test_sell_mirror():
    tr = one(bars([FLAT, FLAT, (100, 101, 69, 75)], signal=-1))
    assert tr.exit_reason == "tp" and tr.r == pytest.approx(2.0)


def test_cost_shifts_entry_against_trade():
    cost = CostParams(spread_floor=0.30, slippage=0.05, point=0.01)
    buy = one(bars([FLAT, FLAT, (100, 131, 99, 125)]), cost=cost)
    assert buy.entry == pytest.approx(100.35) and buy.cost == pytest.approx(0.35)
    sell = one(bars([FLAT, FLAT, (100, 101, 69, 75)], signal=-1), cost=cost)
    assert sell.entry == pytest.approx(99.65)


def test_cost_uses_bar_spread_when_above_floor():
    df = bars([FLAT, FLAT, (100, 131, 99, 125)])
    df["spread"] = 45  # points -> 0.45 > 0.30 floor
    assert one(df, cost=CostParams(0.30, 0.05, 0.01)).cost == pytest.approx(0.50)


def test_wednesday_rollover_is_triple():
    # Tue night = 1, Wed night = 3; Python weekday 2 = Wednesday
    assert rollovers(pd.Timestamp("2024-01-02 22:00"), pd.Timestamp("2024-01-03 02:00"), 2) == (1, 1)
    assert rollovers(pd.Timestamp("2024-01-03 22:00"), pd.Timestamp("2024-01-04 02:00"), 2) == (1, 3)
    assert rollovers(pd.Timestamp("2024-01-02 22:00"), pd.Timestamp("2024-01-04 02:00"), 2) == (2, 4)
    assert rollovers(pd.Timestamp("2024-01-03 01:00"), pd.Timestamp("2024-01-03 23:59"), 2) == (0, 0)


def test_wednesday_triple_swap_charged_in_r():
    swap = SwapParams(long_points=-10.0, short_points=5.0, point=0.01, triple_weekday=2)
    # signal Wed 2024-01-03 21:00, entry 22:00, SL hit on Thu 02:00 bar
    rows = [FLAT, FLAT, FLAT, FLAT, FLAT, (100, 101, 84, 90)]
    tr = one(bars(rows, start="2024-01-03 21:00"), swap=swap)
    assert tr.nights == 1 and tr.swap_nights == 3
    assert tr.swap_r == pytest.approx(3 * -10 * 0.01 / 15)
    assert tr.r == pytest.approx(-1.0 + tr.swap_r)


def test_swap_scaled_by_price():
    swap = SwapParams(-10.0, 5.0, 0.01, triple_weekday=2, reference_close=200.0)
    assert swap.points_per_night(1, 100.0) == pytest.approx(-5.0)
    assert swap.points_per_night(-1, 100.0) == pytest.approx(2.5)


def test_from_symbol_info_maps_mt5_weekday():
    info = {"swap_mode": 1, "swap_long": -77.54, "swap_short": 32.15, "point": 0.01, "swap_rollover3days": 3}
    assert SwapParams.from_symbol_info(info).triple_weekday == 2  # MT5 Wednesday=3 -> Python 2
