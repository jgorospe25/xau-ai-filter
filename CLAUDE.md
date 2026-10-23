# XAU AI Filter — CLAUDE.md

ML filter on top of a rule-based XAUUSD signal. A LightGBM model predicts whether each
EMA/RSI signal will hit TP before SL; an MT5 Expert Advisor runs the model via ONNX and
only trades signals above a probability threshold.

## Context
- Platform: MetaTrader 5 (Windows), broker Vantage, symbol `XAUUSD.sc`
- Accounts: Standard USD and Standard Cent (USC). Lot sizing must work for both via tick value.
- Timeframe: **H1 first**. M5 is a later phase, not now.
- Env: Windows PowerShell, Python via `uv`. MT5 terminal must be running and logged in for data export.
- Existing indicator: `mql5/XAU_EMA_RSI_Signal.mq5` (v1.1) — the **source of truth** for signal rules.
- MT5 data folder: `C:\Users\joshua\AppData\Roaming\MetaQuotes\Terminal\D0E8209F77C8CF37AD8BF550E51FF075` (MQL5/Experts, MQL5/Files live here)
- MetaEditor: `C:\Program Files\MetaTrader 5\MetaEditor64.exe` (CLI compile: `/compile:<file.mq5> /log`)
- Data: H1 history on the server starts **2022-05-16** (~26k bars). Server has a daily break: the 00:00 bar never exists.

## Signal rules (must match the indicator exactly)
Evaluated on **closed** bars only.
- BUY: EMA9 crosses above EMA21 (`fast[i] > slow[i] && fast[i-1] <= slow[i-1]`), 50 < RSI14 < 70, close > EMA200
- SELL: mirror (RSI between 30 and 50, close < EMA200)
- Volatility floor: ATR14[i] >= SMA50(ATR14)[i] * 0.8 (H1 Auto preset)
- No session filter on H1
- SL = 1.5 × ATR14 at signal bar, TP = 2.0 × SL distance, risk 1% of balance

## Costs (Phase 1 review)
- MT5 bar `spread` understates real costs (live spread ~0.26 vs historical p99 0.21).
- Per-trade cost (price units) = **max(bar_spread × point, 0.30) + 0.05 slippage**.
  Zero-spread bars are data artifacts — the 0.30 floor covers them.
- Sensitivity runs at **total** cost 0.50 and 0.75 (spread floor 0.45 / 0.70 + 0.05). Base = 0.35.
- Cost is applied by shifting the entry against the trade (buy at ask); SL/TP are set from that
  fill and checked on bid bars. So SL = exactly −1R and cost shows up as fewer TP / more SL hits.
- **EA requirement:** set SL/TP from the **actual fill price** of the position
  (`PositionGetDouble(POSITION_PRICE_OPEN)`), not from the bar open or the signal close, using the
  signal-bar ATR — exactly as the labels do. Otherwise live R will not match the labels.
- **Swap**: charged for each server-midnight (hour-0) rollover the position is held through,
  ×3 for the rollover at the end of `swap_rollover3days` (MT5 day-of-week, 0=Sunday; 3 = Wed→Thu).
  Symbol swap fields are exported to `data/raw/symbol_info.json` (`swap_mode`, `swap_long`,
  `swap_short`, `swap_rollover3days`). Historical swap scaled by price:
  `swap_now × close_at_signal / last_close`; flat current swap reported for comparison.

## Labels (triple barrier, must match the EA)
- Entry = next bar open (+ cost above). SL/TP from signal-bar ATR.
- **No entry** if the entry bar is at/after Friday 22:00, or follows a > 24-bar gap (stale signal).
- SL and TP both inside one bar → loss.
- **Gap-through fill**: if a bar opens beyond SL/TP, fill at that bar's open (losses can exceed 1R).
- **Forced exits**: at the open of the **Friday 22:00** server bar; and at the close of the last
  bar before any gap of **> 24 bars** (holidays). Timeout after 48 bars → exit at close.
- Outcomes in R, net of costs and swap.

## Features
- **Scale-free only**: ATR multiples, % returns, ratios, oscillator values. No raw prices, no raw ATR.

## Splits (fixed, time-ordered)
- Train: 2022-05-16 .. 2024-12-31
- Validation: 2025-01-01 .. 2025-10-23
- Test (touch once): 2025-10-24 .. 2026-10-23
- Phase 3 baseline reports metrics on these three splits.

## Phase 4 plan: expanding-window walk-forward
- Out-of-sample period **2023-01-01 .. 2025-10-23**, in 6-month folds (2023-01, 2023-07, 2024-01,
  2024-07, 2025-01, 2025-07 → 2025-10-23).
- For each fold: train on **all labeled signals before the fold start** (expanding window),
  predict the fold. Retrain every 6 months.
- **Purge**: drop training samples whose trade exits at/after the fold start (label overlap).
- Threshold and hyperparameters are chosen on the pooled walk-forward OOS predictions only.
- The final **test set 2025-10-24 .. 2026-10-23 stays untouched** until the very end: one final
  model trained on everything before 2025-10-24, evaluated once.

