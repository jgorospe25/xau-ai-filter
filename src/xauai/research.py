"""Variant grid and runner for the session-liquidity research (setups 1, 2a, 2b, 3).

Variant.name format: "<setup>|<entry_tf>|[<trigger_tf>|]<tp_mode>|<trend|notrend>|<session|hold>",
e.g. "1|M5|rr2.0|trend|hold" and "2a|M5|H1|ob|notrend|session" (2a includes its trigger TF).

Skipped-signal counts: `run_variant_detail` returns (trades, {reason: count}); `run_variant` returns the
trades and stores the same dict in `trades.attrs["skipped"]` (attrs can be lost by later pandas ops, so
use `run_variant_detail` when you must keep it).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from xauai import config
from xauai.backtest import bootstrap_ci, metrics, months_between
from xauai.labels import CostParams, SwapParams
from xauai.levels import level_table
from xauai.setups import _h1_atr_series, live_zones, setup1, setup3
from xauai.setups_ob import setup2a, setup2b
from xauai.sim import one_position, simulate_signals
from xauai.structure import combined_trend, swing_points
from xauai.timeutil import TF_LEN, session_windows
from xauai.zones import fvgs, order_blocks

BAR_TFS = ("M1", "M5", "M15", "M30", "H1", "H4", "D1")
ENTRY_TFS = ("M5", "M15", "M30", "H1")
RR_TPS = ("rr1.5", "rr2.0", "rr3.0")
_DAY = pd.Timedelta(days=1)


@dataclass(frozen=True)
class Variant:
    setup: str
    entry_tf: str
    trigger_tf: str | None
    tp_mode: str
    trend: bool
    session_exit: bool
    eligible: bool

    @property
    def name(self) -> str:
        parts = [self.setup, self.entry_tf] + ([self.trigger_tf] if self.trigger_tf else [])
        parts += [self.tp_mode, "trend" if self.trend else "notrend", "session" if self.session_exit else "hold"]
        return "|".join(parts)


def variant_grid() -> list[Variant]:
    """208 variants; `eligible` = M5 entry (every 2a variant enters on M5)."""
    base: list[tuple[str, str, str | None, str]] = []
    for setup, tps in (("1", RR_TPS + ("next_level",)), ("2b", RR_TPS + ("next_level",)), ("3", RR_TPS + ("owner",))):
        base += [(setup, tf, None, tp) for tf in ENTRY_TFS for tp in tps]
    base += [("2a", "M5", trg, tp) for trg in ("M30", "H1") for tp in ("ob", "fvg")]
    return [
        Variant(s, tf, trg, tp, trend, sess, tf == "M5")
        for s, tf, trg, tp in base
        for trend in (False, True)
        for sess in (False, True)
    ]


@dataclass
class Inputs:
    """Everything a variant needs. `h1_atr` is indexed by H1 bar OPEN time, as live_zones/setups expect
    (they add the bar length themselves to get the close). Caches fill lazily and are shared by variants."""

    bars: dict[str, pd.DataFrame]
    windows: pd.DataFrame
    levels: pd.DataFrame
    obs: pd.DataFrame
    gaps: pd.DataFrame
    h1_atr: pd.Series
    info: dict
    signals: dict[tuple, pd.DataFrame] = field(default_factory=dict)  # (setup, entry_tf, trigger_tf)
    zones: dict[str, pd.DataFrame] = field(default_factory=dict)  # per TF
    trend_signals: dict[tuple, pd.DataFrame] = field(default_factory=dict)


def _cut() -> pd.Timestamp:
    return pd.Timestamp(config.TEST_END) + _DAY - pd.Timedelta(seconds=1)


def load_inputs() -> Inputs:
    """Load all research bars (cut at TEST_END 23:59:59 server time) and derive levels/OBs/FVGs/ATR."""
    bars: dict[str, pd.DataFrame] = {}
    for tf in BAR_TFS:
        df = pd.read_parquet(config.research_path(tf))
        bars[tf] = df[df["time"] <= _cut()].reset_index(drop=True)
    info = json.loads(config.RESEARCH_SYMBOL_INFO_PATH.read_text())
    t0, t1 = bars["M1"]["time"].min().normalize(), bars["M1"]["time"].max().normalize()
    windows = session_windows(str(t0 - _DAY)[:10], str(t1 + _DAY)[:10])
    h1 = bars["H1"]
    return Inputs(
        bars=bars, windows=windows, levels=level_table(bars, windows),
        obs=order_blocks(h1, swing_points(h1, "H1")), gaps=fvgs(h1, bars["M1"]),
        h1_atr=_h1_atr_series(h1), info=info,
    )


def _zones(inp: Inputs, tf: str) -> pd.DataFrame:
    if tf not in inp.zones:
        inp.zones[tf] = live_zones(inp.levels, inp.bars[tf], inp.h1_atr, tf)
    return inp.zones[tf]


def _signals(v: Variant, inp: Inputs) -> pd.DataFrame:
    key = (v.setup, v.entry_tf, v.trigger_tf)
    if key in inp.signals:
        return inp.signals[key]
    b, h1 = inp.bars, inp.bars["H1"]
    if v.setup == "1":
        sig = setup1(b[v.entry_tf], v.entry_tf, inp.levels, h1, _zones(inp, v.entry_tf))
    elif v.setup == "3":
        sig = setup3(b[v.entry_tf], v.entry_tf, inp.levels, h1, b["D1"], _zones(inp, v.entry_tf))
    elif v.setup == "2b":
        sig = setup2b(b[v.entry_tf], v.entry_tf, inp.obs, h1)
    elif v.setup == "2a":
        trg = v.trigger_tf
        sig = setup2a(b[trg], trg, b["M5"], inp.levels, inp.obs, inp.gaps, h1, _zones(inp, trg))
    else:
        raise ValueError(f"unknown setup {v.setup!r}")
    inp.signals[key] = sig
    return sig


def _trend_filtered(v: Variant, inp: Inputs) -> pd.DataFrame:
    """Signals whose direction equals combined_trend at the signal bar's close (0 -> dropped)."""
    key = (v.setup, v.entry_tf, v.trigger_tf)
    if key not in inp.trend_signals:
        sig = _signals(v, inp)
        if len(sig):
            at = sig["signal_time"] + TF_LEN[v.trigger_tf or v.entry_tf]
            tr = combined_trend(at, inp.bars["M30"], inp.bars["H1"])
            sig = sig[sig["direction"].to_numpy() == tr]
        inp.trend_signals[key] = sig
    return inp.trend_signals[key]


