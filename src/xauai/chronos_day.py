"""Chronos day trade: one trade per weekday, decision 10:00 Manila, time exit 12:00 London.

All times are naive broker server time. Local wall-clock times go local -> UTC -> server, so the
UK and US DST switches (different dates) are handled by the zones, not by fixed offsets.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from xauai import config
from xauai.indicators import atr
from xauai.timeutil import utc_to_server

_UTC = ZoneInfo("UTC")
_HOUR = pd.Timedelta(hours=1)
_MAX_START_LAG = pd.Timedelta(minutes=5)
_PREV_LOOKBACK = pd.Timedelta(hours=72)
DAY_COLS = [
    "day", "decision_time", "entry_time", "exit_time", "h1_last_open", "last_close", "atr", "horizon",
    "prev_exit_price",
]


def local_to_server(d: date, zone: str, hour: int) -> pd.Timestamp:
    """Local wall-clock `hour` on date `d` in `zone` -> naive server time."""
    local = datetime(d.year, d.month, d.day, hour, tzinfo=ZoneInfo(zone))
    utc = pd.Series([pd.Timestamp(local.astimezone(_UTC).replace(tzinfo=None))])
    return utc_to_server(utc).iloc[0]


def _first_at_or_after(times: np.ndarray, t: pd.Timestamp) -> int:
    return int(np.searchsorted(times, np.datetime64(t), side="left"))


def prev_weekday(d: date) -> date:
    """Previous Mon-Fri date (Monday -> Friday)."""
    p = d - timedelta(days=1)
    while p.weekday() >= 5:
        p -= timedelta(days=1)
    return p


def prev_exit_price(m1_t: np.ndarray, m1_close: np.ndarray, d: date) -> float:
    """Close of the last M1 bar strictly before the previous weekday's exit, within 72 h, else NaN."""
    pe = local_to_server(prev_weekday(d), *config.CHRONOS_EXIT)
    i = _first_at_or_after(m1_t, pe) - 1
    if i < 0 or pd.Timestamp(m1_t[i]) < pe - _PREV_LOOKBACK:
        return float("nan")
    return float(m1_close[i])


def day_table(h1: pd.DataFrame, m1: pd.DataFrame, start: str, end: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(days, skipped) for every Mon-Fri local PH date start..end inclusive.

    Missing decision H1 bar, entry/exit M1 bar or ATR means no trade that day (never shifted).
    """
    h1 = h1.reset_index(drop=True)
    h_t = h1["time"].to_numpy()
    h_atr = atr(h1["high"].to_numpy(float), h1["low"].to_numpy(float), h1["close"].to_numpy(float), 14)
    m_t = m1["time"].to_numpy()
    m_close = m1["close"].to_numpy(float)
    rows: list[dict] = []
    skipped: list[dict] = []

    def skip(day: date, reason: str) -> None:
        skipped.append({"day": day, "reason": reason})

    for ts in pd.date_range(start, end, freq="D"):
        if ts.weekday() >= 5:
            continue
        day = ts.date()
        dec = local_to_server(day, *config.CHRONOS_DECISION)
        ext = local_to_server(day, *config.CHRONOS_EXIT)
        # the bar closing exactly at the decision must exist
        i = _first_at_or_after(h_t, dec - _HOUR)
        if i >= len(h_t) or pd.Timestamp(h_t[i]) != dec - _HOUR:
            skip(day, "no_decision_bar")
            continue
        j = _first_at_or_after(m_t, dec)
        if j >= len(m_t) or pd.Timestamp(m_t[j]) - dec >= _MAX_START_LAG:
            skip(day, "no_entry_bar")
            continue
        k = _first_at_or_after(m_t, ext)
        if k >= len(m_t) or pd.Timestamp(m_t[k]) - ext >= _MAX_START_LAG:
            skip(day, "no_exit_bar")
            continue
        if np.isnan(h_atr[i]):
            skip(day, "atr_nan")
            continue
        entry, exit_t = pd.Timestamp(m_t[j]), pd.Timestamp(m_t[k])
        rows.append({
            "day": day, "decision_time": dec, "entry_time": entry, "exit_time": exit_t,
            "h1_last_open": pd.Timestamp(h_t[i]), "last_close": float(h1["close"].iloc[i]),
            "atr": float(h_atr[i]), "horizon": int(round((exit_t - entry) / _HOUR)),
            "prev_exit_price": prev_exit_price(m_t, m_close, day),
        })
    return pd.DataFrame(rows, columns=DAY_COLS), pd.DataFrame(skipped, columns=["day", "reason"])
