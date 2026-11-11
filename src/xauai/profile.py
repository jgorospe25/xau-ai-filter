"""Volume profile (POC / value area) from M1 bars, tick volume spread evenly over covered bins."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from xauai import config


@dataclass(frozen=True)
class Profile:
    poc: float
    vah: float
    val: float


def volume_profile(m1: pd.DataFrame, bin_size: float = config.VP_BIN, va: float = config.VALUE_AREA) -> Profile | None:
    """Profile of `m1` (None if empty or no volume).

    Bin k = [k*b, (k+1)*b); a bar covers floor(low/b)..floor(high/b) and splits tick_volume evenly.
    Prices returned are bin centres. POC ties -> lower bin; value-area growth ties -> upper bin.
    """
    if m1.empty:
        return None
    lo = np.floor(m1["low"].to_numpy(dtype=float) / bin_size).astype(np.int64)
    hi = np.floor(m1["high"].to_numpy(dtype=float) / bin_size).astype(np.int64)
    vol = m1["tick_volume"].to_numpy(dtype=float)
    base = int(lo.min())
    size = int(hi.max()) - base + 2
    per_bin = vol / (hi - lo + 1)
    diff = np.zeros(size)
    np.add.at(diff, lo - base, per_bin)
    np.add.at(diff, hi - base + 1, -per_bin)
    hist = np.round(np.maximum(np.cumsum(diff)[:-1], 0.0), 9)  # drop cumsum float noise
    total = hist.sum()
    if total <= 0:
        return None
    poc = int(np.argmax(hist))  # first max = lower bin on ties
    lo_i = hi_i = poc
    acc = hist[poc]
    n = len(hist)
    while acc < va * total - 1e-9:
        can_down, can_up = lo_i > 0, hi_i < n - 1
        if not (can_down or can_up):
            break
        down = hist[lo_i - 1] if can_down else 0.0
        up = hist[hi_i + 1] if can_up else 0.0
        if can_up and (not can_down or up >= down):  # tie -> upper
            hi_i += 1
            acc += up
        else:
            lo_i -= 1
            acc += down

    def centre(i: int) -> float:
        return (base + i + 0.5) * bin_size

    return Profile(poc=centre(poc), vah=centre(hi_i), val=centre(lo_i))
