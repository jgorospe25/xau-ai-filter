"""Setups 1 and 3 on synthetic M5 bars with hand-placed levels.

Bars 0..19 are a flat warm-up (range 2 -> ATR14 = 2, tolerance ~0.2). Levels become valid at bar 20
(01:40); the H1 frame gives ATR14(H1) = 2 (merge distance 0.5). Bar i opens at 00:00 + 5 min * i.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from conftest import make_bars

from xauai.setups import live_zones, setup1, setup3
from xauai.sim import SIGNAL_COLS

START = "2024-01-02 00:00"
WARM = [(100.0, 101.0, 99.0, 100.0)] * 20
FLAT = (100.0, 101.0, 99.0, 100.0)
VF = pd.Timestamp("2024-01-02 01:40")
H1 = make_bars([FLAT] * 30, "2024-01-01 00:00", "1h")
D1 = make_bars([FLAT] * 3, "2023-12-30 00:00", "1D")


def T(i: int) -> pd.Timestamp:
    return pd.Timestamp(START) + pd.Timedelta(minutes=5 * i)


def bars(rows, tail=FLAT, n_tail=5, warm=WARM):
    return make_bars(list(warm) + list(rows) + [tail] * n_tail, START, "5min")


def mirror(rows):
    return [(200 - o, 200 - l, 200 - h, 200 - c) for o, h, l, c in rows]


def lv(*kinds_prices, vf=VF, vt=pd.NaT):
    return pd.DataFrame(
        {
            "valid_from": [vf] * len(kinds_prices),
            "valid_to": pd.to_datetime([vt] * len(kinds_prices)),
            "kind": [k for k, _ in kinds_prices],
            "price": [float(p) for _, p in kinds_prices],
        }
    )


# --- Setup 1 -------------------------------------------------------------------------------

S1_BASIC = [
    FLAT,  # 20
    (100.5, 103.6, 100.2, 101.5),  # 21 sweep of 103: opens below, wick above, closes below
    (101.5, 102.0, 101.0, 101.4),  # 22
    (101.4, 103.0, 101.2, 102.0),  # 23 retest: high >= 103 - tol, close < 103
]


def test_setup1_sweep_reclaim_retest_sells_next_open():
    sig = setup1(bars(S1_BASIC), "M5", lv(("pwh", 103)), H1)
    assert list(sig.columns) == SIGNAL_COLS
    assert len(sig) == 1
    r = sig.iloc[0]
    assert r["signal_time"] == T(23)
    assert r["entry_time"] == T(24)
    assert r["direction"] == -1
    assert r["sl"] == pytest.approx(103.6)  # sweep wick tip
    assert r["setup"] == "1"
    assert r["entry_tf"] == "M5"
    assert pd.isna(r["trigger_tf"])
    assert r["level_kind"] == "pwh"
    assert r["level_price"] == pytest.approx(103.0)
    assert r["ref_time"] == VF
    assert np.isnan(r["target_ob"]) and np.isnan(r["target_fvg"]) and np.isnan(r["target_owner"])


def test_setup1_sl_includes_higher_retest_wick():
    rows = S1_BASIC[:3] + [(101.4, 103.8, 101.2, 102.0)]  # retest wick above the sweep tip
    sig = setup1(bars(rows), "M5", lv(("pwh", 103)), H1)
    assert sig["signal_time"].tolist() == [T(23)]
    assert sig["sl"].iloc[0] == pytest.approx(103.8)


def test_setup1_bar_opening_above_level_is_not_a_sweep():
    rows = [FLAT, (103.2, 103.6, 101.0, 101.5)] + S1_BASIC[2:]
    sig = setup1(bars(rows), "M5", lv(("pwh", 103)), H1)
    assert sig.empty


def test_setup1_near_miss_within_tolerance_counts():
    near = [FLAT, (100.5, 102.9, 100.2, 101.5), S1_BASIC[2], (101.4, 102.85, 101.2, 102.0)]
    sig = setup1(bars(near), "M5", lv(("pwh", 103)), H1)
    assert sig["signal_time"].tolist() == [T(23)]
    assert sig["sl"].iloc[0] == pytest.approx(102.9)
    far = [FLAT, (100.5, 102.6, 100.2, 101.5), S1_BASIC[2], (101.4, 102.85, 101.2, 102.0)]
    assert setup1(bars(far), "M5", lv(("pwh", 103)), H1).empty


def test_setup1_invalidated_by_close_above_sweep_tip():
    rows = [
        FLAT,
        (100.5, 103.05, 100.2, 101.5),  # 21 sweep, tip 103.05
        (101.5, 103.1, 101.0, 103.1),  # 22 closes above the tip (but within tolerance of 103)
        (102.0, 103.0, 101.2, 102.0),  # 23 would be the retest
    ]
    b = bars(rows)
    assert setup1(b, "M5", lv(("pwh", 103)), H1).empty
    z = live_zones(lv(("pwh", 103)), b, H1.set_index("time")["close"] * 0 + 2.0)
    assert pd.isna(z.loc[z["price"] == 103.0, "broken_at"].iloc[0])  # invalidation, not a broken zone


def test_setup1_expires_after_12_bars():
    filler = (101.5, 102.0, 101.0, 101.5)
    retest = (101.4, 103.0, 101.2, 102.0)
    late = S1_BASIC[:2] + [filler] * 12 + [retest]  # retest at bar 34 = sweep + 13
    assert setup1(bars(late), "M5", lv(("pwh", 103)), H1).empty
    edge = S1_BASIC[:2] + [filler] * 11 + [retest]  # retest at bar 33 = sweep + 12
    sig = setup1(bars(edge), "M5", lv(("pwh", 103)), H1)
    assert sig["signal_time"].tolist() == [T(33)]
    assert sig["entry_time"].tolist() == [T(34)]


def test_setup1_new_sweep_resets_pending():
    rows = [
        FLAT,
        (100.5, 102.95, 100.2, 101.5),  # 21 near-miss sweep, tip 102.95
        (101.5, 102.99, 100.4, 102.98),  # 22 closes above tip -> invalidated, and is itself a sweep
        (101.4, 102.9, 101.2, 102.0),  # 23 retest of the new sweep
    ]
    sig = setup1(bars(rows), "M5", lv(("pwh", 103)), H1)
    assert sig["signal_time"].tolist() == [T(23)]
    assert sig["sl"].iloc[0] == pytest.approx(102.99)


def test_zone_stays_live_after_sweep_but_dies_when_broken():
    rows = S1_BASIC + [
        (102.0, 102.2, 101.0, 101.5),  # 24
        (101.5, 102.0, 101.0, 101.4),  # 25
        (101.4, 103.4, 101.0, 101.6),  # 26 second sweep of the same zone
        (101.6, 102.0, 101.0, 101.4),  # 27
        (101.4, 102.9, 101.2, 102.0),  # 28 retest -> second signal
        (102.0, 102.2, 101.0, 101.5),  # 29
        (101.5, 103.8, 101.4, 103.6),  # 30 closes above 103 + tol -> zone broken
        (103.6, 103.7, 102.0, 102.2),  # 31
        (102.2, 103.5, 102.0, 102.3),  # 32 would be a sweep
        (102.3, 102.5, 102.0, 102.2),  # 33
        (102.2, 103.0, 102.0, 102.4),  # 34 would be a retest
    ]
    b = bars(rows)
    sig = setup1(b, "M5", lv(("pwh", 103)), H1)
    assert sig["signal_time"].tolist() == [T(23), T(28)]
    assert sig["sl"].tolist() == pytest.approx([103.6, 103.4])
    z = live_zones(lv(("pwh", 103)), b, H1.set_index("time")["close"] * 0 + 2.0)
    assert list(z.columns) == ["zone_id", "side", "price", "kinds", "valid_from", "valid_to", "broken_at"]
    assert z.loc[z["price"] == 103.0, "broken_at"].iloc[0] == T(31)  # close of bar 30


def test_setup1_one_signal_per_bar_keeps_nearer_sl():
    rows = [
        FLAT,
        (103.1, 104.1, 102.9, 103.1),  # 21 sweeps zone 104 (opens above 103: no sweep of 103)
        (102.9, 103.3, 101.8, 102.5),  # 22 sweeps zone 103
        (102.5, 104.0, 102.2, 102.6),  # 23 retests both
    ]
    sig = setup1(bars(rows), "M5", lv(("pwh", 103), ("pdh", 104)), H1)
    assert sig["signal_time"].tolist() == [T(23)]
    assert sig["level_price"].iloc[0] == pytest.approx(103.0)
    assert sig["sl"].iloc[0] == pytest.approx(104.0)


def test_setup1_buy_mirror():
    sig = setup1(bars(mirror(S1_BASIC)), "M5", lv(("pwl", 97)), H1)
    assert sig["signal_time"].tolist() == [T(23)]
    assert sig["entry_time"].tolist() == [T(24)]
    assert sig["direction"].tolist() == [1]
    assert sig["sl"].iloc[0] == pytest.approx(96.4)
    assert sig["level_kind"].iloc[0] == "pwl"
    assert sig["level_price"].iloc[0] == pytest.approx(97.0)


# --- Setup 3 -------------------------------------------------------------------------------

S3_HEAD = [
    (100.0, 101.0, 99.0, 100.5),  # 20
    (100.5, 103.0, 100.3, 102.0),  # 21 touch of 103 (close <= 103)
    (102.0, 102.5, 101.0, 101.5),  # 22
    (101.5, 102.0, 100.5, 101.0),  # 23
    (101.0, 101.5, 99.8, 100.2),  # 24 swing low 99.8 (known at close of 26)
    (100.2, 101.8, 100.0, 101.6),  # 25
    (101.6, 102.6, 101.2, 102.2),  # 26 lower high 102.6 (known at close of 28)
]
S3_BASIC = S3_HEAD + [
    (102.2, 102.4, 101.0, 101.2),  # 27
    (101.2, 101.5, 100.4, 100.6),  # 28
    (100.6, 100.8, 99.2, 99.5),  # 29 break: close < 99.8
    (99.5, 99.7, 99.0, 99.3),  # 30 retest of 99.8
]
S3_TAIL = (99.3, 99.5, 98.9, 99.2)


def test_setup3_lower_high_break_retest_sell_target_pdl():
    sig = setup3(bars(S3_BASIC, tail=S3_TAIL), "M5", lv(("pwh", 103), ("pdl", 95)), H1, D1)
    assert list(sig.columns) == SIGNAL_COLS
    assert len(sig) == 1
    r = sig.iloc[0]
    assert r["signal_time"] == T(30)
    assert r["entry_time"] == T(31)
    assert r["direction"] == -1
    assert r["sl"] == pytest.approx(102.6)  # the lower high
    assert r["setup"] == "3"
    assert r["level_kind"] == "pwh"
    assert r["level_price"] == pytest.approx(103.0)
    assert r["ref_time"] == VF
    assert r["target_owner"] == pytest.approx(95.0)
    assert np.isnan(r["target_ob"]) and np.isnan(r["target_fvg"])


def test_setup3_target_is_unswept_swing_low_nearest_pdl():
    warm = list(WARM)
    warm[3] = (100.0, 101.0, 96.0, 100.0)  # swing low 96, later swept by bar 8
    warm[8] = (100.0, 101.0, 94.5, 100.0)  # swing low 94.5: below the PDL, ignored
    warm[13] = (100.0, 101.0, 97.0, 100.0)  # unswept swing low 97 (nearest the PDL)
    warm[16] = (100.0, 101.0, 98.0, 100.0)  # unswept swing low 98 (nearer the close, not used)
    sig = setup3(bars(S3_BASIC, tail=S3_TAIL, warm=warm), "M5", lv(("pwh", 103), ("pdl", 95)), H1, D1)
    assert sig["signal_time"].tolist() == [T(30)]
    assert sig["target_owner"].iloc[0] == pytest.approx(97.0)


def test_setup3_swept_swing_low_is_ignored_target_pdl():
    warm = list(WARM)
    warm[3] = (100.0, 101.0, 97.0, 100.0)  # swing low 97 ...
    warm[8] = (100.0, 101.0, 96.5, 100.0)  # ... swept by swing low 96.5, itself swept by bar 13
    warm[13] = (100.0, 101.0, 94.0, 100.0)  # swing low below the PDL
    sig = setup3(bars(S3_BASIC, tail=S3_TAIL, warm=warm), "M5", lv(("pwh", 103), ("pdl", 95)), H1, D1)
    assert sig["signal_time"].tolist() == [T(30)]
    assert sig["target_owner"].iloc[0] == pytest.approx(95.0)


def test_setup3_buy_target_is_unswept_swing_high_nearest_pdh():
    warm = list(WARM)
    warm[3] = (100.0, 104.0, 99.0, 100.0)  # swing high 104, swept by bar 8
    warm[8] = (100.0, 105.5, 99.0, 100.0)  # above the PDH, ignored
    warm[13] = (100.0, 103.0, 99.0, 100.0)  # unswept, nearest the PDH
    warm[16] = (100.0, 102.0, 99.0, 100.0)  # unswept, nearer the close
    rows = mirror(S3_BASIC)
    sig = setup3(bars(rows, tail=mirror([S3_TAIL])[0], warm=warm), "M5", lv(("pwl", 97), ("pdh", 105)), H1, D1)
    assert sig["signal_time"].tolist() == [T(30)]
    assert sig["direction"].tolist() == [1]
    assert sig["target_owner"].iloc[0] == pytest.approx(103.0)


def test_setup3_no_owner_target_when_below_pdl():
    sig = setup3(bars(S3_BASIC, tail=S3_TAIL), "M5", lv(("pwh", 103), ("pdl", 99.4)), H1, D1)
    assert sig["signal_time"].tolist() == [T(30)]
    assert sig["direction"].tolist() == [-1]
    assert np.isnan(sig["target_owner"].iloc[0])


def test_setup3_swing_used_only_after_confirmation():
    rows = S3_HEAD + [
        (102.2, 102.4, 99.5, 99.6),  # 27 closes below 99.8, but the lower high is not known yet
        (99.6, 99.7, 99.3, 99.5),  # 28 would be the retest if bar 27 counted as the break
        (99.5, 103.0, 99.4, 102.8),  # 29 closes above the lower high -> invalidated
    ]
    sig = setup3(bars(rows, tail=(102.8, 102.9, 102.0, 102.5)), "M5", lv(("pwh", 103), ("pdl", 95)), H1, D1)
    assert sig.empty


def test_setup3_buy_mirror():
    sig = setup3(bars(mirror(S3_BASIC), tail=mirror([S3_TAIL])[0]), "M5", lv(("pwl", 97), ("pdh", 105)), H1, D1)
    assert sig["signal_time"].tolist() == [T(30)]
    assert sig["entry_time"].tolist() == [T(31)]
    assert sig["direction"].tolist() == [1]
    assert sig["sl"].iloc[0] == pytest.approx(97.4)
    assert sig["level_kind"].iloc[0] == "pwl"
    assert sig["target_owner"].iloc[0] == pytest.approx(105.0)


# --- start of data -------------------------------------------------------------------------


def test_no_signal_without_levels_at_start_of_data():
    late = lv(("pwh", 103), ("pdl", 95), vf=T(32))  # levels only appear after both patterns
    assert setup1(bars(S1_BASIC), "M5", late, H1).empty
    assert setup3(bars(S3_BASIC, tail=S3_TAIL), "M5", late, H1, D1).empty
    h1_short = make_bars([FLAT] * 5, "2024-01-01 21:00", "1h")  # ATR14(H1) NaN at the refresh
    assert setup1(bars(S1_BASIC), "M5", lv(("pwh", 103)), h1_short).empty
    assert setup3(bars(S3_BASIC, tail=S3_TAIL), "M5", lv(("pwh", 103), ("pdl", 95)), h1_short, D1).empty
