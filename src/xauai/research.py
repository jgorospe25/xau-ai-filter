"""Variant grid and runner for the session-liquidity research (setups 1, 2a, 2b, 3).

Variant.name format: "<setup>|<entry_tf>|[<trigger_tf>|]<tp_mode>|<trend|notrend>|<session|hold>",
e.g. "1|M5|rr2.0|trend|hold" and "2a|M5|H1|ob|notrend|session" (2a includes its trigger TF).

Skipped-signal counts: `run_variant_detail` returns (trades, {reason: count}); `run_variant` returns the
trades and stores the same dict in `trades.attrs["skipped"]` (attrs can be lost by later pandas ops, so
use `run_variant_detail` when you must keep it).
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
import pandas as pd

from xauai import config
from xauai.backtest import bootstrap_ci, metrics, months_between
from xauai.labels import CostParams, SwapParams, gap_after_mask
from xauai.levels import level_table
from xauai.setups import _h1_atr_series, live_zones, setup1, setup3
from xauai.setups_ob import setup2a, setup2b
from xauai.sim import one_position, simulate_signals
from xauai.structure import combined_trend, swing_points
from xauai.timeutil import TF_LEN, assign_session, session_windows
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
    null_cands: dict[str, pd.DataFrame] = field(default_factory=dict)  # per entry TF: time, session


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


# --- Task 12: null baseline and selection ---------------------------------------------------------
SELECT_YEARS: tuple[int, ...] = (2022, 2023, 2024, 2025)
NULL_SEED = 7
_TP_EPS = 1e-9  # keeps a drawn 1.0R target from failing the >= 1R check on float rounding


def _dev_bounds() -> tuple[pd.Timestamp, pd.Timestamp]:
    """[DEV_START 00:00, DEV_END 23:59:59] server time."""
    return pd.Timestamp(config.DEV_START), pd.Timestamp(config.DEV_END) + _DAY - pd.Timedelta(seconds=1)


def null_candidates(inp: Inputs, tf: str) -> pd.DataFrame:
    """Entry-TF bar open times in the development period that the simulator accepts as entries (an M1 bar
    exists at that time, not at/after Friday 22:00, not right after a > 25 h gap), with their session label.
    assign_session is slow, so the result is cached per TF."""
    if tf in inp.null_cands:
        return inp.null_cands[tf]
    lo, hi = _dev_bounds()
    t = inp.bars[tf]["time"]
    t = t[(t >= lo) & (t <= hi)].reset_index(drop=True)
    m1t = inp.bars["M1"]["time"].reset_index(drop=True)
    tn, tv = m1t.to_numpy(), t.to_numpy()
    e = np.searchsorted(tn, tv)
    ok = e < len(tn)
    ok[ok] = tn[e[ok]] == tv[ok]
    gap_after = gap_after_mask(m1t)
    prev_gap = np.zeros(len(t), dtype=bool)
    has_prev = ok & (e > 0)
    prev_gap[has_prev] = gap_after[e[has_prev] - 1]
    friday = ((t.dt.day_of_week == 4) & (t.dt.hour >= config.FRIDAY_EXIT_HOUR)).to_numpy()
    t = t[ok & ~prev_gap & ~friday].reset_index(drop=True)
    sess = assign_session(t, inp.windows)["session"].fillna("none").astype(str)
    out = pd.DataFrame({"time": t, "session": sess.to_numpy()})
    inp.null_cands[tf] = out
    return out


def null_draws(taken: pd.DataFrame, cands: pd.DataFrame, rng: np.random.Generator) -> pd.DataFrame:
    """One random run. For every taken trade: a random candidate bar with the same session label, and the same
    direction (so session counts, direction mix and their pairing are exact). Each random trade gets an
    (SL distance in price, TP distance in R) pair drawn jointly, with replacement, from the taken trades.
    Within a session, bars are drawn without replacement unless there are too few."""
    sess = taken["session"].fillna("none").astype(str).to_numpy()
    dirs = taken["direction"].to_numpy(int)
    sl = taken["sl_dist"].to_numpy(float)
    tp_r = (taken["tp"].to_numpy(float) - taken["entry"].to_numpy(float)) * dirs / sl
    pools = {s: g["time"].to_numpy() for s, g in cands.groupby("session")}
    all_times = cands["time"].to_numpy()
    entry = np.empty(len(taken), dtype="datetime64[ns]")
    for s in np.unique(sess):
        idx = np.flatnonzero(sess == s)
        pool = pools.get(s, all_times)
        entry[idx] = rng.choice(pool, size=len(idx), replace=len(idx) > len(pool))
    pick = rng.integers(0, len(taken), size=len(taken))
    return pd.DataFrame(
        {"entry_time": entry, "session": sess, "direction": dirs, "sl_dist": sl[pick], "tp_r": tp_r[pick]}
    )


def _variant_offset(v: Variant) -> int:
    grid = variant_grid()
    return grid.index(v) if v in grid else 0


def null_distribution(
    v: Variant, trades: pd.DataFrame, inp: Inputs, repeats: int = config.NULL_REPEATS, seed: int = NULL_SEED,
    total_cost: float = 0.35,
) -> np.ndarray:
    """Expectancy (one position at a time) of each random run; rng seed = seed + index in variant_grid.
    All runs go through simulate_signals with the variant's own session_exit; fixed-RR variants keep their
    TP mode, target variants use each drawn trade's realised TP distance in R. SL = fill -/+ drawn distance."""
    taken = trades[trades["taken"].astype(bool)].reset_index(drop=True)
    if taken.empty:
        return np.full(repeats, np.nan)
    rng = np.random.default_rng(seed + _variant_offset(v))
    cands = null_candidates(inp, v.entry_tf)
    m1 = inp.bars["M1"]
    tn = m1["time"].to_numpy()
    o, spread = m1["open"].to_numpy(float), m1["spread"].to_numpy(float)
    cost = CostParams(spread_floor=total_cost - config.SLIPPAGE, slippage=config.SLIPPAGE, point=inp.info["point"])
    rr = v.tp_mode.startswith("rr")
    runs: list[pd.DataFrame] = []
    for k in range(repeats):
        d = null_draws(taken, cands, rng)
        e = np.searchsorted(tn, d["entry_time"].to_numpy())
        fee = np.maximum(spread[e] * cost.point, cost.spread_floor) + cost.slippage  # = cost.per_trade
        dirs = d["direction"].to_numpy(int)
        fill = o[e] + dirs * fee
        dist = d["sl_dist"].to_numpy(float)
        runs.append(
            pd.DataFrame(
                {
                    "signal_time": d["entry_time"], "entry_time": d["entry_time"], "direction": dirs,
                    "sl": fill - dirs * dist, "setup": v.setup, "entry_tf": v.entry_tf,
                    "trigger_tf": v.trigger_tf, "level_kind": str(k), "level_price": np.nan,
                    "ref_time": d["entry_time"], "target_ob": np.nan, "target_fvg": np.nan,
                    "target_owner": fill + dirs * d["tp_r"].to_numpy(float) * dist * (1 + _TP_EPS),
                }
            )
        )
    sig = pd.concat(runs, ignore_index=True)
    ref = float(m1.loc[m1["time"] <= _cut(), "close"].iloc[-1])
    swap = SwapParams.from_symbol_info(inp.info, reference_close=ref)
    sim, _ = simulate_signals(sig, m1, cost, swap, v.tp_mode if rr else "owner", v.session_exit, inp.windows)
    out = np.full(repeats, np.nan)
    for k, g in sim.groupby("level_kind"):
        g = one_position(g)
        out[int(k)] = float(g.loc[g["taken"], "r"].mean())
    return out


