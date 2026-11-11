"""Broker server time <-> UTC and trading-session windows.

Server time = UTC + SERVER_BASE_OFFSET_H, plus 1 h while America/New_York is in DST.
All frames/series are naive; "server" columns are broker server time, "utc" naive UTC.
Sessions are defined in their own local zones (config.SESSIONS) so DST is automatic.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from xauai import config

TF_LEN: dict[str, pd.Timedelta] = {
    "M1": pd.Timedelta(minutes=1),
    "M5": pd.Timedelta(minutes=5),
    "M15": pd.Timedelta(minutes=15),
    "M30": pd.Timedelta(minutes=30),
    "H1": pd.Timedelta(hours=1),
    "H4": pd.Timedelta(hours=4),
    "D1": pd.Timedelta(days=1),
}

_NY = ZoneInfo("America/New_York")
_UTC = ZoneInfo("UTC")
_BASE = pd.Timedelta(hours=config.SERVER_BASE_OFFSET_H)
_HOUR = pd.Timedelta(hours=1)


def _ny_dst(utc: pd.Series) -> pd.Series:
    """True where America/New_York is in DST at these naive-UTC instants."""
    local = utc.dt.tz_localize("UTC").dt.tz_convert(_NY).dt.tz_localize(None)
    return (local - utc) == pd.Timedelta(hours=-4)


def utc_to_server(t: pd.Series) -> pd.Series:
    """Naive UTC -> naive server time."""
    return t + _BASE + _ny_dst(t).astype("int64") * _HOUR


def server_to_utc(t: pd.Series) -> pd.Series:
    """Naive server time -> naive UTC (inverse of utc_to_server away from DST transitions)."""
    summer = t - _BASE - _HOUR
    winter = t - _BASE
    return summer.where(_ny_dst(summer), winter)


def session_windows(start: str, end: str) -> pd.DataFrame:
    """Session windows (server time) for local dates start..end inclusive, Mon-Fri only.

    Columns: session, start, end; one row per session per weekday, sorted by start.
    """
    rows: list[tuple[str, datetime, datetime]] = []
    for d in pd.date_range(start, end, freq="D"):
        if d.weekday() >= 5:
            continue
        for name, (zone, h0, h1) in config.SESSIONS.items():
            tz = ZoneInfo(zone)
            s = datetime(d.year, d.month, d.day, h0, tzinfo=tz).astimezone(_UTC).replace(tzinfo=None)
            e = datetime(d.year, d.month, d.day, h1, tzinfo=tz).astimezone(_UTC).replace(tzinfo=None)
            rows.append((name, s, e))
    df = pd.DataFrame(rows, columns=["session", "start", "end"])
    df["start"] = utc_to_server(pd.to_datetime(df["start"]))
    df["end"] = utc_to_server(pd.to_datetime(df["end"]))
    return df.sort_values(["start", "session"]).reset_index(drop=True)


def assign_session(times: pd.Series, windows: pd.DataFrame) -> pd.DataFrame:
    """Session label per time (server): most recently started active session, else the next to start.

    Returns columns session, session_end indexed like `times`; times beyond the windows get NaN/NaT.
    """
    w = windows.sort_values("start").reset_index(drop=True)
    starts = w["start"].to_numpy()
    ends = w["end"].to_numpy()
    t = pd.to_datetime(times).to_numpy()
    idx = np.searchsorted(starts, t, side="right") - 1  # last window with start <= t
    sess: list[object] = []
    sess_end: list[object] = []
    lookback = len(config.SESSIONS) * 2
    for ti, i in zip(t, idx):
        pick = None
        for j in range(i, max(i - lookback, -1), -1):
            if ends[j] > ti:
                pick = j
                break
        if pick is None:
            pick = i + 1  # next to start
        if pick < len(w):
            sess.append(w.at[pick, "session"])
            sess_end.append(w.at[pick, "end"])
        else:
            sess.append(None)
            sess_end.append(pd.NaT)
    return pd.DataFrame({"session": sess, "session_end": pd.to_datetime(sess_end)}, index=times.index)


def previous_session(t: pd.Timestamp, windows: pd.DataFrame) -> pd.Series | None:
    """Window row with the latest end <= t (a running session is never previous), else None."""
    done = windows[windows["end"] <= t]
    if done.empty:
        return None
    return done.loc[done["end"].idxmax()] if done["end"].is_unique else done.sort_values(["end", "start"]).iloc[-1]
