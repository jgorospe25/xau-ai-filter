"""Generalised M1 simulator: labels.py hooks (stop_open, no timeout, wall-clock gaps) and sim.py."""

import numpy as np
import pandas as pd
import pytest
from conftest import make_bars

from xauai.labels import CostParams, SwapParams, gap_after_mask, simulate_trade
from xauai.sim import SIGNAL_COLS, one_position, simulate_signals
from xauai.timeutil import session_windows

NO_COST = CostParams(spread_floor=0.0, slippage=0.0, point=0.01)
NO_SWAP = SwapParams(long_points=0.0, short_points=0.0, point=0.01, triple_weekday=2)
WINDOWS = session_windows("2024-01-01", "2024-01-31")
FLAT = (2000.0, 2000.5, 1999.5, 2000.0)
MON = "2024-01-08 10:00"  # Monday; London session 10:00-19:00 server (winter)


def sig_frame(entry_time, d=1, sl=1995.0, **kw):
    row = dict.fromkeys(SIGNAL_COLS, np.nan)
    row.update(
        signal_time=pd.Timestamp(entry_time) - pd.Timedelta(minutes=30), entry_time=pd.Timestamp(entry_time),
        direction=d, sl=sl, setup="t", entry_tf="M30", trigger_tf="M30", level_kind="pdh",
        level_price=2000.0, ref_time=pd.Timestamp(entry_time) - pd.Timedelta(hours=1),
    )
    row.update(kw)
    return pd.DataFrame([row], columns=SIGNAL_COLS)


def run(rows, sig, cost=NO_COST, tp_mode="rr2.0", session_exit=False, start=MON, levels=None, m1=None):
    m1 = make_bars(rows, start, "min") if m1 is None else m1
    trades, skipped = simulate_signals(sig, m1, cost, NO_SWAP, tp_mode, session_exit, WINDOWS, levels)
    return trades, skipped


def arrays(rows):
    df = make_bars(rows, MON, "min")
    return [df[k].to_numpy(float) for k in ("open", "high", "low", "close")]


def test_session_end_exit_at_open():
    o, h, l, c = arrays([FLAT] * 10)
    no = np.zeros(10, bool)
    res = simulate_trade(o, h, l, c, no, no, 0, 1, 2000.0, 1990.0, 2020.0, None, stop_open=4)
    assert res == (4, 2000.0, "session_end", "open")
    # end to end: London session ends 19:00 server
    rows = [FLAT] * 600
    t, _ = run(rows, sig_frame("2024-01-08 10:30"), session_exit=True)
    assert t.iloc[0].exit_reason == "session_end" and t.iloc[0].exit_time == pd.Timestamp("2024-01-08 19:00")
    t, _ = run(rows, sig_frame("2024-01-08 10:30"), session_exit=False)
    assert t.iloc[0].exit_reason == "end_of_data"


def test_no_timeout_holds_until_friday():
    n = 3000
    o, h, l, c = arrays([FLAT] * n)
    friday = np.zeros(n, bool)
    friday[2500:] = True
    res = simulate_trade(o, h, l, c, friday, np.zeros(n, bool), 0, 1, 2000.0, 1990.0, 2020.0, None)
    assert res[0] == 2500 and res[2] == "friday"


def test_gap_mask_uses_wall_clock_on_m1():
    t = pd.Series(pd.to_datetime([
        "2024-01-08 22:00", "2024-01-08 22:30",  # 30 minute hole
        "2024-01-08 23:00", "2024-01-09 01:00",  # missing 00:00 bar (2 h)
        "2024-01-09 10:00", "2024-01-10 12:00",  # 26 h holiday hole after 01-09 10:00
        "2024-01-10 12:01",
    ]))
    assert gap_after_mask(t).tolist() == [False, False, False, False, True, False, False]


def test_entry_cost_from_m1_spread():
    m1 = make_bars([FLAT] * 60, MON, "min")
    m1.loc[30, "spread"] = 100  # 1.00 price units at point 0.01 -> cost 1.00 + 0.05
    cost = CostParams()
    t, _ = run(None, sig_frame("2024-01-08 10:30"), cost=cost, m1=m1)
    assert t.iloc[0].cost == pytest.approx(1.05) and t.iloc[0].entry == pytest.approx(2001.05)
    t, _ = run(None, sig_frame("2024-01-08 10:30", d=-1, sl=2005.0), cost=cost, m1=m1)
    assert t.iloc[0].entry == pytest.approx(1998.95)
    t, _ = run(None, sig_frame("2024-01-08 10:10"), cost=cost, m1=m1)  # zero spread -> floor
    assert t.iloc[0].cost == pytest.approx(0.35)


def test_skip_entry_after_friday_2200_m1():
    rows = [FLAT] * 130  # Fri 2024-01-05 21:00 ..
    t, s = run(rows, sig_frame("2024-01-05 22:00"), start="2024-01-05 21:00")
    assert t.empty and s["reason"].tolist() == ["entry_after_friday_close"]
    t, s = run(rows, sig_frame("2024-01-05 21:30"), start="2024-01-05 21:00")
    assert len(t) == 1 and t.iloc[0].exit_reason == "friday"


