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
