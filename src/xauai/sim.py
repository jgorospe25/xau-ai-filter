"""Generalised trade simulator: setup signals -> trades in R, simulated on M1 bars.

Reuses labels.simulate_trade (same exit rules, no bar-count timeout). Per signal:
  entry = open of the M1 bar at entry_time, shifted against the trade by the cost from that bar's
  spread; SL is the signal's price, TP comes from `tp_mode`. Forced exits: open of the first M1 bar
  at/after Friday 22:00, close of the last bar before a > HOLIDAY_GAP_HOURS wall-clock gap, and
  (optionally) the open of the first M1 bar at/after the end of the session the entry fell in.
Each trade is simulated on an M1 slice that stops at the earliest of those bounds.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from xauai import config
from xauai.labels import CostParams, SwapParams, gap_after_mask, just_before_close, rollovers, simulate_trade
from xauai.timeutil import TF_LEN, assign_session

SIGNAL_COLS = [
    "signal_time", "entry_time", "direction", "sl", "setup", "entry_tf", "trigger_tf",
    "level_kind", "level_price", "ref_time", "target_ob", "target_fvg", "target_owner",
]
TP_MODES = ("rr1.5", "rr2.0", "rr3.0", "next_level", "ob", "fvg", "owner")
TRADE_COLS = [
    "signal_time", "entry_time", "exit_time", "direction", "atr", "sl_dist", "cost", "entry", "sl", "tp",
    "exit_price", "exit_reason", "bars_held", "nights", "swap_nights", "swap_r", "r_price", "r", "setup",
    "entry_tf", "tp_mode", "session", "level_kind", "ref_time", "level_price",
]
_SKIP_COLS = ["signal_time", "direction", "reason", "setup"]


def next_level_target(levels: pd.DataFrame, entry_time: pd.Timestamp, fill: float, d: int, dist: float) -> float:
    """Nearest level price in the trade direction >= MIN_TP_R x dist from the fill, valid at entry_time (NaN if none)."""
    valid = (levels["valid_from"] <= entry_time) & (levels["valid_to"].isna() | (entry_time < levels["valid_to"]))
    gap = d * (levels.loc[valid, "price"].to_numpy(float) - fill)
    gap = gap[gap >= config.MIN_TP_R * dist]
    return fill + d * gap.min() if len(gap) else np.nan


def _tp_price(
    row: pd.Series, tp_mode: str, levels: pd.DataFrame | None, fill: float, d: int, dist: float
) -> tuple[float, str | None]:
    """(tp, skip reason)."""
    if tp_mode.startswith("rr"):
        return fill + d * float(tp_mode[2:]) * dist, None
    if tp_mode == "next_level":
        tp = next_level_target(levels, row["entry_time"], fill, d, dist) if levels is not None else np.nan
    else:
        tp = row[f"target_{tp_mode}"]
    if pd.isna(tp):
        return np.nan, "no_target"
    if d * (tp - fill) < config.MIN_TP_R * dist:
        return np.nan, "target_too_close_or_passed"
    return float(tp), None


def simulate_signals(
    sig: pd.DataFrame,
    m1: pd.DataFrame,
    cost: CostParams,
    swap: SwapParams,
    tp_mode: str,
    session_exit: bool,
    windows: pd.DataFrame,
    levels: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Simulate every signal row on M1 bars. Returns (trades, skipped); R in units of fill-to-SL distance."""
    if tp_mode not in TP_MODES:
        raise ValueError(f"unknown tp_mode {tp_mode!r}")
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
    sig = sig.reset_index(drop=True)
    sess = assign_session(sig["entry_time"], windows) if len(sig) else pd.DataFrame(columns=["session", "session_end"])

    trades: list[dict] = []
    skipped: list[dict] = []
    for i, row in sig.iterrows():
        d = int(row["direction"])
        et = row["entry_time"]
        e = int(np.searchsorted(tn, et.to_datetime64()))
        reason = (
            "no_entry_bar" if e >= n or tn[e] != et.to_datetime64()
            else "stale_after_gap" if e > 0 and gap_after[e - 1]
            else "entry_after_friday_close" if friday_close[e]
            else None
        )
        fill = dist = tp = np.nan
        if reason is None:
            k = cost.per_trade(spread[e])
            fill = o[e] + d * k
            dist = d * (fill - row["sl"])
            if dist <= 0:
                reason = "sl_behind_fill"
            else:
                tp, reason = _tp_price(row, tp_mode, levels, fill, d, dist)
        if reason:
            skipped.append({"signal_time": row["signal_time"], "direction": d, "reason": reason, "setup": row["setup"]})
            continue

        # bounds: first Friday-22 bar after e, first gap bar at/after e, session end, end of data
        bound = n - 1
        f = np.searchsorted(friday_idx, e, side="right")
        if f < len(friday_idx):
            bound = min(bound, friday_idx[f])
        g = np.searchsorted(gap_idx, e)
        if g < len(gap_idx):
            bound = min(bound, gap_idx[g])
        stop_abs = None
        s_end = sess.at[i, "session_end"]
        if session_exit and not pd.isna(s_end):
            s_idx = int(np.searchsorted(tn, s_end.to_datetime64()))
            if e < s_idx < n:
                stop_abs = s_idx
                bound = min(bound, s_idx)
        sl_ = slice(e, bound + 1)
        j, px, why, when = simulate_trade(
            o[sl_], h[sl_], l[sl_], c[sl_], friday_close[sl_], gap_after[sl_], 0, d, fill, row["sl"], tp,
            None, None if stop_abs is None else stop_abs - e,
        )
        j += e
        exit_time = t[j] if when == "open" else t[j] + close_offset
        nights, weighted = rollovers(t[e], exit_time, swap.triple_weekday)
        signal_close = c[e - 1] if e > 0 else o[e]
        swap_px = weighted * swap.points_per_night(d, signal_close) * swap.point
        r_price = d * (px - fill) / dist
        trades.append(
            {
                "signal_time": row["signal_time"],
                "entry_time": t[e],
                "exit_time": exit_time,
                "direction": d,
                "atr": np.nan,  # not part of the setup signal
                "sl_dist": dist,
                "cost": k,
                "entry": fill,
                "sl": row["sl"],
                "tp": tp,
                "exit_price": px,
                "exit_reason": why,
                "bars_held": j - e + 1,
                "nights": nights,
                "swap_nights": weighted,
                "swap_r": swap_px / dist,
                "r_price": r_price,
                "r": r_price + swap_px / dist,
                "setup": row["setup"],
                "entry_tf": row["entry_tf"],
                "tp_mode": tp_mode,
                "session": sess.at[i, "session"],
                "level_kind": row["level_kind"],
                "ref_time": row["ref_time"],
                "level_price": row["level_price"],
            }
        )
    return pd.DataFrame(trades, columns=TRADE_COLS), pd.DataFrame(skipped, columns=_SKIP_COLS)


