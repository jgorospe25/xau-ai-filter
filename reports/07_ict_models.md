# 07 — ICT entry models: XAUUSD.sc

## Pre-registration (written and committed before any batch 2 result exists)

### Why
Batch 1 (`reports/05_levels.md`, `reports/06_setups.md`) tested the owner's four setups as exact rules.
208 variants were tried and none was positive in every year. On M5, spread and slippage cost about 14% of
every stop, which is a big hurdle. The owner also uses ICT entry models. Batch 2 tests three of them
(Model 1, Model 3, Model 5), plus a kill-zone filter, under the same honest process: exact rules written first,
real costs, random baselines, and a test period touched once. "These do not work either" is a valid result.
Every rule below is in plain words so the owner can check it against how they really trade.

### Time and sessions
- All bar times are broker server time (bar open time). Sessions are defined in their own local time, so
  daylight saving is handled automatically. PH time is Asia/Manila (no daylight saving).
- **Asian range (for Model 5):** 20:00-24:00 New York. In US winter time that is 09:00-13:00 PH time; during
  US daylight saving it is 08:00-12:00 PH time. The box is ready by early afternoon PH time (owner's choice).
- **Kill zones (ICT):** London 02:00-05:00 New York, and New York 07:00-10:00 New York. In US winter time that
  is 15:00-18:00 and 20:00-23:00 PH time; one hour earlier during US daylight saving.
- **Owner's hours:** 08:00-18:00 PH time (Asia/Manila). No evening trades.
- Batch 1's sessions (Asia 09:00-17:00 Tokyo, London 08:00-17:00 London, New York 08:00-17:00 New York) are
  reused for the per-session labels in the report.

### Time filter (three settings, all models)
1. **none.**
2. **kill zones.**
3. **owner's hours.**

How the filter is applied depends on the entry type:
- **Models 1 and 3 (limit orders):** the order can only fill inside the active window. If price touches the
  limit outside the window, the order does not fill (it stays live until its 12 bars run out or it is cancelled).
- **Model 5 (market entry):** the entry time must be inside the window.

The report also shows results per session and per PH-time band. The owner trades 08:00-18:00 PH time and
loses most in the New York evening. The per-session and per-band tables show whether those evening losses come
from the market or from the trader.

### Direction (H4 and H1)
- Swings exactly as batch 1: a swing high is a bar whose high is strictly above the highs of the 2 bars on each
  side (N = 2); lows mirror. The swing price is the wick tip. A swing is only known 2 bars after it forms.
- On one timeframe, up = the last two known swing highs and lows make a higher high and a higher low; down =
  lower high and lower low; otherwise range.
- **Direction = H4 and H1 agree**, read at the signal bar's close. Disagreement or range = no direction.
- The direction filter is a variant (on / off). "On" means a sell needs "down" and a buy needs "up".

### Levels (as batch 1)
PDH/PDL, PWH/PWL, previous session high/low, the previous session's last M30 swing high/low, and POC/VAH/VAL.
Merging (0.25 x ATR14(H1)), lifetime (a level stays live until a bar closes beyond it by more than the
tolerance, or until levels are recomputed at the next session start) and the near-miss tolerance
(0.10 x ATR14 of the entry timeframe) are exactly as in `reports/06_setups.md`.

### Displacement
A displacement candle is an entry-timeframe candle whose range (high minus low) is at least **2 x the mean range
of the previous 20 candles** (not counting the candle itself), **and** which closes in the trade direction
(a sell needs a bearish close). This is a designer's call, taken from the article's own scorecard.

### Model 1 - FVG entry after a market-structure shift (described as a sell; buys mirror)
1. **Failed break:** an entry-timeframe bar opens below a live level above, trades up to it (within tolerance)
   and closes back below it. The failed break's high is the wick tip. Only the failed break itself has to fall
   within the level's live span. While one failed break is waiting for its MSS, later failed breaks are
   ignored. Scanning resumes after the MSS, a cancel or an expiry.
2. **Market-structure shift (MSS):** within 12 bars after the failed break, a bar closes below the latest M5
   swing low on the trade side that was already known at the failed break's close (the low of the leg that ran
   up into the sweep; a swing that forms later is not needed). At least one bar from the failed break to the
   MSS bar (inclusive) must be a bearish displacement candle. While waiting, a close above the failed-break
   bar's high cancels the setup.
