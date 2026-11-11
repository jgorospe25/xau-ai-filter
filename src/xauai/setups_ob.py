"""Setups 2a (rejection at a level, target the H1 OB / FVG) and 2b (pullback from an H1 OB).

Same mirroring scheme as `setups`: 2a is written for the SELL at a zone above and 2b for the BUY
from a bullish OB; the other direction runs the same code on negated prices (high' = -low,
low' = -high, open'/close' negated, zone/OB prices negated).

No lookahead: at bar i (decided at its close) only bars <= i, ATR values known by its close,
swings with known_at <= close of bar i, level refreshes with valid_from <= time[i], and OBs/FVGs
valid at the time of use (2a: entry_time; 2b: OB known_at <= bar close < invalid_at).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from xauai import config
from xauai.setups import _finish, _h1_atr_series, _oriented, _tolerance, _zone_spans, live_zones
from xauai.structure import swing_points
from xauai.timeutil import TF_LEN
from xauai.zones import valid_at

_H1 = TF_LEN["H1"]


def _norm(z: pd.DataFrame, end_col: str) -> pd.DataFrame:
    """OB/FVG frame with datetime/float/int dtypes (empty frames may come with object columns)."""
    z = z.copy()
    for col in ("known_at", end_col):
        z[col] = pd.to_datetime(z[col])
    for col in ("top", "bottom", "mid"):
        if col in z.columns:
            z[col] = z[col].astype(float)
    z["direction"] = z["direction"].astype(int)
    return z


def _target(z: pd.DataFrame, at: pd.Timestamp, end_col: str, col: str, close: float, d: int) -> float:
    """Sell (d=-1): max `col` among bullish zones valid at `at` with col < close. Buy mirrors
    (bearish zones, min col > close). NaN if none."""
    v = valid_at(z, at, end_col)
    px = v.loc[v["direction"] == -d, col].to_numpy(dtype=float)
    px = px[px < close] if d == -1 else px[px > close]
    if not len(px):
        return np.nan
    return float(px.max()) if d == -1 else float(px.min())


def setup2a(
    trigger: pd.DataFrame, trigger_tf: str, m5: pd.DataFrame, levels: pd.DataFrame,
    obs: pd.DataFrame, gaps: pd.DataFrame, h1: pd.DataFrame, zones: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Setup 2a signals (both directions): rejection of a live level on the trigger TF, entry on M5.

    Sell at a zone above `p` (tolerance = TOL_ATR x ATR14(trigger TF)): rejection bar j opens below
    p, its high reaches p - tol, it closes below p, and its upper wick (high - max(open, close)) is
    >= REJECT_WICK x its range (range > 0). SL = high[j]. entry_time = first M5 bar opening at/after
    the close of j (none -> no signal). target_ob = max top of bullish OBs valid at entry_time below
    close[j]; target_fvg = max mid of bullish FVGs valid at entry_time below close[j] (NaN if none;
    emitted anyway). Buys mirror toward bearish OBs/FVGs above.
    `zones` = optional precomputed `live_zones(levels, trigger, _h1_atr_series(h1), trigger_tf)`.
    """
    trigger = trigger.reset_index(drop=True)
    t = trigger["time"].to_numpy()
    close_t = (trigger["time"] + TF_LEN[trigger_tf]).to_numpy()
    tol = _tolerance(trigger)
    if zones is None:
        zones = live_zones(levels, trigger, _h1_atr_series(h1), trigger_tf)
    a, end = _zone_spans(zones, t, close_t)
    m5t = m5["time"].to_numpy()
    obs, gaps = _norm(obs, "invalid_at"), _norm(gaps, "filled_at")
    raw_c = trigger["close"].to_numpy(dtype=float)
    cands: list[dict] = []
    for d, side in ((-1, 1), (1, -1)):
        o, h, c = _oriented(trigger, d)
        lo = trigger["low"].to_numpy(dtype=float) if d == -1 else -trigger["high"].to_numpy(dtype=float)
        rng = h - lo
        shape = (rng > 0) & (h - np.maximum(o, c) >= config.REJECT_WICK * rng)
        info: dict[int, tuple] = {}
        for zi in np.flatnonzero(zones["side"].to_numpy() == side):
            z = zones.iloc[zi]
            p = -d * float(z["price"])
            s = slice(int(a[zi]), int(end[zi]))
            with np.errstate(invalid="ignore"):
                hit = (o[s] < p) & (h[s] >= p - tol[s]) & (c[s] < p) & shape[s]
            for j in int(a[zi]) + np.flatnonzero(hit):
                if j not in info:
                    k = int(np.searchsorted(m5t, close_t[j], side="left"))
                    if k >= len(m5t):
                        info[j] = None
                    else:
                        et = pd.Timestamp(m5t[k])
                        info[j] = (
                            et,
                            _target(obs, et, "invalid_at", "top" if d == -1 else "bottom", raw_c[j], d),
                            _target(gaps, et, "filled_at", "mid", raw_c[j], d),
                        )
                if info[j] is None:
                    continue
                et, tob, tfvg = info[j]
                cands.append(
                    {
                        "r": j, "direction": d, "sl": -d * float(h[j]), "level_kind": z["kinds"],
                        "level_price": float(z["price"]), "ref_time": z["valid_from"], "target_owner": np.nan,
                        "entry_time": et, "target_ob": tob, "target_fvg": tfvg,
                    }
                )
    return _finish(cands, trigger, "M5", "2a", trigger_tf)


