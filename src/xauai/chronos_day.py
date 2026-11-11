"""Chronos day trade: one trade per weekday, decision 10:00 Manila, time exit 12:00 London.

All times are naive broker server time. Local wall-clock times go local -> UTC -> server, so the
UK and US DST switches (different dates) are handled by the zones, not by fixed offsets.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from xauai import config, research
from xauai.indicators import atr
from xauai.labels import CostParams, SwapParams, gap_after_mask, just_before_close, rollovers, simulate_trade
from xauai.timeutil import TF_LEN, utc_to_server

_UTC = ZoneInfo("UTC")
_HOUR = pd.Timedelta(hours=1)
_MAX_START_LAG = pd.Timedelta(minutes=5)
_PREV_LOOKBACK = pd.Timedelta(hours=72)
DAY_COLS = [
    "day", "decision_time", "entry_time", "exit_time", "h1_last_open", "last_close", "atr", "horizon",
    "prev_exit_price",
]


def local_to_server(d: date, zone: str, hour: int) -> pd.Timestamp:
    """Local wall-clock `hour` on date `d` in `zone` -> naive server time."""
    local = datetime(d.year, d.month, d.day, hour, tzinfo=ZoneInfo(zone))
    utc = pd.Series([pd.Timestamp(local.astimezone(_UTC).replace(tzinfo=None))])
    return utc_to_server(utc).iloc[0]


def _first_at_or_after(times: np.ndarray, t: pd.Timestamp) -> int:
    return int(np.searchsorted(times, np.datetime64(t), side="left"))


def prev_weekday(d: date) -> date:
    """Previous Mon-Fri date (Monday -> Friday)."""
    p = d - timedelta(days=1)
    while p.weekday() >= 5:
        p -= timedelta(days=1)
    return p


def prev_exit_price(m1_t: np.ndarray, m1_close: np.ndarray, d: date) -> float:
    """Close of the last M1 bar strictly before the previous weekday's exit, within 72 h, else NaN."""
    pe = local_to_server(prev_weekday(d), *config.CHRONOS_EXIT)
    i = _first_at_or_after(m1_t, pe) - 1
    if i < 0 or pd.Timestamp(m1_t[i]) < pe - _PREV_LOOKBACK:
        return float("nan")
    return float(m1_close[i])


