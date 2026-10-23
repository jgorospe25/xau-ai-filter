"""Phase 3: label all signals and report the raw (unfiltered) baseline in R.

Usage:  uv run python -m xauai.backtest
Writes data/labels/trades_h1.parquet (base case), reports/03_baseline.md and reports/03_r_hist.png.
"""

from __future__ import annotations

from datetime import datetime

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from xauai import config  # noqa: E402
from xauai.labels import CostParams, SwapParams, label_signals, load_symbol_info  # noqa: E402
from xauai.md import table  # noqa: E402
from xauai.signals import compute_signals  # noqa: E402

SPLITS = {
    "train": (config.TRAIN_START, config.TRAIN_END),
    "val": (config.VAL_START, config.VAL_END),
    "test": (config.TEST_START, config.TEST_END),
}
BASE_COST = config.SPREAD_FLOOR + config.SLIPPAGE
BOOTSTRAP_N = 10_000
SEED = 7
DAYS_PER_MONTH = 30.4375
MIN_SIGNALS_GATE = 300
DIR_NAME = {1: "buy", -1: "sell"}


# --- Metrics (pure) ---------------------------------------------------------
def max_drawdown(r: np.ndarray) -> float:
    equity = np.concatenate(([0.0], np.cumsum(r)))
    return float((np.maximum.accumulate(equity) - equity).max())


def longest_losing_streak(r: np.ndarray) -> int:
    best = cur = 0
    for x in r:
        cur = cur + 1 if x <= 0 else 0
        best = max(best, cur)
    return best


def profit_factor(r: np.ndarray) -> float:
    loss = -r[r < 0].sum()
    return float(r[r > 0].sum() / loss) if loss > 0 else float("inf")


def bootstrap_ci(r: np.ndarray, n: int = BOOTSTRAP_N, seed: int = SEED) -> tuple[float, float]:
    if len(r) == 0:
        return float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    means = r[rng.integers(0, len(r), size=(n, len(r)))].mean(axis=1)
    lo, hi = np.percentile(means, [2.5, 97.5])
    return float(lo), float(hi)


def metrics(r: np.ndarray, months: float) -> dict[str, float]:
    """r must be in time order."""
    n = len(r)
    if n == 0:
        return {"n": 0}
    return {
        "n": n,
        "win_rate": float((r > 0).mean()),
        "expectancy_R": float(r.mean()),
        "profit_factor": profit_factor(r),
        "max_dd_R": max_drawdown(r),
        "max_losing_streak": longest_losing_streak(r),
        "trades_per_month": n / months,
    }


# --- Helpers ----------------------------------------------------------------
def assign_split(times: pd.Series) -> pd.Series:
    out = pd.Series(pd.NA, index=times.index, dtype="object")
    for name, (a, b) in SPLITS.items():
        out[(times >= pd.Timestamp(a)) & (times < pd.Timestamp(b) + pd.Timedelta(days=1))] = name
    return out


def months_between(a: pd.Timestamp, b: pd.Timestamp) -> float:
    return ((b - a) / pd.Timedelta(days=1)) / DAYS_PER_MONTH


def split_months(name: str) -> float:
    a, b = SPLITS[name]
    return months_between(pd.Timestamp(a), pd.Timestamp(b) + pd.Timedelta(days=1))


def metrics_frame(groups: dict[str, tuple[pd.DataFrame, float]], ci: bool = False) -> pd.DataFrame:
    rows = []
    for label, (tr, months) in groups.items():
        r = tr.sort_values("signal_time")["r"].to_numpy()
        m = {"group": label, **metrics(r, months)}
        if ci:
            m["ci95_low"], m["ci95_high"] = bootstrap_ci(r)
        rows.append(m)
    df = pd.DataFrame(rows)
    if "win_rate" in df:
        df["win_rate"] = (df["win_rate"] * 100).round(1).astype(str) + "%"
    return df


def one_position_max(tr: pd.DataFrame) -> pd.DataFrame:
    """Keep trades the EA could take with one position max (skip signals while a trade is open)."""
    kept, busy_until = [], pd.Timestamp.min
    for i, row in tr.sort_values("entry_time").iterrows():
        if row.entry_time >= busy_until:
            kept.append(i)
            busy_until = row.exit_time
    return tr.loc[kept]


