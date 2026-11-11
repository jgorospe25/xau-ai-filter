"""Level-based setups 1 (sweep, reclaim, retest) and 3 (lower high, break, retest) -> signal tables.

Everything is written for the SELL at a zone above; buys run the same code on mirrored prices
(high' = -low, low' = -high, open'/close' negated, zone price negated), which turns a zone below
into a zone above and swing lows into swing highs.

No lookahead: at bar i (decided at its close) only bars <= i, ATR[i], swings with
known_at <= close of bar i, and level refreshes with valid_from <= time[i] are used.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from xauai import config
from xauai.indicators import atr
from xauai.levels import merge_levels
from xauai.sim import SIGNAL_COLS
from xauai.structure import swing_points
from xauai.timeutil import TF_LEN

ZONE_COLS = ["zone_id", "side", "price", "kinds", "valid_from", "valid_to", "broken_at"]
_ATR_N = 14
_H1 = TF_LEN["H1"]
_FAR = np.datetime64("2200-01-01", "ns")


def _nat_to_far(s: pd.Series) -> np.ndarray:
    """datetime64[ns] array with NaT (open-ended) replaced by a far-future sentinel."""
    x = pd.to_datetime(s).to_numpy().astype("datetime64[ns]")
    return np.where(np.isnat(x), _FAR, x)


def _infer_tf(bars: pd.DataFrame) -> str:
    """Timeframe name from the smallest positive spacing of bar times."""
    d = np.diff(bars["time"].to_numpy())
    d = d[d > np.timedelta64(0, "ns")]
    if len(d):
        step = pd.Timedelta(d.min())
        for name, length in TF_LEN.items():
            if length == step:
                return name
    raise ValueError("cannot infer the timeframe of `bars`; pass tf=")


def _tolerance(bars: pd.DataFrame) -> np.ndarray:
    """TOL_ATR x ATR14 of the entry TF per bar (NaN in the ATR warm-up -> no trigger)."""
    h, l, c = (bars[k].to_numpy(dtype=float) for k in ("high", "low", "close"))
    return config.TOL_ATR * atr(h, l, c, _ATR_N)


def _h1_atr_series(h1: pd.DataFrame) -> pd.Series:
    """ATR14(H1) indexed by H1 bar open time."""
    h, l, c = (h1[k].to_numpy(dtype=float) for k in ("high", "low", "close"))
    return pd.Series(atr(h, l, c, _ATR_N), index=pd.DatetimeIndex(h1["time"]))


def _h1_atr_at(h1_atr: pd.Series, t: pd.Timestamp) -> float:
    """ATR14(H1) of the latest H1 bar whose close (open + 1h) <= t; NaN if none."""
    closes = (h1_atr.index + _H1).to_numpy()
    k = int(np.searchsorted(closes, t.to_datetime64(), side="right")) - 1
    return float(h1_atr.iloc[k]) if k >= 0 else np.nan


def live_zones(levels: pd.DataFrame, bars: pd.DataFrame, h1_atr: pd.Series, tf: str | None = None) -> pd.DataFrame:
    """Merged level zones per refresh, with the time each zone was broken (NaT if never in its window).

    `h1_atr` = ATR14(H1) indexed by H1 bar open time. Per refresh: ref price = open of the first
    entry-TF bar at/after valid_from; merge distance from ATR14(H1) of the latest H1 bar closed by
    valid_from (NaN -> refresh skipped). A zone is broken at the close of the first window bar that
    closes beyond its price by more than the tolerance (above: close > price + tol). Sweeps and
    touches do not consume a zone.
    """
    tf = tf or _infer_tf(bars)
    t = bars["time"].to_numpy()
    o, c = bars["open"].to_numpy(dtype=float), bars["close"].to_numpy(dtype=float)
    tol = _tolerance(bars)
    n = len(t)
    rows: list[tuple] = []
    zid = 0
    for vf, g in levels.groupby("valid_from", sort=True):
        vf = pd.Timestamp(vf)
        vt = g["valid_to"].iloc[0]
        a = int(np.searchsorted(t, vf.to_datetime64(), side="left"))
        e = n if pd.isna(vt) else int(np.searchsorted(t, pd.Timestamp(vt).to_datetime64(), side="left"))
        ha = _h1_atr_at(h1_atr, vf)
        if a >= e or np.isnan(ha):
            continue
        for z in merge_levels(g, float(o[a]), ha).itertuples(index=False):
            f = 1.0 if z.side == 1 else -1.0
            with np.errstate(invalid="ignore"):
                hit = np.flatnonzero(f * c[a:e] > f * z.price + tol[a:e])
            broken = pd.Timestamp(t[a + hit[0]]) + TF_LEN[tf] if len(hit) else pd.NaT
            rows.append((zid, int(z.side), float(z.price), z.kinds, vf, vt, broken))
            zid += 1
    out = pd.DataFrame(rows, columns=ZONE_COLS)
    for col in ("valid_from", "valid_to", "broken_at"):
        out[col] = pd.to_datetime(out[col])
    return out


def _zone_spans(zones: pd.DataFrame, t: np.ndarray, close_t: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Per zone: first window bar `a` and `end` (exclusive) of the bars on which it can trigger."""
    a = np.searchsorted(t, zones["valid_from"].to_numpy(), side="left")
    e = np.searchsorted(t, _nat_to_far(zones["valid_to"]), side="left")
    kb = np.searchsorted(close_t, _nat_to_far(zones["broken_at"]), side="left")
    end = np.minimum(e, kb + 1)  # the breaking bar is the last live bar (kb = n when never broken)
    return a.astype(int), end.astype(int)


