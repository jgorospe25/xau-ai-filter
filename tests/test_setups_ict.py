"""ICT Model 1 / Model 3 signals and the displacement check on synthetic M5 bars.

Bars 0..19 are a flat warm-up (range 2 -> ATR14 = 2, tolerance ~0.2, displacement threshold ~4).
Zones are built directly (ZONE_COLS), valid from bar 20. Bar i opens at 00:00 + 5 min * i.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from conftest import make_bars

from xauai.setups import ZONE_COLS
from xauai.setups_ict import displacement, model1, model3
from xauai.sim import LIMIT_SIGNAL_COLS

START = "2024-01-02 00:00"
FLAT = (100.0, 101.0, 99.0, 100.0)
WARM = [FLAT] * 20


def T(i: int) -> pd.Timestamp:
    return pd.Timestamp(START) + pd.Timedelta(minutes=5 * i)


def bars(rows, n_tail=15):
    return make_bars(list(WARM) + list(rows) + [FLAT] * n_tail, START, "5min")


def mirror(rows):
    return [(200 - o, 200 - l, 200 - h, 200 - c) for o, h, l, c in rows]


def zones(*specs):
    """specs: (price, side[, valid_from])."""
    rows = []
    for k, s in enumerate(specs):
        price, side = s[0], s[1]
        vf = s[2] if len(s) > 2 else T(20)
        rows.append((k, side, float(price), "pwh" if side == 1 else "pwl", vf, pd.NaT, pd.NaT))
    out = pd.DataFrame(rows, columns=ZONE_COLS)
    for col in ("valid_from", "valid_to", "broken_at"):
        out[col] = pd.to_datetime(out[col])
    return out


# Sell at a zone at 103. Swing low 98.5 at bar 21 (known at close of 23).
FULL = [
    FLAT,  # 20
    (100.0, 100.5, 98.5, 99.0),  # 21 swing low 98.5
    (99.0, 101.0, 99.0, 100.5),  # 22
    (100.5, 102.0, 100.0, 101.8),  # 23 run-up
    (101.8, 103.5, 101.5, 102.5),  # 24 failed break of 103, wick tip 103.5
    (102.5, 102.6, 98.55, 98.6),  # 25 bearish displacement (range 4.05), close above the swing low
    (98.6, 99.5, 97.5, 97.8),  # 26 MSS: close < 98.5; FVG low[24]=101.5 > high[26]=99.5
]


# --- displacement ------------------------------------------------------------------------------

def test_displacement_needs_2x_mean_range_and_direction():
    bear = make_bars(WARM + [(100.0, 100.5, 96.5, 97.0)], START, "5min")  # range 4.0 = 2 x 2.0
    assert displacement(bear, -1)[20]
    assert not displacement(bear, 1)[20]
    small = make_bars(WARM + [(100.0, 100.5, 96.6, 97.0)], START, "5min")  # range 3.9
    assert not displacement(small, -1)[20]
    bull = make_bars(WARM + [(100.0, 104.0, 99.8, 103.5)], START, "5min")  # range 4.2, bullish
    assert displacement(bull, 1)[20]
    assert not displacement(bull, -1)[20]
    # the current bar is not part of its own mean
    two = make_bars(WARM[:19] + [(100.0, 100.5, 96.5, 97.0)] * 2, START, "5min")
    assert not displacement(two, -1)[20]  # previous mean = (19*2 + 4)/20 = 2.1 -> needs 4.2


def test_displacement_false_in_warmup():
    rows = [FLAT] * 5 + [(100.0, 100.5, 80.0, 81.0)] + [FLAT] * 14  # huge bar at 5
    x = displacement(make_bars(rows, START, "5min"), -1)
    assert x.dtype == bool
    assert len(x) == 20
    assert not x.any()


# --- Model 1 -----------------------------------------------------------------------------------

def test_model1_sell_full_sequence():
    sig = model1(bars(FULL), "M5", zones((103, 1)))
    assert list(sig.columns) == LIMIT_SIGNAL_COLS
    assert len(sig) == 1
    r = sig.iloc[0]
    assert r["direction"] == -1
    assert r["setup"] == "m1"
    assert r["entry_tf"] == "M5"
    assert r["trigger_tf"] is None or pd.isna(r["trigger_tf"])
    assert r["signal_time"] == T(26)
    assert r["entry_time"] == T(27)  # close of the MSS bar
    assert r["limit_price"] == pytest.approx(99.5)  # FVG near edge = high of bar 3
    assert r["fvg_far"] == pytest.approx(101.5)
    assert r["sl"] == pytest.approx(103.5)  # wick tip
    assert r["leg_end"] == pytest.approx(97.5)
    assert r["order_end"] == T(39)  # close of i + 12
    assert r["signal_close"] == pytest.approx(97.8)
    assert np.isfinite(r["signal_atr"]) and r["signal_atr"] > 0
    assert bool(r["ote_overlap"])  # band [101.22, 102.24] overlaps [99.5, 101.5]
    assert r["level_kind"] == "pwh"
    assert r["level_price"] == pytest.approx(103.0)
    assert r["ref_time"] == T(20)
    for col in ("target_ob", "target_fvg", "target_owner"):
        assert pd.isna(r[col])


def test_model1_buy_mirror():
    sig = model1(bars(mirror(FULL)), "M5", zones((97, -1)))
    assert len(sig) == 1
    r = sig.iloc[0]
    assert r["direction"] == 1
    assert r["signal_time"] == T(26)
    assert r["entry_time"] == T(27)
    assert r["limit_price"] == pytest.approx(100.5)
    assert r["fvg_far"] == pytest.approx(98.5)
    assert r["sl"] == pytest.approx(96.5)
    assert r["leg_end"] == pytest.approx(102.5)
    assert r["signal_close"] == pytest.approx(102.2)
    assert r["order_end"] == T(39)
    assert bool(r["ote_overlap"])
    assert r["level_price"] == pytest.approx(97.0)


def test_model1_uses_swing_known_at_failed_break():
    rows = FULL[:5] + [
        (102.5, 102.6, 99.8, 100.2),  # 25 later swing low 99.8 (known at close of 27)
        (100.5, 101.5, 100.3, 101.0),  # 26
        (101.0, 101.8, 100.6, 100.6),  # 27
        (100.1, 100.2, 96.0, 98.7),  # 28 bearish displacement, closes < 99.8 but > 98.5; FVG 100.3 > 100.2
    ]
    assert model1(bars(rows), "M5", zones((103, 1))).empty


def test_model1_no_displacement_no_signal():
    rows = FULL[:5] + [
        (102.5, 102.6, 100.6, 100.8),  # 25 range 2
        (99.5, 99.8, 97.5, 97.8),  # 26 closes below 98.5, range 2.3, FVG exists
    ]
    assert model1(bars(rows), "M5", zones((103, 1))).empty


def test_model1_close_above_tip_cancels():
    rows = FULL[:5] + [
        (102.5, 104.0, 102.4, 103.8),  # 25 closes above the tip 103.5 -> cancel
        (101.0, 101.2, 97.0, 97.2),  # 26 would be MSS with displacement and FVG
    ]
    assert model1(bars(rows), "M5", zones((103, 1))).empty


def test_model1_mss_after_k_bars_no_signal():
    filler = (101.5, 102.0, 101.0, 101.5)
    mss = (100.8, 100.9, 96.5, 96.8)  # displacement, FVG low[i-2]=101.0 > 100.9, close < 98.5
    late = FULL[:5] + [filler] * 12 + [mss]  # MSS at bar 37 = j + 13
    assert model1(bars(late), "M5", zones((103, 1))).empty
    edge = FULL[:5] + [filler] * 11 + [mss]  # MSS at bar 36 = j + 12
    sig = model1(bars(edge), "M5", zones((103, 1)))
    assert sig["signal_time"].tolist() == [T(36)]
    assert sig["limit_price"].iloc[0] == pytest.approx(100.9)


def test_model1_no_fvg_no_signal():
    rows = FULL[:6] + [(98.6, 101.6, 97.5, 97.8)]  # 26 high 101.6 >= low[24] 101.5 -> no gap
    assert model1(bars(rows), "M5", zones((103, 1))).empty


def test_model1_no_known_swing_no_signal():
    rows = [FLAT, FLAT] + FULL[2:]  # no strict swing low anywhere before the failed break
    assert model1(bars(rows), "M5", zones((103, 1))).empty
    tiny = make_bars([FLAT] * 3, START, "5min")
    assert model1(tiny, "M5", zones((103, 1))).empty


def test_model1_limit_not_above_close_no_signal():
    rows = FULL[:5] + [
        (98.0, 98.4, 97.0, 97.2),  # 25 closes < 98.5 but no displacement yet; FVG low[23]=100 > 98.4
        (102.0, 102.2, 98.0, 98.45),  # 26 bearish displacement + MSS; close 98.45 >= limit 98.4
    ]
    assert model1(bars(rows), "M5", zones((103, 1))).empty


def test_model1_order_end_at_cancel_close():
    rows = FULL + [FLAT, FLAT, FLAT, (100.0, 104.0, 99.8, 103.8)]  # bar 30 closes above sl 103.5
    sig = model1(bars(rows), "M5", zones((103, 1)))
    assert len(sig) == 1
    assert sig["order_end"].iloc[0] == T(31)


def test_model1_two_zones_same_mss_keep_earliest_valid_from():
    sig = model1(bars(FULL), "M5", zones((103, 1), (103.3, 1, T(10))))
    assert len(sig) == 1
    assert sig["level_price"].iloc[0] == pytest.approx(103.3)
    assert sig["ref_time"].iloc[0] == T(10)


# --- Model 3 -----------------------------------------------------------------------------------

def test_model3_ote_overlap():
    inside = model1(bars(FULL), "M5", zones((103, 1)))
    below_rows = FULL[:5] + [
        (102.5, 102.6, 99.6, 99.8),  # 25
        (99.8, 100.0, 98.6, 98.7),  # 26 closes above S
        (98.7, 98.8, 94.6, 95.2),  # 27 displacement + MSS; FVG [98.8, 99.6]
    ]
    below = model1(bars(below_rows), "M5", zones((103, 1)))
    assert len(below) == 1
    assert below["limit_price"].iloc[0] == pytest.approx(98.8)
    assert not bool(below["ote_overlap"].iloc[0])  # band [100.12, 101.63] lies above the FVG

    m3 = model3(inside)
    assert list(m3.columns) == LIMIT_SIGNAL_COLS
    assert len(m3) == 1
    assert m3["setup"].tolist() == ["m3"]
    assert m3["limit_price"].iloc[0] == pytest.approx(99.5)
    assert inside["setup"].tolist() == ["m1"]  # input not modified
    assert model3(below).empty
