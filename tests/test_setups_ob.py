"""Setups 2a (rejection at a level, OB/FVG targets) and 2b (pullback from an H1 OB).

2a: trigger bars (M30 or H1) open at 2024-01-02 00:00; bars 0..19 are a flat warm-up (range 2 ->
ATR14 = 2, tolerance 0.2); the level becomes valid at bar 20. M5 bars cover the whole day.
2b: M5 bars from 2024-01-02 00:00 (bar i opens at 00:00 + 5 min * i); warm-up at 102 above a
bullish OB 98..100 known at bar 20 (01:40). ATR14(H1) = 2, so the episode reset is a close > 102.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from conftest import make_bars

from xauai.setups import _h1_atr_series, live_zones, setup1, setup3
from xauai.setups_ob import setup2a, setup2b
from xauai.sim import SIGNAL_COLS

DAY = "2024-01-02 00:00"
FLAT = (100.0, 101.0, 99.0, 100.0)
H1 = make_bars([FLAT] * 30, "2024-01-01 00:00", "1h")  # ATR14(H1) = 2
M5 = make_bars([FLAT] * 400, DAY, "5min")
NAT = pd.NaT


def ts(hhmm: str) -> pd.Timestamp:
    return pd.Timestamp(f"2024-01-02 {hhmm}")


def mirror(rows):
    return [(200 - o, 200 - l, 200 - h, 200 - c) for o, h, l, c in rows]


def lv(*kinds_prices, vf):
    return pd.DataFrame(
        {
            "valid_from": [vf] * len(kinds_prices),
            "valid_to": pd.to_datetime([NAT] * len(kinds_prices)),
            "kind": [k for k, _ in kinds_prices],
            "price": [float(p) for _, p in kinds_prices],
        }
    )


def obs_frame(rows):
    """rows: (direction, top, bottom, known_at, invalid_at)."""
    return pd.DataFrame(
        {
            "ob_id": np.arange(len(rows)),
            "direction": [r[0] for r in rows],
            "top": [float(r[1]) for r in rows],
            "bottom": [float(r[2]) for r in rows],
            "ob_time": pd.to_datetime([pd.Timestamp(r[3]) - pd.Timedelta(hours=3) for r in rows]),
            "known_at": pd.to_datetime([r[3] for r in rows]),
            "invalid_at": pd.to_datetime(pd.Series([r[4] for r in rows], dtype="datetime64[ns]")),
        }
    )


def gaps_frame(rows):
    """rows: (direction, top, bottom, known_at, filled_at); mid = (top + bottom) / 2."""
    return pd.DataFrame(
        {
            "direction": [r[0] for r in rows],
            "top": [float(r[1]) for r in rows],
            "bottom": [float(r[2]) for r in rows],
            "mid": [(r[1] + r[2]) / 2 for r in rows],
            "known_at": pd.to_datetime([r[3] for r in rows]),
            "filled_at": pd.to_datetime(pd.Series([r[4] for r in rows], dtype="datetime64[ns]")),
        }
    )


def mirror_obs(rows):
    return [(-d, 200 - b, 200 - t, k, i) for d, t, b, k, i in rows]


EMPTY_OBS = pd.DataFrame(columns=["ob_id", "direction", "top", "bottom", "ob_time", "known_at", "invalid_at"])
EMPTY_GAPS = pd.DataFrame(columns=["direction", "top", "bottom", "mid", "known_at", "filled_at"])

# --- Setup 2a -------------------------------------------------------------------------------

WARM30 = [FLAT] * 20
REJECT = (100.5, 103.5, 100.2, 101.0)  # opens below 103, high above, closes below; wick 2.5 of 3.3


def trig(rows, freq="30min", n_tail=5):
    return make_bars(WARM30 + [FLAT] + list(rows) + [FLAT] * n_tail, DAY, freq)  # bar 21 = rows[0]


VF30 = ts("10:00")  # trigger bar 20 (M30)


def test_2a_rejection_needs_half_range_upper_wick():
    lvl = lv(("pwh", 103), vf=VF30)
    sig = setup2a(trig([REJECT]), "M30", M5, lvl, EMPTY_OBS, EMPTY_GAPS, H1)
    assert list(sig.columns) == SIGNAL_COLS
    assert len(sig) == 1
    r = sig.iloc[0]
    assert r["signal_time"] == ts("10:30")
    assert r["direction"] == -1
    assert r["sl"] == pytest.approx(103.5)  # rejection wick tip
    assert r["setup"] == "2a"
    assert r["entry_tf"] == "M5"
    assert r["trigger_tf"] == "M30"
    assert r["level_kind"] == "pwh"
    assert r["level_price"] == pytest.approx(103.0)
    assert r["ref_time"] == VF30
    assert np.isnan(r["target_ob"]) and np.isnan(r["target_fvg"]) and np.isnan(r["target_owner"])
    small = (100.5, 103.5, 100.2, 102.0)  # wick 1.5 < 0.5 x 3.3
    assert setup2a(trig([small]), "M30", M5, lvl, EMPTY_OBS, EMPTY_GAPS, H1).empty
    edge = (100.5, 103.5, 100.5, 102.0)  # wick 1.5 == 0.5 x 3.0
    assert setup2a(trig([edge]), "M30", M5, lvl, EMPTY_OBS, EMPTY_GAPS, H1)["signal_time"].tolist() == [ts("10:30")]
    above = (103.1, 103.5, 100.2, 101.0)  # opens above the level: no rejection
    assert setup2a(trig([above]), "M30", M5, lvl, EMPTY_OBS, EMPTY_GAPS, H1).empty
    far = (100.5, 102.7, 100.2, 101.0)  # high 0.3 short of the level (> tolerance 0.2)
    assert setup2a(trig([far]), "M30", M5, lvl, EMPTY_OBS, EMPTY_GAPS, H1).empty
    near = (100.5, 102.85, 100.2, 101.0)  # within tolerance
    assert setup2a(trig([near]), "M30", M5, lvl, EMPTY_OBS, EMPTY_GAPS, H1)["sl"].tolist() == pytest.approx([102.85])


def test_2a_entry_is_first_m5_after_trigger_close():
    lvl = lv(("pwh", 103), vf=VF30)
    sig = setup2a(trig([REJECT]), "M30", M5, lvl, EMPTY_OBS, EMPTY_GAPS, H1)
    assert sig["entry_time"].tolist() == [ts("11:00")]
    gap = M5[~M5["time"].isin([ts("11:00"), ts("11:05")])].reset_index(drop=True)
    sig = setup2a(trig([REJECT]), "M30", gap, lvl, EMPTY_OBS, EMPTY_GAPS, H1)
    assert sig["entry_time"].tolist() == [ts("11:10")]
    short = M5[M5["time"] < ts("11:00")].reset_index(drop=True)
    assert setup2a(trig([REJECT]), "M30", short, lvl, EMPTY_OBS, EMPTY_GAPS, H1).empty
    # H1 trigger: bar 21 opens 21:00, closes 22:00
    sig = setup2a(trig([REJECT], "1h"), "H1", M5, lv(("pwh", 103), vf=ts("20:00")), EMPTY_OBS, EMPTY_GAPS, H1)
    assert sig["signal_time"].tolist() == [ts("21:00")]
    assert sig["entry_time"].tolist() == [ts("22:00")]
    assert sig["trigger_tf"].tolist() == ["H1"]


OBS_2A = [
    (1, 99.5, 98.5, ts("08:00"), NAT),  # valid, below the close 101.0
    (1, 98.0, 97.0, ts("06:00"), NAT),  # valid, farther
    (1, 101.5, 100.8, ts("07:00"), NAT),  # top above the close
    (-1, 100.9, 100.4, ts("07:00"), NAT),  # bearish: wrong kind
    (1, 100.4, 99.9, ts("12:00"), NAT),  # known after entry
    (1, 100.2, 99.6, ts("11:00"), NAT),  # known exactly at entry -> valid, nearest
]
GAPS_2A = [
    (1, 100.5, 99.5, ts("09:00"), ts("10:55")),  # mid 100.0, filled before entry
    (1, 100.3, 99.3, ts("09:00"), ts("11:30")),  # mid 99.8, filled after entry -> valid, nearest
    (1, 99.5, 98.5, ts("05:00"), NAT),  # mid 99.0, valid, farther
    (1, 102.0, 101.0, ts("05:00"), NAT),  # mid 101.5 above the close
    (-1, 100.8, 100.2, ts("05:00"), NAT),  # bearish: wrong kind
]


def test_2a_targets_ob_top_and_fvg_mid():
    lvl = lv(("pwh", 103), vf=VF30)
    sig = setup2a(trig([REJECT]), "M30", M5, lvl, obs_frame(OBS_2A), gaps_frame(GAPS_2A), H1)
    assert sig["signal_time"].tolist() == [ts("10:30")]
    assert sig["target_ob"].iloc[0] == pytest.approx(100.2)
    assert sig["target_fvg"].iloc[0] == pytest.approx(99.8)


def test_2a_ignores_ob_invalidated_before_entry():
    lvl = lv(("pwh", 103), vf=VF30)
    dead = [(1, 100.6, 100.1, ts("08:00"), ts("10:50")), (1, 99.5, 98.5, ts("08:00"), NAT)]
    sig = setup2a(trig([REJECT]), "M30", M5, lvl, obs_frame(dead), EMPTY_GAPS, H1)
    assert sig["target_ob"].tolist() == pytest.approx([99.5])
    at_entry = [(1, 100.6, 100.1, ts("08:00"), ts("11:00")), (1, 99.5, 98.5, ts("08:00"), NAT)]
    sig = setup2a(trig([REJECT]), "M30", M5, lvl, obs_frame(at_entry), EMPTY_GAPS, H1)
    assert sig["target_ob"].tolist() == pytest.approx([99.5])
    later = [(1, 100.6, 100.1, ts("08:00"), ts("11:05")), (1, 99.5, 98.5, ts("08:00"), NAT)]
    sig = setup2a(trig([REJECT]), "M30", M5, lvl, obs_frame(later), EMPTY_GAPS, H1)
    assert sig["target_ob"].tolist() == pytest.approx([100.6])


def test_2a_buy_mirror():
    lvl = lv(("pwl", 97), vf=VF30)
    gaps = [(-d, 200 - b, 200 - t, k, f) for d, t, b, k, f in GAPS_2A]
    sig = setup2a(trig(mirror([REJECT])), "M30", M5, lvl, obs_frame(mirror_obs(OBS_2A)), gaps_frame(gaps), H1)
    assert sig["signal_time"].tolist() == [ts("10:30")]
    assert sig["entry_time"].tolist() == [ts("11:00")]
    assert sig["direction"].tolist() == [1]
    assert sig["sl"].iloc[0] == pytest.approx(96.5)
    assert sig["level_kind"].iloc[0] == "pwl"
    assert sig["level_price"].iloc[0] == pytest.approx(97.0)
    assert sig["target_ob"].iloc[0] == pytest.approx(99.8)  # bottom of the nearest bearish OB above
    assert sig["target_fvg"].iloc[0] == pytest.approx(100.2)
    small = mirror([(100.5, 103.5, 100.2, 102.0)])
    assert setup2a(trig(small), "M30", M5, lvl, EMPTY_OBS, EMPTY_GAPS, H1).empty


def test_2a_buy_ignores_ob_invalidated_before_entry():
    lvl = lv(("pwl", 97), vf=VF30)
    dead = mirror_obs([(1, 100.6, 100.1, ts("08:00"), ts("10:50")), (1, 99.5, 98.5, ts("08:00"), NAT)])
    sig = setup2a(trig(mirror([REJECT])), "M30", M5, lvl, obs_frame(dead), EMPTY_GAPS, H1)
    assert sig["target_ob"].tolist() == pytest.approx([100.5])


def test_2a_precomputed_zones_match():
    lvl = lv(("pwh", 103), vf=VF30)
    b = trig([REJECT])
    z = live_zones(lvl, b, _h1_atr_series(H1), "M30")
    a = setup2a(b, "M30", M5, lvl, obs_frame(OBS_2A), gaps_frame(GAPS_2A), H1)
    c = setup2a(b, "M30", M5, lvl, obs_frame(OBS_2A), gaps_frame(GAPS_2A), H1, zones=z)
    pd.testing.assert_frame_equal(a, c)
    assert len(a) == 1


# --- Setup 2b -------------------------------------------------------------------------------

WARM5 = [(102.0, 103.0, 101.0, 102.0)] * 20
OB_KNOWN = ts("01:40")  # = time of bar 20
B2_BASIC = [
    (102.0, 102.5, 101.5, 102.0),  # 20 first bar at known_at: close above the OB top (approach)
    (102.0, 102.2, 100.5, 100.8),  # 21
    (100.8, 101.0, 99.5, 99.8),  # 22 low <= 100: episode start
    (99.8, 100.6, 99.4, 100.4),  # 23
    (100.4, 101.2, 100.0, 100.6),  # 24 swing high 101.2 (known at close of 26)
    (100.6, 100.8, 99.0, 99.3),  # 25
    (99.3, 99.9, 98.6, 99.5),  # 26 lowest low 98.6
    (99.5, 100.5, 99.2, 100.3),  # 27
    (100.3, 101.6, 100.2, 101.4),  # 28 break: close > 101.2
    (101.4, 101.9, 101.5, 101.8),  # 29 low 101.5 > 101.2 + tol: no retest
    (101.8, 101.9, 101.15, 101.6),  # 30 retest: low <= 101.2 + tol, close > 101.2
]
TAIL5 = (101.6, 101.9, 101.3, 101.6)


def b5(rows, n_tail=5):
    return make_bars(WARM5 + list(rows) + [TAIL5] * n_tail, DAY, "5min")


def T(i: int) -> pd.Timestamp:
    return pd.Timestamp(DAY) + pd.Timedelta(minutes=5 * i)


def ob1(known=OB_KNOWN, inv=NAT):
    return obs_frame([(1, 100.0, 98.0, known, inv)])


def mob1(known=OB_KNOWN, inv=NAT):
    return obs_frame(mirror_obs([(1, 100.0, 98.0, known, inv)]))


def test_2b_break_retest_buy_sl_lowest_low():
    sig = setup2b(b5(B2_BASIC), "M5", ob1(), H1)
    assert list(sig.columns) == SIGNAL_COLS
    assert len(sig) == 1
    r = sig.iloc[0]
    assert r["signal_time"] == T(30)
    assert r["entry_time"] == T(31)
    assert r["direction"] == 1
    assert r["sl"] == pytest.approx(98.6)
    assert r["setup"] == "2b"
    assert r["entry_tf"] == "M5"
    assert pd.isna(r["trigger_tf"])
    assert r["level_kind"] == "ob"
    assert r["level_price"] == pytest.approx(100.0)
    assert r["ref_time"] == OB_KNOWN
    assert np.isnan(r["target_ob"]) and np.isnan(r["target_fvg"]) and np.isnan(r["target_owner"])


def test_2b_sell_mirror():
    sig = setup2b(b5(mirror(B2_BASIC)), "M5", mob1(), H1)
    assert sig["signal_time"].tolist() == [T(30)]
    assert sig["entry_time"].tolist() == [T(31)]
    assert sig["direction"].tolist() == [-1]
    assert sig["sl"].iloc[0] == pytest.approx(101.4)
    assert sig["level_price"].iloc[0] == pytest.approx(100.0)  # bearish OB bottom
    assert sig["ref_time"].iloc[0] == OB_KNOWN


def test_2b_requires_approach_from_above():
    rows = [(99.5, 100.0, 99.0, 99.6)] + B2_BASIC[1:]  # bar 20 closes below the OB top
    assert setup2b(b5(rows), "M5", ob1(), H1).empty
    assert setup2b(b5(mirror(rows)), "M5", mob1(), H1).empty
    # the approach is read at the first bar at/after known_at: known at bar 21 (close 100.8 > 100) still trades
    assert setup2b(b5(rows), "M5", ob1(known=T(21)), H1)["signal_time"].tolist() == [T(30)]


def test_2b_sl_window_starts_after_known_at():
    rows = list(B2_BASIC)
    rows[2] = (100.8, 101.0, 98.3, 99.8)  # bar 22 low 98.3
    assert setup2b(b5(rows), "M5", ob1(), H1)["sl"].tolist() == pytest.approx([98.3])
    # OB known at bar 23: bar 22 is not part of the episode (start = bar 23, low 99.4)
    sig = setup2b(b5(rows), "M5", ob1(known=T(23)), H1)
    assert sig["signal_time"].tolist() == [T(30)]
    assert sig["sl"].tolist() == pytest.approx([98.6])
    assert setup2b(b5(mirror(rows)), "M5", mob1(known=T(23)), H1)["sl"].tolist() == pytest.approx([101.4])


B2_RESET = [
    (102.0, 102.5, 101.5, 102.0),  # 20 approach
    (102.0, 102.2, 100.5, 100.8),  # 21
    (100.8, 101.0, 98.2, 99.8),  # 22 episode 1 starts, low 98.2
    (99.8, 102.5, 99.6, 102.3),  # 23 closes > 100 + 1 x ATR14(H1) = 102: episode ends
    (102.3, 102.6, 101.8, 102.0),  # 24 swing high 102.6 (before episode 2: not used)
    (102.0, 102.1, 99.8, 100.0),  # 25 low <= 100: episode 2 starts
    (100.0, 100.5, 99.0, 100.2),  # 26
    (100.2, 100.6, 99.3, 100.4),  # 27
    (100.4, 101.2, 100.1, 100.7),  # 28 swing high 101.2 (known at close of 30)
    (100.7, 100.9, 98.9, 99.2),  # 29
    (99.2, 100.0, 98.8, 99.6),  # 30 lowest low of episode 2
    (99.6, 100.5, 99.4, 100.4),  # 31
    (100.4, 101.6, 100.3, 101.4),  # 32 break of 101.2
    (101.4, 101.9, 101.5, 101.8),  # 33
    (101.8, 101.9, 101.15, 101.6),  # 34 retest
]


def test_2b_episode_resets_after_leaving_zone():
    sig = setup2b(b5(B2_RESET), "M5", ob1(), H1)
    assert sig["signal_time"].tolist() == [T(34)]
    assert sig["entry_time"].tolist() == [T(35)]
    assert sig["sl"].tolist() == pytest.approx([98.8])  # not 98.2 from episode 1
    m = setup2b(b5(mirror(B2_RESET)), "M5", mob1(), H1)
    assert m["signal_time"].tolist() == [T(34)]
    assert m["direction"].tolist() == [-1]
    assert m["sl"].tolist() == pytest.approx([101.2])


def test_2b_close_at_or_below_reset_keeps_episode():
    rows = list(B2_RESET)
    rows[3] = (99.8, 102.5, 99.6, 101.9)  # bar 23 closes 101.9 <= 102: episode 1 continues
    sig = setup2b(b5(rows), "M5", ob1(), H1)
    assert sig["signal_time"].tolist() == [T(34)]
    assert sig["sl"].tolist() == pytest.approx([98.2])  # SL window still from bar 22
    m = setup2b(b5(mirror(rows)), "M5", mob1(), H1)
    assert m["sl"].tolist() == pytest.approx([101.8])


def test_2b_one_signal_per_ob():
    rows = B2_BASIC + [(101.6, 102.6, 101.5, 102.4)] + B2_RESET[5:]  # reset at 31, replay 32..41
    sig = setup2b(b5(rows), "M5", ob1(), H1)
    assert sig["signal_time"].tolist() == [T(30)]
    two = obs_frame([(1, 100.0, 98.0, OB_KNOWN, NAT), (1, 100.0, 98.0, T(31), NAT)])
    sig = setup2b(b5(rows), "M5", two, H1)  # a second OB can trade the replay
    assert sig["signal_time"].tolist() == [T(30), T(41)]
    assert sig["ref_time"].tolist() == [OB_KNOWN, T(31)]
    assert sig["sl"].tolist() == pytest.approx([98.6, 98.8])
    m = setup2b(b5(mirror(rows)), "M5", mob1(), H1)
    assert m["signal_time"].tolist() == [T(30)]


def test_2b_invalidated_by_h1_close_below_ob():
    assert setup2b(b5(B2_BASIC), "M5", ob1(inv=ts("02:00")), H1).empty  # H1 close before the retest
    assert setup2b(b5(B2_BASIC), "M5", ob1(inv=ts("02:35")), H1).empty  # at the retest bar's close
    sig = setup2b(b5(B2_BASIC), "M5", ob1(inv=ts("03:00")), H1)
    assert sig["signal_time"].tolist() == [T(30)]
    assert setup2b(b5(mirror(B2_BASIC)), "M5", mob1(inv=ts("02:00")), H1).empty
    assert setup2b(b5(mirror(B2_BASIC)), "M5", mob1(inv=ts("03:00")), H1)["signal_time"].tolist() == [T(30)]


def test_2b_retest_expires_after_k_bars():
    hold = (101.4, 101.9, 101.5, 101.8)
    retest = (101.8, 101.9, 101.15, 101.6)
    edge = B2_BASIC[:9] + [hold] * 11 + [retest]  # break at 28, retest at 40 = b + 12
    assert setup2b(b5(edge), "M5", ob1(), H1)["signal_time"].tolist() == [T(40)]
    late = B2_BASIC[:9] + [hold] * 12 + [retest]  # retest at 41 = b + 13
    assert setup2b(b5(late), "M5", ob1(), H1).empty  # the broken swing is used up; no newer one
    assert setup2b(b5(mirror(late)), "M5", mob1(), H1).empty
    assert setup2b(b5(mirror(edge)), "M5", mob1(), H1)["signal_time"].tolist() == [T(40)]


def test_2b_empty_obs():
    sig = setup2b(b5(B2_BASIC), "M5", EMPTY_OBS, H1)
    assert sig.empty
    assert list(sig.columns) == SIGNAL_COLS


# --- precomputed zones for setups 1 and 3 -----------------------------------------------------


def test_setup1_setup3_precomputed_zones_match():
    from test_setups import D1, S1_BASIC, S3_BASIC, S3_TAIL, VF, bars
    from test_setups import H1 as H1S
    from test_setups import lv as lv_s

    b = bars(S1_BASIC)
    lvl = lv_s(("pwh", 103))
    z = live_zones(lvl, b, _h1_atr_series(H1S), "M5")
    a1 = setup1(b, "M5", lvl, H1S)
    pd.testing.assert_frame_equal(a1, setup1(b, "M5", lvl, H1S, zones=z))
    assert len(a1) == 1
    b3 = bars(S3_BASIC, tail=S3_TAIL)
    lvl3 = lv_s(("pwh", 103), ("pdl", 95))
    z3 = live_zones(lvl3, b3, _h1_atr_series(H1S), "M5")
    a3 = setup3(b3, "M5", lvl3, H1S, D1)
    pd.testing.assert_frame_equal(a3, setup3(b3, "M5", lvl3, H1S, D1, zones=z3))
    assert a3["ref_time"].tolist() == [VF]