def beat_share(variant_exp: float, null_exps: np.ndarray) -> float:
    """Share of random runs the variant beats, i.e. runs whose expectancy is <= the variant's
    (the variant's expectancy is >= theirs). NaN runs are ignored."""
    x = np.asarray(null_exps, dtype=float)
    x = x[np.isfinite(x)]
    return float(np.mean(variant_exp >= x)) if len(x) else float("nan")


def null_baseline(
    v: Variant, trades: pd.DataFrame, inp: Inputs, repeats: int = config.NULL_REPEATS, seed: int = NULL_SEED
) -> float:
    """beat_share of the variant's one-position expectancy against its random runs."""
    r = trades.loc[trades["taken"].astype(bool), "r"]
    return beat_share(float(r.mean()), null_distribution(v, trades, inp, repeats, seed))


def selection_checks(table: pd.DataFrame) -> pd.DataFrame:
    """Adds `fail` = the first failed check in order eligible -> n -> years -> null ("" = passes all).
    Needs eligible, n, exp_<year> for SELECT_YEARS (NaN = no trades = fail) and beat_share (NaN = fail)."""
    out = table.copy()
    years_ok = np.ones(len(out), dtype=bool)
    for y in SELECT_YEARS:
        years_ok &= (out[f"exp_{y}"].astype(float) > 0).to_numpy()
    null_ok = (out["beat_share"].astype(float) >= config.NULL_BEAT).to_numpy()
    conds = [~out["eligible"].astype(bool).to_numpy(), (out["n"] < config.SELECT_MIN_N).to_numpy(), ~years_ok, ~null_ok]
    out["fail"] = np.select(conds, ["eligible", "n", "years", "null"], default="")
    return out


def select(table: pd.DataFrame) -> pd.DataFrame:
    """Variants passing every check; the highest expectancy per setup (at most one per setup)."""
    ok = selection_checks(table)
    ok = ok[ok["fail"] == ""].sort_values("expectancy_R", ascending=False, kind="stable")
    return ok.groupby("setup", sort=True).head(1).sort_values("setup").reset_index(drop=True)


# --- Task 12: Part B results ----------------------------------------------------------------------
MARKER = "<!-- RESULTS BELOW: generated by src/xauai/research.py; everything above is the pre-registration -->"
REPORT_NAME = "06_setups.md"
RESEARCH_DATA_DIR = config.DATA_DIR / "research"
SETUPS = ("1", "2a", "2b", "3")
SETUP_NAMES = {
    "1": "Setup 1 (sweep, reclaim, retest)",
    "2a": "Setup 2a (rejection at a level, target H1 OB / FVG)",
    "2b": "Setup 2b (pullback from the H1 OB)",
    "3": "Setup 3 (lower high, break, continue to PDL)",
}
SKIP_REASONS = (
    "no_entry_bar", "stale_after_gap", "entry_after_friday_close", "sl_behind_fill", "no_target",
    "target_too_close_or_passed",
)
SMOKE_VARIANTS = (
    "1|M5|rr2.0|notrend|hold", "2a|M5|H1|ob|notrend|session", "2b|M5|next_level|notrend|hold",
    "3|M5|owner|trend|hold", "1|H1|rr2.0|notrend|hold",
)


@dataclass
class VariantResult:
    v: Variant
    trades: pd.DataFrame  # every simulated signal, `taken` flag = one position at a time
    skipped: dict[str, int]
    sens: dict[float, pd.DataFrame] = field(default_factory=dict)  # total cost -> trades
    null: np.ndarray | None = None  # expectancy of each random run


