"""Batch-2 ICT variant runner, nulls and selection (small synthetic inputs, no real data)."""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest
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


def test_run_ict_limit_empty_window() -> None:
    sig = pd.DataFrame([limit_row(f"{DAY} 07:55", f"{DAY} 08:00", f"{DAY} 10:00", limit=99.5)])
    inp = _inputs(_touch_m1())
    inp.signals[("ict", "m1", "M5")] = sig[LIMIT_SIGNAL_COLS]
    v = IctVariant("m1", "M5", "rr2.0", False, "none", True)
    out, sk = run_ict_detail(v, inp, "2024-01-01", "2024-12-31")
    assert len(out) == 0 and sk == {}
    for col in ("signal_close", "signal_atr", "offset_atr", "taken", "variant", "setup", "entry_tf", "tp_mode"):
        assert col in out.columns
    assert research.flat_metrics(out)["n"] == 0
    assert np.isnan(ict_null(v, out, inp, repeats=3)).all()


def test_run_ict_limit_all_skipped_before_placement() -> None:
    fri = "2025-10-17"  # Friday
    m1 = make_bars([FLAT] * 120, f"{fri} 21:00", "1min")
    sig = pd.DataFrame([limit_row(f"{fri} 21:55", f"{fri} 22:00", f"{fri} 23:00", limit=99.5)])
    inp = _inputs(m1)
    inp.signals[("ict", "m1", "M5")] = sig[LIMIT_SIGNAL_COLS]
    out, sk = run_ict_detail(IctVariant("m1", "M5", "rr2.0", False, "none", True), inp, config.DEV_START, config.DEV_END)
    assert len(out) == 0 and sk == {"entry_after_friday_close": 1}
    assert {"offset_atr", "taken", "variant"} <= set(out.columns)


# --- Task 7: report builder and runner -----------------------------------------------------------
def _trades(model: str, n: int = 8, r: float = 0.5, start: str = "2022-06-01", every: str = "45D") -> pd.DataFrame:
    """Synthetic simulated trades (all taken) spread over the development period."""
    t = pd.date_range(start, periods=n, freq=every) + pd.Timedelta(hours=10)
    i = np.arange(n)
    d = np.where(i % 2 == 0, 1, -1)
    return pd.DataFrame(
        {
            "signal_time": t - pd.Timedelta(minutes=5), "entry_time": t, "exit_time": t + pd.Timedelta(hours=2),
            "direction": d, "sl_dist": 2.0, "cost": 0.35, "entry": 100.0, "sl": 100.0 - d * 2.0,
            "tp": 100.0 + d * 4.0, "r": np.where(i % 3 == 0, -1.0, r * 3), "taken": True,
            "session": [("asia", "london", "ny")[k % 3] for k in i], "setup": model, "entry_tf": "M5",
            "tp_mode": "rr2.0", "offset_atr": 0.3,
        }
    )


def _results(variants: list[IctVariant]) -> list:
    out = []
    for v in variants:
        tr = _trades(v.model).assign(entry_tf=v.entry_tf, variant=v.name)
        sk = {"not_filled": 3, "gap_through_sl": 1, "no_entry_bar": 2} if v.model in research_ict.LIMIT_MODELS else {}
        out.append(research.VariantResult(v, tr, sk))
    return out


def _smoke_set() -> list[IctVariant]:
    by = {v.name: v for v in ict_grid()}
    return [by[n] for n in research_ict.SMOKE_VARIANTS]


