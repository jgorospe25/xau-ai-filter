"""Batch-2 ICT variant runner, nulls and selection (small synthetic inputs, no real data)."""

from __future__ import annotations

import numpy as np
import pandas as pd
from conftest import make_bars

from xauai import config, research, research_ict
from xauai.research import Inputs, Variant
from xauai.research_ict import IctVariant, ict_grid, ict_null, run_ict_detail
from xauai.sim import LIMIT_SIGNAL_COLS, SIGNAL_COLS
from xauai.timeutil import assign_session, time_filter_mask

INFO = {"point": 0.01, "swap_mode": 1, "swap_long": 0.0, "swap_short": 0.0, "swap_rollover3days": 3}
FLAT = (100.0, 100.1, 99.9, 100.0)
DAY = "2025-10-22"  # Wednesday; NY in DST -> server = NY + 7 h: kill zones 09-12 and 14-17 server


def _inputs(m1: pd.DataFrame, m5: pd.DataFrame | None = None) -> Inputs:
    m5 = make_bars([(100.0, 100.5, 99.5, 100.0)] * 144, f"{DAY} 06:00", "5min") if m5 is None else m5
    bars = {tf: m5 for tf in ("M5", "M15", "M30", "H1", "H4", "D1")}
    bars["M1"] = m1
    return Inputs(
        bars=bars, windows=research.session_windows("2025-10-21", "2025-10-23"), levels=pd.DataFrame(),
        obs=pd.DataFrame(), gaps=pd.DataFrame(), h1_atr=pd.Series(dtype=float), info=INFO,
    )


def _flat_m1(hours: int = 12) -> pd.DataFrame:
    return make_bars([FLAT] * (60 * hours), f"{DAY} 06:00", "1min")


def market_row(signal_time: str, entry_time: str, d: int = 1, sl: float = 99.0) -> dict:
    return {
        "signal_time": pd.Timestamp(signal_time), "entry_time": pd.Timestamp(entry_time), "direction": d,
        "sl": sl, "setup": "m5", "entry_tf": "M5", "trigger_tf": None, "level_kind": "asia_high",
        "level_price": 100.0, "ref_time": pd.Timestamp(f"{DAY} 07:00"), "target_ob": np.nan,
        "target_fvg": np.nan, "target_owner": np.nan,
    }


def limit_row(signal_time: str, placed: str, order_end: str, limit: float, d: int = 1, sl: float = 98.0) -> dict:
    row = market_row(signal_time, placed, d, sl)
    row.update(
        setup="m1", level_kind="pdh", limit_price=limit, order_end=pd.Timestamp(order_end), signal_close=100.0,
        signal_atr=1.0, fvg_far=np.nan, leg_end=np.nan, ote_overlap=False,
    )
    return row


def test_ict_grid_144_variants_72_eligible() -> None:
    g = ict_grid()
    assert len(g) == 144
    names = [v.name for v in g]
    assert len(set(names)) == 144
    assert sum(v.eligible for v in g) == 72
    assert all(v.eligible == (v.entry_tf == "M5") for v in g)
    assert "m5|M15|next_level|nodir|owner" in names and "m1|M5|rr2.0|dir|killzone" in names
    assert names[0] == "m1|M5|rr1.5|nodir|none" and names[-1] == "m5|M15|next_level|dir|owner"
    v = g[0]
    assert v.setup == "m1" and v.trigger_tf is None and v.session_exit is False
    assert {v.model for v in g} == set(research_ict.MODELS)
    assert research_ict.MARKER == research.MARKER.replace("research.py", "research_ict.py")