def _view(trades: pd.DataFrame, every: bool) -> pd.DataFrame:
    """'one position' = taken rows; 'every signal' = all rows (marked taken so variant_metrics uses them)."""
    return trades.assign(taken=True) if every else trades


def flat_metrics(trades: pd.DataFrame, every: bool = False) -> dict:
    """variant_metrics on one view, flattened: n, win_rate, expectancy_R, CI, total_R, PF, max DD, streak,
    trades/month, exp_<year> and n_<year> for SELECT_YEARS, mean cost / SL."""
    t = _view(trades, every)
    m = variant_metrics(t, config.DEV_START, config.DEV_END)
    sel = t[t["taken"].astype(bool)]
    out = {k: m.get(k, np.nan) for k in (
        "n", "win_rate", "expectancy_R", "ci95_low", "ci95_high", "profit_factor", "max_dd_R",
        "max_losing_streak", "trades_per_month", "mean_cost_over_sl",
    )}
    out["n"] = int(m["n"])
    out["total_R"] = float(sel["r"].sum()) if len(sel) else 0.0
    yrs = sel["signal_time"].dt.year if len(sel) else pd.Series(dtype=int)
    for y in SELECT_YEARS:
        out[f"exp_{y}"] = m["exp_by_year"].get(y, np.nan)
        out[f"n_{y}"] = int((yrs == y).sum())
    return out


def _exp(trades: pd.DataFrame, every: bool = False) -> float:
    r = _view(trades, every)
    r = r.loc[r["taken"].astype(bool), "r"]
    return float(r.mean()) if len(r) else np.nan


def variant_row(res: VariantResult) -> dict:
    v = res.v
    row = {
        "name": v.name, "setup": v.setup, "entry_tf": v.entry_tf, "trigger_tf": v.trigger_tf or "",
        "tp_mode": v.tp_mode, "trend": v.trend, "session_exit": v.session_exit, "eligible": v.eligible,
        "n_signals": len(res.trades), "n_skipped": int(sum(res.skipped.values())),
    }
    row.update(flat_metrics(res.trades))
    every = flat_metrics(res.trades, every=True)
    row.update({f"all_{k}": every[k] for k in ("win_rate", "expectancy_R", "ci95_low", "ci95_high", "total_R")})
    for c in config.SENSITIVITY_TOTAL_COSTS:
        tr = res.sens.get(c)
        row[f"exp_cost_{c:.2f}"] = _exp(tr) if tr is not None else np.nan
        row[f"all_exp_cost_{c:.2f}"] = _exp(tr, every=True) if tr is not None else np.nan
    if res.null is not None:
        x = res.null[np.isfinite(res.null)]
        row["beat_share"] = beat_share(row["expectancy_R"], res.null)
        row["null_mean"], row["null_p05"], row["null_p95"] = (
            (float(x.mean()), float(np.percentile(x, 5)), float(np.percentile(x, 95))) if len(x) else (np.nan,) * 3
        )
        row["null_runs"] = len(x)
    else:
        row.update(beat_share=np.nan, null_mean=np.nan, null_p05=np.nan, null_p95=np.nan, null_runs=0)
    return row


def build_table(results: list[VariantResult]) -> pd.DataFrame:
    """Per-variant metrics with the selection check columns (`fail`, `selected`)."""
    table = selection_checks(pd.DataFrame([variant_row(r) for r in results]))
    chosen = set(select(table)["name"]) if len(table) else set()
    table["selected"] = table["name"].isin(chosen)
    return table


def pre_null_pass(table: pd.DataFrame) -> pd.Series:
    """Variants passing eligible, n and per-year checks (only these get a null baseline)."""
    return selection_checks(table.assign(beat_share=1.0))["fail"] == ""


def top_variants(table: pd.DataFrame, setup: str, k: int = 3) -> pd.DataFrame:
    """Top-k eligible variants of a setup by one-position expectancy: those with n >= SELECT_MIN_N first,
    then (only if fewer than k) the rest with n > 0."""
    t = table[(table["setup"] == setup) & table["eligible"] & (table["n"] > 0)]
    big = t[t["n"] >= config.SELECT_MIN_N].sort_values("expectancy_R", ascending=False)
    small = t[t["n"] < config.SELECT_MIN_N].sort_values("expectancy_R", ascending=False)
    return pd.concat([big, small]).head(k)


def assert_dev_only(results: list[VariantResult]) -> None:
    """No trade in any result may have a signal_time after DEV_END 23:59:59 (the test set stays untouched)."""
    hi = _dev_bounds()[1]
    for res in results:
        for tr in [res.trades, *res.sens.values()]:
            if len(tr) and tr["signal_time"].max() > hi:
                raise AssertionError(f"{res.v.name}: signal_time after {hi} — test period leaked")


# --- report formatting ---------------------------------------------------------------------------
def _f(x: float, nd: int = 3) -> str:
    return "—" if x is None or pd.isna(x) else ("inf" if np.isinf(x) else f"{x:.{nd}f}")


def _pct(x: float) -> str:
    return "—" if pd.isna(x) else f"{100 * x:.1f}%"


def _ci(lo: float, hi: float) -> str:
    return "—" if pd.isna(lo) else f"[{lo:+.3f}, {hi:+.3f}]"


def _esc(x: object) -> object:
    """Escape `|` for a markdown table cell (variant names contain it; backticks do not protect it in GFM)."""
    return x.replace("|", "\\|") if isinstance(x, str) else x


def _md(rows: list[dict]) -> str:
    from xauai.md import table as md_table

    if not rows:
        return "_none_"
    df = pd.DataFrame(rows)
    return md_table(df.rename(columns=_esc).map(_esc))