def run(sig: pd.DataFrame, info: dict, total_cost: float, scaled_swap: bool) -> tuple[pd.DataFrame, pd.DataFrame]:
    cost = CostParams(spread_floor=total_cost - config.SLIPPAGE, slippage=config.SLIPPAGE, point=info["point"])
    ref = float(sig["close"].iloc[-1]) if scaled_swap else None
    swap = SwapParams.from_symbol_info(info, reference_close=ref)
    trades, skipped = label_signals(sig, cost, swap)
    trades["split"] = assign_split(trades["signal_time"])
    trades["side"] = trades["direction"].map(DIR_NAME)
    return trades, skipped


def plot_hist(tr: pd.DataFrame, path) -> None:
    surface, ink, muted, bar = "#fcfcfb", "#0b0b0b", "#52514e", "#2a78d6"
    r = tr["r"].to_numpy()
    lo, hi = np.floor(r.min() * 10) / 10, np.ceil(r.max() * 10) / 10
    fig, ax = plt.subplots(figsize=(9, 4.5), facecolor=surface)
    ax.set_facecolor(surface)
    # bins centred on multiples of 0.1R so the -1R / +2R spikes fall in one bin each
    ax.hist(r, bins=np.arange(lo - 0.05, hi + 0.1, 0.1), color=bar, edgecolor=surface, linewidth=1.0)
    for x, lab in ((-1, "−1R (SL)"), (config.RR, f"+{config.RR:g}R (TP)")):
        ax.axvline(x, color=muted, linewidth=1, linestyle=(0, (4, 3)))
        ax.text(x, ax.get_ylim()[1] * 0.97, f" {lab}", color=muted, fontsize=9, va="top")
    ax.axvline(r.mean(), color=ink, linewidth=1.5)
    ax.text(r.mean(), ax.get_ylim()[1] * 0.80, f" mean {r.mean():+.3f}R", color=ink, fontsize=9, va="top")
    ax.set_title(f"R per trade — raw baseline, all splits (n={len(r)}, cost {BASE_COST:.2f}, scaled swap)",
                 color=ink, fontsize=11, loc="left")
    ax.set_xlabel("R (net of costs and swap)", color=muted)
    ax.set_ylabel("trades", color=muted)
    ax.grid(axis="y", color="#e4e3df", linewidth=0.8)
    ax.set_axisbelow(True)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color("#c9c8c2")
    ax.tick_params(colors=muted)
    fig.tight_layout()
    fig.savefig(path, dpi=150, facecolor=surface)
    plt.close(fig)