def test_ict_report_keeps_preregistration(tmp_path, monkeypatch) -> None:
    rdir = tmp_path / "reports"
    rdir.mkdir()
    monkeypatch.setattr(config, "REPORTS_DIR", rdir)
    pre = b"# Pre-registration\r\nmixed endings\nline\r\n\r\n" + research_ict.MARKER.encode()
    (rdir / research_ict.REPORT_NAME).write_bytes(pre + b"\nold results\r\n")
    results = _results(_smoke_set())
    table = research_ict.build_ict_table(results)
    text = research_ict.build_ict_results(results, table, tmp_path, 1.0, smoke=False)
    assert (tmp_path / "07_equity.png").exists() and (tmp_path / "07_expectancy.png").exists()
    # in place (the full run) and to another file (the smoke run)
    for out in (rdir / research_ict.REPORT_NAME, tmp_path / "07_ict_smoke.md"):
        research_ict.write_ict_report(text, out)
        got = out.read_bytes()
        assert got.startswith(pre) and b"old results" not in got
        assert got.count(research_ict.MARKER.encode()) == 1
    # a report without the marker is refused
    (rdir / research_ict.REPORT_NAME).write_text("no marker", encoding="utf-8")
    with pytest.raises(RuntimeError):
        research_ict.write_ict_report(text, tmp_path / "x.md")


def test_ict_report_states_cumulative_counts(tmp_path) -> None:
    results = _results(ict_grid())
    table = research_ict.build_ict_table(results)
    assert len(table) == 144 and int(table["eligible"].sum()) == 72
    text = research_ict.build_ict_results(results, table, tmp_path, 60.0, smoke=False)
    assert "Variants tried: **144** (pre-registered: 144)" in text
    assert "Eligible for selection (M5 entries): **72** (pre-registered: 72)" in text
    assert "**352** variants (**136** eligible)" in text
    assert "luck" in text
    for heading in (
        "### In plain words", "### Selection outcome per model", "### Charts", "### Top 3 eligible variants per model",
        "### Null baseline (random entries)", "### Cost sensitivity", "### Cost as a share of the stop",
        "### Results by session and by Philippine time", "### Model 5: Asian box height and stop size",
        "### Models 1 and 3: fill rate and limit offset", "### Entry-timeframe comparison",
        "### Skipped signals by reason", "### All variants", "### Caveats", "## Test set (Part C)",
    ):
        assert heading in text, heading
    assert "not_filled" in text and "gap_through_sl" in text
    assert "evening" in text.lower()
    assert "SMOKE" not in text


def test_ict_select_best_per_model() -> None:
    by = {v.name: v for v in ict_grid()}
    good = _trades("m1", n=130, r=0.5, start="2022-05-20", every="10D")
    assert set(good["signal_time"].dt.year) >= set(research.SELECT_YEARS)

    def res(name: str, r_scale: float, null: float = -0.5):
        v = by[name]
        tr = good.assign(r=good["r"] * r_scale, setup=v.model, entry_tf=v.entry_tf)
        return research.VariantResult(v, tr, {}, null=np.full(20, null))

    results = [
        res("m1|M5|rr2.0|nodir|none", 1.0),
        res("m1|M5|rr3.0|nodir|none", 2.0),  # best m1
        res("m3|M5|rr2.0|nodir|none", 1.0, null=5.0),  # fails the null
        res("m5|M5|rr2.0|nodir|none", 1.0),
        res("m5|M15|rr2.0|nodir|none", 3.0),  # comparison only, never selected
    ]
    table = research_ict.build_ict_table(results)
    for col in ("name", "setup", "entry_tf", "tp_mode", "direction", "time_filter", "eligible", "fail", "selected"):
        assert col in table.columns
    assert set(table.loc[table["selected"], "name"]) == {"m1|M5|rr3.0|nodir|none", "m5|M5|rr2.0|nodir|none"}
    t = table.set_index("name")
    assert t.loc["m3|M5|rr2.0|nodir|none", "fail"] == "null"
    assert t.loc["m5|M15|rr2.0|nodir|none", "fail"] == "eligible"


def test_ict_assert_dev_only() -> None:
    v = IctVariant("m1", "M5", "rr2.0", False, "none", True)
    ok = _trades("m1")
    research.assert_dev_only([research.VariantResult(v, ok, {})])
    leak = pd.concat([ok, ok.tail(1).assign(signal_time=pd.Timestamp("2025-10-24 00:00"))], ignore_index=True)
    for res in (research.VariantResult(v, leak, {}), research.VariantResult(v, ok, {}, sens={0.5: leak})):
        with pytest.raises(AssertionError):
            research.assert_dev_only([res])


