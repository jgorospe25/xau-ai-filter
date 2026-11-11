"""Part A of the session-liquidity research: level study and FVG fill study (development period only).

Level study: for each session-start refresh, does price touch each level in the window (that session plus
the next) and, once touched, reverse by 1 ATR before going 1 ATR through? Compared with "random levels" =
the real levels of OTHER sessions transplanted (same signed distance in ATR units from that session's open).

FVG fill study: share of big H1 FVG midpoints reached within 1/4/12/24/48 h of `known_at`, vs random points
at the same signed ATR distance from random other H1 closes.

Outcome codes: 0 untouched, 1 reversed, 2 broke, 3 touched_no_resolution. The touch bar counts for the
reversal check (its low, for a level above). A bar that both reverses and goes through (including the touch
bar) is counted as "broke" (conservative: unknown intrabar order).
Only M1 data up to DEV_END 23:59:59 is read: sessions / FVG horizons / random pools that pass it are dropped.
"""

from __future__ import annotations

from datetime import datetime

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from xauai import config  # noqa: E402
from xauai.md import table  # noqa: E402
from xauai.research import Inputs, load_inputs  # noqa: E402
from xauai.setups import _h1_atr_at  # noqa: E402
from xauai.timeutil import TF_LEN, assign_session  # noqa: E402

N_RANDOM = 500
SEED = 7
HORIZONS_H = (1, 4, 12, 24, 48)
CODES = ("untouched", "reversed", "broke", "touched_no_resolution")
KIND_ORDER = ("pdh", "pdl", "pwh", "pwl", "psh", "psl", "swh", "swl", "poc", "vah", "val")
_H1 = TF_LEN["H1"]
_DAY = pd.Timedelta(days=1)


# --------------------------------------------------------------------------- level reaction core
def _sparse_min(x: np.ndarray) -> np.ndarray:
    n = len(x)
    k = max(int(np.floor(np.log2(n))) + 1, 1)
    st = np.full((k, n), np.inf)
    st[0] = x
    for j in range(1, k):
        w, half = 1 << j, 1 << (j - 1)
        if n - w + 1 > 0:
            st[j, : n - w + 1] = np.minimum(st[j - 1, : n - w + 1], st[j - 1, half : half + n - w + 1])
    return st


def _codes_above(h: np.ndarray, l: np.ndarray, p: np.ndarray, atr: float) -> np.ndarray:
    """Outcome codes for levels `p` ABOVE the open, on one window (arrays h, l)."""
    n = len(h)
    c = np.maximum.accumulate(h)
    t0 = np.searchsorted(c, p, side="left")  # first bar with high >= p
    touched = t0 < n
    tt = np.searchsorted(c, p + atr, side="left")  # first bar with high >= p + atr
    lo, hi = t0, tt  # reversal window = touch bar .. bar before the through bar; a bar that also goes through = broke
    has = touched & (hi > lo)
    length = np.where(has, hi - lo, 1)
    k = np.floor(np.log2(length)).astype(int)
    st = _sparse_min(l)
    lo_c = np.clip(lo, 0, n - 1)
    hi_c = np.clip(hi - (1 << k), 0, n - 1)
    m = np.minimum(st[k, lo_c], st[k, hi_c])
    rev = has & (m <= p - atr)
    out = np.zeros(len(p), dtype=np.int8)
    out[touched] = 3
    out[touched & (tt < n)] = 2
    out[rev] = 1
    return out


def window_codes(h: np.ndarray, l: np.ndarray, price: np.ndarray, side: np.ndarray, atr: float) -> np.ndarray:
    """Outcome codes for many levels on one window. side=+1 level above the open, -1 below."""
    price, side = np.asarray(price, dtype=float), np.asarray(side)
    out = np.zeros(len(price), dtype=np.int8)
    up = side > 0
    if up.any():
        out[up] = _codes_above(h, l, price[up], atr)
    if (~up).any():
        out[~up] = _codes_above(-l, -h, -price[~up], atr)  # mirror
    return out


