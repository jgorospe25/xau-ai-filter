"""Variant grid and runner (small synthetic inputs, no real data)."""

from __future__ import annotations

import numpy as np
import pandas as pd
from conftest import make_bars

from xauai import config, research
from xauai.research import Inputs, Variant, run_variant, variant_grid, variant_metrics
from xauai.sim import SIGNAL_COLS

INFO = {
    "point": 0.01, "swap_mode": 1, "swap_long": 0.0, "swap_short": 0.0, "swap_rollover3days": 3,
}
FLAT = (100.0, 100.5, 99.5, 100.0)


def sig_row(signal_time: str, entry_time: str, d: int = 1, sl: float = 99.0, **kw) -> dict:
    row = {
        "signal_time": pd.Timestamp(signal_time), "entry_time": pd.Timestamp(entry_time), "direction": d,
        "sl": sl, "setup": "2a", "entry_tf": "M5", "trigger_tf": "H1", "level_kind": "pdh",
        "level_price": 100.0, "ref_time": pd.Timestamp("2025-01-01"), "target_ob": np.nan,
        "target_fvg": np.nan, "target_owner": np.nan,
    }
    row.update(kw)
    return row


def synthetic_inputs(sig: pd.DataFrame, key=("2a", "M5", "H1")) -> Inputs:
    m1 = pd.concat(
        [make_bars([FLAT] * 120, "2025-10-22 10:00", "1min"), make_bars([FLAT] * 120, "2025-10-27 10:00", "1min")],
        ignore_index=True,
    )
    h1 = make_bars([FLAT] * 5, "2025-10-22 08:00", "1h")
    bars = {tf: h1 for tf in ("M5", "M15", "M30", "H1", "H4", "D1")}
    bars["M1"] = m1
    inp = Inputs(
        bars=bars, windows=pd.DataFrame({"session": [], "start": [], "end": []}).astype(
            {"start": "datetime64[ns]", "end": "datetime64[ns]"}
        ),
        levels=pd.DataFrame(), obs=pd.DataFrame(), gaps=pd.DataFrame(),
        h1_atr=pd.Series(dtype=float), info=INFO,
    )
    inp.signals[key] = sig[SIGNAL_COLS]
    return inp


def v2a(tp: str = "ob", trend: bool = False) -> Variant:
    return Variant("2a", "M5", "H1", tp, trend, False, True)


def test_grid_has_208_variants_64_eligible() -> None:
    g = variant_grid()
    assert len(g) == 208
    assert len({v.name for v in g}) == 208
    assert sum(v.eligible for v in g) == 64
    assert Variant("1", "M5", None, "rr2.0", True, False, True).name == "1|M5|rr2.0|trend|hold"
    assert v2a().name == "2a|M5|H1|ob|notrend|hold"
    assert Variant("2a", "M5", "H1", "ob", False, True, True).name == "2a|M5|H1|ob|notrend|session"


def test_trend_filter_drops_countertrend_signals(monkeypatch) -> None:
    sig = pd.DataFrame(
        [
            sig_row("2025-10-22 10:00", "2025-10-22 11:00", d=1, target_ob=105.0),
            sig_row("2025-10-22 10:00", "2025-10-22 11:10", d=-1, sl=101.0, target_ob=95.0),
            sig_row("2025-10-22 10:00", "2025-10-22 11:20", d=1, target_ob=105.0),
        ]
    )
    inp = synthetic_inputs(sig)
    seen: list[pd.Series] = []

    def fake(at, m30, h1):
        seen.append(at)
        return np.array([1, 1, 0])

    monkeypatch.setattr(research, "combined_trend", fake)
    out = run_variant(v2a(trend=True), inp, "2025-10-01", "2025-10-23")
    assert list(out["direction"]) == [1]  # sell is countertrend, third has trend 0
    # decision time = signal bar close = signal_time + trigger TF length
    assert (seen[0] == pd.Timestamp("2025-10-22 11:00")).all()


def test_target_passed_after_cost_is_skipped() -> None:
    # buy fill = 100 + 0.35 = 100.35; OB target 100.2 is already behind the fill
    sig = pd.DataFrame([sig_row("2025-10-22 10:00", "2025-10-22 11:00", target_ob=100.2)])
    sig.loc[0, "entry_time"] = pd.Timestamp("2025-10-22 10:30")
    inp = synthetic_inputs(sig)
    out = run_variant(v2a(), inp, "2025-10-01", "2025-10-23")
    assert len(out) == 0 and "taken" in out.columns and "variant" in out.columns
    assert out.attrs["skipped"] == {"target_too_close_or_passed": 1}
    assert not (out.get("tp", pd.Series(dtype=float)) < 0).any()


