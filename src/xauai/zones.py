"""H1 order blocks and fair value gaps, strictly no-lookahead.

Every zone carries `known_at` (when it can first be used) and an end time (`invalid_at` for
order blocks, `filled_at` for FVGs). `valid_at` selects the zones usable at a given time.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from xauai import config
from xauai.indicators import atr
from xauai.timeutil import TF_LEN

H1_LEN = TF_LEN["H1"]
_OB_COLS = ["ob_id", "direction", "top", "bottom", "ob_time", "known_at", "invalid_at"]
_FVG_COLS = ["direction", "top", "bottom", "mid", "known_at", "filled_at"]


def first_touch(m1: pd.DataFrame, after: pd.Timestamp, price: float, side: int) -> pd.Timestamp | None:
    """Open time of the first M1 bar at/after `after` that touches `price`, else None.

    side=-1: price reached from above (low <= price); side=+1: from below (high >= price).
    """
    start = int(np.searchsorted(m1["time"].to_numpy(), np.datetime64(after), side="left"))
    col = m1["low"] if side < 0 else m1["high"]
    v = col.to_numpy(dtype=float)[start:]
    hit = (v <= price) if side < 0 else (v >= price)
    if not hit.any():
        return None
    return pd.Timestamp(m1["time"].iloc[start + int(np.argmax(hit))])


def _obs_one_side(h1: pd.DataFrame, swings: pd.DataFrame, d: int) -> pd.DataFrame:
    """Order blocks of direction d (+1 bullish: break a swing high, OB = last bearish candle)."""
    t = h1["time"].to_numpy()
    o, c = h1["open"].to_numpy(float), h1["close"].to_numpy(float)
    hi, lo = h1["high"].to_numpy(float), h1["low"].to_numpy(float)
    s = swings[swings["kind"] == d].sort_values("known_at", kind="stable")
    if s.empty or len(h1) < 2:
        return pd.DataFrame(columns=_OB_COLS[1:])
    px = s["price"].to_numpy(float)
    sbar = np.searchsorted(t, s["bar_time"].to_numpy())
    # latest swing known before bar i opened (known_at <= time[i])
    k = np.searchsorted(s["known_at"].to_numpy(), t, side="right") - 1
    brk = (k >= 0) & (d * (c - px[np.maximum(k, 0)]) > 0)
    bi = np.flatnonzero(brk)
    _, first = np.unique(k[bi], return_index=True)  # first break of each swing
    bi = np.sort(bi[first])
    # last opposite-colour candle at/before each index
    opp = (c < o) if d > 0 else (c > o)
    last_opp = np.maximum.accumulate(np.where(opp, np.arange(len(c)), -1))
    cand = last_opp[bi - 1] if len(bi) else np.array([], dtype=int)
    cand = np.where(bi >= 1, cand, -1)
    ok = cand > sbar[k[bi]] if len(bi) else np.array([], dtype=bool)
    bi, cand = bi[ok], cand[ok]
    top, bot = hi[cand], lo[cand]
    known = pd.to_datetime(t[bi]) + H1_LEN
    inv = []
    for kn, tp, bt in zip(known, top, bot):
        j0 = int(np.searchsorted(t, np.datetime64(kn), side="left"))
        hit = (c[j0:] < bt) if d > 0 else (c[j0:] > tp)
        inv.append(pd.Timestamp(t[j0 + int(np.argmax(hit))]) + H1_LEN if hit.any() else pd.NaT)
    return pd.DataFrame(
        {
            "direction": d,
            "top": top,
            "bottom": bot,
            "ob_time": pd.to_datetime(t[cand]),
            "known_at": known,
            "invalid_at": pd.to_datetime(pd.Series(inv, dtype="datetime64[ns]")),
        }
    )


def order_blocks(h1: pd.DataFrame, h1_swings: pd.DataFrame) -> pd.DataFrame:
    """H1 order blocks (+1 bullish / -1 bearish).

    Bullish: bar i closes above the latest swing high known at its open (each swing's first
    break only); the OB is the last bearish candle strictly between that swing bar and bar i
    (full high/low range). known_at = close of bar i. invalid_at = close of the first H1 bar
    opening at/after known_at that closes below the OB bottom (NaT if never). Bearish mirrors.
    """
    out = pd.concat([_obs_one_side(h1, h1_swings, 1), _obs_one_side(h1, h1_swings, -1)], ignore_index=True)
    out = out.sort_values(["known_at", "direction"], ascending=[True, False], kind="stable").reset_index(drop=True)
    out.insert(0, "ob_id", np.arange(len(out)))
    return out[_OB_COLS].astype({"direction": int, "top": float, "bottom": float})


def fvgs(h1: pd.DataFrame, m1: pd.DataFrame) -> pd.DataFrame:
    """H1 fair value gaps whose middle candle body >= FVG_BODY_ATR x ATR14(H1) at that candle.

    Bullish: high[i-2] < low[i] (zone bottom = high[i-2], top = low[i]); bearish mirrors.
    known_at = close of bar i. filled_at = first M1 touch of the midpoint after known_at.
    """
    o, c = h1["open"].to_numpy(float), h1["close"].to_numpy(float)
    hi, lo = h1["high"].to_numpy(float), h1["low"].to_numpy(float)
    a = atr(hi, lo, c, 14)
    t = h1["time"].to_numpy()
    rows = []
    if len(h1) >= 3:
        mid_body = np.abs(c[1:-1] - o[1:-1])
        with np.errstate(invalid="ignore"):
            big = mid_body >= config.FVG_BODY_ATR * a[1:-1]  # NaN ATR -> False
            bull = big & (hi[:-2] < lo[2:])
            bear = big & (lo[:-2] > hi[2:])
        for d, mask in ((1, bull), (-1, bear)):
            for i in np.flatnonzero(mask) + 2:
                bottom, top = (hi[i - 2], lo[i]) if d > 0 else (hi[i], lo[i - 2])
                mid = (top + bottom) / 2
                known = pd.Timestamp(t[i]) + H1_LEN
                rows.append((d, top, bottom, mid, known, first_touch(m1, known, mid, -d)))
    out = pd.DataFrame(rows, columns=_FVG_COLS)
    out["filled_at"] = pd.to_datetime(out["filled_at"])
    out["known_at"] = pd.to_datetime(out["known_at"])
    return out.sort_values("known_at", kind="stable").reset_index(drop=True).astype({"direction": int})


def valid_at(zones: pd.DataFrame, t: pd.Timestamp, end_col: str) -> pd.DataFrame:
    """Zones known by `t` whose end time (`end_col`) is NaT or after `t`."""
    end = zones[end_col]
    return zones[(zones["known_at"] <= t) & (end.isna() | (end > t))]