## Indicator parity gotchas (MT5 formulas)
- `iMA` EMA: alpha = 2/(n+1); warm-up differences vanish after a few hundred bars — compare only after warm-up.
- `iRSI`: Wilder smoothing (SMA seed for first value, then (prev*(n-1)+x)/n).
- `iATR`: **simple moving average of True Range**, NOT Wilder. Easy to get wrong.
- All prices = PRICE_CLOSE. Bar time = open time, broker server time.
- Seeds (as in MT5's reference sources): EMA starts at price[0]; RSI first value at index n from
  diffs 1..n; ATR first value at index n = mean(TR[1..n]), then running-sum SMA. TR[0] is unused.
- The indicator only evaluates bars with index >= g_minBars = max(200, 50+14)+5 = 205, counted
  from the **first bar of terminal history** — Python data must start at the same bar.
- Parity workflow: run `Scripts/xauai/DumpSignals` on any chart → `uv run python -m xauai.parity`
  (copies `MQL5/Files/xauai_dump_signals_h1.csv` to `data/mt5/`, writes `reports/02_parity.md`).

## Project layout
```
src/xauai/
  export.py        # MT5 → data/raw/xauusd_h1.parquet (copy_rates_range, includes spread)
  indicators.py    # EMA/RSI/ATR matching MT5
  signals.py       # replicate indicator signals
  labels.py        # triple-barrier outcomes in R
  features.py      # features known at signal-bar close only
  backtest.py      # expectancy / win rate / drawdown in R
  train.py         # time-ordered splits, LightGBM, threshold selection
  export_onnx.py   # LightGBM → ONNX + parity check
mql5/
  XAU_EMA_RSI_Signal.mq5   # existing indicator (do not change rules)
  DumpSignals.mq5          # script: iCustom on indicator → CSV for parity tests
  XAU_AI_Filter_EA.mq5     # EA with embedded ONNX model
data/ (gitignored)   reports/   tests/
```

## Hard rules
1. **No lookahead.** Features use only data available at the signal bar's close. Labels start at the **next bar's open**.
2. **Time-ordered splits only.** Never shuffle. Use the fixed dates in "Splits"; the test set is **touched once**, at the end.
3. **Labels are conservative:** entry = next bar open + spread; if SL and TP are both inside one bar, count it as a loss; timeout after 48 bars → label by realized R at close.
4. **Costs included:** spread floor + slippage + swap as defined in "Costs".
5. **Every number in a report comes from code**, saved in `reports/`. No hand-estimated metrics.
6. **Baseline first.** Always report the raw (unfiltered) signal expectancy next to the filtered one.
7. **Keep the model small** (shallow trees, few features) — signals are only a few hundred samples.
8. **Feature parity is the #1 production risk.** The EA must compute features identically; prove it with a fixture CSV comparison before any live use.
9. ONNX export with **`zipmap=False`** and float32 inputs — MQL5 cannot read ZipMap outputs.
10. Never place live orders from Python. Python is for research; the EA trades, demo first.

## Metrics to report (in R units)
Signal count, win rate, expectancy per trade, profit factor, max drawdown, longest losing streak,
trades per month — for baseline vs filtered, on validation and test separately.
Breakeven win rate at 1:2 RR = 33.3%.

## Phases (stop and report after each gate)
0. Setup: `uv init`, deps (MetaTrader5, pandas, numpy, pyarrow, lightgbm, scikit-learn,
   onnxmltools, onnx, onnxruntime, matplotlib, pytest). Use a Python version that MetaTrader5 ships wheels for.
1. Export max available H1 history; sanity report (date range, gaps, weekend bars, spread stats).
2. Indicators + signals. **Gate:** matches `DumpSignals.mq5` CSV (signal times identical after warm-up).
3. Labels + baseline backtest. **Gate:** ≥ ~300 signals total; report raw expectancy.
4. Features + LightGBM, expanding-window walk-forward (see "Phase 4 plan"), threshold chosen on
   walk-forward OOS only. **Gate:** filtered expectancy beats baseline on walk-forward OOS AND holds
   on test with enough trades. If not → stop, report honestly.
   **Result (2026-10-24): FAIL** — LR and LightGBM OOS AUC < 0.5; neither beats baseline or the rule
   baseline. Test set untouched. See `reports/04_model.md`.
5. ONNX export. **Gate:** onnxruntime vs LightGBM probabilities match (max abs diff < 1e-5).
6. EA: same signals + same features + ONNX + CTrade, lot sizing via tick value, one position max,
   magic number, close all Friday 22:00 server time. Strategy Tester (real ticks) vs Python results.
7. Demo forward test ≥ 4 weeks before any live cent-account use.

## Conventions
- Type hints, small pure functions, pytest for indicators/labels/parity.
- Config (periods, multipliers, thresholds, symbol) in one `config.py`, mirrored as EA inputs.
- Commit after each phase gate passes.