def _oriented(bars: pd.DataFrame, d: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(open, high, close) in sell orientation: raw for d=-1, mirrored (high = -low) for d=+1."""
    o, h, l, c = (bars[k].to_numpy(dtype=float) for k in ("open", "high", "low", "close"))
    return (o, h, c) if d == -1 else (-o, -l, -c)


def _sweep_retest(
    o: np.ndarray, h: np.ndarray, c: np.ndarray, tol: np.ndarray, p: float, a: int, end: int, k: int
) -> list[tuple[int, int]]:
    """Setup 1 at one zone above `p` (sell orientation) over bars [a, end): list of (retest r, sweep j).

    Sweep j: open < p, high >= p - tol, close < p. Retest r in (j, j+k]: high >= p - tol and
    close < p. Pending sweep dies when a bar closes above its tip or k bars pass; a new sweep after
    that replaces it. The retest bar is not also a new sweep.
    """
    with np.errstate(invalid="ignore"):
        reach = h[a:end] >= p - tol[a:end]
        cand = np.flatnonzero((o[a:end] < p) & reach & (c[a:end] < p))
    if not len(cand):
        return []
    out: list[tuple[int, int]] = []
    j, tip = -1, 0.0
    for i in range(a + int(cand[0]), end):
        if j >= 0 and (i - j > k or c[i] > tip):
            j = -1
        hit = bool(reach[i - a]) and c[i] < p
        if j >= 0 and hit:
            out.append((i, j))
            j = -1
            continue
        if hit and o[i] < p:
            j, tip = i, h[i]
    return out


def setup1(entry: pd.DataFrame, tf: str, levels: pd.DataFrame, h1: pd.DataFrame) -> pd.DataFrame:
    """Setup 1 signals (both directions) on entry-TF bars: sweep, close back inside, retest within K."""
    entry = entry.reset_index(drop=True)
    t = entry["time"].to_numpy()
    close_t = (entry["time"] + TF_LEN[tf]).to_numpy()
    tol = _tolerance(entry)
    zones = live_zones(levels, entry, _h1_atr_series(h1), tf)
    a, end = _zone_spans(zones, t, close_t)
    cands: list[dict] = []
    for d, side in ((-1, 1), (1, -1)):
        o, h, c = _oriented(entry, d)
        for zi in np.flatnonzero(zones["side"].to_numpy() == side):
            z = zones.iloc[zi]
            p = -d * float(z["price"])
            for r, j in _sweep_retest(o, h, c, tol, p, int(a[zi]), int(end[zi]), config.RETEST_BARS):
                cands.append(_cand(r, d, -d * float(h[j : r + 1].max()), z, np.nan))
    return _finish(cands, entry, tf, "1")


def _swing_arrays(entry: pd.DataFrame, tf: str, d: int) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Sell-orientation swings: (hi_px, hi_bar) of the swing high confirmed AT each bar (NaN/-1 if none),
    and (lo_px, lo_conf): every swing low price with the bar index at which it becomes known."""
    t = entry["time"].to_numpy()
    close_t = (entry["time"] + TF_LEN[tf]).to_numpy()
    sw = swing_points(entry, tf)
    conf = np.searchsorted(close_t, sw["known_at"].to_numpy(), side="left")
    bar = np.searchsorted(t, sw["bar_time"].to_numpy(), side="left")
    px = -d * sw["price"].to_numpy(dtype=float)
    hi = (sw["kind"].to_numpy() == -d).astype(bool)  # sell: swing highs; buy: swing lows (mirrored)
    n = len(t)
    hi_px, hi_bar = np.full(n, np.nan), np.full(n, -1, dtype=int)
    hi_px[conf[hi]], hi_bar[conf[hi]] = px[hi], bar[hi]
    order = np.argsort(conf[~hi], kind="stable")
    return hi_px, hi_bar, px[~hi][order], conf[~hi][order]


def _last_known(px: np.ndarray, conf: np.ndarray, n: int) -> np.ndarray:
    """Price of the latest swing known at each bar's close (conf <= i), NaN before the first."""
    out = np.full(n, np.nan)
    out[conf] = px  # conf is sorted and unique per kind
    return pd.Series(out).ffill().to_numpy()


def _lower_high_break(
    h: np.ndarray, c: np.ndarray, tol: np.ndarray, p: float, a: int, end: int, k: int,
    hi_px: np.ndarray, hi_bar: np.ndarray, last_lo: np.ndarray,
) -> list[tuple[int, float]]:
    """Setup 3 at one zone above `p` (sell orientation): list of (retest r, lower-high price).

    Touch j in [a, end): high >= p - tol, close <= p; pending until `end`. Lower high = first swing
    high confirmed at a bar < end with swing bar > j and price < high[j]. Break b: close < latest
    known swing low S. Retest r in (b, b+k]: high >= S - tol, close < S. A close above the lower
    high, or k bars after b, resets. Once the lower high is known the sequence may run past `end`.
    """
    with np.errstate(invalid="ignore"):
        cand = np.flatnonzero((h[a:end] >= p - tol[a:end]) & (c[a:end] <= p))
    if not len(cand):
        return []
    out: list[tuple[int, float]] = []
    state, j, hj, lh, b, s = 0, -1, 0.0, 0.0, -1, 0.0
    i, n = a + int(cand[0]), len(c)
    while i < n:
        if state == 0 and i >= end:
            break
        if state >= 2 and c[i] > lh:
            state = 0
        if state == 3:
            if i - b > k:
                state = 0
            elif h[i] >= s - tol[i] and c[i] < s:
                out.append((i, lh))
                state = 0
                i += 1
                continue
        if state == 1:
            if i >= end:
                state = 0
            elif hi_bar[i] > j and hi_px[i] < hj:
                state, lh = 2, hi_px[i]
        if state == 2 and c[i] < last_lo[i]:
            state, b, s = 3, i, last_lo[i]
        if state == 0 and i < end and h[i] >= p - tol[i] and c[i] <= p:
            state, j, hj = 1, i, h[i]
        i += 1
    return out


def _owner_targets(levels: pd.DataFrame, kind: str) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(valid_from, valid_to (NaT -> max), price) of one level kind, sorted by valid_from."""
    x = levels[levels["kind"] == kind].sort_values("valid_from")
    vf = pd.to_datetime(x["valid_from"]).to_numpy().astype("datetime64[ns]")
    return vf, _nat_to_far(x["valid_to"]), x["price"].to_numpy(dtype=float)


def _owner_target(
    at: np.datetime64, cr: float, d: int, tgt: tuple[np.ndarray, np.ndarray, np.ndarray],
    lo_px: np.ndarray, lo_conf: np.ndarray, r: int,
) -> float:
    """Sell orientation: nearer of PDL (valid at `at`) and the highest known swing low in (PDL, close).

    `cr` = oriented close of the signal bar. NaN if no PDL or close is already at/below it.
    """
    vf, vt, px = tgt
    q = int(np.searchsorted(vf, at, side="right")) - 1
    if q < 0 or not at < vt[q]:
        return np.nan
    pdl = -d * px[q]
    if cr <= pdl:
        return np.nan
    known = lo_px[: int(np.searchsorted(lo_conf, r, side="right"))]
    between = known[(known > pdl) & (known < cr)]
    return -d * (float(between.max()) if len(between) else pdl)


def setup3(entry: pd.DataFrame, tf: str, levels: pd.DataFrame, h1: pd.DataFrame, d1: pd.DataFrame) -> pd.DataFrame:
    """Setup 3 signals (both directions): touch a zone, lower high, break the last swing low, retest.

    target_owner = PDL (sell) / PDH (buy) from the `levels` rows valid at signal_time, or the nearer
    known entry-TF swing low/high between the signal close and it. `d1` is accepted for the
    interface; the PDL/PDH come from the level table (same D1 data, already no-lookahead).
    """
    del d1
    entry = entry.reset_index(drop=True)
    t = entry["time"].to_numpy()
    close_t = (entry["time"] + TF_LEN[tf]).to_numpy()
    n = len(t)
    tol = _tolerance(entry)
    zones = live_zones(levels, entry, _h1_atr_series(h1), tf)
    a, end = _zone_spans(zones, t, close_t)
    cands: list[dict] = []
    for d, side, kind in ((-1, 1, "pdl"), (1, -1, "pdh")):
        _, h, c = _oriented(entry, d)
        hi_px, hi_bar, lo_px, lo_conf = _swing_arrays(entry, tf, d)
        last_lo = _last_known(lo_px, lo_conf, n)
        tgt = _owner_targets(levels, kind)
        for zi in np.flatnonzero(zones["side"].to_numpy() == side):
            z = zones.iloc[zi]
            p = -d * float(z["price"])
            hits = _lower_high_break(h, c, tol, p, int(a[zi]), int(end[zi]), config.RETEST_BARS, hi_px, hi_bar, last_lo)
            for r, lh in hits:
                owner = _owner_target(t[r], c[r], d, tgt, lo_px, lo_conf, r)
                cands.append(_cand(r, d, -d * lh, z, owner))
    return _finish(cands, entry, tf, "3")


def _cand(r: int, d: int, sl: float, z: pd.Series, owner: float) -> dict:
    return {
        "r": r, "direction": d, "sl": sl, "level_kind": z["kinds"], "level_price": float(z["price"]),
        "ref_time": z["valid_from"], "target_owner": owner,
    }


def _finish(cands: list[dict], entry: pd.DataFrame, tf: str, setup: str) -> pd.DataFrame:
    """One signal per bar and direction (earliest zone valid_from, then nearer SL); entry = next bar open."""
    if not cands:
        return _empty()
    t = entry["time"].to_numpy()
    c = entry["close"].to_numpy(dtype=float)
    x = pd.DataFrame(cands)
    x = x[x["r"] + 1 < len(t)]
    if x.empty:
        return _empty()
    x["_dist"] = (x["sl"] - c[x["r"].to_numpy()]).abs()
    x = x.sort_values(["r", "direction", "ref_time", "_dist"], kind="stable")
    x = x.drop_duplicates(["r", "direction"], keep="first")
    r = x["r"].to_numpy()
    out = pd.DataFrame(
        {
            "signal_time": pd.to_datetime(t[r]),
            "entry_time": pd.to_datetime(t[r + 1]),
            "direction": x["direction"].to_numpy(dtype=int),
            "sl": x["sl"].to_numpy(dtype=float),
            "setup": setup,
            "entry_tf": tf,
            "trigger_tf": None,
            "level_kind": x["level_kind"].to_numpy(),
            "level_price": x["level_price"].to_numpy(dtype=float),
            "ref_time": pd.to_datetime(x["ref_time"].to_numpy()),
            "target_ob": np.nan,
            "target_fvg": np.nan,
            "target_owner": x["target_owner"].to_numpy(dtype=float),
        }
    )
    return out.sort_values(["signal_time", "direction"], kind="stable").reset_index(drop=True)[SIGNAL_COLS]


def _empty() -> pd.DataFrame:
    out = pd.DataFrame({col: pd.Series(dtype=object) for col in SIGNAL_COLS})
    for col in ("signal_time", "entry_time", "ref_time"):
        out[col] = pd.to_datetime(out[col])
    for col in ("sl", "level_price", "target_ob", "target_fvg", "target_owner"):
        out[col] = out[col].astype(float)
    out["direction"] = out["direction"].astype(int)
    return out