def test_direction_filter_uses_h4_h1(monkeypatch) -> None:
    sig = pd.DataFrame(
        [
            market_row(f"{DAY} 09:00", f"{DAY} 09:05", d=1),
            market_row(f"{DAY} 09:10", f"{DAY} 09:15", d=-1, sl=101.0),
            market_row(f"{DAY} 09:20", f"{DAY} 09:25", d=1),
        ]
    )
    inp = _inputs(_flat_m1())
    inp.bars["H4"] = inp.bars["H4"].assign(tag="h4")
    inp.bars["H1"] = inp.bars["H1"].assign(tag="h1")
    inp.signals[("ict", "m5", "M5")] = sig[SIGNAL_COLS]
    seen: list = []

    def fake(at, h4, h1):
        seen.append((at, h4, h1))
        return np.array([1, 1, 0])

    monkeypatch.setattr(research_ict, "htf_direction", fake)
    v = IctVariant("m5", "M5", "rr2.0", True, "none", True)
    out, _ = run_ict_detail(v, inp, config.DEV_START, config.DEV_END)
    assert list(out["direction"]) == [1]  # the sell is against the H4/H1 direction, the third has 0
    at, h4, h1 = seen[0]
    assert (pd.Series(at).reset_index(drop=True) == sig["signal_time"] + pd.Timedelta(minutes=5)).all()
    assert (h4["tag"] == "h4").all() and (h1["tag"] == "h1").all()
    assert set(out["variant"]) == {v.name} and set(out["setup"]) == {"m5"} and set(out["tp_mode"]) == {"rr2.0"}
    # without the filter all three are simulated
    out, _ = run_ict_detail(IctVariant("m5", "M5", "rr2.0", False, "none", True), inp, config.DEV_START, config.DEV_END)
    assert len(out) == 3


def test_time_filter_model5_by_entry_time() -> None:
    sig = pd.DataFrame(
        [
            market_row(f"{DAY} 08:50", f"{DAY} 08:55"),  # entry outside the kill zone
            market_row(f"{DAY} 08:55", f"{DAY} 09:00"),  # entry at the kill-zone start
        ]
    )
    inp = _inputs(_flat_m1())
    inp.signals[("ict", "m5", "M5")] = sig[SIGNAL_COLS]
    kz, _ = run_ict_detail(IctVariant("m5", "M5", "rr2.0", False, "killzone", True), inp, config.DEV_START, config.DEV_END)
    assert list(kz["entry_time"]) == [pd.Timestamp(f"{DAY} 09:00")]
    none, _ = run_ict_detail(IctVariant("m5", "M5", "rr2.0", False, "none", True), inp, config.DEV_START, config.DEV_END)
    assert len(none) == 2


def _touch_m1() -> pd.DataFrame:
    m1 = _flat_m1()
    for hhmm in ("08:30", "09:30"):  # one touch outside the kill zone, one inside
        k = int(np.flatnonzero(m1["time"] == pd.Timestamp(f"{DAY} {hhmm}"))[0])
        m1.loc[k, "low"] = 99.0
    return m1


def test_time_filter_limit_uses_live_mask() -> None:
    sig = pd.DataFrame([limit_row(f"{DAY} 07:55", f"{DAY} 08:00", f"{DAY} 10:00", limit=99.5)])
    inp = _inputs(_touch_m1())
    inp.signals[("ict", "m1", "M5")] = sig[LIMIT_SIGNAL_COLS]
    none, sk = run_ict_detail(IctVariant("m1", "M5", "rr2.0", False, "none", True), inp, config.DEV_START, config.DEV_END)
    assert list(none["entry_time"]) == [pd.Timestamp(f"{DAY} 08:30")] and sk == {}
    kz, _ = run_ict_detail(IctVariant("m1", "M5", "rr2.0", False, "killzone", True), inp, config.DEV_START, config.DEV_END)
    assert list(kz["entry_time"]) == [pd.Timestamp(f"{DAY} 09:30")]
    # offset of the limit from the signal close in ATR units: buy limit 0.5 below the close, ATR 1.0
    assert kz["offset_atr"].tolist() == [0.5]
    # an order whose whole window lies outside the kill zone does not fill
    inp.signals[("ict", "m1", "M5")] = pd.DataFrame(
        [limit_row(f"{DAY} 07:55", f"{DAY} 08:00", f"{DAY} 09:00", limit=99.5)]
    )[LIMIT_SIGNAL_COLS]
    kz, sk = run_ict_detail(IctVariant("m1", "M5", "rr2.0", False, "killzone", True), inp, config.DEV_START, config.DEV_END)
    assert len(kz) == 0 and sk == {"not_filled": 1}