3. **FVG:** the latest bearish fair value gap (bar 1 low above bar 3 high, any size) whose bar 3 lies between
   the failed-break bar and the MSS bar (inclusive). None = no setup. No setup either if the MSS bar's close
   is already at or beyond the limit price, or if the stop would equal the limit.
4. **Entry (limit order):** a sell limit at the gap's near edge (its bottom = bar 3's high).
5. **Stop loss:** the highest high from the failed break to the MSS bar. This is the same as "to the fill",
   because price must pass the limit before it can reach the stop. A stop touch inside the fill minute counts
   as a loss (conservative).

### Model 3 - OTE
Model 1, plus a filter. The impulse leg runs from the failed break's wick tip down to the lowest low from the
failed break to the MSS bar. The OTE zone (sell) = leg low + 0.62 to 0.79 x leg height. The Model 1 gap must
**overlap** the OTE zone, otherwise there is no setup. Entry, stop and cancel rules are identical to Model 1,
so the only difference between Models 1 and 3 is "does the OTE filter help".

### Model 5 - Asian-range box (described as a sell; buys mirror at the box low)
1. **Box:** the M1 high and low of 20:00-24:00 New York, known at 00:00 New York and used until the next
   20:00 New York. No minimum or maximum box size (the report shows the box-height distribution).
2. **Breakout start:** a bar trades above the box high and its previous close was at or below the box high.
   Within 12 bars a close below the box low cancels. Otherwise a close below the box high is the failure.
3. **Retest:** within the next 12 bars, a bar whose high comes within tolerance of the box high and which
   closes below it. A close above the box high, or below the box low, before that cancels.
4. **Entry** at the next bar's open (market entry). **Stop loss** = the highest high from the breakout start to
   the retest bar.
5. After a signal, a cancel or an expiry, scanning resumes at the next bar.
6. The stop size is uncapped (as in batch 1, pre-registered, not tuned). The report shows the stop-size
   distribution and cost as a percentage of the stop.

### Limit-order rules (Models 1 and 3)
- **Live window:** a limit order is live from the MSS bar's close for 12 entry-timeframe bars.
- **Cancelled earlier** by an entry-timeframe close beyond the stop, at Friday 22:00 server time, or before a
  gap of more than 25 hours.
- **Fill:** on M1 bars, the first M1 bar (inside the live window and the time filter) whose high reaches the
  limit (for a buy: whose low reaches it). If that bar opens beyond the limit, the fill is at its open
  (gap-open fill).
- A fill bar that opens at or beyond the stop = no trade (skip reason `gap_through_sl`). The order still
  blocked the variant.
- A stop touch inside the fill minute = a loss. A target touched inside the fill minute does not count.
- **Cost** is the batch 1 cost, taken from the fill minute's spread (see below). A limit order still pays the
  spread, so this is conservative.
- **One position or pending order at a time** per variant. New signals of that variant while an order is
  pending or a trade is open are dropped.

### Take profit and exits
- Fixed **1:1.5, 1:2, 1:3** measured from the fill, or **next level** = the nearest level in the trade direction
  at least 1R away (none = skip), as in batch 1.
- Exit: hold until stop, target or a forced exit. No session-close variant in batch 2 (it keeps the variant
  count down, and batch 1's session-exit variants did not change any outcome).

### Trade simulation and costs (as batch 1)
- Signals are detected on the entry timeframe; every trade is simulated on M1 bars. If stop and target are both
  inside one M1 bar, it is a loss. If a bar opens beyond the stop or target, the fill is at that bar's open (a
  loss can exceed 1R).
- Cost per trade = max(M1 spread x point, **0.30**) + **0.05** slippage, shifted against the trade. Sensitivity
  at total cost 0.50 and 0.75. Swap per server-midnight rollover held, x3 for Wed to Thu, historical swap scaled
  by price, from `data/raw/research/symbol_info.json`.
- R = profit in units of the trade's own stop distance, net of cost and swap.
- Forced exits: at the open of the first bar at or after Friday 22:00 server time; and at the close of the last
  bar before a gap of more than 25 hours. No bar-count timeout. No entry at or after Friday 22:00, none after
  such a gap.