def run_variant_detail(
    v: Variant, inp: Inputs, start: str, end: str, total_cost: float = 0.35
) -> tuple[pd.DataFrame, dict[str, int]]:
    """Trades (all rows, `taken` flag from one_position) and skipped-signal counts by reason."""
    sig = _trend_filtered(v, inp) if v.trend else _signals(v, inp)
    lo, hi = pd.Timestamp(start), pd.Timestamp(end) + _DAY  # end date inclusive through 23:59:59
    sig = sig[(sig["signal_time"] >= lo) & (sig["signal_time"] < hi)]
    m1 = inp.bars["M1"]
    ref = float(m1.loc[m1["time"] <= _cut(), "close"].iloc[-1])
    cost = CostParams(spread_floor=total_cost - config.SLIPPAGE, slippage=config.SLIPPAGE, point=inp.info["point"])
    swap = SwapParams.from_symbol_info(inp.info, reference_close=ref)
    trades, skipped = simulate_signals(
        sig, m1, cost, swap, v.tp_mode, v.session_exit, inp.windows,
        inp.levels if v.tp_mode == "next_level" else None,
    )
    out = one_position(trades)
    out["variant"], out["setup"], out["entry_tf"], out["tp_mode"] = v.name, v.setup, v.entry_tf, v.tp_mode
    counts = {str(k): int(n) for k, n in skipped["reason"].value_counts().items()}
    return out, counts


def run_variant(v: Variant, inp: Inputs, start: str, end: str, total_cost: float = 0.35) -> pd.DataFrame:
    """Like run_variant_detail but returns trades only; skipped counts in `.attrs["skipped"]`."""
    out, counts = run_variant_detail(v, inp, start, end, total_cost)
    out.attrs["skipped"] = counts
    return out


def variant_metrics(trades: pd.DataFrame, start: str, end: str) -> dict:
    """Metrics on `taken` trades over [start, end] (inclusive days): backtest.metrics + bootstrap CI,
    expectancy per calendar year (by signal_time) and mean cost / sl_dist."""
    t = trades[trades["taken"].astype(bool)]
    if len(t):
        t = t.sort_values("signal_time", kind="stable")
    months = months_between(pd.Timestamp(start), pd.Timestamp(end) + _DAY)
    r = t["r"].to_numpy(dtype=float) if len(t) else np.array([])
    m: dict = dict(metrics(r, months))
    if len(r) == 0:
        return {**m, "ci95_low": np.nan, "ci95_high": np.nan, "exp_by_year": {}, "mean_cost_over_sl": np.nan}
    m["ci95_low"], m["ci95_high"] = bootstrap_ci(r)
    m["exp_by_year"] = {int(y): float(g.mean()) for y, g in t.groupby(t["signal_time"].dt.year)["r"]}
    m["mean_cost_over_sl"] = float((t["cost"] / t["sl_dist"]).mean())
    return m