def test_model3_comes_from_model1_cache() -> None:
    sig = pd.DataFrame(
        [
            limit_row(f"{DAY} 07:55", f"{DAY} 08:00", f"{DAY} 10:00", limit=99.5),
            limit_row(f"{DAY} 10:55", f"{DAY} 11:00", f"{DAY} 12:00", limit=99.5),
        ]
    )
    sig.loc[1, "ote_overlap"] = True
    inp = _inputs(_touch_m1())
    inp.signals[("ict", "m1", "M5")] = sig[LIMIT_SIGNAL_COLS]
    out, sk = run_ict_detail(IctVariant("m3", "M5", "rr2.0", False, "none", True), inp, config.DEV_START, config.DEV_END)
    assert set(inp.signals[("ict", "m3", "M5")]["setup"]) == {"m3"} and len(inp.signals[("ict", "m3", "M5")]) == 1
    assert len(out) == 0 and sk == {"not_filled": 1}  # no touch after 11:00


# --- nulls ----------------------------------------------------------------------------------------
def _null_inputs() -> Inputs:
    rows = [(100.0, 100.6, 99.4, 100.0), (100.0, 100.2, 99.8, 100.1)] * 360  # 12 h of M1
    return _inputs(make_bars(rows, f"{DAY} 06:00", "1min"))


def _taken_limit(inp: Inputs, mode: str) -> pd.DataFrame:
    cands = research_ict.null_cands_ict(inp, "M5", mode)
    pick = cands.iloc[[0, 3, 10, 20, 30, 40]].reset_index(drop=True)
    d = np.array([1, 1, -1, 1, -1, -1])
    sl = np.array([0.5, 0.8, 1.0, 0.5, 2.0, 0.7])
    return pd.DataFrame(
        {
            "entry_time": pick["time"], "session": pick["session"], "direction": d, "sl_dist": sl,
            "entry": 100.0, "tp": 100.0 + d * sl * 2.0, "r": 0.1, "taken": True,
            "offset_atr": [0.2, 0.3, 0.1, 0.4, 0.2, 0.3],
        }
    )


def test_ict_null_preserves_session_direction_and_window() -> None:
    inp = _null_inputs()
    v = IctVariant("m1", "M5", "next_level", False, "killzone", True)
    taken = _taken_limit(inp, "killzone")
    rng = np.random.default_rng(0)
    sig = research_ict.limit_null_signals(v, taken, inp, rng, repeats=10)
    assert time_filter_mask(sig["entry_time"], "killzone").all()
    assert set(sig["level_kind"]) == {str(k) for k in range(10)}
    lab = assign_session(sig["entry_time"], inp.windows)["session"].fillna("none").to_numpy()
    assert (lab == sig["session"].to_numpy()).all()
    for _, g in sig.groupby("level_kind"):
        assert sorted(g["session"]) == sorted(taken["session"])
        assert sorted(g["direction"]) == sorted(taken["direction"])
    # limit placed at a drawn offset (ATR = 1.0 on these M5 bars, ref close 100.0)
    off = -sig["direction"] * (sig["limit_price"] - sig["signal_close"]) / sig["signal_atr"]
    assert set(np.round(off, 9)) <= set(taken["offset_atr"])
    assert (sig["order_end"] == sig["entry_time"] + 12 * pd.Timedelta(minutes=5)).all()
    assert (sig["ref_time"] == sig["entry_time"]).all()
    assert sig["null_sl_dist"].isin(taken["sl_dist"]).all()
    exps = ict_null(v, taken, inp, repeats=5)
    assert len(exps) == 5 and np.isfinite(exps).any()
    assert np.array_equal(exps, ict_null(v, taken, inp, repeats=5), equal_nan=True)