def test_dev_window_excludes_test_period() -> None:
    sig = pd.DataFrame(
        [
            sig_row("2025-10-22 10:00", "2025-10-22 10:30", target_ob=103.0),
            sig_row("2025-10-23 23:55", "2025-10-27 10:30", target_ob=103.0),  # inside end day
            sig_row("2025-10-24 00:00", "2025-10-27 10:40", target_ob=103.0),
        ]
    )
    inp = synthetic_inputs(sig)
    out = run_variant(v2a(), inp, config.DEV_START, config.DEV_END)
    assert len(out) == 2
    assert (out["signal_time"] < pd.Timestamp(config.TEST_START)).all()
    assert out["taken"].all() and set(out["variant"]) == {v2a().name}
    assert set(out["tp_mode"]) == {"ob"} and set(out["setup"]) == {"2a"}


def test_variant_metrics_handles_empty_and_counts() -> None:
    empty = pd.DataFrame({"taken": pd.Series(dtype=bool)})
    assert variant_metrics(empty, "2023-01-01", "2023-12-31")["n"] == 0
    t = pd.DataFrame(
        {
            "taken": [True, True, False],
            "signal_time": pd.to_datetime(["2023-02-01", "2024-02-01", "2024-03-01"]),
            "r": [2.0, -1.0, 5.0], "cost": [0.35, 0.35, 0.35], "sl_dist": [1.0, 0.7, 1.0],
        }
    )
    m = variant_metrics(t, "2023-01-01", "2024-12-31")
    assert m["n"] == 2 and m["expectancy_R"] == 0.5
    assert m["exp_by_year"] == {2023: 2.0, 2024: -1.0}
    assert abs(m["mean_cost_over_sl"] - (0.35 + 0.5) / 2) < 1e-9


# --- Task 12: selection and null baseline -------------------------------------------------------
def sel_row(name: str, setup: str, exp: float, years=(0.1, 0.1, 0.1, 0.1), n: int = 150,
            beat: float = 0.99, eligible: bool = True) -> dict:
    row = {"name": name, "setup": setup, "eligible": eligible, "n": n, "expectancy_R": exp, "beat_share": beat}
    row.update({f"exp_{y}": v for y, v in zip(research.SELECT_YEARS, years)})
    return row


def test_select_requires_every_year_positive() -> None:
    table = pd.DataFrame(
        [
            sel_row("a", "1", 0.30, years=(0.2, -0.01, 0.5, 0.4)),  # 2023 negative
            sel_row("b", "1", 0.25, years=(np.nan, 0.2, 0.2, 0.2)),  # no trades in 2022
            sel_row("c", "1", 0.20, years=(0.1, 0.1, 0.0, 0.1)),  # 2024 exactly zero is not > 0
            sel_row("d", "1", 0.05),
        ]
    )
    out = research.select(table)
    assert list(out["name"]) == ["d"]
    checks = research.selection_checks(table).set_index("name")
    assert checks.loc["a", "fail"] == "years" and checks.loc["b", "fail"] == "years"
    assert checks.loc["c", "fail"] == "years" and checks.loc["d", "fail"] == ""


def test_select_one_per_setup() -> None:
    table = pd.DataFrame(
        [
            sel_row("1-best", "1", 0.30), sel_row("1-second", "1", 0.20),
            sel_row("1-cmp", "1", 0.90, eligible=False),  # comparison-only TF: never selected
            sel_row("1-small", "1", 0.80, n=99),
            sel_row("2a-luck", "2a", 0.70, beat=0.94), sel_row("2a-ok", "2a", 0.10),
            sel_row("2b-nonull", "2b", 0.50, beat=np.nan),  # null not computed -> fails
            sel_row("3-x", "3", 0.15),
        ]
    )
    out = research.select(table)
    assert sorted(out["name"]) == ["1-best", "2a-ok", "3-x"]
    assert out["setup"].is_unique and len(out) <= 4
    checks = research.selection_checks(table).set_index("name")
    assert checks.loc["1-cmp", "fail"] == "eligible" and checks.loc["1-small", "fail"] == "n"
    assert checks.loc["2a-luck", "fail"] == "null" and checks.loc["2b-nonull", "fail"] == "null"


def _null_inputs() -> Inputs:
    rows = [(100.0, 100.6, 99.4, 100.0), (100.0, 100.2, 99.8, 100.1)] * 360  # 12 h of M1
    m1 = make_bars(rows, "2025-10-22 06:00", "1min")
    m5 = make_bars([FLAT] * 144, "2025-10-22 06:00", "5min")
    windows = research.session_windows("2025-10-21", "2025-10-23")
    bars = {tf: m5 for tf in ("M5", "M15", "M30", "H1", "H4", "D1")}
    bars["M1"] = m1
    return Inputs(bars=bars, windows=windows, levels=pd.DataFrame(), obs=pd.DataFrame(), gaps=pd.DataFrame(),
                  h1_atr=pd.Series(dtype=float), info=INFO)


