"""Export max available H1 history from a running MT5 terminal and write a sanity report.

Usage:  uv run python -m xauai.export

MT5 bar times are broker server time encoded as epoch seconds; they are kept as
naive datetimes (no timezone conversion). The still-forming last bar is dropped.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone

import MetaTrader5 as mt5
import numpy as np
import pandas as pd

from xauai import config
from xauai.timeutil import TF_LEN

KEEP_COLS = ["time", "open", "high", "low", "close", "tick_volume", "spread"]
SYMBOL_FIELDS = [
    "digits",
    "point",
    "trade_tick_size",
    "trade_tick_value",
    "trade_tick_value_profit",
    "trade_tick_value_loss",
    "trade_contract_size",
    "volume_min",
    "volume_step",
    "volume_max",
    "spread",
    "spread_float",
    "currency_base",
    "currency_profit",
    "currency_margin",
    "swap_mode",  # 1 = SYMBOL_SWAP_MODE_POINTS
    "swap_long",
    "swap_short",
    "swap_rollover3days",  # day of week, 0 = Sunday
]
# Earliest start dates to try; some terminals reject very old dates.
START_CANDIDATES = [datetime(y, 1, 1, tzinfo=timezone.utc) for y in (1970, 1990, 2000, 2005, 2010)]
MAX_FETCH_ROUNDS = 5  # repeat while the terminal is still downloading history
BAR = pd.Timedelta(hours=1)


# --- MT5 -------------------------------------------------------------------
def connect(symbol: str) -> None:
    if not mt5.initialize():
        raise RuntimeError(f"mt5.initialize() failed: {mt5.last_error()} (is the terminal running and logged in?)")
    if not mt5.symbol_select(symbol, True):
        raise RuntimeError(f"symbol_select({symbol!r}) failed: {mt5.last_error()}")


def fetch_rates(symbol: str, timeframe: int) -> pd.DataFrame:
    """copy_rates_range from the earliest accepted start date to now.

    Repeats until the bar count stops growing, since the first request can return
    only what the terminal has cached while it downloads more from the server.
    """
    date_to = datetime.now(timezone.utc) + timedelta(days=2)  # server time may run ahead of UTC
    best: np.ndarray | None = None
    for date_from in START_CANDIDATES:
        prev_len = -1
        for _ in range(MAX_FETCH_ROUNDS):
            rates = mt5.copy_rates_range(symbol, timeframe, date_from, date_to)
            if rates is None or len(rates) == 0:
                break
            if best is None or len(rates) > len(best):
                best = rates
            if len(rates) == prev_len:
                break
            prev_len = len(rates)
        if best is not None:
            break
    if best is None:
        raise RuntimeError(f"copy_rates_range returned no data: {mt5.last_error()}")
    df = pd.DataFrame(best)
    df["time"] = pd.to_datetime(df["time"], unit="s")
    return df[KEEP_COLS]


def drop_forming_bar(df: pd.DataFrame, server_now: pd.Timestamp, bar: pd.Timedelta = BAR) -> pd.DataFrame:
    """Drop the last bar if it has not closed yet at the latest tick time."""
    if len(df) and df["time"].iloc[-1] + bar > server_now:
        return df.iloc[:-1]
    return df


def symbol_snapshot(symbol: str) -> dict:
    info = mt5.symbol_info(symbol)
    if info is None:
        raise RuntimeError(f"symbol_info({symbol!r}) failed: {mt5.last_error()}")
    snap = {f: getattr(info, f) for f in SYMBOL_FIELDS}
    acct = mt5.account_info()
    term = mt5.terminal_info()
    snap["account_currency"] = acct.currency if acct else None
    snap["account_company"] = acct.company if acct else None
    snap["account_server"] = acct.server if acct else None
    snap["terminal_maxbars"] = term.maxbars if term else None
    snap["terminal_build"] = term.build if term else None
    return snap


# --- Sanity checks (pure) ---------------------------------------------------
def classify_gaps(times: pd.Series) -> pd.DataFrame:
    """One row per gap > 1 bar: previous bar time, next bar time, missing bars, kind.

    kind = "weekend" if the gap starts on Friday and ends Sat/Sun/Mon within 4 days,
    otherwise "other".
    """
    t = times.reset_index(drop=True)
    delta = t.diff()
    idx = delta[delta > BAR].index
    gaps = pd.DataFrame(
        {
            "prev_bar": t[idx - 1].to_numpy(),
            "next_bar": t[idx].to_numpy(),
            "missing_bars": ((delta[idx] / BAR) - 1).astype(int).to_numpy(),
        }
    )
    weekend = (
        (gaps["prev_bar"].dt.day_of_week == 4)
        & (gaps["next_bar"].dt.day_of_week.isin([5, 6, 0]))
        & (gaps["next_bar"] - gaps["prev_bar"] < pd.Timedelta(days=4))
    )
    gaps["kind"] = np.where(weekend, "weekend", "other")
    return gaps


def ohlc_issues(df: pd.DataFrame) -> dict[str, int]:
    return {
        "high < max(open, close)": int((df["high"] < df[["open", "close"]].max(axis=1)).sum()),
        "low > min(open, close)": int((df["low"] > df[["open", "close"]].min(axis=1)).sum()),
        "non-positive price": int((df[["open", "high", "low", "close"]] <= 0).any(axis=1).sum()),
        "NaN in any column": int(df.isna().any(axis=1).sum()),
        "tick_volume == 0": int((df["tick_volume"] == 0).sum()),
    }


# --- Report -----------------------------------------------------------------
def _md_table(df: pd.DataFrame) -> str:
    cols = [str(c) for c in df.columns]
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for row in df.itertuples(index=False):
        lines.append("| " + " | ".join(str(v) for v in row) + " |")
    return "\n".join(lines)


def build_report(df: pd.DataFrame, snap: dict, dropped_forming: bool) -> str:
    point = snap["point"]
    times = df["time"]
    n = len(df)
    dup_count = int(times.duplicated(keep=False).sum())
    monotonic = bool(times.is_monotonic_increasing)
    weekend_bars = df[times.dt.day_of_week >= 5]
    gaps = classify_gaps(times.drop_duplicates().sort_values())
    other = gaps[gaps["kind"] == "other"]

    spread_px = df["spread"] * point
    q = [0.0, 0.05, 0.25, 0.5, 0.75, 0.95, 0.99, 1.0]
    spread_q = spread_px.quantile(q)
    by_year = (
        df.assign(year=times.dt.year, spread_px=spread_px)
        .groupby("year")
        .agg(bars=("spread_px", "size"), median=("spread_px", "median"), p95=("spread_px", lambda s: s.quantile(0.95)),
             zero_spread_bars=("spread_px", lambda s: int((s == 0).sum())))
        .reset_index()
    )
    bars_by_year = by_year[["year", "bars"]]

    out: list[str] = []
    w = out.append
    w(f"# 01 — Data sanity: {config.SYMBOL} {config.TIMEFRAME}\n")
    w(f"Generated by `src/xauai/export.py` at {datetime.now():%Y-%m-%d %H:%M} (local). "
      f"Source: `{config.RAW_H1_PATH.relative_to(config.ROOT).as_posix()}`. Times are broker server time (bar open).\n")

    w("## Coverage\n")
    w(f"- First bar: **{times.iloc[0]}**")
    w(f"- Last closed bar: **{times.iloc[-1]}**" + ("  (still-forming bar dropped)" if dropped_forming else ""))
    w(f"- Span: {(times.iloc[-1] - times.iloc[0]).days} days")
    w(f"- Bar count: **{n:,}**")
    w(f"- Terminal `maxbars` setting: {snap['terminal_maxbars']:,}" if snap["terminal_maxbars"] else "- Terminal `maxbars`: n/a")
    if snap["terminal_maxbars"] and n >= snap["terminal_maxbars"] - 10:
        w("- **Warning:** bar count is at the terminal's \"Max bars in chart\" limit — history is likely truncated. "
          "Raise it in Tools → Options → Charts and re-export.")
    w("")
    w("Bars per year:\n")
    w(_md_table(bars_by_year))
    w("")

    w("## Integrity\n")
    w(f"- Duplicate timestamps (rows involved): **{dup_count}**")
    w(f"- Timestamps strictly increasing: **{monotonic and dup_count == 0}**")
    w(f"- Bars on Saturday/Sunday: **{len(weekend_bars)}**")
    if len(weekend_bars):
        w("")
        w(_md_table(weekend_bars.head(20)))
    w("")
    w("OHLC checks (row counts):\n")
    w(_md_table(pd.DataFrame(list(ohlc_issues(df).items()), columns=["check", "rows"])))
    w("")

    w("## Gaps\n")
    w(f"- Gaps > 1 bar: {len(gaps):,} (weekend: {int((gaps['kind'] == 'weekend').sum()):,}, other: {len(other):,})")
    w("- \"Weekend\" = gap starting Friday and resuming Sat/Sun/Mon within 4 days. Everything else is listed below.\n")
    if len(other):
        w("Non-weekend gaps by size (missing bars):\n")
        size = other["missing_bars"]
        buckets = pd.cut(size, bins=[0, 1, 2, 5, 24, 72, np.inf], labels=["1", "2", "3-5", "6-24", "25-72", ">72"])
        w(_md_table(buckets.value_counts(sort=False).rename_axis("missing_bars").reset_index(name="gaps")))
        w("")
        w("Hour (server) of the first missing bar, for 1-bar gaps — shows recurring daily breaks:\n")
        one = other[other["missing_bars"] == 1]
        if len(one):
            hours = (one["prev_bar"] + BAR).dt.hour.value_counts().sort_index()
            w(_md_table(hours.rename_axis("hour").reset_index(name="gaps")))
        w("")
        w("Largest 25 non-weekend gaps:\n")
        top = other.sort_values("missing_bars", ascending=False).head(25).copy()
        top["prev_dow"] = top["prev_bar"].dt.day_name().str[:3]
        w(_md_table(top[["prev_bar", "prev_dow", "next_bar", "missing_bars"]]))
    w("")

    w("## Spread (price units = spread points × point)\n")
    w(f"Point = {point}. Bar `spread` is as recorded by MT5 for each bar.\n")
    w(_md_table(pd.DataFrame({"quantile": [f"p{int(x * 100)}" for x in q], "spread": spread_q.round(5).to_numpy()})))
    w("")
    w(f"- Mean: {spread_px.mean():.5f}; zero-spread bars: {int((df['spread'] == 0).sum()):,} (data artifacts)")
    for floor in (config.SPREAD_FLOOR, *(c - config.SLIPPAGE for c in config.SENSITIVITY_TOTAL_COSTS)):
        w(f"- Bars where the {floor:.2f} cost floor binds: {(spread_px < floor).mean():.1%}")
    w(f"- Cost model: max(bar spread, floor) + {config.SLIPPAGE} slippage")
    w("")
    w("By year:\n")
    by_year[["median", "p95"]] = by_year[["median", "p95"]].round(5)
    w(_md_table(by_year))
    w("")

    w("## Symbol info (live snapshot from terminal)\n")
    rows = [(k, snap[k]) for k in SYMBOL_FIELDS] + [
        (k, snap[k]) for k in ("account_currency", "account_company", "account_server", "terminal_build")
    ]
    w(_md_table(pd.DataFrame(rows, columns=["field", "value"])))
    w("")
    w("Tick values are in account currency and reflect the current price (they change for non-USD profit currencies).")
    return "\n".join(out) + "\n"


# --- Entry point ------------------------------------------------------------
def main() -> None:
    tf = getattr(mt5, f"TIMEFRAME_{config.TIMEFRAME}")
    connect(config.SYMBOL)
    try:
        df = fetch_rates(config.SYMBOL, tf)
        tick = mt5.symbol_info_tick(config.SYMBOL)
        server_now = pd.to_datetime(tick.time, unit="s") if tick else df["time"].iloc[-1] + BAR
        n_before = len(df)
        df = drop_forming_bar(df, server_now).reset_index(drop=True)
        d1 = fetch_rates(config.SYMBOL, mt5.TIMEFRAME_D1)
        d1 = drop_forming_bar(d1, server_now, pd.Timedelta(days=1)).reset_index(drop=True)
        snap = symbol_snapshot(config.SYMBOL)
    finally:
        mt5.shutdown()

    config.RAW_DIR.mkdir(parents=True, exist_ok=True)
    df.to_parquet(config.RAW_H1_PATH, index=False)
    d1.to_parquet(config.RAW_D1_PATH, index=False)
    config.SYMBOL_INFO_PATH.write_text(json.dumps(snap, indent=2, default=str), encoding="utf-8")
    print(f"Saved {len(df):,} bars {df['time'].iloc[0]} -> {df['time'].iloc[-1]} to {config.RAW_H1_PATH}")

    config.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report_path = config.REPORTS_DIR / "01_data.md"
    report_path.write_text(build_report(df, snap, dropped_forming=len(df) < n_before), encoding="utf-8")
    print(f"Wrote {report_path}")


RESEARCH_TFS = ("M1", "M5", "M15", "M30", "H1", "H4", "D1")


def export_research(tfs: tuple[str, ...] = RESEARCH_TFS) -> dict[str, int]:
    """Export closed bars for each timeframe to data/raw/research/ (one parquet per TF).

    Also writes the symbol snapshot to data/raw/research/symbol_info.json. Phase 3 files
    (data/raw/xauusd_h1.parquet, xauusd_d1.parquet, symbol_info.json) are never touched.
    """
    counts: dict[str, int] = {}
    connect(config.SYMBOL)
    try:
        tick = mt5.symbol_info_tick(config.SYMBOL)
        server_now = pd.to_datetime(tick.time, unit="s") if tick else None
        config.RESEARCH_RAW_DIR.mkdir(parents=True, exist_ok=True)
        for tf in tfs:
            df = fetch_rates(config.SYMBOL, getattr(mt5, f"TIMEFRAME_{tf}"))
            now = server_now if server_now is not None else df["time"].iloc[-1] + TF_LEN[tf]
            df = drop_forming_bar(df, now, TF_LEN[tf]).reset_index(drop=True)
            df.to_parquet(config.research_path(tf), index=False)
            counts[tf] = len(df)
            print(f"{tf}: {len(df):,} bars {df['time'].iloc[0]} -> {df['time'].iloc[-1]}")
        snap = symbol_snapshot(config.SYMBOL)
    finally:
        mt5.shutdown()
    config.RESEARCH_SYMBOL_INFO_PATH.write_text(json.dumps(snap, indent=2, default=str), encoding="utf-8")
    return counts


def cli(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Export XAUUSD bars from a running MT5 terminal.")
    parser.add_argument("--research", action="store_true", help="export M1..D1 to data/raw/research/")
    args = parser.parse_args(argv)
    if args.research:
        export_research()
    else:
        main()


if __name__ == "__main__":
    cli()