- No lookahead: a fact is used only if it was known by the signal bar's close. All data is cut at 2026-10-23.

### Fixed parameters (reported, never tuned)
| Parameter | Value |
|---|---|
| Swing N | 2 bars each side |
| Window K (MSS, breakout failure, retest, limit order) | 12 entry-timeframe bars |
| Tolerance | 0.10 x ATR14 (entry timeframe) |
| Level merge distance | 0.25 x ATR14(H1) |
| Displacement | range >= 2 x mean range of previous 20 bars, closes in trade direction |
| OTE zone | 0.62 to 0.79 of the impulse leg |
| Fixed reward:risk | 1.5 / 2.0 / 3.0 |
| Minimum TP distance (next level) | 1R |
| SL buffer | 0 |
| Cost | max(spread, 0.30) + 0.05; sensitivity 0.50 / 0.75 |
| Null repeats | 200 |
| ATR | MT5 iATR (simple average of true range), period 14 |

### Variant grid
Model {1, 3, 5} x entry timeframe {M5, M15} x TP {rr1.5, rr2.0, rr3.0, next_level} x direction filter
{dir, nodir} x time filter {none, killzone, owner} = **144 variants**. Names look like `m1|M5|rr2.0|dir|killzone`.
**72 are eligible** for selection and the test (M5 only). The 72 M15 variants are **comparison only**, because
of batch 1's cost finding (cost is a much bigger share of a small M5 stop): never selected, never on the test set.

**Cumulative count with batch 1: 208 + 144 = 352 variants tried, 64 + 72 = 136 eligible.** Some will pass by
luck; the count is stated next to every result.

### Metrics per variant (development period 2022-05-16 .. 2025-10-23)
Trades, win rate, expectancy in R with bootstrap 95% CI, total R, profit factor, max drawdown (R), longest
losing streak, trades per month, per year, per session, per PH-time band, buy vs sell, skipped-signal counts,
cost as a share of the stop, and cost sensitivity at 0.50 / 0.75.

### Null baseline (200 random runs per variant)
- **Models 1 and 3 (limit entries):**
  - Random placement bars are drawn only inside the variant's active window, and match each taken trade's
    session label (real trades are labelled by their fill session) and direction.
  - The limit distance is a taken trade's (limit minus signal close) in ATR14 units of the entry timeframe,
    applied to the close and ATR of the bar before the random placement.
  - The stop distance and target R are drawn jointly from the variant's taken trades.
  - Same 12-bar fill window and the same simulator. An unfilled random order is no trade.
  - Seeds differ from batch 1.
- **Model 5 (market entries):** as batch 1, with placements drawn only inside the active window.
- Reported as the share of random runs whose expectancy the variant beats.

### Selection rule (development period only)
Per model, at most 3 selected. A variant must pass all of these, in order:
1. M5 entry (eligible).
2. n >= 100 trades (one position at a time).
3. Expectancy > 0 in each of 2022 (partial), 2023, 2024 and 2025 (to Oct 23).
4. Beats >= 95% of its 200 random runs.
5. Then the best expectancy per model.

A model with nothing selected is reported FAIL and not tested.

### Part C - test set (run once)
Run only the selected variants on **2025-10-24 .. 2026-10-23, once**, with a bootstrap 95% CI. **PASS** =
expectancy >= +0.10R, n >= 30, and lower CI bound > -0.10R. Nothing selected = the test is not run. Whatever
the result, no re-tuning and no re-run.

### Honesty notes
- (a) Batch 1's results were known when batch 2 was designed. Batch 2's rules are not independent of that.
- (b) The M15 comparison exists because of batch 1's cost finding; it is never selected.
- (c) Several rules are designer's calls, marked in the text above (displacement, stop inside the fill minute,
  full cost on limit fills, uncapped Model 5 stops). They were fixed before any result and are not tuned.
- (d) Where the implementation refines the design spec (the failed-break span, the swing known at the failed
  break, the SL definition, the cancel and skip rules), this document states the implemented rule.
- (e) The owner's evening losses are examined per session and per PH-time band, to separate market from trader.
- (f) The test period 2025-10-24 .. 2026-10-23 has not been touched. No batch 2 result exists at the time of
  writing.

<!-- RESULTS BELOW: generated by src/xauai/research_ict.py; everything above is the pre-registration -->