def _h1_atr_at_close(h1: pd.DataFrame, close_t: np.ndarray) -> np.ndarray:
    """Per bar: ATR14(H1) of the latest H1 bar closed by that bar's close (NaN if none)."""
    s = _h1_atr_series(h1)
    k = np.searchsorted((s.index + _H1).to_numpy(), close_t, side="right") - 1
    return np.where(k >= 0, s.to_numpy()[np.maximum(k, 0)], np.nan)


def _latest_swing(entry: pd.DataFrame, tf: str, kind: int, d: int) -> tuple[np.ndarray, np.ndarray]:
    """Per bar i: (oriented price, bar index) of the latest swing of `kind` known by close of i (NaN/-1)."""
    t = entry["time"].to_numpy()
    close_t = (entry["time"] + TF_LEN[tf]).to_numpy()
    sw = swing_points(entry, tf)
    sw = sw[sw["kind"] == kind]
    conf = np.searchsorted(close_t, sw["known_at"].to_numpy(), side="left")
    bar = np.searchsorted(t, sw["bar_time"].to_numpy(), side="left").astype(float)
    n = len(t)
    px, bi = np.full(n, np.nan), np.full(n, np.nan)
    px[conf], bi[conf] = d * sw["price"].to_numpy(dtype=float), bar  # conf unique per kind
    px, bi = pd.Series(px).ffill().to_numpy(), pd.Series(bi).ffill().fillna(-1).to_numpy()
    return px, bi.astype(int)


def _ob_episode(
    low: np.ndarray, c: np.ndarray, tol: np.ndarray, reset: np.ndarray, lh_px: np.ndarray,
    lh_bar: np.ndarray, top: float, s0: int, stop: int, k: int,
) -> tuple[int, int] | None:
    """Setup 2b at one bullish OB (buy orientation) over bars [s0, stop): (episode start e, retest r).

    Episode starts at the first bar with low <= top. Lower high = latest known swing high with
    bar >= e. Break b: close > it. Retest r in (b, b+k]: low <= level + tol and close > level.
    A close > `reset` (top + OB_RESET_ATR x ATR14(H1)) before a trade ends the episode (a retest
    on that bar still trades); the next bar with low <= top starts a fresh one. A break that
    expires after k bars uses up its swing: a new break needs a newer swing high.
    """
    i = s0
    while i < stop:
        with np.errstate(invalid="ignore"):
            hit = np.flatnonzero(low[i:stop] <= top)
        if not len(hit):
            return None
        e = i = i + int(hit[0])
        b, lvl, used = -1, np.nan, e - 1
        while i < stop:
            if b >= 0 and i - b > k:
                b = -1
            if b >= 0 and low[i] <= lvl + tol[i] and c[i] > lvl:
                return e, i
            if c[i] > reset[i]:
                break
            if b < 0 and lh_bar[i] > used and c[i] > lh_px[i]:
                b, lvl, used = i, lh_px[i], lh_bar[i]
            i += 1
        i += 1
    return None


def setup2b(entry: pd.DataFrame, tf: str, obs: pd.DataFrame, h1: pd.DataFrame) -> pd.DataFrame:
    """Setup 2b signals: buy from bullish H1 OBs, sell from bearish ones, on entry-TF bars.

    Per OB: the first entry-TF bar opening at/after known_at must close above the OB top
    (approached from above), else the OB is skipped. Bars are used while their close < invalid_at.
    Break of the latest swing high since the episode start, retest within K (see `_ob_episode`);
    entry = next bar open; SL = lowest low from the episode start to the retest bar. At most one
    signal per ob_id. level_kind = "ob", level_price = OB top (bottom for bearish), ref_time = known_at.
    """
    entry = entry.reset_index(drop=True)
    t = entry["time"].to_numpy()
    close_t = (entry["time"] + TF_LEN[tf]).to_numpy()
    n = len(t)
    tol = _tolerance(entry)
    h1_atr = _h1_atr_at_close(h1, close_t)
    obs = _norm(obs, "invalid_at")
    cands: list[dict] = []
    for d in (1, -1):
        low = entry["low"].to_numpy(dtype=float) if d == 1 else -entry["high"].to_numpy(dtype=float)
        c = d * entry["close"].to_numpy(dtype=float)
        lh_px, lh_bar = _latest_swing(entry, tf, d, d)  # buy: swing highs; sell: swing lows (mirrored)
        for ob in obs[obs["direction"] == d].itertuples(index=False):
            top = ob.top if d == 1 else -ob.bottom
            s0 = int(np.searchsorted(t, ob.known_at.to_datetime64(), side="left"))
            stop = n if pd.isna(ob.invalid_at) else int(np.searchsorted(close_t, ob.invalid_at.to_datetime64(), side="left"))
            if s0 >= stop or not c[s0] > top:
                continue
            reset = top + config.OB_RESET_ATR * h1_atr
            hit = _ob_episode(low, c, tol, reset, lh_px, lh_bar, top, s0, stop, config.RETEST_BARS)
            if hit is None:
                continue
            e, r = hit
            cands.append(
                {
                    "r": r, "direction": d, "sl": d * float(low[e : r + 1].min()), "level_kind": "ob",
                    "level_price": float(ob.top if d == 1 else ob.bottom), "ref_time": ob.known_at,
                    "target_owner": np.nan,
                }
            )
    return _finish(cands, entry, tf, "2b")
