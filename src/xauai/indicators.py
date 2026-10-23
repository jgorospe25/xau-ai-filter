"""EMA / RSI / ATR replicating MT5's built-in iMA, iRSI and iATR (PRICE_CLOSE).

Values the MT5 indicator leaves at 0 during warm-up are NaN here.
"""

from __future__ import annotations

import numpy as np


def ema(x: np.ndarray, n: int) -> np.ndarray:
    """iMA MODE_EMA: seeded with the first price, then a*x + (1-a)*prev, a = 2/(n+1)."""
    a = 2.0 / (n + 1.0)
    out = np.empty(len(x), dtype=float)
    if len(x) == 0:
        return out
    out[0] = x[0]
    for i in range(1, len(x)):
        out[i] = x[i] * a + out[i - 1] * (1.0 - a)
    return out


def rsi(close: np.ndarray, n: int) -> np.ndarray:
    """iRSI: first value at index n = SMA of gains/losses, then Wilder smoothing."""
    out = np.full(len(close), np.nan)
    if len(close) <= n:
        return out
    diff = np.diff(close, prepend=close[0])
    up = np.where(diff > 0, diff, 0.0)
    dn = np.where(diff < 0, -diff, 0.0)
    pos = up[1 : n + 1].sum() / n
    neg = dn[1 : n + 1].sum() / n
    out[n] = _rsi_value(pos, neg)
    for i in range(n + 1, len(close)):
        pos = (pos * (n - 1) + up[i]) / n
        neg = (neg * (n - 1) + dn[i]) / n
        out[i] = _rsi_value(pos, neg)
    return out


def _rsi_value(pos: float, neg: float) -> float:
    if neg != 0.0:
        return 100.0 - 100.0 / (1.0 + pos / neg)
    return 100.0 if pos != 0.0 else 50.0


def true_range(high: np.ndarray, low: np.ndarray, close: np.ndarray) -> np.ndarray:
    prev = np.concatenate(([np.nan], close[:-1]))
    tr = np.fmax(high, prev) - np.fmin(low, prev)  # fmax/fmin ignore the NaN on bar 0
    return tr


def atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, n: int) -> np.ndarray:
    """iATR: SIMPLE average of True Range (not Wilder), first value at index n.

    Uses MT5's running-sum update so float rounding tracks the terminal.
    """
    tr = true_range(high, low, close)
    out = np.full(len(close), np.nan)
    if len(close) <= n:
        return out
    out[n] = tr[1 : n + 1].sum() / n
    for i in range(n + 1, len(close)):
        out[i] = out[i - 1] + (tr[i] - tr[i - n]) / n
    return out
