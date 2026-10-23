"""Scale-free features known at the signal-bar close. See reports/04_model.md (pre-registration).

Directional features are oriented by trade direction (x +1 buy / -1 sell): positive = with the trade.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from xauai.indicators import ema

D1_EMA_PERIOD = 50
SLOPE_BARS = 20
RSI_CHANGE_BARS = 3

FEATURES = [
    "dist_ema200_atr",
    "ema200_slope_atr",
    "ema_gap_atr",
    "rsi_mid",
    "rsi_chg3",
    "atr_ratio",
    "range_atr",
    "body_ratio",
    "d1_trend_pct",
    "hour",
    "dow",
    "direction",
]


def d1_ema_at(close_time: pd.Series, d1: pd.DataFrame, period: int = D1_EMA_PERIOD) -> np.ndarray:
    """EMA(period) of D1 closes from the last D1 bar whose day has ended at or before close_time."""
    d = pd.DataFrame({"available_at": d1["time"] + pd.Timedelta(days=1), "d1_ema": ema(d1["close"].to_numpy(float), period)})
    q = pd.DataFrame({"t": close_time.to_numpy(), "_i": np.arange(len(close_time))}).sort_values("t")
    m = pd.merge_asof(q, d.sort_values("available_at"), left_on="t", right_on="available_at", direction="backward")
    return m.sort_values("_i")["d1_ema"].to_numpy()


def compute_features(sig: pd.DataFrame, d1: pd.DataFrame) -> pd.DataFrame:
    """sig = compute_signals() output (all bars). Returns one row per signal bar: signal_time + FEATURES."""
    s = sig.reset_index(drop=True)
    d = s["signal"].astype(float)
    atr = s["atr"]
    rng = s["high"] - s["low"]
    f = pd.DataFrame({"signal_time": s["time"]})
    f["dist_ema200_atr"] = d * (s["close"] - s["ema_trend"]) / atr
    f["ema200_slope_atr"] = d * (s["ema_trend"] - s["ema_trend"].shift(SLOPE_BARS)) / atr
    f["ema_gap_atr"] = d * (s["ema_fast"] - s["ema_slow"]) / atr
    f["rsi_mid"] = d * (s["rsi"] - 50.0)
    f["rsi_chg3"] = d * (s["rsi"] - s["rsi"].shift(RSI_CHANGE_BARS))
    f["atr_ratio"] = atr / s["atr_avg"]
    f["range_atr"] = rng / atr
    f["body_ratio"] = np.where(rng > 0, d * (s["close"] - s["open"]) / rng.where(rng > 0, 1.0), 0.0)
    f["d1_trend_pct"] = d * (s["close"] / d1_ema_at(s["time"] + pd.Timedelta(hours=1), d1) - 1.0) * 100.0
    f["hour"] = s["time"].dt.hour
    f["dow"] = s["time"].dt.day_of_week
    f["direction"] = s["signal"].astype(int)
    return f[s["signal"] != 0].reset_index(drop=True)
