"""ICT entry models (batch 2): Model 1 (failed break -> MSS -> FVG limit), Model 3 (Model 1 + OTE)
and Model 5 (failed breakout of the Asian-range box, retest, market entry).

Written for the SELL at a zone above; buys run the same code on mirrored prices (as `setups`):
open'/close' negated, high' = -low, low' = -high, zone price negated. Oriented prices are turned back
into real ones with real = -d * oriented.

No lookahead: the failed break j uses only bar j and the zone live at j; the swing low to break is the
latest one known at the close of j; the MSS bar i, the FVG, SL and leg use bars [j, i] only. The
limit order is placed at the close of i.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from xauai import config
from xauai.indicators import atr
from xauai.setups import _empty as _empty_market
from xauai.setups import _finish, _last_known, _tolerance, _zone_spans
from xauai.sim import LIMIT_SIGNAL_COLS
from xauai.structure import swing_points
from xauai.timeutil import TF_LEN, local_windows

_ATR_N = 14


def displacement(bars: pd.DataFrame, d: int) -> np.ndarray:
    """Bool per bar: candle in direction `d` (d * (close - open) > 0) with range >= DISPLACEMENT_MULT x
    the mean range of the previous DISPLACEMENT_N bars (current bar excluded). False in warm-up."""
    o, h, l, c = (bars[k].to_numpy(dtype=float) for k in ("open", "high", "low", "close"))
    rng = h - l
    prev = pd.Series(rng).rolling(config.DISPLACEMENT_N).mean().shift(1).to_numpy()
    with np.errstate(invalid="ignore"):
        ok = (d * (c - o) > 0) & (rng >= config.DISPLACEMENT_MULT * prev)
    return np.asarray(ok & ~np.isnan(prev), dtype=bool)


def _oriented4(bars: pd.DataFrame, d: int) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """(open, high, low, close) in sell orientation: raw for d=-1, mirrored for d=+1."""
    o, h, l, c = (bars[k].to_numpy(dtype=float) for k in ("open", "high", "low", "close"))
    return (o, h, l, c) if d == -1 else (-o, -l, -h, -c)


def _trade_swing(bars: pd.DataFrame, tf: str, d: int, close_t: np.ndarray) -> np.ndarray:
    """Oriented price of the latest trade-side swing (sell: swing low; buy: swing high) known at
    each bar's close; NaN before the first."""
    n = len(close_t)
    sw = swing_points(bars, tf)
    sw = sw[sw["kind"] == d]
    conf = np.searchsorted(close_t, sw["known_at"].to_numpy(), side="left")
    keep = conf < n
    return _last_known(-d * sw["price"].to_numpy(dtype=float)[keep], conf[keep], n)


def _fvg(h: np.ndarray, low: np.ndarray, j: int, i: int) -> int:
    """Latest m in [j, i] (m >= 2) with low[m-2] > high[m] (sell orientation); -1 if none."""
    for m in range(i, max(j, 2) - 1, -1):
        if low[m - 2] > h[m]:
            return m
    return -1


def _model1_zone(
    o: np.ndarray, h: np.ndarray, low: np.ndarray, c: np.ndarray, tol: np.ndarray, disp: np.ndarray,
    swing: np.ndarray, p: float, a: int, end: int, k: int,
) -> list[dict]:
    """Model 1 at one zone above `p` (sell orientation), failed breaks in [a, end). Oriented values."""
    n = len(c)
    with np.errstate(invalid="ignore"):
        fb = (o[a:end] < p) & (h[a:end] >= p - tol[a:end]) & (c[a:end] < p) & ~np.isnan(swing[a:end])
    cand = a + np.flatnonzero(fb)  # failed breaks with a known trade-side swing
    out: list[dict] = []
    q0 = 0
    while q0 < len(cand):
        j = int(cand[q0])
        q0 += 1
        s, tip = swing[j], h[j]
        seen = bool(disp[j])
        # Current behaviour: if the failed-break close c[j] is already beyond the swing s, no check requires
        # c[j] >= s; any later close beyond s after a displacement bar counts as the MSS. Rare, left as is.
        mss = -1
        stop = min(j + k, n - 1)
        q = j + 1
        while q <= stop:
            if c[q] > tip:
                break  # cancelled at q
            seen = seen or bool(disp[q])
            if seen and c[q] < s:
                mss = q
                break
            q += 1
        if mss < 0:
            resume = q + 1 if q <= stop else stop + 1  # bar after the cancel or expiry bar
        else:
            i = mss
            m = _fvg(h, low, j, i)
            sl = float(h[j : i + 1].max())
            if m >= 0 and c[i] < h[m] and sl > h[m]:  # sl == limit would be a zero-risk order
                leg = float(low[j : i + 1].min())
                out.append({"j": j, "i": i, "m": m, "sl": sl, "leg_end": leg})
            resume = i + 1
        q0 = int(np.searchsorted(cand, resume, side="left"))  # later failed breaks while pending are ignored
    return out


