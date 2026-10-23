"""Triple-barrier labels in R for indicator signals, with the EA's exit rules.

Per signal (closed bar s), entry is at the open of bar e = s + 1. Bars are bid prices.
Cost (spread floor + slippage) is applied by shifting the effective entry against the
trade: entry = open[e] + dir * cost. SL/TP are set from that entry and checked against
bid high/low — equivalent to buying at the ask / a sell exiting at the ask.

Each bar j of the trade is processed in this order:
  at the open (j > e):  gap-through SL/TP -> fill at open; Friday >= 22:00 -> exit at open
  intrabar:             SL and TP both touched -> loss at SL; else SL or TP
  at the close:         next bar after a > HOLIDAY_GAP_BARS gap -> exit at close;
                        TIMEOUT_BARS held -> exit at close; last bar of data -> exit at close
Signals are skipped (not traded) if the entry bar is at/after Friday 22:00 or follows a
> HOLIDAY_GAP_BARS gap (stale signal), or there is no next bar.

Swap: charged per server-midnight rollover held, x3 for the rollover at the end of the
symbol's triple day; none for rollovers ending Saturday/Sunday.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import numpy as np
import pandas as pd

from xauai import config

HOUR = pd.Timedelta(hours=1)
JUST_BEFORE_CLOSE = HOUR - pd.Timedelta(seconds=1)  # intrabar/close exits happen before the next hour


@dataclass(frozen=True)
class CostParams:
    spread_floor: float = config.SPREAD_FLOOR
    slippage: float = config.SLIPPAGE
    point: float = 0.01

    def per_trade(self, spread_points: float) -> float:
        return max(spread_points * self.point, self.spread_floor) + self.slippage


@dataclass(frozen=True)
class SwapParams:
    long_points: float  # per night, SYMBOL_SWAP_MODE_POINTS
    short_points: float
    point: float
    triple_weekday: int  # Python weekday (Mon=0) whose end-of-day rollover is charged x3
    reference_close: float | None = None  # None = flat swap; else scale by close[signal] / reference_close

    @classmethod
    def from_symbol_info(cls, info: dict, reference_close: float | None = None) -> SwapParams:
        if info["swap_mode"] != 1:
            raise NotImplementedError(f"swap_mode {info['swap_mode']} (only points mode = 1 supported)")
        return cls(
            long_points=info["swap_long"],
            short_points=info["swap_short"],
            point=info["point"],
            triple_weekday=(info["swap_rollover3days"] - 1) % 7,  # MT5 Sunday=0 -> Python Monday=0
            reference_close=reference_close,
        )

    def points_per_night(self, direction: int, signal_close: float) -> float:
        base = self.long_points if direction == 1 else self.short_points
        if self.reference_close is None:
            return base
        return base * signal_close / self.reference_close


def load_symbol_info() -> dict:
    return json.loads(config.SYMBOL_INFO_PATH.read_text(encoding="utf-8"))


def rollovers(entry_time: pd.Timestamp, exit_time: pd.Timestamp, triple_weekday: int) -> tuple[int, int]:
    """(nights, weighted_nights) for midnights m with entry_time < m <= exit_time.

    Each midnight is weighted by the weekday of the day it ends: triple day x3, Sat/Sun x0.
    """
    days = pd.date_range(entry_time.normalize(), exit_time.normalize() - pd.Timedelta(days=1), freq="D")
    wd = days.day_of_week
    weights = np.where(wd == triple_weekday, 3, np.where(wd >= 5, 0, 1))
    return len(days), int(weights.sum())


def simulate_trade(
    o: np.ndarray,
    h: np.ndarray,
    l: np.ndarray,
    c: np.ndarray,
    friday_close: np.ndarray,
    gap_after: np.ndarray,
    e: int,
    direction: int,
    entry: float,
    sl: float,
    tp: float,
    timeout: int,
) -> tuple[int, float, str, str]:
    """Return (exit bar, exit price, reason, when) with when in {"open", "intrabar", "close"}."""
    d = direction
    n = len(o)
    for j in range(e, n):
        if j > e:
            if d * (o[j] - sl) <= 0:
                return j, o[j], "gap_sl", "open"
            if d * (o[j] - tp) >= 0:
                return j, o[j], "gap_tp", "open"
            if friday_close[j]:
                return j, o[j], "friday", "open"
        hit_sl = l[j] <= sl if d == 1 else h[j] >= sl
        hit_tp = h[j] >= tp if d == 1 else l[j] <= tp
        if hit_sl:
            return j, sl, ("sl_same_bar" if hit_tp else "sl"), "intrabar"
        if hit_tp:
            return j, tp, "tp", "intrabar"
        if gap_after[j]:
            return j, c[j], "holiday", "close"
        if j - e + 1 >= timeout:
            return j, c[j], "timeout", "close"
    return n - 1, c[n - 1], "end_of_data", "close"


def label_signals(
    df: pd.DataFrame,
    cost: CostParams,
    swap: SwapParams,
    sl_mult: float = config.SL_ATR_MULT,
    rr: float = config.RR,
    timeout: int = config.TIMEOUT_BARS,
    friday_hour: int = config.FRIDAY_EXIT_HOUR,
    holiday_gap_bars: int = config.HOLIDAY_GAP_BARS,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Label every signal in df (columns: time, open, high, low, close, spread, atr, signal).

    Returns (trades, skipped). R is in units of the SL distance (sl_mult * ATR at signal bar).
    """
    df = df.reset_index(drop=True)
    t = df["time"]
    o, h, l, c = (df[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    spread, atr_v, sig = df["spread"].to_numpy(float), df["atr"].to_numpy(float), df["signal"].to_numpy()
    friday_close = ((t.dt.day_of_week == 4) & (t.dt.hour >= friday_hour)).to_numpy()
    step = t.diff().shift(-1)  # time to the next bar
    gap_after = (step > (holiday_gap_bars + 1) * HOUR).to_numpy()

    trades, skipped = [], []
    for s in np.flatnonzero(sig != 0):
        d = int(sig[s])
        e = s + 1
        reason = (
            "no_next_bar" if e >= len(df)
            else "stale_after_gap" if gap_after[s]
            else "entry_after_friday_close" if friday_close[e]
            else None
        )
        if reason:
            skipped.append({"signal_time": t[s], "direction": d, "reason": reason})
            continue

        dist = sl_mult * atr_v[s]
        k = cost.per_trade(spread[e])
        entry = o[e] + d * k
        sl, tp = entry - d * dist, entry + d * rr * dist
        j, px, why, when = simulate_trade(o, h, l, c, friday_close, gap_after, e, d, entry, sl, tp, timeout)

        exit_time = t[j] if when == "open" else t[j] + JUST_BEFORE_CLOSE
        nights, weighted = rollovers(t[e], exit_time, swap.triple_weekday)
        swap_px = weighted * swap.points_per_night(d, c[s]) * swap.point
        r_price = d * (px - entry) / dist
        trades.append(
            {
                "signal_time": t[s],
                "entry_time": t[e],
                "exit_time": exit_time,
                "direction": d,
                "atr": atr_v[s],
                "sl_dist": dist,
                "cost": k,
                "entry": entry,
                "sl": sl,
                "tp": tp,
                "exit_price": px,
                "exit_reason": why,
                "bars_held": j - e + 1,
                "nights": nights,
                "swap_nights": weighted,
                "swap_r": swap_px / dist,
                "r_price": r_price,
                "r": r_price + swap_px / dist,
            }
        )
    return pd.DataFrame(trades), pd.DataFrame(skipped, columns=["signal_time", "direction", "reason"])