def test_ict_null_seed_differs_from_batch1(monkeypatch) -> None:
    inp = _null_inputs()
    g = ict_grid()
    v5 = IctVariant("m5", "M5", "rr2.0", False, "owner", True)
    seen: dict = {}

    def fake(v, trades, inp_, repeats, seed, total_cost, cands=None, seed_offset=None):
        seen.update(cands=cands, seed_offset=seed_offset, seed=seed)
        return np.zeros(repeats)

    monkeypatch.setattr(research, "null_distribution", fake)
    trades = pd.DataFrame({"taken": [True], "r": [0.1]})
    ict_null(v5, trades, inp, repeats=3)
    assert seen["seed_offset"] == research_ict.NULL_SEED_BASE + g.index(v5) and seen["seed"] == research.NULL_SEED
    assert time_filter_mask(seen["cands"]["time"], "owner").all() and len(seen["cands"])
    monkeypatch.undo()
    # different offsets give different draws
    taken = _taken_limit(inp, "none")
    cands = research.null_candidates(inp, "M5")
    a = research.null_draws(taken, cands, np.random.default_rng(research.NULL_SEED + research_ict.NULL_SEED_BASE))
    b = research.null_draws(taken, cands, np.random.default_rng(research.NULL_SEED))
    assert not a.equals(b)


def test_batch1_null_draws_unchanged_with_default_extra() -> None:
    cands = pd.DataFrame(
        {"time": pd.date_range(f"{DAY} 06:00", periods=12, freq="5min"), "session": ["asia"] * 6 + ["london"] * 6}
    )
    taken = pd.DataFrame(
        {"session": ["asia", "london", "london"], "direction": [1, -1, 1], "sl_dist": [0.5, 1.0, 2.0],
         "entry": 100.0, "tp": [101.0, 98.5, 106.0], "offset_atr": [0.1, 0.2, 0.3]}
    )
    base = research.null_draws(taken, cands, np.random.default_rng(3))
    pd.testing.assert_frame_equal(base, research.null_draws(taken, cands, np.random.default_rng(3), extra=()))
    expected = pd.DataFrame(
        {
            "entry_time": pd.to_datetime([f"{DAY} 06:20", f"{DAY} 06:35", f"{DAY} 06:30"]),
            "session": ["asia", "london", "london"], "direction": [1, -1, 1], "sl_dist": [0.5, 2.0, 2.0],
            "tp_r": [2.0, 3.0, 3.0],
        }
    )
    pd.testing.assert_frame_equal(base, expected, check_dtype=False)
    ext = research.null_draws(taken, cands, np.random.default_rng(3), extra=("offset_atr",))
    pd.testing.assert_frame_equal(ext[base.columns], base)
    assert ext["offset_atr"].tolist() == [0.1, 0.3, 0.3]  # same pick as sl_dist / tp_r


def test_batch1_null_distribution_defaults_unchanged() -> None:
    inp = _null_inputs()
    v = Variant("1", "M5", None, "rr2.0", False, False, True)
    cands = research.null_candidates(inp, "M5")
    pick = cands.iloc[[2, 5, 30, 60]].reset_index(drop=True)
    taken = pd.DataFrame(
        {"entry_time": pick["time"], "session": pick["session"], "direction": [1, -1, 1, -1],
         "sl_dist": [0.5, 0.8, 1.0, 0.5], "entry": 100.0, "tp": 100.0 + np.array([1, -1, 1, -1]) * 1.0,
         "r": 0.0, "taken": True}
    )
    a = research.null_distribution(v, taken, inp, repeats=4)
    b = research.null_distribution(v, taken, inp, repeats=4, cands=None, seed_offset=None)
    c = research.null_distribution(v, taken, inp, repeats=4, cands=cands, seed_offset=research._variant_offset(v))
    assert np.array_equal(a, b, equal_nan=True) and np.array_equal(a, c, equal_nan=True)
    d = research.null_distribution(v, taken, inp, repeats=4, seed_offset=research._variant_offset(v) + 1)
    assert not np.array_equal(a, d, equal_nan=True)


def test_selection_best_per_model() -> None:
    def row(name: str, model: str, exp: float, eligible: bool = True) -> dict:
        r = {"name": name, "setup": model, "eligible": eligible, "n": 150, "expectancy_R": exp, "beat_share": 0.99}
        r.update({f"exp_{y}": 0.1 for y in research.SELECT_YEARS})
        return r

    table = pd.DataFrame(
        [row("a", "m1", 0.2), row("b", "m1", 0.3), row("c", "m3", 0.1), row("d", "m5", 0.5, eligible=False)]
    )
    out = research.select(table)
    assert list(out["name"]) == ["b", "c"] and len(out) <= 3