def one_position(trades: pd.DataFrame) -> pd.DataFrame:
    """One position at a time for ONE variant's trades; adds a boolean `taken` column (all rows kept).

    Sorted by entry_time, then ref_time (earlier-detected level wins), then sl_dist (nearer SL).
    A trade is taken if its entry_time >= the exit_time of the last taken trade.
    """
    out = trades.sort_values(["entry_time", "ref_time", "sl_dist"], kind="stable").reset_index(drop=True)
    taken = np.zeros(len(out), dtype=bool)
    free_at = pd.Timestamp.min
    for i, (et, xt) in enumerate(zip(out["entry_time"], out["exit_time"])):
        if et >= free_at:
            taken[i] = True
            free_at = xt
    out["taken"] = taken
    return out


# --- limit orders (batch 2) -------------------------------------------------------------------
# A limit signal is placed at entry_time (signal-bar close) and may fill until order_end. It is
# cancelled before the first Friday-22:00 M1 bar and before any bar followed by a > HOLIDAY_GAP_HOURS
# hole, and (optionally) can only fill on bars where `live` is True. Fill = limit price, or the bar's
# open if it opened through the limit; the full batch-1 cost from the fill bar's spread is applied
# against the trade. Inside the fill bar an SL touch counts (loss), a TP touch does not.