def test_skip_stale_after_gap_and_sl_behind_fill():
    m1 = make_bars([FLAT] * 60, MON, "min")
    m1.loc[30:, "time"] += pd.Timedelta(hours=30)
    t, s = run(None, sig_frame(m1.loc[30, "time"]), m1=m1)
    assert t.empty and s["reason"].tolist() == ["stale_after_gap"]
    t, s = run([FLAT] * 60, sig_frame("2024-01-08 10:30", sl=2001.0))
    assert t.empty and s["reason"].tolist() == ["sl_behind_fill"]


def test_sl_tp_same_m1_bar_is_loss():
    rows = [FLAT] * 31 + [(2000.0, 2030.0, 1990.0, 2000.0)]  # fill 2000, sl 1995, tp 2010
    t, _ = run(rows, sig_frame("2024-01-08 10:30"))
    assert t.iloc[0].exit_reason == "sl_same_bar" and t.iloc[0].r == pytest.approx(-1.0)


def test_rr_tp_from_fill():
    cost = CostParams()  # fill = 2000 + 0.35, dist = 5.35
    rows = [FLAT] * 32
    for mode, rr in (("rr1.5", 1.5), ("rr2.0", 2.0), ("rr3.0", 3.0)):
        t, _ = run(rows, sig_frame("2024-01-08 10:30"), cost=cost, tp_mode=mode)
        tr = t.iloc[0]
        assert tr.entry == pytest.approx(2000.35) and tr.sl_dist == pytest.approx(5.35)
        assert tr.tp == pytest.approx(2000.35 + rr * 5.35) and tr.tp_mode == mode
        assert tr.session == "london" and tr.setup == "t" and tr.level_kind == "pdh"
    t, _ = run(rows, sig_frame("2024-01-08 10:30", d=-1, sl=2005.0), cost=cost)
    assert t.iloc[0].tp == pytest.approx(1999.65 - 2 * (2005.0 - 1999.65))


def test_target_modes_and_next_level():
    rows = [FLAT] * 32  # fill 2000, dist 5, MIN_TP_R 1 -> target must be >= 2005
    t, s = run(rows, sig_frame("2024-01-08 10:30", target_ob=2012.0), tp_mode="ob")
    assert t.iloc[0].tp == 2012.0
    t, s = run(rows, sig_frame("2024-01-08 10:30", target_fvg=2004.0), tp_mode="fvg")
    assert s["reason"].tolist() == ["target_too_close_or_passed"]
    t, s = run(rows, sig_frame("2024-01-08 10:30"), tp_mode="owner")
    assert s["reason"].tolist() == ["no_target"]
    ts = pd.Timestamp
    levels = pd.DataFrame({
        "valid_from": [ts("2024-01-08 00:00")] * 3 + [ts("2024-01-08 11:00")],
        "valid_to": [pd.NaT, ts("2024-01-08 10:15"), pd.NaT, pd.NaT],
        "kind": ["pdh", "pwh", "poc", "psh"],
        "price": [2003.0, 2006.0, 2009.0, 2007.0],  # 2003 too close, 2006 expired, 2007 not yet valid
    })
    t, s = run(rows, sig_frame("2024-01-08 10:30"), tp_mode="next_level", levels=levels)
    assert t.iloc[0].tp == 2009.0
    t, s = run(rows, sig_frame("2024-01-08 10:30", d=-1, sl=2005.0), tp_mode="next_level", levels=levels)
    assert s["reason"].tolist() == ["no_target"]


def test_one_position_tie_break():
    ts = pd.Timestamp
    base = dict(entry_time=ts("2024-01-08 10:00"), exit_time=ts("2024-01-08 12:00"))
    trades = pd.DataFrame([
        {**base, "ref_time": ts("2024-01-08 09:00"), "sl_dist": 5.0, "id": "later_ref"},
        {**base, "ref_time": ts("2024-01-08 08:00"), "sl_dist": 9.0, "id": "early_ref_wide"},
        {**base, "ref_time": ts("2024-01-08 08:00"), "sl_dist": 4.0, "id": "early_ref_tight"},
        {"entry_time": ts("2024-01-08 11:59"), "exit_time": ts("2024-01-08 13:00"),
         "ref_time": ts("2024-01-08 08:00"), "sl_dist": 1.0, "id": "blocked"},
        {"entry_time": ts("2024-01-08 12:00"), "exit_time": ts("2024-01-08 13:00"),
         "ref_time": ts("2024-01-08 08:00"), "sl_dist": 1.0, "id": "free"},
    ])
    out = one_position(trades)
    assert len(out) == 5
    assert out.loc[out["taken"], "id"].tolist() == ["early_ref_tight", "free"]
    assert out["taken"].dtype == bool