def _taken(inp: Inputs, r: float) -> pd.DataFrame:
    cands = research.null_candidates(inp, "M5")
    pick = cands.iloc[[2, 5, 30, 60, 90, 100]].reset_index(drop=True)
    return pd.DataFrame(
        {
            "entry_time": pick["time"], "session": pick["session"], "direction": [1, 1, -1, 1, -1, -1],
            "sl_dist": [0.5, 0.8, 1.0, 0.5, 2.0, 0.7], "entry": 100.0,
            "tp": 100.0 + np.array([1, 1, -1, 1, -1, -1]) * np.array([0.5, 0.8, 1.0, 0.5, 2.0, 0.7]) * 2.0,
            "r": r, "taken": True,
        }
    )


def test_null_preserves_session_and_direction_mix() -> None:
    inp = _null_inputs()
    taken = _taken(inp, 0.1)
    taken.loc[3, "tp"] = 100.0 + 0.5 * 3.0  # a 3R target, drawn jointly with its 0.5 SL
    cands = research.null_candidates(inp, "M5")
    assert cands["time"].between(pd.Timestamp("2025-10-22"), pd.Timestamp("2025-10-23")).all()
    rng = np.random.default_rng(0)
    for _ in range(20):
        d = research.null_draws(taken, cands, rng)
        assert len(d) == len(taken)
        assert sorted(d["session"]) == sorted(taken["session"])
        assert sorted(d["direction"]) == sorted(taken["direction"])
        # each drawn entry bar really has the session it claims
        lab = cands.set_index("time")["session"]
        assert (lab.loc[d["entry_time"]].to_numpy() == d["session"].to_numpy()).all()
        pairs = set(zip(taken["sl_dist"], (taken["tp"] - taken["entry"]).abs() / taken["sl_dist"]))
        assert set(zip(d["sl_dist"], d["tp_r"])) <= {(s, round(t, 9)) for s, t in pairs} | pairs


def test_null_beat_share_bounds() -> None:
    inp = _null_inputs()
    v = Variant("1", "M5", None, "rr2.0", False, False, True)
    exps = research.null_distribution(v, _taken(inp, 0.0), inp, repeats=5)
    assert len(exps) == 5 and np.isfinite(exps).all()
    hi = research.null_baseline(v, _taken(inp, 100.0), inp, repeats=5)
    lo = research.null_baseline(v, _taken(inp, -100.0), inp, repeats=5)
    assert hi == 1.0 and lo == 0.0
    mid = research.null_baseline(v, _taken(inp, float(np.median(exps))), inp, repeats=5)
    assert 0.0 <= mid <= 1.0
    # deterministic for a given variant and seed
    again = research.null_distribution(v, _taken(inp, 0.0), inp, repeats=5)
    assert np.array_equal(exps, again)


def test_report_pipeline_keeps_preregistration(tmp_path) -> None:
    sig = pd.DataFrame(
        [
            sig_row("2025-10-22 10:00", "2025-10-22 10:30", target_ob=103.0),
            sig_row("2025-10-22 10:00", "2025-10-22 11:00", d=-1, sl=101.0, target_ob=97.0),
        ]
    )
    inp = synthetic_inputs(sig)
    results = []
    for v in (v2a(), Variant("2a", "M5", "H1", "ob", False, True, True)):
        tr, sk = research.run_variant_detail(v, inp, config.DEV_START, config.DEV_END)
        results.append(research.VariantResult(v, tr, sk))
    research.assert_dev_only(results)
    table = research.build_table(results)
    assert len(table) == 2 and not table["selected"].any() and set(table["fail"]) == {"n"}
    text = research.build_results(results, table, tmp_path, 1.0, smoke=False)
    assert "has NOT been touched" in text and "FAIL" in text and "comparison only" in text
    assert (tmp_path / "06_equity.png").exists() and (tmp_path / "06_expectancy.png").exists()
    out = tmp_path / "06.md"
    research.write_report(text, out)
    src = (config.REPORTS_DIR / research.REPORT_NAME).read_text(encoding="utf-8")
    pre = src.split(research.MARKER)[0] + research.MARKER
    assert out.read_text(encoding="utf-8").startswith(pre)


def test_assert_dev_only_rejects_test_period_trades() -> None:
    tr = pd.DataFrame({"signal_time": pd.to_datetime(["2025-10-24 00:00"]), "taken": [True]})
    res = research.VariantResult(v2a(), tr, {})
    try:
        research.assert_dev_only([res])
    except AssertionError:
        return
    raise AssertionError("expected a test-period leak to be rejected")
