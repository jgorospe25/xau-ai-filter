"""Replicate XAU_EMA_RSI_Signal.mq5 confirmed signals (SignalAt + VolOK) on closed bars.

Usage:  uv run python -m xauai.signals   (prints signal counts for the raw H1 data)
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from xauai import config
from xauai.indicators import atr, ema, rsi


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Add ema_fast/ema_slow/ema_trend/rsi/atr/atr_avg columns. df must be sorted by time."""
    c = df["close"].to_numpy(float)
    h = df["high"].to_numpy(float)
    l = df["low"].to_numpy(float)
    out = df.copy()
    out["ema_fast"] = ema(c, config.EMA_FAST)
    out["ema_slow"] = ema(c, config.EMA_SLOW)
    out["ema_trend"] = ema(c, config.EMA_TREND)
    out["rsi"] = rsi(c, config.RSI_PERIOD)
    out["atr"] = atr(h, l, c, config.ATR_PERIOD)
    # VolOK averages ATR over the last ATR_AVG_PERIOD bars including the current one
    out["atr_avg"] = out["atr"].rolling(config.ATR_AVG_PERIOD).mean()
    return out


def session_ok(time: pd.Series) -> np.ndarray:
    if not config.USE_SESSION_FILTER:
        return np.ones(len(time), dtype=bool)
    hour = time.dt.hour.to_numpy()
    s, e = config.SESSION_START_HOUR, config.SESSION_END_HOUR
    return (hour >= s) & (hour < e) if s <= e else (hour >= s) | (hour < e)


def compute_signals(df: pd.DataFrame) -> pd.DataFrame:
    """Return df with indicator columns, vol_ok and signal (+1 buy, -1 sell, 0 none).

    Bar indices below config.MIN_BARS never signal (the indicator starts there), so
    df must begin at the same first bar as the terminal's history.
    """
    out = compute_indicators(df)
    fast, slow = out["ema_fast"].to_numpy(), out["ema_slow"].to_numpy()
    pf, ps = np.roll(fast, 1), np.roll(slow, 1)
    cross_up = (fast > slow) & (pf <= ps)
    cross_dn = (fast < slow) & (pf >= ps)

    a, avg = out["atr"].to_numpy(), out["atr_avg"].to_numpy()
    vol_ok = a >= avg * config.ATR_FLOOR  # NaN during warm-up -> False
    r, c, t = out["rsi"].to_numpy(), out["close"].to_numpy(), out["ema_trend"].to_numpy()
    trend_up = (c > t) if config.USE_TREND_FILTER else np.ones(len(c), dtype=bool)
    trend_dn = (c < t) if config.USE_TREND_FILTER else np.ones(len(c), dtype=bool)

    base = vol_ok & session_ok(out["time"])
    base[: config.MIN_BARS] = False
    buy = base & cross_up & (r > config.RSI_MID) & (r < config.RSI_UPPER) & trend_up
    sell = base & cross_dn & (r < config.RSI_MID) & (r > config.RSI_LOWER) & trend_dn

    out["vol_ok"] = vol_ok
    out["signal"] = np.where(buy, 1, np.where(sell, -1, 0)).astype(np.int8)
    return out


def main() -> None:
    df = pd.read_parquet(config.RAW_H1_PATH)
    sig = compute_signals(df)
    s = sig[sig["signal"] != 0]
    print(f"{len(s)} signals ({(s['signal'] == 1).sum()} buy / {(s['signal'] == -1).sum()} sell) "
          f"from {s['time'].iloc[0]} to {s['time'].iloc[-1]}")
    print(s.groupby(s["time"].dt.year)["signal"].agg(total="size", buys=lambda x: (x == 1).sum()))


if __name__ == "__main__":
    main()