def model1(entry: pd.DataFrame, tf: str, zones: pd.DataFrame) -> pd.DataFrame:
    """Model 1 limit-order signals (both directions): failed break at a live zone, MSS through the
    swing known at the failed break (with a displacement candle in [j, i]) within K bars, limit at the
    near edge of the latest FVG in [j, i]. `zones` = `setups.live_zones(...)`."""
    entry = entry.reset_index(drop=True)
    n = len(entry)
    t = entry["time"].to_numpy()
    close_t = (entry["time"] + TF_LEN[tf]).to_numpy()
    if n == 0 or zones.empty:
        return _empty()
    tol = _tolerance(entry)
    hr, lr, cr = (entry[x].to_numpy(dtype=float) for x in ("high", "low", "close"))
    atr14 = atr(hr, lr, cr, _ATR_N)
    a, end = _zone_spans(zones, t, close_t)
    k = config.RETEST_BARS
    cands: list[dict] = []
    for d, side in ((-1, 1), (1, -1)):
        o, h, low, c = _oriented4(entry, d)
        disp = displacement(entry, d)
        swing = _trade_swing(entry, tf, d, close_t)
        for zi in np.flatnonzero(zones["side"].to_numpy() == side):
            z = zones.iloc[zi]
            p = -d * float(z["price"])
            for x in _model1_zone(o, h, low, c, tol, disp, swing, p, int(a[zi]), int(end[zi]), k):
                i, m, sl = x["i"], x["m"], x["sl"]
                lim, far, leg = float(h[m]), float(low[m - 2]), x["leg_end"]
                height = sl - leg
                ote_lo, ote_hi = leg + config.OTE_LO * height, leg + config.OTE_HI * height
                last = min(i + k, n - 1)
                above = np.flatnonzero(c[i + 1 : last + 1] > sl)
                q = i + 1 + int(above[0]) if len(above) else last
                cands.append(
                    {
                        "i": i, "direction": d, "sl": -d * sl, "level_kind": z["kinds"],
                        "level_price": float(z["price"]), "ref_time": z["valid_from"],
                        "limit_price": -d * lim, "order_end": close_t[q], "signal_close": cr[i],
                        "signal_atr": float(atr14[i]), "fvg_far": -d * far, "leg_end": -d * leg,
                        "ote_overlap": bool(lim <= ote_hi and far >= ote_lo),
                    }
                )
    return _finish_limit(cands, entry, tf, "m1")


def model3(m1_signals: pd.DataFrame) -> pd.DataFrame:
    """Model 1 signals whose FVG overlaps the OTE band, relabelled setup = "m3"."""
    out = m1_signals[m1_signals["ote_overlap"].astype(bool)].copy()
    out["setup"] = "m3"
    return out.reset_index(drop=True)[LIMIT_SIGNAL_COLS]


def _finish_limit(cands: list[dict], entry: pd.DataFrame, tf: str, setup: str) -> pd.DataFrame:
    """One signal per bar and direction (earliest zone valid_from, then nearer SL) in LIMIT_SIGNAL_COLS."""
    if not cands:
        return _empty()
    t = entry["time"].to_numpy()
    close_t = (entry["time"] + TF_LEN[tf]).to_numpy()
    c = entry["close"].to_numpy(dtype=float)
    x = pd.DataFrame(cands)
    x["_dist"] = (x["sl"] - c[x["i"].to_numpy()]).abs()
    x = x.sort_values(["i", "direction", "ref_time", "_dist"], kind="stable")
    x = x.drop_duplicates(["i", "direction"], keep="first")
    i = x["i"].to_numpy()
    out = pd.DataFrame(
        {
            "signal_time": pd.to_datetime(t[i]),
            "entry_time": pd.to_datetime(close_t[i]),
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
            "target_owner": np.nan,
            "limit_price": x["limit_price"].to_numpy(dtype=float),
            "order_end": pd.to_datetime(x["order_end"].to_numpy()),
            "signal_close": x["signal_close"].to_numpy(dtype=float),
            "signal_atr": x["signal_atr"].to_numpy(dtype=float),
            "fvg_far": x["fvg_far"].to_numpy(dtype=float),
            "leg_end": x["leg_end"].to_numpy(dtype=float),
            "ote_overlap": x["ote_overlap"].to_numpy(dtype=bool),
        }
    )
    return out.sort_values(["signal_time", "direction"], kind="stable").reset_index(drop=True)[LIMIT_SIGNAL_COLS]


def _empty() -> pd.DataFrame:
    out = pd.DataFrame({col: pd.Series(dtype=object) for col in LIMIT_SIGNAL_COLS})
    for col in ("signal_time", "entry_time", "ref_time", "order_end"):
        out[col] = pd.to_datetime(out[col])
    for col in ("sl", "level_price", "target_ob", "target_fvg", "target_owner", "limit_price",
                "signal_close", "signal_atr", "fvg_far", "leg_end"):
        out[col] = out[col].astype(float)
    out["direction"] = out["direction"].astype(int)
    out["ote_overlap"] = out["ote_overlap"].astype(bool)
    return out


