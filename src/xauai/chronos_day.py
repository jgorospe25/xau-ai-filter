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

FORECAST_PATH = research.RESEARCH_DATA_DIR / "08_forecasts.parquet"  # development days only
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
    hit = np.sign(trades["exit_price"] - trades["entry"]) == trades["direction"] if len(trades) else pd.Series(dtype=bool)
    out["hit_rate"] = float(hit.mean()) if len(trades) else float("nan")
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