def all_variants_md(table: pd.DataFrame) -> str:
    t = table.sort_values(["setup", "expectancy_R"], ascending=[True, False], na_position="last")
    rows = [
        {
            "variant": r.name, "eligible": "yes" if r.eligible else "comparison only", "n taken": r.n,
            "n signals": r.n_signals, "win%": _pct(r.win_rate), "exp R": _f(r.expectancy_R),
            "95% CI": _ci(r.ci95_low, r.ci95_high), "total R": _f(r.total_R, 1), "PF": _f(r.profit_factor, 2),
        }
        for r in t.itertuples()
    ]
    return _md(rows)


def _group_rows(trades: pd.DataFrame, key: pd.Series, label: str) -> list[dict]:
    rows = []
    for g, sub in trades.groupby(key, sort=True):
        r = sub["r"].to_numpy(float)
        rows.append({label: g, "n": len(r), "win%": _pct((r > 0).mean()), "exp R": _f(r.mean()),
                     "total R": _f(r.sum(), 1)})
    return rows


def detail_md(res: VariantResult, row: pd.Series) -> str:
    """Every metric the spec lists, for the one-position and every-signal views."""
    lines = [f"#### `{res.v.name}`", ""]
    head = []
    for every, label in ((False, "one position (used for selection)"), (True, "every signal")):
        m = flat_metrics(res.trades, every)
        sens = {c: (_exp(res.sens[c], every) if c in res.sens else np.nan) for c in config.SENSITIVITY_TOTAL_COSTS}
        head.append(
            {
                "view": label, "trades": m["n"], "win%": _pct(m["win_rate"]), "exp R": _f(m["expectancy_R"]),
                "95% CI": _ci(m["ci95_low"], m["ci95_high"]), "total R": _f(m["total_R"], 1),
                "PF": _f(m["profit_factor"], 2), "max DD R": _f(m["max_dd_R"], 1),
                "longest losing streak": m["max_losing_streak"], "trades/month": _f(m["trades_per_month"], 2),
                **{f"exp R @ cost {c:.2f}": _f(x) for c, x in sens.items()},
            }
        )
    lines += [_md(head), ""]
    if not pd.isna(row.get("beat_share", np.nan)):
        lines += [
            f"Null baseline: beats {_pct(row['beat_share'])} of {int(row['null_runs'])} random runs "
            f"(random expectancy mean {_f(row['null_mean'])}R, 5th {_f(row['null_p05'])}R, "
            f"95th {_f(row['null_p95'])}R).", "",
        ]
    else:
        lines += ["Null baseline: not computed (it is only run for variants that pass n and every-year checks).", ""]
    for every, label in ((False, "one position"), (True, "every signal")):
        t = _view(res.trades, every)
        t = t[t["taken"].astype(bool)]
        if t.empty:
            continue
        side = t["direction"].map({1: "buy", -1: "sell"})
        rows = (
            [{"group": f"year {x['year']}", **{k: v for k, v in x.items() if k != "year"}}
             for x in _group_rows(t, t["signal_time"].dt.year, "year")]
            + [{"group": f"session {x['session']}", **{k: v for k, v in x.items() if k != "session"}}
               for x in _group_rows(t, t["session"].fillna("none"), "session")]
            + [{"group": x["side"], **{k: v for k, v in x.items() if k != "side"}} for x in _group_rows(t, side, "side")]
        )
        lines += [f"Breakdown, {label}:", "", _md(rows), ""]
    return "\n".join(lines)


def cost_share_md(results: list[VariantResult]) -> str:
    """cost / SL distance per entry TF over unique simulated trades (deduplicated across variants)."""
    frames = [r.trades[["setup", "entry_tf", "entry_time", "direction", "sl", "cost", "sl_dist"]] for r in results]
    frames = [f for f in frames if len(f)]
    if not frames:
        return "_no trades_"
    u = pd.concat(frames).drop_duplicates(["setup", "entry_tf", "entry_time", "direction", "sl"])
    u = u.assign(share=u["cost"] / u["sl_dist"])
    rows = []
    for tf in ENTRY_TFS:
        g = u[u["entry_tf"] == tf]
        if g.empty:
            continue
        rows.append({
            "entry TF": tf, "unique trades": len(g), "mean cost / SL": _pct(g["share"].mean()),
            "median cost / SL": _pct(g["share"].median()), "median SL distance ($)": _f(g["sl_dist"].median(), 2),
            "median cost ($)": _f(g["cost"].median(), 2),
        })
    return _md(rows)


def skipped_md(results: list[VariantResult]) -> str:
    """Skipped-signal counts per base combination (from the notrend|hold variant; the exit mode does not
    change skipping, the trend filter only removes signals first)."""
    rows = []
    for r in results:
        v = r.v
        if v.trend or v.session_exit:
            continue
        row = {"setup": v.setup, "entry TF": v.entry_tf, "trigger TF": v.trigger_tf or "", "TP mode": v.tp_mode,
               "simulated": len(r.trades)}
        row.update({k: r.skipped.get(k, 0) for k in SKIP_REASONS})
        other = sum(n for k, n in r.skipped.items() if k not in SKIP_REASONS)
        row["other"] = other
        rows.append(row)
    return _md(rows)