def test_ict_fill_rate_and_offset() -> None:
    v = IctVariant("m1", "M5", "rr2.0", False, "none", True)
    tr = _trades("m1", n=4).assign(offset_atr=[0.1, 0.2, 0.4, 0.9])
    sk = {"no_entry_bar": 2, "stale_after_gap": 1, "entry_after_friday_close": 1, "not_filled": 6,
          "gap_through_sl": 1, "sl_behind_fill": 1, "target_too_close_or_passed": 1, "no_target": 1}
    s = research_ict.fill_stats(research.VariantResult(v, tr, sk))
    assert s["placed"] == 4 + 6 + 4 and s["filled"] == 4 + 4
    assert s["fill_rate"] == pytest.approx(8 / 14) and s["median_offset_atr"] == pytest.approx(0.3)


def test_ph_band() -> None:
    t = pd.Series(pd.to_datetime([
        "2025-10-22 05:00",  # NY summer, server = UTC+3: UTC 02:00 -> Manila 10:00
        "2025-10-22 15:00",  # UTC 12:00 -> Manila 20:00
        "2025-10-22 20:00",  # UTC 17:00 -> Manila 01:00
        "2025-10-22 22:00",  # UTC 19:00 -> Manila 03:00
        "2025-01-15 02:00",  # NY winter, server = UTC+2: UTC 00:00 -> Manila 08:00
        "2025-01-15 01:59",  # UTC 23:59 -> Manila 07:59
    ]))
    assert research_ict.ph_band(t).tolist() == ["08-18", "18-02", "18-02", "02-08", "08-18", "02-08"]


def _smoke_inputs() -> Inputs:
    inp = _null_inputs()
    inp.levels = pd.DataFrame(  # no levels -> next_level orders are skipped with no_target
        {"valid_from": pd.Series(dtype="datetime64[ns]"), "valid_to": pd.Series(dtype="datetime64[ns]"),
         "kind": pd.Series(dtype=str), "price": pd.Series(dtype=float)}
    )
    sig1 = pd.DataFrame(
        [
            limit_row(f"{DAY} 07:55", f"{DAY} 08:00", f"{DAY} 09:00", limit=99.5),
            limit_row(f"{DAY} 10:55", f"{DAY} 11:00", f"{DAY} 12:00", limit=100.5, d=-1, sl=102.0),
        ]
    )[LIMIT_SIGNAL_COLS]
    sig5 = pd.DataFrame([market_row(f"{DAY} 09:00", f"{DAY} 09:05"), market_row(f"{DAY} 13:00", f"{DAY} 13:05")])
    for tf in ("M5", "M15"):
        for dr in ((), ("dir",)):
            inp.signals[("ict", "m1", tf, *dr)] = sig1.assign(entry_tf=tf)
            inp.signals[("ict", "m3", tf, *dr)] = sig1.iloc[:0]
            inp.signals[("ict", "m5", tf, *dr)] = sig5[SIGNAL_COLS].assign(entry_tf=tf)
    return inp


def test_ict_main_smoke_writes_only_smoke_dir(tmp_path, monkeypatch) -> None:
    rdir, ddir = tmp_path / "reports", tmp_path / "data_research"
    rdir.mkdir()
    pre = "# pre\r\n" + research_ict.MARKER
    (rdir / research_ict.REPORT_NAME).write_bytes((pre + "\nold\n").encode())
    monkeypatch.setattr(config, "REPORTS_DIR", rdir)
    monkeypatch.setattr(research_ict, "RESEARCH_DATA_DIR", ddir)
    monkeypatch.setattr(research, "load_inputs", _smoke_inputs)
    research_ict.main(["--smoke"])
    assert [p.name for p in rdir.iterdir()] == [research_ict.REPORT_NAME]
    assert (rdir / research_ict.REPORT_NAME).read_bytes() == (pre + "\nold\n").encode()
    assert [p.name for p in ddir.iterdir()] == ["smoke_ict"]
    smoke = ddir / "smoke_ict"
    names = {p.name for p in smoke.iterdir()}
    assert {"07_ict_smoke.md", "07_variants.parquet", "07_selected.json", "07_equity.png", "07_expectancy.png"} <= names
    md = (smoke / "07_ict_smoke.md").read_bytes()
    assert md.startswith(pre.encode()) and b"SMOKE RUN" in md
    table = pd.read_parquet(smoke / "07_variants.parquet")
    assert set(table["name"]) == set(research_ict.SMOKE_VARIANTS)


