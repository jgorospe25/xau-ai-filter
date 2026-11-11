"""Swing points and trend state, strictly no-lookahead.

A swing at bar i needs n bars on each side, so it is only *known* at the close of bar i+n.
Swings are defined by bar position (not clock time): weekends / daily breaks are just skipped.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from xauai import config
from xauai.timeutil import TF_LEN

_COLS = ["kind", "price", "bar_time", "known_at"]


def _is_extreme(x: np.ndarray, n: int, sign: int) -> np.ndarray:
    """Bool mask: x[i] strictly beyond (sign*) the n bars on each side. Edges are False."""
    m = len(x)
    ok = np.zeros(m, dtype=bool)
    if m < 2 * n + 1:
        return ok
    ok[n : m - n] = True
    for k in range(1, n + 1):
        left, right = x[n - k : m - n - k], x[n + k : m - n + k]
        centre = x[n : m - n]
        ok[n : m - n] &= (sign * (centre - left) > 0) & (sign * (centre - right) > 0)
    return ok


def swing_points(bars: pd.DataFrame, tf: str, n: int = config.SWING_N) -> pd.DataFrame:
    """Swing highs (+1, wick high) and lows (-1, wick low), strict vs n bars each side.

    `known_at = time[i+n] + TF_LEN[tf]` (close of the confirming bar). Sorted by known_at.
    """
    t = bars["time"].to_numpy()
    parts = []
    for kind, col, sign in ((1, "high", 1), (-1, "low", -1)):
        v = bars[col].to_numpy(dtype=float)
        idx = np.flatnonzero(_is_extreme(v, n, sign))
        parts.append(
            pd.DataFrame(
                {
                    "kind": kind,
                    "price": v[idx],
                    "bar_time": pd.to_datetime(t[idx]),
                    "known_at": pd.to_datetime(t[idx + n]) + TF_LEN[tf],
                }
            )
        )
    out = pd.concat(parts, ignore_index=True)
    out = out.sort_values(["known_at", "kind"], ascending=[True, False]).reset_index(drop=True)
    return out[_COLS].astype({"kind": int, "price": float})


def _latest_two(swings: pd.DataFrame, kind: int, close: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(last, prev, has_two) swing price of `kind` known at each close time."""
    s = swings[swings["kind"] == kind].sort_values("known_at", kind="stable")
    ka = s["known_at"].to_numpy()
    px = s["price"].to_numpy(dtype=float)
    c = np.searchsorted(ka, close, side="right")
    has = c >= 2
    last = np.where(has, px[np.maximum(c - 1, 0)] if len(px) else np.nan, np.nan)
    prev = np.where(has, px[np.maximum(c - 2, 0)] if len(px) else np.nan, np.nan)
    return last, prev, has


def trend_series(bars: pd.DataFrame, swings: pd.DataFrame, tf: str) -> pd.Series:
    """+1 (HH and HL), -1 (LH and LL), else 0, as known at each bar's close.

    Uses only swings with known_at <= time + TF_LEN[tf]; needs two known swings of each kind.
    """
    close = (bars["time"] + TF_LEN[tf]).to_numpy()
    hi, hi_p, hh_ok = _latest_two(swings, 1, close)
    lo, lo_p, ll_ok = _latest_two(swings, -1, close)
    ok = hh_ok & ll_ok
    with np.errstate(invalid="ignore"):
        up = ok & (hi > hi_p) & (lo > lo_p)
        dn = ok & (hi < hi_p) & (lo < lo_p)
    return pd.Series(np.where(up, 1, np.where(dn, -1, 0)), index=bars.index, dtype=int)


def _trend_at(at: pd.Series, bars: pd.DataFrame, tf: str) -> np.ndarray:
    """Trend of the latest bar whose close <= at; 0 if none."""
    tr = trend_series(bars, swing_points(bars, tf), tf)
    closes = (bars["time"] + TF_LEN[tf]).to_numpy()
    left = pd.DataFrame({"at": pd.to_datetime(at).to_numpy(), "_o": np.arange(len(at))}).sort_values("at")
    right = pd.DataFrame({"close": closes, "trend": tr.to_numpy()}).sort_values("close")
    m = pd.merge_asof(left, right, left_on="at", right_on="close", direction="backward")
    m = m.sort_values("_o")
    return m["trend"].fillna(0).to_numpy(dtype=int)


def combined_trend(at: pd.Series, m30: pd.DataFrame, h1: pd.DataFrame) -> np.ndarray:
    """+1/-1 only where M30 and H1 trends agree, else 0, at decision times `at` (bar closes).

    `m30`/`h1` are raw bar frames; swings and trend are computed inside. For each timeframe the
    trend of the latest bar with close (time + TF_LEN) <= at is used (merge_asof), 0 if none.
    """
    a = _trend_at(at, m30, "M30")
    b = _trend_at(at, h1, "H1")
    return np.where(a == b, a, 0)


def htf_direction(at: pd.Series, h4: pd.DataFrame, h1: pd.DataFrame) -> np.ndarray:
    """+1/-1 only where H4 and H1 trends agree, else 0, at decision times `at`."""
    a = _trend_at(at, h4, "H4")
    b = _trend_at(at, h1, "H1")
    return np.where(a == b, a, 0)