# --- Report -----------------------------------------------------------------
def build_report(raw: pd.DataFrame, sig: pd.DataFrame, info: dict, base: pd.DataFrame, skipped: pd.DataFrame,
                 grid: dict[tuple[float, bool], pd.DataFrame]) -> str:
    out: list[str] = []
    w = out.append
    n_sig = int((sig["signal"] != 0).sum())
    gate = len(base) >= MIN_SIGNALS_GATE
    w(f"# 03 — Labels & raw baseline: {config.SYMBOL} {config.TIMEFRAME}\n")
    w(f"Generated by `src/xauai/backtest.py` at {datetime.now():%Y-%m-%d %H:%M} (local). "
      "All numbers below come from that script.\n")
    w("**Base case:** entry at next bar open; cost = max(bar spread, "
      f"{config.SPREAD_FLOOR:.2f}) + {config.SLIPPAGE:.2f} slippage (applied by shifting the entry against the "
      f"trade); SL = {config.SL_ATR_MULT}×ATR, TP = {config.RR}×SL; SL+TP in one bar = loss; gap-through fills "
      f"at the bar open; forced exit at Friday {config.FRIDAY_EXIT_HOUR}:00 open and at the close before any "
      f">{config.HOLIDAY_GAP_BARS}-bar gap; timeout {config.TIMEOUT_BARS} bars at close. Swap per server-midnight "
      "rollover, ×3 for the Wed→Thu rollover, scaled by price (swap_now × close_at_signal / last_close).\n")
    w(f"**Gate (≥ ~{MIN_SIGNALS_GATE} labeled signals): {'PASS' if gate else 'FAIL'}** — "
      f"{len(base)} labeled trades from {n_sig} signals.\n")

    # 1. Counts
    w("## 1. Signal counts per split and direction\n")
    c = base.pivot_table(index="split", columns="side", values="r", aggfunc="size", fill_value=0)
    c = c.reindex(list(SPLITS)).fillna(0).astype(int)
    c["total"] = c.sum(axis=1)
    c = c.reset_index()
    c.loc[len(c)] = ["all", *c[["buy", "sell", "total"]].sum().tolist()]
    w(table(c[["split", "buy", "sell", "total"]]))
    w("")
    w(f"Signals not traded: {len(skipped)}" + (
        " — " + ", ".join(f"{k}: {v}" for k, v in skipped["reason"].value_counts().items()) if len(skipped) else ""))
    w("(The EA must apply the same entry rules: no entries from Friday 22:00 until the weekly open, and no "
      "stale signals across holiday gaps.)\n")

    # 2. Exit breakdown
    w("## 2. Exit breakdown\n")
    order = ["tp", "sl", "sl_same_bar", "gap_sl", "gap_tp", "timeout", "friday", "holiday", "end_of_data"]
    e = base.pivot_table(index="exit_reason", columns="split", values="r", aggfunc="size", fill_value=0)
    e = e.reindex(index=[o for o in order if o in e.index], columns=list(SPLITS), fill_value=0)
    e["all"] = e.sum(axis=1)
    e["share"] = (e["all"] / e["all"].sum() * 100).round(1).astype(str) + "%"
    avg_r = base.groupby("exit_reason")["r"].mean()
    e["mean_R"] = avg_r.reindex(e.index).round(3)
    w(table(e.reset_index()))
    w("")
    w("- `sl_same_bar`: SL and TP both touched in one bar → counted as a loss.")
    w("- `gap_sl` / `gap_tp`: bar opened beyond the level → filled at that open.")
    w(f"- Trades losing more than 1R: {int((base['r_price'] < -1 - 1e-9).sum())} "
      f"(worst {base['r_price'].min():.3f}R before swap).\n")

    # 3. Metrics
    w("## 3. Baseline metrics\n")
    w("Expectancy, drawdown and streaks are in R, net of costs and swap, in signal-time order "
      "(every signal counted, overlapping trades included).\n")
    w("### Per split\n")
    groups = {s: (base[base["split"] == s], split_months(s)) for s in SPLITS}
    first, last = raw["time"].iloc[0], raw["time"].iloc[-1] + pd.Timedelta(hours=1)
    groups["all"] = (base, months_between(first, last))
    w(table(metrics_frame(groups)))
    w("")
    w("### Per year\n")
    years = {}
    for y, g in base.groupby(base["signal_time"].dt.year):
        a = max(pd.Timestamp(f"{y}-01-01"), first)
        b = min(pd.Timestamp(f"{y + 1}-01-01"), last)
        years[str(y)] = (g, months_between(a, b))
    w(table(metrics_frame(years)))
    w("")
    w("### Per direction (by split)\n")
    dgroups = {}
    for s in [*SPLITS, "all"]:
        sub, months = groups[s]
        for d in ("buy", "sell"):
            dgroups[f"{s} / {d}"] = (sub[sub["side"] == d], months)
    w(table(metrics_frame(dgroups)))
    w("")
    w(f"Breakeven win rate at 1:{config.RR:g} with exact −1R/+2R outcomes = 33.3%; costs and swap raise it.\n")

    # 4. Bootstrap
    w(f"## 4. Bootstrap 95% CI for expectancy ({BOOTSTRAP_N:,} resamples, seed {SEED})\n")
    ci = metrics_frame(groups, ci=True)[["group", "n", "expectancy_R", "ci95_low", "ci95_high"]]
    w(table(ci))
    w("")

    # 5. Sensitivity
    w("## 5. Cost & swap sensitivity (expectancy, R)\n")
    w("Cost = total per trade (spread floor + 0.05 slippage). Floors 0.30 / 0.45 / 0.70 bind on every bar "
      "except where the bar spread exceeds the floor.\n")
    rows = []
    for (cost, scaled), tr in grid.items():
        row = {"cost": f"{cost:.2f}", "swap": "scaled" if scaled else "flat"}
        for s in SPLITS:
            row[s] = tr.loc[tr["split"] == s, "r"].mean()
        row["all"] = tr["r"].mean()
        rows.append(row)
    w(table(pd.DataFrame(rows)))
    w("")

    # 6. Nights and swap
    w("## 6. Nights held and swap, per direction\n")
    rows = []
    for scaled in (True, False):
        tr = grid[(BASE_COST, scaled)]
        for d in ("buy", "sell"):
            g = tr[tr["side"] == d]
            rows.append({
                "swap": "scaled" if scaled else "flat", "side": d, "trades": len(g),
                "avg_nights": g["nights"].mean(), "avg_charged_nights": g["swap_nights"].mean(),
                "share_held_overnight": f"{(g['nights'] > 0).mean():.1%}",
                "avg_swap_R": g["swap_r"].mean(), "total_swap_R": g["swap_r"].sum(),
            })
    w(table(pd.DataFrame(rows)))
    w("")
    w(f"Current swap (points/night/lot): long {info['swap_long']}, short {info['swap_short']}; charged-nights "
      "counts the Wed→Thu rollover as 3.\n")

    # 7. Spread
    w("## 7. Bars with spread above the 0.30 floor\n")
    sp = raw["spread"] * info["point"]
    above = int((sp > config.SPREAD_FLOOR + 1e-12).sum())
    w(f"- Bars with bar_spread > {config.SPREAD_FLOOR:.2f}: **{above:,} of {len(raw):,} = {above / len(raw):.4%}**")
    ent = base["cost"] > BASE_COST + 1e-12
    w(f"- Trades whose entry-bar spread exceeded the floor: {int(ent.sum())} of {len(base)}\n")

    # 8. Histogram
    w("## 8. Distribution of R per trade\n")
    w("![R per trade](03_r_hist.png)\n")
    q = base["r"].quantile([0.05, 0.25, 0.5, 0.75, 0.95])
    w(f"Quantiles: p5 {q[0.05]:.3f}, p25 {q[0.25]:.3f}, median {q[0.5]:.3f}, p75 {q[0.75]:.3f}, p95 {q[0.95]:.3f}.\n")

    # Notes
    w("## Notes for Phase 4\n")
    opm = one_position_max(base)
    w(f"- **Overlap:** with one position max (EA rule), {len(opm)} of {len(base)} trades would be taken; "
      f"expectancy of that subset {opm['r'].mean():+.3f}R. Labels keep every signal as a training sample.")
    for s, (a, b) in SPLITS.items():
        g = base[base["split"] == s]
        spill = int((g["exit_time"] >= pd.Timestamp(b) + pd.Timedelta(days=1)).sum())
        w(f"- **Label spill-over ({s}):** {spill} trade(s) exit after the split ends — purge/embargo at fold edges.")
    w("")
    return "\n".join(out) + "\n"


def main() -> None:
    raw = pd.read_parquet(config.RAW_H1_PATH)
    info = load_symbol_info()
    sig = compute_signals(raw)

    grid: dict[tuple[float, bool], pd.DataFrame] = {}
    skipped = None
    for cost in (BASE_COST, *config.SENSITIVITY_TOTAL_COSTS):
        for scaled in (True, False):
            tr, sk = run(sig, info, cost, scaled)
            grid[(cost, scaled)] = tr
            skipped = sk if skipped is None else skipped
    base = grid[(BASE_COST, True)]

    config.LABELS_PATH.parent.mkdir(parents=True, exist_ok=True)
    base.to_parquet(config.LABELS_PATH, index=False)
    plot_hist(base, config.REPORTS_DIR / "03_r_hist.png")
    report = config.REPORTS_DIR / "03_baseline.md"
    report.write_text(build_report(raw, sig, info, base, skipped, grid), encoding="utf-8")
    print(f"{len(base)} trades, expectancy {base['r'].mean():+.3f}R -> {report}")


if __name__ == "__main__":
    main()
