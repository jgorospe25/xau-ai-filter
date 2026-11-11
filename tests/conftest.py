from __future__ import annotations

from collections.abc import Sequence

import pandas as pd


def make_bars(rows: Sequence[tuple[float, float, float, float]], start: str, freq: str) -> pd.DataFrame:
    """Bar frame from (open, high, low, close) rows; tick_volume=1, spread=0."""
    return pd.DataFrame(
        {
            "time": pd.date_range(start, periods=len(rows), freq=freq),
            "open": [float(r[0]) for r in rows],
            "high": [float(r[1]) for r in rows],
            "low": [float(r[2]) for r in rows],
            "close": [float(r[3]) for r in rows],
            "tick_volume": 1,
            "spread": 0,
        }
    )