LIMIT_SIGNAL_COLS = SIGNAL_COLS + [
    "limit_price", "order_end", "signal_close", "signal_atr", "fvg_far", "leg_end", "ote_overlap",
]
ORDER_COLS = TRADE_COLS + ["placed_at", "busy_until", "limit_price", "traded"]
_ONE_MIN = pd.Timedelta(minutes=1)


def _finite(x: object) -> bool:
    return x is not None and not pd.isna(x) and bool(np.isfinite(float(x)))


def simulate_limit_signals(
    sig: pd.DataFrame,
    m1: pd.DataFrame,
    cost: CostParams,
    swap: SwapParams,
    tp_mode: str,
    windows: pd.DataFrame,
    levels: pd.DataFrame | None = None,
    live: np.ndarray | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Simulate limit-order signals on M1 bars. Returns (orders, skipped).

    `orders` holds every placed order (filled or not) with `busy_until` = when the order or its
    position stops blocking the variant; `traded` marks the rows that became trades. Orders not
    placed at all (no bar, stale, after Friday 22:00) appear only in `skipped`.
    """
    if tp_mode not in TP_MODES and tp_mode != "drawn":
        raise ValueError(f"unknown tp_mode {tp_mode!r}")
    m1 = m1.reset_index(drop=True)
    t = m1["time"]
    tn = t.to_numpy()
    o, h, l, c = (m1[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    spread = m1["spread"].to_numpy(float)
    n = len(m1)
    live_ = np.ones(n, dtype=bool) if live is None else np.asarray(live, dtype=bool)
    friday_close = ((t.dt.day_of_week == 4) & (t.dt.hour >= config.FRIDAY_EXIT_HOUR)).to_numpy()
    gap_after = gap_after_mask(t)
    friday_idx, gap_idx = np.flatnonzero(friday_close), np.flatnonzero(gap_after)
    close_offset = just_before_close(TF_LEN["M1"])
    sig = sig.reset_index(drop=True)

    orders: list[dict] = []
    skipped: list[dict] = []

    def skip(row: pd.Series, d: int, reason: str) -> None:
        skipped.append({"signal_time": row["signal_time"], "direction": d, "reason": reason, "setup": row["setup"]})

    for _, row in sig.iterrows():
        d = int(row["direction"])
        p = float(row["limit_price"])
        et = pd.Timestamp(row["entry_time"])
        e = int(np.searchsorted(tn, et.to_datetime64()))  # placement bar: first M1 bar at/after entry_time
        reason = (
            "no_entry_bar" if e >= n
            else "stale_after_gap" if e > 0 and gap_after[e - 1]
            else "entry_after_friday_close" if friday_close[e]
            else None
        )
        if reason:
            skip(row, d, reason)
            continue

        base = dict.fromkeys(ORDER_COLS, np.nan)
        base.update(
            signal_time=row["signal_time"], entry_time=pd.NaT, exit_time=pd.NaT, direction=d, sl=row["sl"],
            setup=row["setup"], entry_tf=row["entry_tf"], tp_mode=tp_mode, session=None,
            level_kind=row["level_kind"], ref_time=row["ref_time"], level_price=row["level_price"],
            placed_at=et, limit_price=p, traded=False,
        )

        # fill window [e, stop): stop = first of order_end, first Friday-22 bar, first gap bar, end of data
        order_end = pd.Timestamp(row["order_end"])
        stop = min(n, int(np.searchsorted(tn, order_end.to_datetime64())))
        cancel_at = order_end
        for idx in (friday_idx, gap_idx):
            q = np.searchsorted(idx, e)
            if q < len(idx) and idx[q] < stop:
                stop = int(idx[q])
                cancel_at = min(cancel_at, t[stop])
        win = np.arange(e, max(stop, e))
        reach = (h[win] >= p) if d == -1 else (l[win] <= p)
        hits = win[reach & live_[win]]
        if not len(hits):
            skip(row, d, "not_filled")
            orders.append({**base, "busy_until": cancel_at})
            continue

        j = int(hits[0])
        raw = o[j] if d * (p - o[j]) >= 0 else p  # bar opened through the limit -> fill at its open
        k = cost.per_trade(spread[j])
        fill = raw + d * k
        null_dist = row.get("null_sl_dist", np.nan)
        drawn_sl = _finite(null_dist)
        sl = fill - d * float(null_dist) if drawn_sl else float(row["sl"])
        dist = d * (fill - sl)
        tp = np.nan
        if not drawn_sl and d * (o[j] - sl) <= 0:
            reason = "gap_through_sl"
        elif dist <= 0:
            reason = "sl_behind_fill"
        elif tp_mode == "drawn":
            tp_r = row.get("null_tp_r", np.nan)
            tp, reason = (fill + d * float(tp_r) * dist, None) if _finite(tp_r) else (np.nan, "no_target")
        else:
            at_fill = row.copy()
            at_fill["entry_time"] = t[j]  # next_level: levels valid at the fill time
            tp, reason = _tp_price(at_fill, tp_mode, levels, fill, d, dist)
        if reason:
            skip(row, d, reason)
            orders.append({**base, "entry_time": t[j], "busy_until": t[j] + _ONE_MIN})
            continue

        # exit bounds as simulate_signals (no session exit): first Friday-22 bar after j, first gap bar at/after j
        bound = n - 1
        f = np.searchsorted(friday_idx, j, side="right")
        if f < len(friday_idx):
            bound = min(bound, friday_idx[f])
        g = np.searchsorted(gap_idx, j)
        if g < len(gap_idx):
            bound = min(bound, gap_idx[g])
        sl_ = slice(j, bound + 1)
        hh, ll = h[sl_].copy(), l[sl_].copy()
        if d == -1:
            ll[0] = raw  # fill bar: a TP touch never counts, an SL touch does (loss)
        else:
            hh[0] = raw
        x, px, why, when = simulate_trade(
            o[sl_], hh, ll, c[sl_], friday_close[sl_], gap_after[sl_], 0, d, fill, sl, tp, None, None
        )
        x += j
        exit_time = t[x] if when == "open" else t[x] + close_offset
        nights, weighted = rollovers(t[j], exit_time, swap.triple_weekday)
        signal_close = c[e - 1] if e > 0 else o[e]
        swap_px = weighted * swap.points_per_night(d, signal_close) * swap.point
        r_price = d * (px - fill) / dist
        orders.append(
            {
                **base,
                "entry_time": t[j],
                "exit_time": exit_time,
                "sl_dist": dist,
                "cost": k,
                "entry": fill,
                "sl": sl,
                "tp": tp,
                "exit_price": px,
                "exit_reason": why,
                "bars_held": x - j + 1,
                "nights": nights,
                "swap_nights": weighted,
                "swap_r": swap_px / dist,
                "r_price": r_price,
                "r": r_price + swap_px / dist,
                "busy_until": exit_time,
                "traded": True,
            }
        )
    out = pd.DataFrame(orders, columns=ORDER_COLS)
    out["traded"] = out["traded"].astype(bool)
    filled = out["traded"].to_numpy()
    if filled.any():
        out.loc[filled, "session"] = assign_session(out.loc[filled, "entry_time"], windows)["session"].to_numpy()
    return out, pd.DataFrame(skipped, columns=_SKIP_COLS)


def one_order(orders: pd.DataFrame) -> pd.DataFrame:
    """One order or position at a time for ONE variant's orders; returns traded rows with a bool `taken`.

    Sorted by placed_at, then ref_time, then |limit_price - sl|. An order (filled or not) is accepted
    if its placed_at >= the busy_until of the last accepted order, so unfilled/skipped orders block too.
    """
    out = orders.assign(_dist=(orders["limit_price"] - orders["sl"]).abs())
    out = out.sort_values(["placed_at", "ref_time", "_dist"], kind="stable").reset_index(drop=True)
    taken = np.zeros(len(out), dtype=bool)
    free_at = pd.Timestamp.min
    for i, (pt, bu) in enumerate(zip(out["placed_at"], out["busy_until"])):
        if pt >= free_at:
            taken[i] = True
            free_at = bu
    out["taken"] = taken
    out = out[out["traded"].astype(bool).to_numpy()].drop(columns="_dist").reset_index(drop=True)
    out["taken"] = out["taken"].astype(bool)
    return out
