"""Level table (PDH/PDL, PWH/PWL, previous-session H/L, swing anchors, volume profile) and merging.

Every level row is valid from `valid_from` and only uses data finished by then (no lookahead).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from xauai import config
from xauai.profile import volume_profile
from xauai.structure import swing_points
from xauai.timeutil import TF_LEN, previous_session

LEVEL_KINDS = ("pdh", "pdl", "pwh", "pwl", "psh", "psl", "swh", "swl", "poc", "vah", "val")
_DAY = pd.Timedelta(days=1)


def refresh_times(windows: pd.DataFrame, d1: pd.DataFrame) -> pd.Series:
    """Every session start and every D1 bar open, sorted and unique."""
    t = pd.concat([windows["start"], d1["time"]], ignore_index=True)
    return pd.Series(pd.to_datetime(t).drop_duplicates().sort_values().to_numpy(), name="valid_from")


def _weekly(d1: pd.DataFrame) -> pd.DataFrame:
    """High/low per server ISO week, indexed by the week's Monday, sorted."""
    monday = d1["time"].dt.normalize() - pd.to_timedelta(d1["time"].dt.day_of_week, unit="D")
    g = d1.groupby(monday)
    return pd.DataFrame({"high": g["high"].max(), "low": g["low"].min()}).sort_index()


def _anchor(
    sw: pd.DataFrame, s: pd.Timestamp, e: pd.Timestamp, vf: pd.Timestamp
) -> tuple[float, pd.Timestamp] | None:
    """Latest-bar_time swing (already one kind) with bar_time in [s, e) and known_at <= vf."""
    x = sw[(sw["bar_time"] >= s) & (sw["bar_time"] < e) & (sw["known_at"] <= vf)]
    if x.empty:
        return None
    r = x.loc[x["bar_time"].idxmax()]
    return float(r["price"]), pd.Timestamp(r["bar_time"])


def level_table(bars: dict[str, pd.DataFrame], windows: pd.DataFrame) -> pd.DataFrame:
    """Rows `valid_from, valid_to, kind, price`, one per level per refresh; NaN levels dropped."""
    m1, m30, d1 = bars["M1"], bars["M30"], bars["D1"]
    rt = refresh_times(windows, d1).to_numpy()
    d1_close = (d1["time"] + _DAY).to_numpy()
    d1h, d1l = d1["high"].to_numpy(dtype=float), d1["low"].to_numpy(dtype=float)
    wk = _weekly(d1)
    wk_idx = wk.index.to_numpy()
    m1t = m1["time"].to_numpy()
    m1h, m1l = m1["high"].to_numpy(dtype=float), m1["low"].to_numpy(dtype=float)
    swings = swing_points(m30, "M30")
    sw_hi = swings[swings["kind"] == 1]
    sw_lo = swings[swings["kind"] == -1]
    m30_len, m1_len = TF_LEN["M30"], TF_LEN["M1"]
    rows: list[tuple[pd.Timestamp, pd.Timestamp, str, float]] = []

    for k, vf_np in enumerate(rt):
        vf = pd.Timestamp(vf_np)
        vt = pd.Timestamp(rt[k + 1]) if k + 1 < len(rt) else pd.NaT
        lv: dict[str, float] = {}
        i = int(np.searchsorted(d1_close, vf_np, side="right")) - 1
        if i >= 0:
            lv["pdh"], lv["pdl"] = d1h[i], d1l[i]
        cur_monday = (vf.normalize() - pd.Timedelta(days=vf.weekday())).to_datetime64()
        j = int(np.searchsorted(wk_idx, cur_monday, side="left")) - 1
        if j >= 0:
            lv["pwh"], lv["pwl"] = float(wk["high"].iloc[j]), float(wk["low"].iloc[j])
        ps = previous_session(vf, windows)
        if ps is not None:
            s, e = pd.Timestamp(ps["start"]), pd.Timestamp(ps["end"])
            a = int(np.searchsorted(m1t, s.to_datetime64(), side="left"))
            b = int(np.searchsorted(m1t, e.to_datetime64(), side="left"))
            if b > a:
                ih, il = a + int(np.argmax(m1h[a:b])), a + int(np.argmin(m1l[a:b]))
                lv["psh"], lv["psl"] = m1h[ih], m1l[il]
                ah, al = _anchor(sw_hi, s, e, vf), _anchor(sw_lo, s, e, vf)
                # (price, bar_time, bar_end); fall back to the session extreme (M1 bar)
                th = (ah[0], ah[1], ah[1] + m30_len) if ah else (m1h[ih], pd.Timestamp(m1t[ih]), pd.Timestamp(m1t[ih]) + m1_len)
                tl = (al[0], al[1], al[1] + m30_len) if al else (m1l[il], pd.Timestamp(m1t[il]), pd.Timestamp(m1t[il]) + m1_len)
                lv["swh"], lv["swl"] = th[0], tl[0]
                r0, r1 = min(th[1], tl[1]), max(th[2], tl[2])
                pa = int(np.searchsorted(m1t, r0.to_datetime64(), side="left"))
                pb = int(np.searchsorted(m1t, r1.to_datetime64(), side="left"))
                prof = volume_profile(m1.iloc[pa:pb])
                if prof is not None:
                    lv["poc"], lv["vah"], lv["val"] = prof.poc, prof.vah, prof.val
        for kind in LEVEL_KINDS:
            p = lv.get(kind)
            if p is not None and not np.isnan(p):
                rows.append((vf, vt, kind, float(p)))
    out = pd.DataFrame(rows, columns=["valid_from", "valid_to", "kind", "price"])
    out["valid_to"] = pd.to_datetime(out["valid_to"])
    return out


def merge_levels(levels: pd.DataFrame, ref_price: float, h1_atr: float, merge: float = config.MERGE_ATR) -> pd.DataFrame:
    """Cluster levels per side of `ref_price` (>= ref counts as above); gap > merge*atr starts a new cluster.

    Price = outer edge (cluster max above, min below); `kinds` comma-joined in price order.
    Levels are grouped by (valid_from, valid_to) so a multi-refresh table can be passed.
    """
    cols = ["side", "price", "kinds", "valid_from", "valid_to"]
    if levels.empty:
        return pd.DataFrame(columns=cols)
    out = []
    for (vf, vt), g in levels.groupby(["valid_from", "valid_to"], dropna=False, sort=True):
        for side in (1, -1):
            m = g["price"] >= ref_price if side == 1 else g["price"] < ref_price
            s = g[m].sort_values("price", kind="stable")
            if s.empty:
                continue
            px, kd = s["price"].to_numpy(dtype=float), s["kind"].tolist()
            start = 0
            for n in range(1, len(px) + 1):
                if n == len(px) or px[n] - px[n - 1] > merge * h1_atr:
                    seg = px[start:n]
                    out.append((side, seg.max() if side == 1 else seg.min(), ",".join(kd[start:n]), vf, vt))
                    start = n
    return pd.DataFrame(out, columns=cols)