# --- Asian-range box and Model 5 -----------------------------------------------------------------

BOX_COLS = ["box_start", "known_at", "valid_to", "high", "low"]


def asian_boxes(m1: pd.DataFrame) -> pd.DataFrame:
    """Asian-range boxes from M1 bars: one per `config.ASIA_BOX` window that has bars.

    high / low over M1 bars with start <= time < end; known_at = window end (NY midnight);
    valid_to = start of the next window. Windows without M1 bars are dropped.
    """
    t = m1["time"].to_numpy()
    if len(t) == 0:
        out = pd.DataFrame({c: pd.Series(dtype=float) for c in BOX_COLS})
        for col in BOX_COLS[:3]:
            out[col] = pd.to_datetime(out[col])
        return out
    first, last = pd.Timestamp(t[0]).normalize(), pd.Timestamp(t[-1]).normalize()
    w = local_windows(str((first - pd.Timedelta(days=2)).date()), str((last + pd.Timedelta(days=2)).date()), *config.ASIA_BOX)
    starts, ends = w["start"].to_numpy(), w["end"].to_numpy()
    hi, lo = m1["high"].to_numpy(dtype=float), m1["low"].to_numpy(dtype=float)
    lo_i = np.searchsorted(t, starts, side="left")
    hi_i = np.searchsorted(t, ends, side="left")
    rows = []
    for k in range(len(w) - 1):  # the last window has no successor -> no valid_to
        a, b = int(lo_i[k]), int(hi_i[k])
        if b > a:
            rows.append((starts[k], ends[k], starts[k + 1], float(hi[a:b].max()), float(lo[a:b].min())))
    out = pd.DataFrame(rows, columns=BOX_COLS)
    for col in BOX_COLS[:3]:
        out[col] = pd.to_datetime(out[col])
    return out


def _model5_box(
    h: np.ndarray, c: np.ndarray, tol: np.ndarray, big_b: float, big_l: float, a: int, end: int, k: int
) -> list[tuple[int, int]]:
    """Model 5 at one box (sell orientation: B = box high, L = box low) over bars [a, end).
    Returns (retest r, breakout start b)."""
    out: list[tuple[int, int]] = []
    i = a
    while i < end:
        if not (h[i] > big_b and (i == a or c[i - 1] <= big_b)):
            i += 1
            continue
        b = i
        f = -1
        last = min(b + k, end - 1)
        q = b
        while q <= last:
            if c[q] < big_l:
                break  # cancelled
            if c[q] < big_b:
                f = q
                break
            q += 1
        if f < 0:
            i = q + 1 if q <= last else q  # after the cancel bar, or right after the expiry window
            continue
        last = min(f + k, end - 1)
        r = -1
        q = f + 1
        while q <= last:
            if c[q] > big_b or c[q] < big_l:
                break  # cancelled
            if h[q] >= big_b - tol[q] and c[q] < big_b:
                r = q
                break
            q += 1
        if r >= 0:
            out.append((r, b))
        i = q + 1 if q <= last else q  # after the signal / cancel bar, or right after the expiry window
    return out


def model5(entry: pd.DataFrame, tf: str, boxes: pd.DataFrame) -> pd.DataFrame:
    """Model 5 market-entry signals (both directions): failed breakout of an Asian box edge, close
    back inside within K bars, retest of the edge, entry at the next bar open. SL = extreme of
    [breakout start, retest]. Everything happens inside one box's span (known_at .. valid_to)."""
    entry = entry.reset_index(drop=True)
    if len(entry) == 0 or boxes.empty:
        return _empty_market()
    t = entry["time"].to_numpy()
    close_t = (entry["time"] + TF_LEN[tf]).to_numpy()
    tol = _tolerance(entry)
    a_all = np.searchsorted(t, boxes["known_at"].to_numpy(), side="left")
    e_all = np.searchsorted(close_t, boxes["valid_to"].to_numpy(), side="right")
    k = config.RETEST_BARS
    cands: list[dict] = []
    for d, kind, edge, far in ((-1, "asia_high", "high", "low"), (1, "asia_low", "low", "high")):
        _, h, _, c = _oriented4(entry, d)
        for bi in range(len(boxes)):
            a, end = int(a_all[bi]), int(e_all[bi])
            if a >= end:
                continue
            bx = boxes.iloc[bi]
            big_b, big_l = -d * float(bx[edge]), -d * float(bx[far])
            for r, b in _model5_box(h, c, tol, big_b, big_l, a, end, k):
                cands.append(
                    {
                        "r": r, "direction": d, "sl": -d * float(h[b : r + 1].max()), "level_kind": kind,
                        "level_price": float(bx[edge]), "ref_time": bx["known_at"], "target_owner": np.nan,
                    }
                )
    return _finish(cands, entry, tf, "m5")