def comparison_md(table: pd.DataFrame) -> str:
    rows = []
    for (s, tf), g in table.groupby(["setup", "entry_tf"], sort=True):
        g = g[g["n"] > 0]
        if g.empty:
            continue
        b = g.sort_values("expectancy_R", ascending=False).iloc[0]
        rows.append({
            "setup": s, "entry TF": tf, "status": "eligible (M5)" if tf == "M5" else "comparison only",
            "variants": len(g), "variants exp > 0": int((g["expectancy_R"] > 0).sum()),
            "median exp R": _f(g["expectancy_R"].median()), "best variant": b["name"], "best n": int(b["n"]),
            "best exp R": _f(b["expectancy_R"]), "best 95% CI": _ci(b["ci95_low"], b["ci95_high"]),
        })
    return _md(rows)


def selection_outcome(table: pd.DataFrame, setup: str) -> tuple[str, str]:
    """(short outcome, plain-words explanation) for one setup."""
    t = table[(table["setup"] == setup) & table["eligible"]]
    if t.empty:
        return "FAIL", "no eligible variant was run"
    sel = t[t["selected"]]
    n_ok = t[t["n"] >= config.SELECT_MIN_N]
    yr_ok = n_ok[(n_ok[[f"exp_{y}" for y in SELECT_YEARS]] > 0).all(axis=1)]
    nul_ok = yr_ok[yr_ok["beat_share"] >= config.NULL_BEAT]
    funnel = (f"{len(t)} eligible -> {len(n_ok)} with n >= {config.SELECT_MIN_N} -> {len(yr_ok)} positive in every "
              f"year -> {len(nul_ok)} beat >= {config.NULL_BEAT:.0%} of random runs")
    if len(sel):
        b = sel.iloc[0]
        return (f"SELECTED `{b['name']}`",
                f"{funnel}. Best: n = {int(b['n'])}, expectancy {b['expectancy_R']:+.3f}R, 95% CI "
                f"{_ci(b['ci95_low'], b['ci95_high'])}, beats {_pct(b['beat_share'])} of random runs.")
    if n_ok.empty:
        b = t.sort_values("n", ascending=False).iloc[0]
        why = (f"FAIL at n >= {config.SELECT_MIN_N}: the most trades any eligible variant had was {int(b['n'])} "
               f"(`{b['name']}`), {config.SELECT_MIN_N - int(b['n'])} short.")
    elif yr_ok.empty:
        yc = [f"exp_{y}" for y in SELECT_YEARS]
        worst = n_ok[yc].min(axis=1, skipna=False).fillna(-np.inf)
        b = n_ok.loc[worst.idxmax()]
        wy = min(SELECT_YEARS, key=lambda y: b[f"exp_{y}"] if not pd.isna(b[f"exp_{y}"]) else -np.inf)
        why = (f"FAIL at 'expectancy > 0 in every year': closest is `{b['name']}`, whose worst year {wy} was "
               f"{_f(b[f'exp_{wy}'])}R (needs > 0); {int((n_ok[yc] > 0).sum(axis=1).max())} of 4 years positive at best.")
    else:
        b = yr_ok.sort_values("beat_share", ascending=False).iloc[0]
        why = (f"FAIL at the null baseline: best is `{b['name']}`, which beat {_pct(b['beat_share'])} of its random "
               f"runs (needs >= {config.NULL_BEAT:.0%}).")
    return "FAIL", f"{funnel}. {why}"


def _charts(results: dict[str, VariantResult], table: pd.DataFrame, best: dict[str, str], out_dir) -> list[str]:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    surface, ink, muted, grid = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
    colors = {"1": "#2a78d6", "2a": "#eb6834", "2b": "#1baf7a", "3": "#eda100"}
    names = []
    # 1) equity curves (one position, R) of each setup's best eligible variant
    fig, ax = plt.subplots(figsize=(10, 4.8), facecolor=surface)
    ax.set_facecolor(surface)
    for s in SETUPS:
        if s not in best:
            continue
        t = results[best[s]].trades
        t = t[t["taken"]].sort_values("signal_time")
        if t.empty:
            continue
        eq = t["r"].cumsum().to_numpy()
        ax.plot(t["signal_time"], eq, color=colors[s], lw=2, label=f"{s}: {best[s]}")
        ax.annotate(f"{s}  {eq[-1]:+.0f}R", (t["signal_time"].iloc[-1], eq[-1]), xytext=(4, 0),
                    textcoords="offset points", color=ink, fontsize=8, va="center")
    ax.axhline(0, color=muted, lw=0.8)
    ax.grid(color=grid, lw=0.6)
    ax.set_ylabel("cumulative R (one position at a time)", color=muted)
    ax.set_title("Equity in R of each setup's best eligible (M5) variant, development period", color=ink, fontsize=11)
    ax.legend(fontsize=7.5, frameon=False, loc="best")
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)
    fig.tight_layout()
    fig.savefig(out_dir / "06_equity.png", dpi=110, facecolor=surface)
    plt.close(fig)
    names.append("06_equity.png")
    # 2) expectancy +- CI for every variant, one panel per setup
    fig, axes = plt.subplots(1, 4, figsize=(13, 4.8), sharey=True, facecolor=surface)
    for ax, s in zip(axes, SETUPS):
        ax.set_facecolor(surface)
        g = table[(table["setup"] == s) & (table["n"] > 0)].sort_values("expectancy_R").reset_index(drop=True)
        for i, r in g.iterrows():
            c = colors[s] if r["eligible"] else "#9a9994"
            ax.plot([i, i], [r["ci95_low"], r["ci95_high"]], color=c, lw=1, alpha=0.6)
            ax.plot(i, r["expectancy_R"], "o", ms=4, color=c, mfc=c if r["eligible"] else surface)
        ax.axhline(0, color=muted, lw=0.8)
        ax.grid(axis="y", color=grid, lw=0.6)
        ax.set_title(f"Setup {s}", color=ink, fontsize=10)
        ax.set_xticks([])
        ax.set_xlabel("variants, sorted by expectancy", color=muted, fontsize=8)
        for sp in ("top", "right"):
            ax.spines[sp].set_visible(False)
    axes[0].set_ylabel("expectancy R per trade (95% CI)", color=muted)
    fig.suptitle("Expectancy of every variant: filled = eligible (M5 entry), hollow grey = comparison only",
                 color=ink, fontsize=11)
    fig.tight_layout()
    fig.savefig(out_dir / "06_expectancy.png", dpi=110, facecolor=surface)
    plt.close(fig)
    names.append("06_expectancy.png")
    return names