def day_table(h1: pd.DataFrame, m1: pd.DataFrame, start: str, end: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(days, skipped) for every Mon-Fri local PH date start..end inclusive.

    Missing decision H1 bar, entry/exit M1 bar or ATR means no trade that day (never shifted).
    """
    h1 = h1.reset_index(drop=True)
    h_t = h1["time"].to_numpy()
    h_atr = atr(h1["high"].to_numpy(float), h1["low"].to_numpy(float), h1["close"].to_numpy(float), 14)
    m_t = m1["time"].to_numpy()
    m_close = m1["close"].to_numpy(float)
    rows: list[dict] = []
    skipped: list[dict] = []

    def skip(day: date, reason: str) -> None:
        skipped.append({"day": day, "reason": reason})

    for ts in pd.date_range(start, end, freq="D"):
        if ts.weekday() >= 5:
            continue
        day = ts.date()
        dec = local_to_server(day, *config.CHRONOS_DECISION)
        ext = local_to_server(day, *config.CHRONOS_EXIT)
        # the bar closing exactly at the decision must exist
        i = _first_at_or_after(h_t, dec - _HOUR)
        if i >= len(h_t) or pd.Timestamp(h_t[i]) != dec - _HOUR:
            skip(day, "no_decision_bar")
            continue
        j = _first_at_or_after(m_t, dec)
        if j >= len(m_t) or pd.Timestamp(m_t[j]) - dec >= _MAX_START_LAG:
            skip(day, "no_entry_bar")
            continue
        k = _first_at_or_after(m_t, ext)
        if k >= len(m_t) or pd.Timestamp(m_t[k]) - ext >= _MAX_START_LAG:
            skip(day, "no_exit_bar")
            continue
        if np.isnan(h_atr[i]):
            skip(day, "atr_nan")
            continue
        entry, exit_t = pd.Timestamp(m_t[j]), pd.Timestamp(m_t[k])
        rows.append({
            "day": day, "decision_time": dec, "entry_time": entry, "exit_time": exit_t,
            "h1_last_open": pd.Timestamp(h_t[i]), "last_close": float(h1["close"].iloc[i]),
            "atr": float(h_atr[i]), "horizon": int(round((exit_t - entry) / _HOUR)),
            "prev_exit_price": prev_exit_price(m_t, m_close, day),
        })
    return pd.DataFrame(rows, columns=DAY_COLS), pd.DataFrame(skipped, columns=["day", "reason"])


# --- M1 day-trade simulator ---------------------------------------------------------------------

TRADE_COLS = [
    "day", "signal_time", "entry_time", "exit_time", "direction", "atr", "sl_dist", "cost", "entry", "sl", "tp",
    "exit_price", "exit_reason", "swap_r", "r_price", "r", "taken",
]
_EXIT_TP = {"tp1.5": 1.5, "tp2.0": 2.0}


def simulate_trail_trade(
    o: np.ndarray,
    h: np.ndarray,
    l: np.ndarray,
    c: np.ndarray,
    friday_close: np.ndarray,
    gap_after: np.ndarray,
    direction: int,
    fill: float,
    sl: float,
    be_at: float,
    trail_dist: float,
    stop_open: int | None = None,
) -> tuple[int, float, str, str]:
    """Break-even + trailing-stop trade starting at bar 0 (fill = bar 0 open + cost). Returns as labels.simulate_trade.

    The stop is `sl` until a bar's favourable extreme reaches `be_at`; from the next bar on it is
    max_d(fill, best - d*trail_dist) with best through the previous closed bar, never loosened. Each bar
    first checks the current stop (so a bar that also makes a new best is still a stop-out), then
    updates best/stop for the next bar. Reasons: sl, be_sl, trail_sl, gap_sl, friday, session_end,
    holiday, end_of_data.
    """
    d = direction
    n = len(o)
    stop = sl
    best = fill
    for j in range(n):
        if j > 0:
            if d * (o[j] - stop) <= 0:
                return j, o[j], "gap_sl", "open"
            if friday_close[j]:
                return j, o[j], "friday", "open"
            if j == stop_open:
                return j, o[j], "session_end", "open"
        if (l[j] <= stop) if d == 1 else (h[j] >= stop):
            why = "sl" if stop == sl else "be_sl" if stop == fill else "trail_sl"
            return j, stop, why, "intrabar"
        ext = h[j] if d == 1 else l[j]
        best = max(best, ext) if d == 1 else min(best, ext)
        if d * (best - be_at) >= 0:
            trail = best - d * trail_dist
            stop = max(stop, fill, trail) if d == 1 else min(stop, fill, trail)
        if gap_after[j]:
            return j, c[j], "holiday", "close"
    return n - 1, c[n - 1], "end_of_data", "close"


def simulate_days(
    days: pd.DataFrame,
    m1: pd.DataFrame,
    cost: CostParams,
    swap: SwapParams,
    m: float,
    exit_style: str,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Simulate one M1 trade per day row (needs a `direction` column). Returns (trades, skipped).

    exit_style: "tp1.5" / "tp2.0" (fixed TP at that multiple of the stop distance) or "be_trail".
    Stop distance = m x the day's ATR from the fill; R is in units of that distance, net of swap.
    """
    if exit_style not in (*_EXIT_TP, "be_trail"):
        raise ValueError(f"unknown exit_style {exit_style!r}")
    m1 = m1.reset_index(drop=True)
    t = m1["time"]
    tn = t.to_numpy()
    o, h, l, c = (m1[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    spread = m1["spread"].to_numpy(float)
    n = len(m1)
    friday_close = ((t.dt.day_of_week == 4) & (t.dt.hour >= config.FRIDAY_EXIT_HOUR)).to_numpy()
    gap_after = gap_after_mask(t)
    friday_idx, gap_idx = np.flatnonzero(friday_close), np.flatnonzero(gap_after)
    close_offset = just_before_close(TF_LEN["M1"])

    trades: list[dict] = []
    skipped: list[dict] = []
    for _, row in days.iterrows():
        d = int(row["direction"])
        et, xt = row["entry_time"], row["exit_time"]
        e = int(np.searchsorted(tn, et.to_datetime64()))
        x = int(np.searchsorted(tn, xt.to_datetime64()))
        reason = (
            "no_entry_bar" if e >= n or tn[e] != et.to_datetime64()
            else "stale_after_gap" if e > 0 and gap_after[e - 1]
            else "entry_after_friday_close" if friday_close[e]
            else "no_exit_after_entry" if x <= e or x >= n
            else None
        )
        dist = m * float(row["atr"])
        if reason is None and not dist > 0:
            reason = "sl_behind_fill"
        if reason:
            skipped.append({"day": row["day"], "reason": reason})
            continue

        k = cost.per_trade(spread[e])
        fill = o[e] + d * k
        sl = fill - d * dist
        bound = min(n - 1, x)
        f = np.searchsorted(friday_idx, e, side="right")
        if f < len(friday_idx):
            bound = min(bound, friday_idx[f])
        g = np.searchsorted(gap_idx, e)
        if g < len(gap_idx):
            bound = min(bound, gap_idx[g])
        s = slice(e, bound + 1)
        tp = np.nan
        if exit_style == "be_trail":
            j, px, why, when = simulate_trail_trade(
                o[s], h[s], l[s], c[s], friday_close[s], gap_after[s], d, fill, sl,
                fill + d * config.CHRONOS_BE_R * dist, dist, x - e,
            )
        else:
            tp = fill + d * _EXIT_TP[exit_style] * dist
            j, px, why, when = simulate_trade(
                o[s], h[s], l[s], c[s], friday_close[s], gap_after[s], 0, d, fill, sl, tp, None, x - e
            )
        j += e
        exit_time = t[j] if when == "open" else t[j] + close_offset
        _, weighted = rollovers(t[e], exit_time, swap.triple_weekday)
        swap_px = weighted * swap.points_per_night(d, float(row["last_close"])) * swap.point
        r_price = d * (px - fill) / dist
        trades.append({
            "day": row["day"], "signal_time": row["decision_time"], "entry_time": t[e], "exit_time": exit_time,
            "direction": d, "atr": float(row["atr"]), "sl_dist": dist, "cost": k, "entry": fill, "sl": sl,
            "tp": tp, "exit_price": px, "exit_reason": why, "swap_r": swap_px / dist, "r_price": r_price,
            "r": r_price + swap_px / dist, "taken": True,
        })
    return pd.DataFrame(trades, columns=TRADE_COLS), pd.DataFrame(skipped, columns=["day", "reason"])


# ---- Forecasts ---------------------------------------------------------------------------------

MARKER = "<!-- RESULTS BELOW: generated by src/xauai/chronos_day.py; everything above is the pre-registration -->"
REPORT_NAME = "08_chronos.md"
FORECAST_PATH =research.RESEARCH_DATA_DIR / "08_forecasts.parquet"  # development days only
FORECAST_COLS = ["day", "last_close", "median", "horizon", "model_id", "revision"]
_BATCH = 64


class Predictor(Protocol):
    model_id: str
    revision: str

    def predict_median(self, contexts: np.ndarray, horizons: np.ndarray) -> np.ndarray:
        """contexts (n, 512) float -> (n,) 0.5 quantile at each row's step h."""
        ...


class ChronosBoltPredictor:
    """Zero-shot Chronos-Bolt on CPU. chronos/torch/huggingface_hub are imported here only."""

    def __init__(self, model_id: str = config.CHRONOS_MODEL):
        import torch
        from chronos import BaseChronosPipeline
        from huggingface_hub import HfApi

        self.model_id = model_id
        try:
            self.revision = str(HfApi().model_info(model_id).sha)
        except Exception as exc:  # offline etc.
            print(f"WARNING: could not resolve revision of {model_id}: {exc}")
            self.revision = "unknown"
        self._torch = torch
        self._pipe = BaseChronosPipeline.from_pretrained(model_id, device_map="cpu", torch_dtype=torch.float32)

    def predict_median(self, contexts: np.ndarray, horizons: np.ndarray) -> np.ndarray:
        out = np.empty(len(contexts), dtype=np.float64)
        for s in range(0, len(contexts), _BATCH):
            ctx = self._torch.tensor(np.asarray(contexts[s:s + _BATCH], dtype=np.float32))
            hz = horizons[s:s + _BATCH]
            q, _ = self._pipe.predict_quantiles(
                ctx, prediction_length=int(hz.max()), quantile_levels=[0.5]
            )
            q = q.numpy()
            out[s:s + len(hz)] = q[np.arange(len(hz)), hz.astype(int) - 1, 0]
        return out


def _last_idx(days: pd.DataFrame, h1: pd.DataFrame) -> np.ndarray:
    """Index of the H1 bar with open == h1_last_open (inclusive upper bound), per day."""
    t = h1["time"].to_numpy()
    return np.searchsorted(t, days["h1_last_open"].to_numpy(), side="right") - 1


def warmup_mask(days: pd.DataFrame, h1: pd.DataFrame, n: int = config.CHRONOS_CONTEXT) -> np.ndarray:
    """True where fewer than n H1 bars exist with open <= h1_last_open (day is a warm-up skip)."""
    return _last_idx(days, h1) + 1 < n


def contexts_for(days: pd.DataFrame, h1: pd.DataFrame, n: int = config.CHRONOS_CONTEXT) -> np.ndarray:
    """(len(days), n) float64: the n closes ending at the bar opening at h1_last_open, inclusive."""
    close = h1["close"].to_numpy(dtype=np.float64)
    idx = _last_idx(days, h1)
    if (idx + 1 < n).any():
        raise ValueError("days with fewer than n H1 bars; drop them with warmup_mask first")
    return np.stack([close[i - n + 1:i + 1] for i in idx]) if len(idx) else np.empty((0, n))


def forecast_days(days: pd.DataFrame, h1: pd.DataFrame, predictor: Predictor) -> pd.DataFrame:
    """One row per day with a full context; warm-up days are dropped (see warmup_mask)."""
    d = days.loc[~warmup_mask(days, h1)].reset_index(drop=True)
    if d.empty:
        return pd.DataFrame(columns=FORECAST_COLS)
    med = predictor.predict_median(contexts_for(d, h1), d["horizon"].to_numpy(dtype=int))
    return pd.DataFrame({
        "day": d["day"], "last_close": d["last_close"].astype(float), "median": np.asarray(med, dtype=float),
        "horizon": d["horizon"].astype(int), "model_id": predictor.model_id, "revision": predictor.revision,
    })[FORECAST_COLS]


def load_or_build_forecasts(
    days: pd.DataFrame, h1: pd.DataFrame, predictor_factory: Callable[[], Predictor], path=FORECAST_PATH,
    model_id: str = config.CHRONOS_MODEL, revision: str | None = None,
) -> pd.DataFrame:
    """Reuse the cache when model_id (and revision, if given) match and every forecastable day is in
    it; else build with predictor_factory() and save. Returns rows for the requested days only."""
    path = Path(path)
    want = days.loc[~warmup_mask(days, h1), "day"]
    if path.exists():
        cache = pd.read_parquet(path)
        ok = len(cache) > 0 and (cache["model_id"] == model_id).all()
        if ok and revision is not None:
            ok = bool((cache["revision"] == revision).all())
        if ok and set(want) <= set(cache["day"]):
            return cache[cache["day"].isin(set(want))].reset_index(drop=True)
    fc = forecast_days(days, h1, predictor_factory())
    path.parent.mkdir(parents=True, exist_ok=True)
    fc.to_parquet(path, index=False)
    return fc


# ---- Directions, rivals, grid, selection -------------------------------------------------------

from dataclasses import dataclass  # noqa: E402
from itertools import product  # noqa: E402

_FLAT_ATR = 0.25
_STOP_MULTS = (1.5, 2.0, 3.0)
_EXITS = ("tp1.5", "tp2.0", "be_trail")


@dataclass(frozen=True)
class ChronosVariant:
    m: float
    exit_style: str
    flat: bool

    @property
    def name(self) -> str:
        return f"m{self.m:.1f}|{self.exit_style}|{'flat' if self.flat else 'all'}"


def chronos_grid() -> list[ChronosVariant]:
    """18 versions in the order m -> exit -> flat (off before on)."""
    return [ChronosVariant(m, e, f) for m, e, f in product(_STOP_MULTS, _EXITS, (False, True))]


def chronos_directions(days: pd.DataFrame, fc: pd.DataFrame, flat: bool) -> pd.DataFrame:
    """days joined to the forecast on `day`, plus direction = sign(median - last_close).

    A zero sign is dropped; with `flat`, |median - last_close| < 0.25 x atr is dropped too.
    """
    f = fc[["day", "median"]]
    d = days.merge(f, on="day", how="inner")
    diff = d["median"].to_numpy(float) - d["last_close"].to_numpy(float)
    keep = diff != 0
    if flat:
        keep &= np.abs(diff) >= _FLAT_ATR * d["atr"].to_numpy(float)
    d = d.loc[keep].copy()
    d["direction"] = np.sign(diff[keep]).astype(int)
    return d.reset_index(drop=True)


def overnight_directions(days: pd.DataFrame) -> pd.Series:
    """sign(last_close - prev_exit_price); a zero or NaN move is left out (no trade)."""
    s = np.sign(days["last_close"].astype(float) - days["prev_exit_price"].astype(float))
    return s[s.notna() & (s != 0)].astype(int)


def coin_flip_exps(
    days_traded: pd.DataFrame, m1: pd.DataFrame, cost: CostParams, swap: SwapParams,
    v: ChronosVariant, repeats: int, seed: int,
) -> np.ndarray:
    """Expectancy of each of `repeats` runs with a random +-1 direction per day (same m and exit).

    Both directions are simulated once per day; a run just picks one per day, which is the same as
    simulating that run's random directions.
    """
    rng = np.random.default_rng(seed)
    r = {}
    for d in (1, -1):
        tr, _ = simulate_days(days_traded.assign(direction=d), m1, cost, swap, v.m, v.exit_style)
        r[d] = tr.set_index("day")["r"]
    both = pd.concat([r[1].rename("up"), r[-1].rename("down")], axis=1).dropna()
    if both.empty:
        return np.full(repeats, np.nan)
    up, down = both["up"].to_numpy(float), both["down"].to_numpy(float)
    out = np.empty(repeats)
    for i in range(repeats):
        pick = rng.integers(0, 2, size=len(up)).astype(bool)
        out[i] = float(np.where(pick, up, down).mean())
    return out


def hit_rate(days: pd.DataFrame, m1: pd.DataFrame) -> float:
    """Share of days whose direction matches sign(M1 open at exit_time - M1 open at entry_time): the forecast's
    call on the raw move to the time exit, whatever the trade's stop or target did. Zero moves count as misses."""
    if days.empty:
        return float("nan")
    tn = m1["time"].to_numpy()
    o = m1["open"].to_numpy(float)
    e = np.searchsorted(tn, days["entry_time"].to_numpy())
    x = np.searchsorted(tn, days["exit_time"].to_numpy())
    return float((np.sign(o[x] - o[e]) == days["direction"].to_numpy()).mean())


def _breakeven_wr(r: pd.Series) -> float:
    w, l = r[r > 0], r[r < 0]
    if w.empty or l.empty:
        return float("nan")
    al = abs(float(l.mean()))
    return al / (float(w.mean()) + al)


def run_chronos_variant(
    v: ChronosVariant, days: pd.DataFrame, fc: pd.DataFrame, m1: pd.DataFrame,
    cost: CostParams, swap: SwapParams, with_rivals: bool = True,
    repeats: int = config.NULL_REPEATS, seed: int | None = None,
) -> dict:
    """flat_metrics of the variant's trades plus rivals (beat_share, overnight_exp/n), hit_rate, breakeven_wr, trades."""
    if seed is None:
        seed = research.NULL_SEED + 3000 + chronos_grid().index(v)
    dd = chronos_directions(days, fc, v.flat)
    trades, skipped = simulate_days(dd, m1, cost, swap, v.m, v.exit_style)
    out = research.flat_metrics(trades)
    out["name"] = v.name
    out["n_skipped"] = len(skipped)
    out["skips"] = {str(k): int(n) for k, n in skipped["reason"].value_counts().items()}
    joined = days.merge(fc[["day", "median"]], on="day", how="inner")
    diff = joined["median"].to_numpy(float) - joined["last_close"].to_numpy(float)
    out["n_zero_forecast"] = int((diff == 0).sum())
    out["n_flat_dropped"] = int(((diff != 0) & (np.abs(diff) < _FLAT_ATR * joined["atr"].to_numpy(float))).sum()) if v.flat else 0
    out["hit_rate"] = hit_rate(dd[dd["day"].isin(set(trades["day"]))], m1) if len(trades) else float("nan")
    out["breakeven_wr"] = _breakeven_wr(trades["r"]) if len(trades) else float("nan")
    out.update(beat_share=float("nan"), overnight_exp=float("nan"), overnight_n=0)
    if with_rivals and len(trades):
        traded = dd[dd["day"].isin(set(trades["day"]))]
        exps = coin_flip_exps(traded, m1, cost, swap, v, repeats, seed)
        out["beat_share"] = research.beat_share(out["expectancy_R"], exps)
        od = overnight_directions(traded)
        if len(od):
            ov, _ = simulate_days(traded.loc[od.index].assign(direction=od), m1, cost, swap, v.m, v.exit_style)
            out["overnight_exp"] = float(ov["r"].mean()) if len(ov) else float("nan")
            out["overnight_n"] = len(ov)
    out["trades"] = trades
    return out


def chronos_select(table: pd.DataFrame) -> pd.DataFrame:
    """Adds `fail` (first failed of n / years / null / overnight; "" = passes) and `selected`
    (True only for the passing row with the highest expectancy_R)."""
    out = table.copy()
    years_ok = np.ones(len(out), dtype=bool)
    for y in research.SELECT_YEARS:
        years_ok &= (out[f"exp_{y}"].astype(float) > 0).to_numpy()
    exp = out["expectancy_R"].astype(float)
    ov = out["overnight_exp"].astype(float)
    conds = [
        (out["n"] < config.SELECT_MIN_N).to_numpy(),
        ~years_ok,
        ~(out["beat_share"].astype(float) >= config.NULL_BEAT).to_numpy(),
        ~(exp > ov).to_numpy(),  # NaN overnight fails
    ]
    out["fail"] = np.select(conds, ["n", "years", "null", "overnight"], default="")
    out["selected"] = False
    ok = out.index[out["fail"] == ""]
    if len(ok):
        out.loc[exp.loc[ok].idxmax(), "selected"] = True
    return out


# ---- Runner and report -------------------------------------------------------------------------

import argparse  # noqa: E402
import json  # noqa: E402
import time  # noqa: E402

RESEARCH_DATA_DIR = research.RESEARCH_DATA_DIR
SMOKE_DIR = "smoke_chronos"
SMOKE_REPORT = "08_chronos_smoke.md"
SMOKE_VARIANTS = ("m2.0|tp2.0|all", "m2.0|be_trail|flat", "m3.0|tp1.5|all")
SMOKE_DAYS = 60
SMOKE_REPEATS = 3
PRIOR_TRIED, PRIOR_ELIGIBLE = 208 + 144, 64 + 72  # batches 1 and 2
SIM_SKIP_REASONS = (
    "no_entry_bar", "stale_after_gap", "entry_after_friday_close", "no_exit_after_entry", "sl_behind_fill",
)
_CHECK_ORDER = {"": 4, "overnight": 3, "null": 2, "years": 1, "n": 0}


def _wilson(p: float, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson 95% interval for a share p out of n."""
    if n <= 0 or pd.isna(p):
        return float("nan"), float("nan")
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * float(np.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / d
    return c - h, c + h


def _cost_params(inp, total_cost: float = 0.35) -> CostParams:
    return CostParams(spread_floor=total_cost - config.SLIPPAGE, slippage=config.SLIPPAGE, point=inp.info["point"])


def _swap_params(inp) -> SwapParams:
    m1 = inp.bars["M1"]
    ref = float(m1.loc[m1["time"] <= research._cut(), "close"].iloc[-1])  # price-scaled swap, as research does
    return SwapParams.from_symbol_info(inp.info, reference_close=ref)


def dev_days(h1: pd.DataFrame, m1: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, int]:
    """(forecastable development days, day_table skips, number of warm-up days dropped). Never past DEV_END."""
    days, skipped = day_table(h1, m1, config.DEV_START, config.DEV_END)
    wm = warmup_mask(days, h1) if len(days) else np.zeros(0, dtype=bool)
    out = days.loc[~wm].reset_index(drop=True)
    if len(out) and pd.Timestamp(out["day"].max()) > pd.Timestamp(config.DEV_END):
        raise AssertionError("development day after DEV_END")
    return out, skipped, int(wm.sum())


def _cached(days: pd.DataFrame, path, model_id: str) -> pd.DataFrame | None:
    """Cache rows for `days`, or None when missing, from another model, or incomplete (never writes)."""
    path = Path(path)
    if not path.exists():
        return None
    cache = pd.read_parquet(path)
    if len(cache) == 0 or not (cache["model_id"] == model_id).all() or not set(days["day"]) <= set(cache["day"]):
        return None
    out = cache[cache["day"].isin(set(days["day"]))].reset_index(drop=True)
    j = days[["day", "last_close", "horizon"]].merge(out[["day", "last_close", "horizon"]], on="day", suffixes=("", "_c"))
    if (np.abs(j["last_close"] - j["last_close_c"]) > 1e-9).any() or (j["horizon"] != j["horizon_c"]).any():
        raise SystemExit(f"forecast cache {path} does not match the current data (last_close or horizon differ): "
                         "delete it and rerun --forecasts")
    return out


def assert_dev_only_chronos(trades: pd.DataFrame, fc: pd.DataFrame | None = None) -> None:
    """Every trade signal/exit time and every forecast day must be <= DEV_END 23:59:59 (test year untouched)."""
    hi = pd.Timestamp(config.DEV_END) + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)
    for col in ("signal_time", "exit_time"):
        if len(trades) and pd.Timestamp(trades[col].max()) > hi:
            raise RuntimeError(f"trade {col} after {hi}: test period leaked")
    if fc is not None and len(fc) and pd.Timestamp(max(fc["day"])) > hi:
        raise RuntimeError(f"forecast day after {hi}: test period leaked")


def results_table(res: list[dict]) -> pd.DataFrame:
    """One row per version (trades dropped), hit-rate Wilson CI added, then chronos_select columns."""
    rows = []
    for r in res:
        row = {k: v for k, v in r.items() if k not in ("trades", "skips")}
        for reason in SIM_SKIP_REASONS:
            row[f"skip_{reason}"] = int(r["skips"].get(reason, 0))
        row["hit_lo"], row["hit_hi"] = _wilson(row["hit_rate"], int(row["n"]))
        rows.append(row)
    return chronos_select(pd.DataFrame(rows))


def closest_failing(table: pd.DataFrame) -> pd.Series | None:
    """Failing version that got furthest through the checks (n, years, null, overnight); ties by expectancy."""
    f = table[table["fail"] != ""]
    if f.empty:
        return None
    key = f.assign(_k=f["fail"].map(_CHECK_ORDER)).sort_values(
        ["_k", "expectancy_R"], ascending=[False, False], na_position="last")
    return key.iloc[0]


def _fail_text(r: pd.Series) -> str:
    yrs = ", ".join(f"{y}: {research._f(r[f'exp_{y}'])}" for y in research.SELECT_YEARS)
    return {
        "n": f"only {int(r['n'])} trades (needs >= {config.SELECT_MIN_N})",
        "years": f"expectancy is not above zero in every year ({yrs})",
        "null": f"it beat only {research._pct(r['beat_share'])} of its coin flips (needs >= {research._pct(config.NULL_BEAT)})",
        "overnight": f"expectancy {research._f(r['expectancy_R'])}R is not above the overnight rival's "
                     f"{research._f(r['overnight_exp'])}R",
    }[r["fail"]]


def _pkg_version() -> str:
    try:
        from importlib.metadata import version

        return version("chronos-forecasting")
    except Exception:
        return "not installed"


def _equity_chart(trades: pd.DataFrame, name: str, out_dir) -> str:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    surface, ink, muted, grid = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
    t = trades.sort_values("signal_time", kind="stable")
    eq = t["r"].cumsum().to_numpy()
    fig, ax = plt.subplots(figsize=(10, 4.4), facecolor=surface)
    ax.set_facecolor(surface)
    ax.plot(np.arange(1, len(eq) + 1), eq, color="#2a78d6", lw=2)
    ax.axhline(0, color=muted, lw=0.8)
    ax.grid(color=grid, lw=0.6)
    ax.set_xlabel("trade number", color=muted)
    ax.set_ylabel("cumulative R", color=muted)
    ax.set_title(f"Equity in R, best version by expectancy: {name} (development period)", color=ink, fontsize=11)
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)
    fig.tight_layout()
    fig.savefig(Path(out_dir) / "08_equity.png", dpi=110, facecolor=surface)
    plt.close(fig)
    return "08_equity.png"


def cost_sensitivity(
    table: pd.DataFrame, days: pd.DataFrame, fc: pd.DataFrame, m1: pd.DataFrame, inp, k: int = 3
) -> pd.DataFrame:
    """Top-k versions by expectancy re-simulated at total cost 0.50 / 0.75."""
    by = {v.name: v for v in chronos_grid()}
    swap = _swap_params(inp)
    rows = []
    top = table[table["n"] > 0].sort_values("expectancy_R", ascending=False).head(k)
    for _, r in top.iterrows():
        v = by[r["name"]]
        row = {"version": v.name, "n": int(r["n"]), "base 0.35": research._f(r["expectancy_R"])}
        dd = chronos_directions(days, fc, v.flat)
        for c in config.SENSITIVITY_TOTAL_COSTS:
            tr, _ = simulate_days(dd, m1, _cost_params(inp, c), swap, v.m, v.exit_style)
            assert_dev_only_chronos(tr)
            row[f"total {c:.2f}"] = research._f(float(tr["r"].mean()) if len(tr) else float("nan"))
        rows.append(row)
    return pd.DataFrame(rows)


def test_section_md(selected: list[str]) -> list[str]:
    body = (
        [f"Not yet run. Selected: {', '.join(f'`{n}`' for n in selected)}. It is run once, on "
         f"{config.TEST_START} .. {config.TEST_END}, with `--final`.", ""]
        if selected else
        ["Not run: no version passed the development selection. Per the pre-registration, the test period "
         f"{config.TEST_START} .. {config.TEST_END} stays untouched.", ""]
    )
    return ["", research.TEST_HEADING, ""] + body


def build_chronos_results(
    table: pd.DataFrame, sens: pd.DataFrame, fc: pd.DataFrame, skipped: pd.DataFrame, warmup_n: int,
    n_days: int, out_dir, trades: dict[str, pd.DataFrame], runtime_s: float, smoke: bool,
    pkg_version: str | None = None,
) -> str:
    """The results markdown placed after MARKER. Every number comes from the arguments."""
    f, pct, md = research._f, research._pct, research._md
    n_v = len(table)
    sel = table.loc[table["selected"], "name"].tolist()
    ok = table[table["n"] > 0]
    beat_coin = int((table["beat_share"] >= config.NULL_BEAT).sum())
    beat_ov = int((table["expectancy_R"] > table["overnight_exp"]).sum())
    pos = int((table["expectancy_R"] > 0).sum())
    hit_sig = int((table["hit_lo"] > 0.5).sum())
    med = float(ok["expectancy_R"].median()) if len(ok) else float("nan")
    tried, elig = PRIOR_TRIED + n_v, PRIOR_ELIGIBLE + n_v
    best = ok.sort_values("expectancy_R", ascending=False).iloc[0] if len(ok) else None
    close = closest_failing(table)
    runs = SMOKE_REPEATS if smoke else config.NULL_REPEATS
    L = ["", f"## Results (development period {config.DEV_START} .. {config.DEV_END})", ""]
    if smoke:
        L += [f"**SMOKE RUN**: first {SMOKE_DAYS} forecast days and {n_v} versions only. Not a result.", ""]
    L += [f"Runtime {runtime_s / 60:.1f} min. Versions tried here: **{n_v}** (pre-registered: 18), all eligible.", ""]
    L += ["### In plain words", ""]
    passed = ("Yes: " + ", ".join(f"`{s}`" for s in sel)) if sel else "No. Nothing is sent to the test year"
    L += [
        f"- Did any version pass the pre-registered selection? **{passed}**.",
        f"- Did Chronos beat a coin flip? {beat_coin} of {n_v} versions beat at least {pct(config.NULL_BEAT)} of their "
        f"{runs} coin-flip runs.",
        f"- Did Chronos beat simply reading the overnight move? {beat_ov} of {n_v} versions have a higher expectancy "
        "than the overnight rival on the same days.",
        f"- {pos} of {n_v} versions have a positive expectancy over the whole period. The median version has an "
        f"expectancy of {f(med)}R per trade.",
        "- The direction hit rate (move from entry to the time exit) has a 95% interval entirely above 50% in "
        f"{hit_sig} of {n_v} versions.",
        (f"- Smoke: {n_v} versions only; the full run has 18; cumulative across batches 1 to 3 in the full run: "
         f"**{PRIOR_TRIED + 18}** versions tried, **{PRIOR_ELIGIBLE + 18}** eligible."
         if smoke else
         f"- Cumulative across batches 1 to 3: **{tried}** versions tried, **{elig}** eligible. Some pass by luck."), "",
    ]
    L += ["### Selection outcome", ""]
    if sel:
        r = table.set_index("name").loc[sel[0]]
        L += [f"Selected for the test year (one version, best expectancy among those passing): `{sel[0]}` with "
              f"{f(r['expectancy_R'])}R over {int(r['n'])} trades. The test year is run once with `--final`.", ""]
    else:
        L += ["No version passed all four checks, so the batch is **FAIL** and the test year stays locked.", ""]
        if close is not None:
            L += [f"Closest failing version: `{close['name']}` (expectancy {f(close['expectancy_R'])}R, n = "
                  f"{int(close['n'])}). It failed the **{close['fail']}** check: {_fail_text(close)}.", ""]
    counts = table["fail"].replace("", "pass").value_counts()
    L += ["First failed check, counted over versions: "
          + ", ".join(f"{k}: {int(v)}" for k, v in counts.items()) + ".", ""]
    L += ["### All versions", ""]
    t = table.sort_values("expectancy_R", ascending=False, na_position="last")
    perf = [{
        "version": r["name"], "n": int(r["n"]), "win%": pct(r["win_rate"]), "break-even win%": pct(r["breakeven_wr"]),
        "exp R": f(r["expectancy_R"]), "95% CI": research._ci(r["ci95_low"], r["ci95_high"]),
        "total R": f(r["total_R"], 1), "PF": f(r["profit_factor"], 2), "max DD R": f(r["max_dd_R"], 1),
        "trades/mo": f(r["trades_per_month"], 1), "losing streak": f(r["max_losing_streak"], 0),
        "cost % of stop": pct(r["mean_cost_over_sl"]),
        **{str(y): f(r[f"exp_{y}"]) for y in research.SELECT_YEARS},
    } for _, r in t.iterrows()]
    riv = [{
        "version": r["name"], "n": int(r["n"]), "beat coin": pct(r["beat_share"]),
        "overnight exp R": f(r["overnight_exp"]), "overnight n": int(r["overnight_n"]), "hit rate": pct(r["hit_rate"]),
        "hit 95% CI": "—" if pd.isna(r["hit_lo"]) else f"[{pct(r['hit_lo'])}, {pct(r['hit_hi'])}]",
        "fails": r["fail"] or "pass",
    } for _, r in t.iterrows()]
    L += ["Performance (R, net of costs and swap):", "", md(perf), "", "Rivals and hit rate:", "", md(riv), "",
          "Expectancy per year by signal date (2022 is a partial year). Break-even win rate comes from the version's own "
          "average win and loss. Hit rate: share of traded days where the forecast direction matched the sign of the "
          "raw move from entry to the time exit, with a Wilson 95% interval, against 50%.", ""]
    L += ["### Cost sensitivity", ""]
    L += [md(sens.to_dict("records")) if len(sens) else "_none_", "",
          "Top 3 versions by expectancy at total cost 0.50 and 0.75 (spread floor + 0.05 slippage); the other "
          "versions are not re-run at these costs.", ""]
    L += ["### Equity of the best version", ""]
    if best is not None and best["name"] in trades:
        _equity_chart(trades[best["name"]], best["name"], out_dir)
        L += [f"![equity of {best['name']}](08_equity.png)", "",
              f"Best by expectancy: `{best['name']}` ({f(best['expectancy_R'])}R per trade, total "
              f"{f(best['total_R'], 1)}R, max drawdown {f(best['max_dd_R'], 1)}R). Cumulative R by trade, "
              "development period.", ""]
    else:
        L += ["_no trades_", ""]
    first = fc["day"].min() if len(fc) else None
    L += ["### First forecast day and skipped days", ""]
    reasons = skipped["reason"].value_counts() if len(skipped) else pd.Series(dtype=int)
    L += [f"- First forecast day: **{first}** ({len(fc)} forecast days).",
          f"- Warm-up days (fewer than {config.CHRONOS_CONTEXT} closed H1 bars before the day): **{warmup_n}**.",
          "- Days with no trade by reason (day table): "
          + (", ".join(f"{k}: {int(v)}" for k, v in reasons.items()) if len(reasons) else "none") + ".",
          "", "Days lost per version (simulator skips by reason, exact-zero forecasts, flat-filter drops; also in "
          "`08_variants.parquet`):", ""]
    skip_cols = [f"skip_{x}" for x in SIM_SKIP_REASONS]
    L += [md([{"version": r["name"], **{c[5:]: int(r[c]) for c in skip_cols},
               "zero_forecast": int(r["n_zero_forecast"]), "flat_filter": int(r["n_flat_dropped"])}
              for _, r in t.iterrows()]), ""]
    models = ", ".join(sorted(map(str, fc["model_id"].unique()))) if len(fc) else "—"
    revs = ", ".join(sorted(map(str, fc["revision"].unique()))) if len(fc) else "—"
    L += ["### Model", "", f"- Model: `{models}`", f"- Hugging Face revision: `{revs}`",
          f"- chronos-forecasting package: `{pkg_version if pkg_version is not None else _pkg_version()}`", ""]
    L += ["### Caveats", "",
          "- Chronos was pretrained on data that may include gold prices from the development period, so a "
          "development pass is weaker evidence than usual. The test year is the verdict that counts.",
          "- The 18 versions share one forecast table, so they are highly correlated, not 18 independent tries; the "
          "cumulative count is still stated.",
          "- be_trail in practice trails 1R behind the best price from +1R (the trail distance equals the starting "
          "stop), so the stop rarely sits exactly at break-even.",
          "- Win rate is not the criterion; expectancy in R after costs and swap is.",
          "- **The test set has NOT been touched.** It is run once, with `--final`, only for a selected version.", ""]
    L += test_section_md(sel)
    return "\n".join(L)


def write_chronos_report(results_md: str, path) -> None:
    """Keep everything up to and including MARKER from reports/08_chronos.md byte for byte (line endings
    untouched); write it plus `results_md` to `path`."""
    with open(config.REPORTS_DIR / REPORT_NAME, encoding="utf-8", newline="") as fh:
        src = fh.read()
    if MARKER not in src:
        raise RuntimeError(f"marker missing from {REPORT_NAME}")
    pre = src.split(MARKER)[0] + MARKER + "\n"
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(pre + results_md)


def _log(msg: str, t0: float) -> None:
    print(f"[{(time.time() - t0) / 60:6.1f} min] {msg}", flush=True)


def run(
    mode: str, predictor_factory: Callable[[], Predictor] = ChronosBoltPredictor,
    model_id: str = config.CHRONOS_MODEL,
) -> None:
    """mode: 'forecasts', 'smoke' or 'full'."""
    t0 = time.time()
    if mode == "full" and not Path(FORECAST_PATH).exists():
        raise SystemExit("forecast cache missing: run --forecasts first")
    inp = research.load_inputs()
    h1, m1 = inp.bars["H1"], inp.bars["M1"]
    days, skipped, warm_n = dev_days(h1, m1)
    _log(f"{len(days)} development days (+{warm_n} warm-up)", t0)

    if mode == "forecasts":
        fc = load_or_build_forecasts(days, h1, predictor_factory, FORECAST_PATH, model_id=model_id)
        first = fc["day"].min() if len(fc) else None
        print(f"forecasts: {len(fc)} days, first {first}, model {fc['model_id'].iloc[0] if len(fc) else None}, "
              f"revision {fc['revision'].iloc[0] if len(fc) else None}, runtime {(time.time() - t0) / 60:.1f} min")
        return

    smoke = mode == "smoke"
    out_dir = RESEARCH_DATA_DIR / SMOKE_DIR if smoke else config.REPORTS_DIR
    data_dir = RESEARCH_DATA_DIR / SMOKE_DIR if smoke else RESEARCH_DATA_DIR
    grid = chronos_grid()
    repeats = config.NULL_REPEATS
    if smoke:
        days = days.head(SMOKE_DAYS)
        grid = [v for v in grid if v.name in SMOKE_VARIANTS]
        repeats = SMOKE_REPEATS
        fc = _cached(days, FORECAST_PATH, model_id)
        if fc is None:
            data_dir.mkdir(parents=True, exist_ok=True)
            fc = load_or_build_forecasts(days, h1, predictor_factory, data_dir / "08_forecasts_smoke.parquet",
                                         model_id=model_id)
    else:
        fc = _cached(days, FORECAST_PATH, model_id)
        if fc is None:
            raise SystemExit("forecast cache missing or incomplete: run --forecasts first")
    out_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    cost, swap = _cost_params(inp), _swap_params(inp)
    res = []
    for i, v in enumerate(grid):
        r = run_chronos_variant(v, days, fc, m1, cost, swap, True, repeats)
        res.append(r)
        _log(f"{i + 1}/{len(grid)} {v.name}: n={r['n']}", t0)
    assert_dev_only_chronos(pd.concat([r["trades"] for r in res], ignore_index=True), fc)
    table = results_table(res)
    sens = cost_sensitivity(table, days, fc, m1, inp)
    table.to_parquet(data_dir / "08_variants.parquet", index=False)
    sel = table.loc[table["selected"], "name"].tolist()
    (data_dir / "08_selected.json").write_text(
        json.dumps({"generated": pd.Timestamp.now().isoformat(timespec="seconds"), "smoke": smoke, "selected": sel},
                   indent=2), encoding="utf-8")
    text = build_chronos_results(
        table, sens, fc, skipped, warm_n, len(days), out_dir, {r["name"]: r["trades"] for r in res},
        time.time() - t0, smoke,
    )
    write_chronos_report(text, out_dir / (SMOKE_REPORT if smoke else REPORT_NAME))
    _log(f"report written to {out_dir}; selected: {sel or 'none'}", t0)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Batch 3: Chronos H1 day trade")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--forecasts", action="store_true", help="build or reuse the development forecast cache")
    g.add_argument("--smoke", action="store_true",
                   help=f"{SMOKE_DAYS} days, 3 versions; writes only to data/research/{SMOKE_DIR}/")
    args = ap.parse_args(argv)
    run("forecasts" if args.forecasts else "smoke" if args.smoke else "full")


if __name__ == "__main__":
    main()