def level_reaction(m1: pd.DataFrame, price: float, side: int, start: pd.Timestamp, end: pd.Timestamp, atr: float) -> str:
    """"untouched" | "reversed" | "broke" | "touched_no_resolution" for one level on M1 bars in [start, end)."""
    w = m1[(m1["time"] >= start) & (m1["time"] < end)]
    if w.empty:
        return "untouched"
    code = window_codes(w["high"].to_numpy(float), w["low"].to_numpy(float), np.array([price]), np.array([side]), atr)
    return CODES[int(code[0])]


# --------------------------------------------------------------------------- random placement
def draw_other(rng: np.random.Generator, n: int, n_choices: int, exclude: int) -> np.ndarray:
    """`n` random indices in [0, n_choices) never equal to `exclude`."""
    idx = rng.integers(0, n_choices - 1, size=n)
    return np.where(idx >= exclude, idx + 1, idx)


def transplant_points(signed_dist_atr: float, base: np.ndarray, atr: np.ndarray) -> np.ndarray:
    """Points at the same signed distance (in ATR units) from each base price, using each base's ATR."""
    return np.asarray(base, dtype=float) + signed_dist_atr * np.asarray(atr, dtype=float)


# --------------------------------------------------------------------------- FVG fill
def _first_idx(h: np.ndarray, l: np.ndarray, price: np.ndarray, side: np.ndarray) -> np.ndarray:
    """Index of the first bar touching each price (side +1: high >= p; -1: low <= p); len(h) if none."""
    cmax, cmin = np.maximum.accumulate(h), np.minimum.accumulate(l)
    up = side > 0
    out = np.empty(len(price), dtype=np.int64)
    out[up] = np.searchsorted(cmax, price[up], side="left")
    out[~up] = np.searchsorted(-cmin, -price[~up], side="left")  # -cmin is ascending
    return out


def fill_hours(m1: pd.DataFrame, after: pd.Timestamp, price: float, side: int, horizon_h: int = 48) -> float:
    """Hours from `after` to the open of the first M1 bar touching `price` within [after, after + horizon).

    side=-1: price is reached from above (low <= price); +1: from below (high >= price). NaN if never.
    """
    t = m1["time"].to_numpy()
    a = int(np.searchsorted(t, np.datetime64(after), side="left"))
    b = int(np.searchsorted(t, np.datetime64(after + pd.Timedelta(hours=horizon_h)), side="left"))
    if b <= a:
        return float("nan")
    i = int(_first_idx(m1["high"].to_numpy(float)[a:b], m1["low"].to_numpy(float)[a:b], np.array([price]), np.array([side]))[0])
    if i >= b - a:
        return float("nan")
    return float((t[a + i] - np.datetime64(after)) / np.timedelta64(1, "h"))


# --------------------------------------------------------------------------- level study driver
def build_sessions(inp: Inputs) -> pd.DataFrame:
    """One row per development-period session start: window [start, end of the following session), open, ATR."""
    w = inp.windows.sort_values(["start", "session"]).drop_duplicates("start").reset_index(drop=True)
    m1 = inp.bars["M1"]
    t = m1["time"].to_numpy()
    lo, hi = pd.Timestamp(config.DEV_START), pd.Timestamp(config.DEV_END) + _DAY
    rows = []
    dropped = 0
    for i in range(len(w) - 1):
        s = w.at[i, "start"]
        if not (lo <= s < hi):
            continue
        e = max(w.at[i, "end"], w.at[i + 1, "end"])
        if e > hi - pd.Timedelta(seconds=1):  # window would read past DEV_END 23:59:59
            dropped += 1
            continue
        a = int(np.searchsorted(t, s.to_datetime64(), side="left"))
        b = int(np.searchsorted(t, e.to_datetime64(), side="left"))
        if b <= a or pd.Timestamp(t[a]) - s > _H1:
            continue
        atr = _h1_atr_at(inp.h1_atr, s)
        if np.isnan(atr) or atr <= 0:
            continue
        rows.append((s, e, w.at[i, "session"], a, b, float(m1["open"].iloc[a]), atr))
    out = pd.DataFrame(rows, columns=["start", "end", "session", "a", "b", "open", "atr"])
    out.attrs["dropped_past_dev_end"] = dropped
    return out