def _ict_final_env(tmp_path, monkeypatch, selected: list[str]):
    rdir, ddir = tmp_path / "reports", tmp_path / "data_research"
    rdir.mkdir()
    ddir.mkdir()
    monkeypatch.setattr(config, "REPORTS_DIR", rdir)
    monkeypatch.setattr(research_ict, "RESEARCH_DATA_DIR", ddir)
    (ddir / "07_selected.json").write_text(json.dumps({"selected": selected}), encoding="utf-8")
    (rdir / research_ict.REPORT_NAME).write_bytes(("pre\n" + research_ict.MARKER + "\nbody\n").encode())
    monkeypatch.setattr(research, "load_inputs", lambda: (_ for _ in ()).throw(AssertionError("loaded")))
    return rdir


def test_ict_final_refuses_second_run(tmp_path, monkeypatch) -> None:
    rdir = _ict_final_env(tmp_path, monkeypatch, ["m1|M5|rr2.0|nodir|none"])
    (rdir / research_ict.MARKER_NAME).write_text("{}", encoding="utf-8")
    with pytest.raises(SystemExit):
        research_ict.main(["--final"])


def test_ict_final_empty_selection_does_not_load_test_data(tmp_path, monkeypatch) -> None:
    rdir = _ict_final_env(tmp_path, monkeypatch, [])
    research_ict.main(["--final"])
    marker = json.loads((rdir / research_ict.MARKER_NAME).read_text())
    assert marker["selected"] == [] and "not run" in marker["note"]
    text = (rdir / research_ict.REPORT_NAME).read_bytes().decode()
    assert research.TEST_HEADING in text and research_ict.TEST_NOT_RUN_TEXT in text
    assert "\r" not in text and text.startswith("pre\n" + research_ict.MARKER)
    with pytest.raises(SystemExit):  # the marker now locks the test set
        research_ict.main(["--final"])
    assert (rdir / research_ict.REPORT_NAME).read_bytes().decode().count(research.TEST_HEADING) == 1


def test_ict_final_refuses_smoke_selection(tmp_path, monkeypatch) -> None:
    rdir = _ict_final_env(tmp_path, monkeypatch, ["m1|M5|rr2.0|nodir|none"])
    (research_ict.RESEARCH_DATA_DIR / "07_selected.json").write_text(
        json.dumps({"smoke": True, "selected": ["m1|M5|rr2.0|nodir|none"]}), encoding="utf-8")
    with pytest.raises(SystemExit):
        research_ict.main(["--final"])
    assert not (rdir / research_ict.MARKER_NAME).exists()


def test_ict_final_writes_marker_before_computing(tmp_path, monkeypatch) -> None:
    rdir = _ict_final_env(tmp_path, monkeypatch, ["m1|M5|rr2.0|nodir|none"])
    with pytest.raises(AssertionError, match="loaded"):
        research_ict.main(["--final"])
    assert json.loads((rdir / research_ict.MARKER_NAME).read_text())["selected"] == ["m1|M5|rr2.0|nodir|none"]


def test_run_ict_test_set_columns(monkeypatch) -> None:
    monkeypatch.setattr(config, "TEST_START", DAY)
    monkeypatch.setattr(config, "TEST_END", DAY)
    res = research_ict.run_ict_test_set(pd.DataFrame({"name": ["m5|M5|rr2.0|nodir|none"]}), _smoke_inputs())
    assert list(res.columns) == ["name", "n", "expectancy_R", "ci95_low", "ci95_high", "verdict"]
    assert res.loc[0, "verdict"] in ("PASS", "FAIL")