def build_results(results: list[VariantResult], table: pd.DataFrame, out_dir, runtime_s: float, smoke: bool) -> str:
    """Markdown for everything below the pre-registration marker."""
    by_name = {r.v.name: r for r in results}
    elig = table[table["eligible"]]
    best: dict[str, str] = {}
    for s in SETUPS:
        sel = table[(table["setup"] == s) & table["selected"]]
        top = top_variants(table, s, 1)
        if len(sel):
            best[s] = sel.iloc[0]["name"]
        elif len(top):
            best[s] = top.iloc[0]["name"]
    charts = _charts(by_name, table, best, out_dir)
    outcomes = {s: selection_outcome(table, s) for s in SETUPS}
    n_selected = int(table["selected"].sum())
    hi = _dev_bounds()[1]
    exit_after = sum(int((r.trades["taken"] & (r.trades["exit_time"] > hi)).sum()) for r in results)
    n_null = int(table["null_runs"].gt(0).sum())

    L = ["", "## Results — Part B (development period 2022-05-16 .. 2025-10-23)", ""]
    if smoke:
        L += ["**SMOKE RUN — a handful of variants and very few null repeats; not a result.**", ""]
    L += [
        f"Generated by `src/xauai/research.py` at {datetime.now():%Y-%m-%d %H:%M} (local), runtime {runtime_s / 60:.1f} min. "
        "Every number below comes from that script. **The test set (2025-10-24 .. 2026-10-23) has NOT been touched**: "
        f"no trade below has a signal time after {hi}.",
        "",
        f"Variants tried: **{len(table)}** (pre-registered: 208). Eligible for selection (M5 entries): **{len(elig)}** "
        "(pre-registered: 64). Because this many variants were tried, one or two can look good by pure luck.",
        "",
        "### In plain words",
        "",
    ]
    pos = int((elig["expectancy_R"] > 0).sum())
    L += [
        "- *R* is the size of the trade's own stop: -1R = a normal stop-out, +2R = won twice the risk. *Expectancy* "
        "is the average R per trade after spread, slippage and swap; above 0 means the rules made money in this "
        "period. The *95% CI* is the range the true average plausibly lies in; if it includes 0, the result could be luck.",
        f"- Of the {len(elig)} eligible M5 versions, {pos} had a positive average; the median eligible version "
        f"averaged {_f(elig['expectancy_R'].median())}R per trade.",
        "- To be selected a version needed at least 100 trades, a positive average in every calendar year "
        "2022-2025, and to beat at least 95% of 200 random-entry runs with the same sessions, directions and stops "
        f"(the random runs show what plain luck and volatility give). {n_null} version(s) got far enough to be "
        "compared with random entries.",
    ]
    if n_selected:
        L.append(f"- **{n_selected} version(s) passed** and go to the one-time test: "
                 + ", ".join(f"`{n}`" for n in table.loc[table["selected"], "name"])
                 + ". Passing here is not proof; only the untouched test year can confirm it.")
    else:
        L.append("- **No version passed, so nothing goes to the test set.** As exact rules with real costs, these "
                 "setups did not show an edge in 2022-2025. That is a valid, useful result.")
    L += ["", "### Selection outcome per setup", "",
          _md([{"setup": SETUP_NAMES[s], "outcome": outcomes[s][0], "details": outcomes[s][1]} for s in SETUPS]),
          "", "### Charts", ""]
    L += [f"![{c}]({c})" for c in charts]
    L += ["", "Equity chart: each setup's selected variant, or (if none was selected) its top eligible variant by "
          "expectancy among those with >= 100 trades — shown for comparison, not as a selection.", ""]

    L += ["### Top 3 eligible variants per setup", "",
          "Ranked by one-position expectancy among eligible variants with >= 100 trades (filled up with smaller ones "
          "only if fewer than 3 have that many). *One position* = a new signal is ignored while that variant's trade is "
          "open (used for selection and the test). *Every signal* = every signal is traded, overlapping allowed. "
          "PF = profit factor (gross won / gross lost). Max DD = largest peak-to-trough fall of the R equity curve. "
          "Cost sensitivity = the same variant re-run with total cost per trade 0.50 / 0.75 instead of >= 0.35.", ""]
    for s in SETUPS:
        L += [f"#### {SETUP_NAMES[s]}", ""]
        top = top_variants(table, s)
        if top.empty:
            L += ["_no eligible variant had any trade_", ""]
        for _, row in top.iterrows():
            L += [detail_md(by_name[row["name"]], row), ""]

    nt = table[table["null_runs"] > 0].sort_values("expectancy_R", ascending=False)
    L += ["### Null baseline (random entries)", "",
          "Computed only for eligible variants that already pass n >= 100 and every-year > 0. Each random run takes "
          "the same number of entries at random M5 bars in the same sessions with the same directions; each gets a "
          "(stop distance, target distance in R) pair drawn from the variant's own trades; same exit mode, same "
          "simulator, one position at a time. *Beats* = share of random runs whose expectancy is not above the "
          "variant's.", ""]
    L += [_md([{"variant": r["name"], "n": int(r["n"]), "exp R": _f(r["expectancy_R"]), "random runs": int(r["null_runs"]),
                "random mean R": _f(r["null_mean"]), "random 5th R": _f(r["null_p05"]), "random 95th R": _f(r["null_p95"]),
                "beats": _pct(r["beat_share"])} for _, r in nt.iterrows()]), ""]

    sens = table[table["eligible"] & (table["n"] >= config.SELECT_MIN_N)].sort_values(["setup", "expectancy_R"], ascending=[True, False])
    L += ["### Cost sensitivity (eligible variants with >= 100 trades)", "",
          _md([{"variant": r["name"], "n": int(r["n"]), "exp R base": _f(r["expectancy_R"]),
                "exp R @ 0.50": _f(r["exp_cost_0.50"]), "exp R @ 0.75": _f(r["exp_cost_0.75"]),
                "every signal base": _f(r["all_expectancy_R"]), "every signal @ 0.50": _f(r["all_exp_cost_0.50"]),
                "every signal @ 0.75": _f(r["all_exp_cost_0.75"])} for _, r in sens.iterrows()]), ""]

    L += ["### Cost as a share of the stop, per entry timeframe", "",
          "cost = spread (min $0.30) + $0.05 slippage, in price; SL distance = entry fill to stop. Over unique "
          "simulated trades (the same trade shared by several variants counts once). A 0.35 cost on a 1.00 stop "
          "means every trade starts 0.35R behind.", "", cost_share_md(results), ""]

    L += ["### Entry-timeframe comparison (M15 / M30 / H1 rows: comparison only — never selected or tested)", "",
          "One row per setup and entry timeframe, over its 16 variants (TP mode x trend x exit).", "",
          comparison_md(table), ""]

    L += ["### Skipped signals by reason", "",
          "Per base combination (no trend filter, hold; the exit mode does not change skipping). no_entry_bar = no M1 "
          "bar at the entry time; stale_after_gap = entry right after a > 25 h gap; entry_after_friday_close = entry "
          "at/after Friday 22:00; sl_behind_fill = the stop is on the wrong side of the fill after costs; no_target = "
          "no OB / FVG / owner / next level target; target_too_close_or_passed = target < 1R from the fill.", "",
          skipped_md(results), ""]

    L += ["### All variants", "",
          "Sorted by setup, then one-position expectancy. n taken = one position at a time; n signals = every simulated "
          "signal.", "", all_variants_md(table), ""]

    L += ["### Caveats", "",
          f"- Trades opened near 2025-10-23 may exit after it ({exit_after} taken trades across all variants exit after "
          f"{hi}); they are kept, with their full outcome, because their signal is in the development period.",
          "- Historical swap is scaled by price with the 2026-10-23 close as the reference (the current swap snapshot "
          "is from then), as in Phase 3. This uses one price from after the development period, only to scale swap.",
          "- Setup 3's 'lower high -> break' stage has no time limit once the lower high is known; only the retest "
          "after the break has the K = 12 bar window.",
          f"- {len(table)} variants were tried; with {len(elig)} eligible, a few can pass the development checks by luck. "
          "The test set is the only honest check.",
          "- **The test set has NOT been touched.** It is run once, in Part C, only for selected variants.", ""]
    L += test_section_md(table.loc[table["selected"], "name"].tolist())
    return "\n".join(L)