def eval_by_session(sess: pd.DataFrame, m1: pd.DataFrame, q_sess: np.ndarray, q_d: np.ndarray) -> np.ndarray:
    """Outcome code per query: query j places a level at open + q_d[j]*atr of session q_sess[j], and is
    evaluated on that session's window. Vectorised per session."""
    h_all, l_all = m1["high"].to_numpy(float), m1["low"].to_numpy(float)
    order = np.argsort(q_sess, kind="stable")
    qs = q_sess[order]
    bounds = np.searchsorted(qs, np.arange(len(sess) + 1))
    out = np.zeros(len(q_sess), dtype=np.int8)
    a_, b_, o_, atr_ = (sess[c].to_numpy() for c in ("a", "b", "open", "atr"))
    for s in range(len(sess)):
        i0, i1 = bounds[s], bounds[s + 1]
        if i0 == i1:
            continue
        sel = order[i0:i1]
        d = q_d[sel]
        out[sel] = window_codes(h_all[a_[s]:b_[s]], l_all[a_[s]:b_[s]], o_[s] + d * atr_[s], np.where(d > 0, 1, -1), atr_[s])
    return out


def level_study(inp: Inputs, n_random: int = N_RANDOM, seed: int = SEED) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Returns (per-kind summary, per-level detail with real code)."""
    sess = build_sessions(inp)
    sidx = sess[["start", "open", "atr"]].reset_index().rename(columns={"index": "sess"})
    lv = inp.levels.merge(sidx, left_on="valid_from", right_on="start", how="inner").reset_index(drop=True)
    lv["d"] = (lv["price"] - lv["open"]) / lv["atr"]
    m1 = inp.bars["M1"]
    sess_idx = lv["sess"].to_numpy()
    real = eval_by_session(sess, m1, sess_idx, lv["d"].to_numpy())
    rng = np.random.default_rng(seed)
    n = len(lv)
    other = np.empty((n, n_random), dtype=np.int32)
    for j in range(n):
        other[j] = draw_other(rng, n_random, len(sess), int(sess_idx[j]))
    rand = eval_by_session(sess, m1, other.ravel(), np.repeat(lv["d"].to_numpy(), n_random)).reshape(n, n_random)
    lv["real"] = real
    lv["r_touch"] = (rand > 0).sum(axis=1)
    lv["r_rev"] = (rand == 1).sum(axis=1)
    lv["r_broke"] = (rand == 2).sum(axis=1)
    rows = []
    for kind in KIND_ORDER:
        g = lv[lv["kind"] == kind]
        if g.empty:
            continue
        nn = len(g)
        touched = int((g["real"] > 0).sum())
        rev = int((g["real"] == 1).sum())
        rt, rr = int(g["r_touch"].sum()), int(g["r_rev"].sum())
        rn = nn * n_random
        rows.append({
            "kind": kind, "n": nn, "n_touched": touched,
            "touch%": 100 * touched / nn,
            "reversal% of touched": 100 * rev / touched if touched else np.nan,
            "broke% of touched": 100 * int((g["real"] == 2).sum()) / touched if touched else np.nan,
            "no-resolution% of touched": 100 * int((g["real"] == 3).sum()) / touched if touched else np.nan,
            "random touch%": 100 * rt / rn,
            "random reversal%": 100 * rr / rt if rt else np.nan,
            "random broke%": 100 * int(g["r_broke"].sum()) / rt if rt else np.nan,
        })
    summ = pd.DataFrame(rows)
    summ["touch +-"] = 1.96 * 100 * np.sqrt(summ["touch%"] / 100 * (1 - summ["touch%"] / 100) / summ["n"])
    summ["reversal +-"] = 1.96 * 100 * np.sqrt(
        summ["reversal% of touched"] / 100 * (1 - summ["reversal% of touched"] / 100) / summ["n_touched"])
    summ.attrs["dropped_sessions"] = sess.attrs.get("dropped_past_dev_end", 0)
    summ["touch diff (pp)"] = summ["touch%"] - summ["random touch%"]
    summ["reversal diff (pp)"] = summ["reversal% of touched"] - summ["random reversal%"]
    return summ, lv


# --------------------------------------------------------------------------- FVG study driver
def fvg_study(inp: Inputs, n_random: int = N_RANDOM, seed: int = SEED) -> pd.DataFrame:
    """Per FVG (known_at in the development period): real fill hours; random fill hours in attrs["rand"]."""
    m1, h1 = inp.bars["M1"], inp.bars["H1"]
    t = m1["time"].to_numpy()
    h_all, l_all = m1["high"].to_numpy(float), m1["low"].to_numpy(float)
    lo, hi = pd.Timestamp(config.DEV_START), pd.Timestamp(config.DEV_END) + _DAY
    in_dev = inp.gaps[(inp.gaps["known_at"] >= lo) & (inp.gaps["known_at"] < hi)]
    keep = in_dev["known_at"] + pd.Timedelta(hours=48) <= hi - pd.Timedelta(seconds=1)  # 48 h horizon inside DEV
    g = in_dev[keep].reset_index(drop=True)
    n_dropped = int((~keep).sum())
    h1o = pd.DatetimeIndex(h1["time"])
    close_by_open = pd.Series(h1["close"].to_numpy(float), index=h1o)
    atr_by_open = inp.h1_atr.reindex(h1o)
    c3 = close_by_open.reindex(g["known_at"] - _H1).to_numpy()
    a3 = atr_by_open.reindex(g["known_at"] - _H1).to_numpy()
    d = (g["mid"].to_numpy(float) - c3) / a3
    sides = np.where(d > 0, 1, -1)
    real = np.array([fill_hours(m1, k, mid, int(s)) for k, mid, s in zip(g["known_at"], g["mid"], sides)])
    # random bases: dev-period H1 bars with a valid ATR (known at their close)
    bt = h1o + _H1
    ok = (bt >= lo) & (bt + pd.Timedelta(hours=48) <= hi - pd.Timedelta(seconds=1)) & ~np.isnan(atr_by_open.to_numpy())
    base_t = bt[ok].to_numpy()
    base_c, base_a = close_by_open.to_numpy()[ok], atr_by_open.to_numpy()[ok]
    rng = np.random.default_rng(seed)
    own = np.clip(np.searchsorted(base_t, g["known_at"].to_numpy()), 0, len(base_t) - 1)
    pick = np.stack([draw_other(rng, n_random, len(base_t), int(o)) for o in own])
    q_bar = pick.ravel()
    q_d = np.repeat(d, n_random)
    order = np.argsort(q_bar, kind="stable")
    uniq, starts = np.unique(q_bar[order], return_index=True)
    ends = np.append(starts[1:], len(q_bar))
    flat = np.full(len(q_bar), np.nan)
    for bi, s0, s1 in zip(uniq, starts, ends):
        sel = order[s0:s1]
        a = int(np.searchsorted(t, base_t[bi], side="left"))
        b = int(np.searchsorted(t, base_t[bi] + np.timedelta64(48, "h"), side="left"))
        if b <= a:
            continue
        dd = q_d[sel]
        idx = _first_idx(h_all[a:b], l_all[a:b], base_c[bi] + dd * base_a[bi], np.where(dd > 0, 1, -1))
        hrs = (t[a:b][np.minimum(idx, b - a - 1)] - base_t[bi]) / np.timedelta64(1, "h")
        flat[sel] = np.where(idx < b - a, hrs, np.nan)
    g = g.assign(real_h=real)
    g["session"] = assign_session(g["known_at"], inp.windows)["session"].to_numpy()
    g.attrs["rand"] = flat.reshape(pick.shape)
    g.attrs["dropped"] = n_dropped
    return g


def fvg_table(g: pd.DataFrame) -> pd.DataFrame:
    rand = g.attrs["rand"]
    groups: list[tuple[str, np.ndarray]] = [
        ("all", np.ones(len(g), bool)),
        ("bullish", (g["direction"] == 1).to_numpy()),
        ("bearish", (g["direction"] == -1).to_numpy()),
    ]
    groups += [(f"session: {s}", (g["session"] == s).to_numpy()) for s in config.SESSIONS]
    rows = []
    for name, m in groups:
        if not m.any():
            continue
        r, x = g["real_h"].to_numpy()[m], rand[m].ravel()
        row: dict = {"group": name, "n": int(m.sum())}
        for h in HORIZONS_H:
            row[f"within {h}h"] = f"{100 * np.mean(r <= h):.1f} / {100 * np.mean(x <= h):.1f}"
        row["never in 48h"] = f"{100 * np.mean(np.isnan(r)):.1f} / {100 * np.mean(np.isnan(x)):.1f}"
        mr = np.nanmedian(r) if (~np.isnan(r)).any() else np.nan
        mx = np.nanmedian(x) if (~np.isnan(x)).any() else np.nan
        row["median h (filled only)"] = f"{mr:.2f} / {mx:.2f}"
        rows.append(row)
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- report
def _charts(summ: pd.DataFrame, g: pd.DataFrame, out_dir) -> list[str]:
    names = []
    x = np.arange(len(summ))
    for col, rcol, title, fn in (
        ("touch%", "random touch%", "Touch rate: real vs random levels (% of levels)", "05_levels_touch.png"),
        ("reversal% of touched", "random reversal%", "Reversal rate of touched levels (%)", "05_levels_reversal.png"),
    ):
        fig, ax = plt.subplots(figsize=(9, 4))
        ax.bar(x - 0.2, summ[col], 0.4, label="real", color="#2a6fb0")
        ax.bar(x + 0.2, summ[rcol], 0.4, label="random (transplanted)", color="#b8b8b8")
        ax.set_xticks(x, summ["kind"])
        ax.set_title(title)
        ax.legend()
        fig.tight_layout()
        fig.savefig(out_dir / fn, dpi=110)
        plt.close(fig)
        names.append(fn)
    hrs = np.linspace(0, 48, 97)
    r, rd = g["real_h"].to_numpy(), g.attrs["rand"].ravel()
    fig, ax = plt.subplots(figsize=(7, 4))
    ax.plot(hrs, [100 * np.mean(r <= h) for h in hrs], label="real FVG midpoints", color="#2a6fb0")
    ax.plot(hrs, [100 * np.mean(rd <= h) for h in hrs], label="random points", color="#888888")
    ax.set_xlabel("hours after the gap is known")
    ax.set_ylabel("% reached")
    ax.set_title("FVG midpoint fill: real vs random")
    ax.legend()
    fig.tight_layout()
    fig.savefig(out_dir / "05_fvg_fill.png", dpi=110)
    plt.close(fig)
    names.append("05_fvg_fill.png")
    return names


def build_report(summ: pd.DataFrame, lv: pd.DataFrame, g: pd.DataFrame, charts: list[str]) -> str:
    o: list[str] = []
    w = o.append
    w("# 05 — Part A: do the levels and FVGs behave better than chance?\n")
    w(f"Generated by `src/xauai/levelstudy.py` at {datetime.now():%Y-%m-%d %H:%M} (local). All numbers below come from that script. "
      f"Development period only: {config.DEV_START} .. {config.DEV_END}. Random seed {SEED}; {N_RANDOM} random placements per real level / per FVG.\n")
    w("## Definitions in plain words\n")
    w("- **Terms.** *M1* = one-minute candles (the finest data used to decide what was touched first). *ATR* = Average True Range, "
      "the typical candle size; here the average over the last 14 H1 (hourly) candles, used as a yardstick for 'distance'. "
      "*Volume profile* = how much trading volume happened at each price in the previous session. "
      "*Candle 1 / candle 3* of an FVG = the first and third of three consecutive H1 candles; the gap is the empty space between candle 1's extreme and candle 3's.")
    w("- **Level kinds.** pdh/pdl = previous day's high/low; pwh/pwl = previous week's high/low; psh/psl = high/low of the "
      "previous trading session; swh/swl = the last swing high/low of the previous session; poc = price with the most volume in "
      "the previous session's volume profile; vah/val = top/bottom of the zone holding 70% of that volume.")
    w("- **Which levels are studied.** Only the level set that becomes valid at the START of each session (Asia, London, NY), not the "
      "day-start refreshes. For each such session the **session-open price** is the open of the first M1 bar at/after the session start, "
      "and **ATR** is ATR14 on H1 as known at that moment (no lookahead). A level is *above* if it is higher than the session-open price, otherwise *below*.")
    w("- **The window.** From the session start to the end of the NEXT session (the session itself plus the one after it), measured on M1 bars.")
    w("- **Touched.** Price reaches the level (M1 high >= level for a level above, M1 low <= level for one below) inside the window.")
    w("- **Reversed / broke.** After the first touch we watch what happens first: price moving 1 ATR back away from the level "
      "(towards the session open) = *reversed* (price bounced back at least one ATR); price moving 1 ATR through it = *broke*. The touch bar's own low (for a level above) counts for the reversal check. If a single M1 bar contains both, it is counted as *broke* "
      "(we cannot know the order). If neither happens before the window ends it is *touched, no resolution*, reported separately. "
      "'Reversal %' is the share of **touched** levels that reversed.")
    w(f"- **Random comparison.** Chance level: for each real level we take {N_RANDOM} OTHER sessions at random and put a level at the same signed "
      "distance from THAT session's open (distance measured in ATR units, scaled by that session's ATR), on the same side, then run the identical test in that "
      "session's window. 'Random' columns pool all those placements. A real level 'matters' only if it beats this.")
    w(f"- **FVG (fair value gap).** On H1: bullish if the high of candle 1 is below the low of candle 3 (bearish: mirror), and the middle candle's body "
      f"is at least {config.FVG_BODY_ATR}×ATR14(H1) (a 'big' gap). Known at the close of candle 3. Target = the gap's **midpoint**. 'Filled within N hours' = M1 price touches the "
      "midpoint within N hours of the gap being known (horizon 48 h). Random points = the same signed distance (in ATR units) from the close of the gap's third candle, "
      f"transplanted onto {N_RANDOM} random other H1 closes in the development period (same side); same fill test.\n")
    w("## How to read this\n")
    w("If a level really attracts or repels price, its **touch %** should be higher than the random one (random levels at that distance are what plain "
      "volatility would give you) and its **reversal %** higher than random if it acts as support/resistance. A difference of a few percentage points is "
      "probably noise; look for gaps that are large and that repeat across related kinds. The FVG tables answer 'does price always fill FVGs?': "
      "compare real with random at each horizon. If both are close, FVGs are filled about as often as any nearby price would be touched.\n")
    w(f"Sessions in study: {lv['sess'].nunique()}; real levels evaluated: {len(lv)}; FVGs: {len(g)}. "
      f"Dropped so that no data after {config.DEV_END} 23:59:59 is read: {summ.attrs.get('dropped_sessions', 0)} sessions "
      f"(window ends after it) and {g.attrs.get('dropped', 0)} FVGs (48 h horizon passes it); random pools use the same rule.\n")
    w("**Noise bands:** `touch +-` and `reversal +-` are approximate 95% binomial half-widths (1.96*sqrt(p(1-p)/n)) on the real rate, "
      "in percentage points; a `diff` smaller than about that is indistinguishable from noise. The random pool is NOT matched by "
      "session type (Asia/London/NY), only by distance and side, so small systematic differences can come from that.\n")
    w("## Level study per kind\n")
    cols = ["kind", "n", "touch%", "touch +-", "random touch%", "touch diff (pp)", "reversal% of touched", "reversal +-",
            "random reversal%", "reversal diff (pp)",
            "broke% of touched", "no-resolution% of touched", "random broke%"]
    w(table(summ[cols], floatfmt=".1f") + "\n")
    w("`diff (pp)` = real minus random, in percentage points.\n")
    for fn in charts[:2]:
        w(f"![{fn}]({fn})\n")
    w("## FVG fill study\n")
    w("Each cell is `real / random` (percent unless stated). 'never in 48h' = share whose midpoint was not reached within 48 h.\n")
    w(table(fvg_table(g)) + "\n")
    w(f"![{charts[2]}]({charts[2]})\n")
    return "\n".join(o)


def main() -> None:
    t0 = datetime.now()
    inp = load_inputs()
    summ, lv = level_study(inp)
    g = fvg_study(inp)
    config.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    charts = _charts(summ, g, config.REPORTS_DIR)
    text = build_report(summ, lv, g, charts)
    (config.REPORTS_DIR / "05_levels.md").write_text(text, encoding="utf-8")
    print(text)
    print(f"done in {(datetime.now() - t0).total_seconds():.0f}s")


if __name__ == "__main__":
    main()