def write_report(results_md: str, path) -> None:
    """Keep everything up to and including MARKER from reports/06_setups.md; replace what follows."""
    src = (config.REPORTS_DIR / REPORT_NAME).read_text(encoding="utf-8")
    if MARKER not in src:
        raise RuntimeError(f"marker missing from {REPORT_NAME}")
    pre = src.split(MARKER)[0] + MARKER + "\n"
    path.write_text(pre + results_md, encoding="utf-8")


# --- Task 13: Part C (test set, one run) ---------------------------------------------------------
TEST_HEADING = "## Test set (Part C)"
NOT_RUN_TEXT = (
    "Not run: no setup was selected in Part B. Per the pre-registration, the test period "
    "2025-10-24 .. 2026-10-23 stays untouched."
)
MARKER_NAME = "06_test_done.json"


def _test_marker():
    return config.REPORTS_DIR / MARKER_NAME


def test_section_md(selected: list[str], results: pd.DataFrame | None = None) -> list[str]:
    """Lines of the Test set section: not-run text, pending text, or the one-time results table."""
    if results is not None:
        rows = [{
            "variant": r["name"], "n": int(r["n"]), "exp R": _f(r["expectancy_R"]),
            "95% CI": _ci(r["ci95_low"], r["ci95_high"]), "verdict": r["verdict"],
        } for _, r in results.iterrows()]
        body = [_md(rows), "",
                f"PASS = expectancy >= {config.TEST_MIN_EXP:+.2f}R, n >= {config.TEST_MIN_N} and CI low > "
                f"{config.TEST_MIN_CI_LO:+.2f}R. Bootstrap 95% CI, 10,000 resamples. Run once.", ""]
    elif not selected:
        body = [NOT_RUN_TEXT, ""]
    elif _test_marker().exists():
        body = [f"Run once; see reports/{MARKER_NAME}.", ""]
    else:
        body = ["Not yet run. Selected: " + ", ".join(f"`{n}`" for n in selected) + ".", ""]
    return ["", TEST_HEADING, ""] + body


def run_test_set(selected: pd.DataFrame, inp: Inputs) -> pd.DataFrame:
    """Test period TEST_START..TEST_END for each selected variant (one position at a time), net of costs.
    Verdict PASS needs expectancy >= TEST_MIN_EXP, n >= TEST_MIN_N and bootstrap CI low > TEST_MIN_CI_LO."""
    by_name = {v.name: v for v in variant_grid()}
    rows = []
    for name in selected["name"]:
        tr, _ = run_variant_detail(by_name[name], inp, config.TEST_START, config.TEST_END)
        r = tr.loc[tr["taken"].astype(bool), "r"].to_numpy(float)
        lo, hi = bootstrap_ci(r)
        exp = float(r.mean()) if len(r) else float("nan")
        ok = len(r) >= config.TEST_MIN_N and exp >= config.TEST_MIN_EXP and lo > config.TEST_MIN_CI_LO
        rows.append({"name": name, "n": len(r), "expectancy_R": exp, "ci95_low": lo, "ci95_high": hi,
                     "verdict": "PASS" if ok else "FAIL"})
    return pd.DataFrame(rows, columns=["name", "n", "expectancy_R", "ci95_low", "ci95_high", "verdict"])


def replace_test_section(report_path, lines: list[str]) -> None:
    """Replace (or append) the Test set section of the report, leaving everything else unchanged."""
    text = report_path.read_text(encoding="utf-8")
    new = "\n".join(lines).strip("\n") + "\n"
    i = text.find(TEST_HEADING)
    if i < 0:
        text = text.rstrip("\n") + "\n\n" + new
    else:
        j = text.find("\n## ", i + 1)
        text = text[:i] + new + (text[j:] if j >= 0 else "")
    report_path.write_text(text, encoding="utf-8")


def final_main() -> None:
    marker = _test_marker()
    if marker.exists():
        raise SystemExit(f"{marker} exists: the test set was already run once and must not be run again.")
    sel = json.loads((RESEARCH_DATA_DIR / "06_selected.json").read_text(encoding="utf-8"))["selected"]
    report = config.REPORTS_DIR / REPORT_NAME
    if not sel:
        replace_test_section(report, test_section_md([]))
        print(NOT_RUN_TEXT)
        return
    marker.write_text(
        json.dumps({"selected": sel, "timestamp": datetime.now().isoformat(timespec="seconds")}, indent=2),
        encoding="utf-8",
    )
    res = run_test_set(pd.DataFrame({"name": sel}), load_inputs())
    replace_test_section(report, test_section_md(sel, res))
    print(res.to_string(index=False))


def _log(msg: str, t0: float) -> None:
    print(f"[{(time.time() - t0) / 60:6.1f} min] {msg}", flush=True)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Part B: development backtests, null baseline, selection")
    ap.add_argument("--smoke", action="store_true",
                    help="run a handful of variants with 3 null repeats; writes to data/research/smoke/, not reports/")
    ap.add_argument("--final", action="store_true",
                    help="Part C: run the selected variants on the test period ONCE (refuses a second run)")
    args = ap.parse_args(argv)
    if args.final:
        return final_main()
    t0 = time.time()
    smoke = args.smoke
    out_dir = RESEARCH_DATA_DIR / "smoke" if smoke else config.REPORTS_DIR
    data_dir = RESEARCH_DATA_DIR / "smoke" if smoke else RESEARCH_DATA_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    repeats = 3 if smoke else config.NULL_REPEATS

    inp = load_inputs()
    _log("inputs loaded", t0)
    grid = variant_grid()
    if smoke:
        grid = [v for v in grid if v.name in SMOKE_VARIANTS]
    results: list[VariantResult] = []
    for i, v in enumerate(grid):
        tr, sk = run_variant_detail(v, inp, config.DEV_START, config.DEV_END)
        results.append(VariantResult(v, tr, sk))
        _log(f"{i + 1}/{len(grid)} {v.name}: {int(tr['taken'].sum())} taken / {len(tr)} signals", t0)
    assert_dev_only(results)

    table = build_table(results)
    need_sens = set(table.loc[table["eligible"] & (table["n"] >= config.SELECT_MIN_N), "name"])
    for s in SETUPS:
        need_sens |= set(top_variants(table, s)["name"])
    if smoke:
        need_sens = {r.v.name for r in results if len(r.trades)}
    for res in results:
        if res.v.name in need_sens:
            for c in config.SENSITIVITY_TOTAL_COSTS:
                res.sens[c] = run_variant_detail(res.v, inp, config.DEV_START, config.DEV_END, total_cost=c)[0]
            _log(f"cost sensitivity {res.v.name}", t0)
    assert_dev_only(results)

    need_null = set(table.loc[pre_null_pass(table), "name"])
    if smoke:
        need_null = {r.v.name for r in results if r.v.eligible and r.trades["taken"].any()}
    for res in results:
        if res.v.name in need_null:
            res.null = null_distribution(res.v, res.trades, inp, repeats=repeats)
            _log(f"null baseline {res.v.name}: {repeats} runs", t0)

    table = build_table(results)
    table.to_parquet(data_dir / "06_variants.parquet", index=False)
    sel = table.loc[table["selected"], "name"].tolist()
    (data_dir / "06_selected.json").write_text(
        json.dumps({"generated": datetime.now().isoformat(timespec="seconds"), "smoke": smoke, "selected": sel},
                   indent=2), encoding="utf-8")
    text = build_results(results, table, out_dir, time.time() - t0, smoke)
    write_report(text, out_dir / (("06_setups_smoke.md") if smoke else REPORT_NAME))
    _log(f"report written to {out_dir}; selected: {sel or 'none'}", t0)


if __name__ == "__main__":
    main()
