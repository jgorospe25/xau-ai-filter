# 06 — Session & liquidity setups: XAUUSD.sc

## Pre-registration (written and committed before any Part B or Part C result exists)

### Why
Phase 4 failed: the EMA/RSI signal's raw edge was about +0.06R out-of-sample with a confidence interval
spanning zero, and neither model improved it (`reports/04_model.md`). This project instead tests the owner's
own discretionary setups (session levels, liquidity sweeps, volume profile, H1 order blocks) as exact rules,
with real costs, null baselines and an untouched test set. "These do not work" is a valid result.
Every definition below is written in plain words so the owner can check it against how they really trade.

### Time and sessions
- All bar times are broker server time (bar open time). Each session is defined in its own local time, so
  daylight saving is handled automatically:
  - **Asia:** 09:00-17:00 Tokyo (about 08:00-16:00 PH time, when the owner usually trades).
  - **London:** 08:00-17:00 London.
  - **New York:** 08:00-17:00 New York.
- "Previous session" at time t = the most recent session that has **fully ended** at or before t. A session in
  progress is never "previous". Signals may fire at any time the market is open, even outside all sessions.
- A bar's session (for reports and the session-close exit) = the session that started most recently among
  those active at its open; a bar outside all sessions belongs to the next session to start.
- "Previous day" = the previous broker server day (D1 bar). "Previous week" = the previous broker server week.

### Swings and trend
- Swing high = a bar whose **high** is strictly higher than the highs of the N bars on each side (N = 2 on every
  timeframe). Swing low mirrors on lows. The swing price is the wick tip. A swing is only known N bars after
  it forms, and is never used earlier.
- Trend on one timeframe: from the last two confirmed swing highs and lows, up = higher high and higher low,
  down = lower high and lower low, otherwise range. **Trend state = M30 and H1 agree**; disagreement or range
  = no trend. It is read at the signal bar's close. Requiring trend alignment is a variant (with / without).

### Levels
Marked at each session start and recomputed as sessions end:
1. Previous day high / low (PDH / PDL).
2. Previous week high / low (PWH / PWL).
3. Previous session high / low.
4. The previous session's last confirmed swing high and swing low on M30 (wick tips). If the session has no
   confirmed swing of one kind, that session's extreme is used.
5. Fixed-range volume profile anchored on the range between those two swings (from whichever came first to
   the other), built from M1 bars: each M1 bar's tick volume is spread evenly over the $0.50 price bins between
   its low and high. POC = the bin with most volume; the value area holds 70% of volume, grown outward from the
   POC one bin at a time, adding the side with more volume. Levels used: **POC, VAH, VAL**. (Tick volume only;
   real volume is zero on every bar.)

We say "level above" / "level below" instead of buy-side / sell-side liquidity, because the owner uses those
two terms the opposite way round to ICT/SMC convention.

Level handling:
- **Merging:** levels within 0.25 x ATR14(H1) of each other become one zone. Its outer edge is the level used
  for triggers and SL anchoring. One bar gives at most one signal per setup and direction.
- **Open rule:** a sweep or rejection of a level above needs the bar to open below it (mirror for levels
  below). A bar that opens beyond a level cannot sweep it.
- **Lifetime:** a sweep or rejection does **not** use a level up. It stays live and can trigger again until a
  bar **closes** beyond it by more than the tolerance (then it is broken), or until levels are recomputed at the
  next session start.
- **Near-miss tolerance:** a wick that comes within 0.10 x ATR14 (entry timeframe) of a level counts as
  reaching it. "Closes back below" is measured against the level itself.

### Order blocks (H1)
- Bullish OB = the last **bearish** H1 candle before an up-move whose close breaks above the most recent
  confirmed H1 swing high. Zone = that candle's high to low. Known only once the breaking bar has closed.
  Bearish OB mirrors.
- (Clarified during implementation) The breaking bar uses the latest swing high known at its open; each swing
  produces at most one OB (its first break); the OB candle is the last opposite candle strictly between the
  swing bar and the breaking bar (none = no OB).
- An OB is invalidated when an H1 bar closes beyond its far side (below the low for a bullish OB). An OB can
  produce at most one Setup 2b trade.

### Fair value gaps (H1)
- Bullish FVG = three consecutive H1 bars where bar 1's high < bar 3's low. Gap = bar 1 high to bar 3 low.
  Known once bar 3 has closed. Bearish mirrors. Only gaps with a **huge** middle candle count: bar 2's body
  >= 1.5 x ATR14(H1). Target price = the gap's **midpoint**. A gap stops being a valid target once price has
  traded to its midpoint after it formed.

### The four setups (described as sells; buys mirror them)
Fixed parameters used below: K = 12 bars of the entry timeframe; tolerance = 0.10 x ATR14 (entry timeframe);
SL buffer beyond a wick = 0 (costs already widen the effective stop).

**Setup 1 - sweep, reclaim, retest.** Entry timeframes M5, M15, M30, H1.
1. Sweep: a bar's high trades above a level above and the same bar closes back below it.
2. Retest: within K bars a later bar trades back up to the level (high >= level - tolerance) and closes below it.
3. Enter sell at the next bar's open. SL = just above the sweep wick tip.
- Invalidated if any bar closes above the sweep wick tip, or K bars pass before the retest.
- (Clarified during implementation) SL is the highest high from the sweep bar through the retest bar, so a
  higher retest wick counts as part of the sweep. A second sweep that closes back below is itself a retest.

**Setup 2a - rejection at a level, target the H1 OB or FVG.** Rejection read on M30 or H1 (both tested); entry
on M5 only.
1. Rejection: an M30/H1 bar opens below a level above, trades up to it (within tolerance of the trigger
   timeframe's ATR), closes back below it, and its upper wick is >= 50% of the bar's range.
2. Enter sell at the open of the first M5 bar after the rejection bar closes. SL = the rejection bar's high.
   No extra M5 confirmation pattern (the owner described none; adding one would invent a rule).
3. TP (two variants, both tested): **OB** = the near edge (top) of the nearest valid bullish H1 OB below the
   entry; **FVG** = the midpoint of the nearest valid bullish H1 FVG below the entry.
4. No such target below = no trade in that variant. Skip if the TP is less than 1R away.

**Setup 2b - pullback from the H1 OB.** Entry timeframes M5, M15, M30, H1.
1. Price trades into a valid bullish H1 OB zone. The OB must be approached from above (price above the zone
   when the OB is confirmed). "Entered the zone" = the first entry-timeframe bar after the OB is known whose
   low reaches the zone's top.
2. On the entry timeframe, a bar closes above the **last confirmed lower high** formed since price entered the
   zone (break of structure).
3. Retest: within K bars price trades back down to that broken level (low <= level + tolerance) and the bar
   closes above it. Enter buy at the next bar's open.
4. SL = below the lowest wick since price entered the zone.
- Invalidated if an H1 bar closes below the OB's low, or K bars pass after the break.
- If price closes back above the zone's top + 1.0 x ATR14(H1) without a trade, the episode ends; the next entry
  into the zone starts a fresh one (new lower-high search and SL window). Price need not stay inside the zone.
- (Clarified during implementation) A break with no retest inside K is cancelled and that swing high cannot be
  reused (a newer one is needed). One signal per OB.

**Setup 3 - lower high, break, continue to the PDL.** Entry timeframes M5, M15, M30, H1.
1. Price reaches a level above (within tolerance) without closing above it. The first touch is kept.
2. After the touch, a confirmed swing high forms on the entry timeframe that is **lower** than the touch high
   (the lower high).
3. Break: a bar closes below the last confirmed swing low (the "last higher low", the latest swing low known
   at that bar), taking the stops resting there.
4. Retest: within K bars price trades back up to that broken low (high >= level - tolerance) and the bar closes
   below it. Enter sell at the next bar's open.
5. SL = just above the lower high. Invalidated if a bar closes above the lower high, or K bars pass.
6. Owner target: the **PDL** (buys: PDH), unless an **unswept** entry-timeframe swing low lies above the PDL and
   below the signal close. Then the target is the unswept swing low nearest the PDL ("the last swing low just
   above it", the owner's confirmed words). Unswept = no bar after the swing traded below its price before the
   signal close. If price is already below the PDL there is no owner target, so no trade in that mode. Skip if
   the target is less than 1R away. (Clarified during implementation, with the owner, before any backtest
   result: the first wording, "nearest to the entry", gave targets around 0.02R.)

### Take-profit and exit variants
- Setups 1 and 2b: fixed **1:1.5, 1:2, 1:3**, or **next level** = the nearest level in the trade direction at
  least 1R away, taken from the raw (unmerged) level list valid at entry time; none = skip the trade.
- Setup 2a: OB edge or FVG midpoint (above). Setup 3: owner target (above) or fixed 1:1.5, 1:2, 1:3.
- Exit variants: **hold** = run until SL, TP or a forced exit; **session** = also close at the end of the
  session the trade was opened in.

### Trade simulation
- Signals are detected on the entry timeframe; **every trade is simulated on M1 bars** from the entry bar's
  open time. If SL and TP are both inside one M1 bar, it counts as a loss. If a bar opens beyond SL/TP, the
  fill is at that bar's open (a loss can exceed 1R).
- Cost per trade = max(M1 entry-bar spread x point, **0.30**) + **0.05** slippage, applied by shifting the entry
  against the trade. Sensitivity at total cost 0.50 and 0.75. Swap as in Phase 3: charged per server-midnight
  rollover held, x3 for the Wed to Thu rollover, historical swap scaled by price; the swap fields come from the
  research symbol snapshot `data/raw/research/symbol_info.json`.
- R = profit in units of the trade's own SL distance (entry fill to SL), net of cost and swap.
- **Forced exits:** at the open of the first bar at or after Friday 22:00 server time; and at the close of the
  last bar before any gap of more than 24 hours between bars (holidays). No bar-count timeout. No entry at or
  after Friday 22:00, and none after such a gap (stale signal).
- **One position at a time per variant:** a position blocks only its own variant; signals of that variant that
  arrive while it is open are dropped. Same-bar ties: the earlier-detected level wins, then the nearer SL. Both
  "every signal" and "one position" views are reported; **selection and the test use one position at a time**.
- No lookahead: a fact is used only if it was known by the entry bar's close. All data is cut at 2026-10-23.

### Fixed parameters (reported, never tuned)
| Parameter | Value |
|---|---|
| Swing N | 2 bars each side |
| Retest window K | 12 entry-timeframe bars |
| Tolerance | 0.10 x ATR14 (entry timeframe) |
| Level merge distance | 0.25 x ATR14(H1) |
| Volume profile bin / value area | $0.50 / 70% |
| Rejection wick (2a) | >= 50% of bar range |
| 2b episode reset | zone top + 1.0 x ATR14(H1) |
| FVG middle-candle body | >= 1.5 x ATR14(H1) |
| Minimum TP distance | 1R |
| Fixed reward:risk | 1.5 / 2.0 / 3.0 |
| SL buffer | 0 |
| Cost | max(spread, 0.30) + 0.05; sensitivity 0.50 / 0.75 |
| Null repeats | 200 |
| ATR | MT5 iATR (simple average of true range), period 14 |

### Variant grid (Part B)
Dimensions: setup x entry timeframe x TP mode x trend filter (with / without) x exit mode (hold / session).
Variant names look like `1|M5|rr2.0|trend|hold` and `2a|M5|H1|ob|notrend|session`.

| Setup | Entry timeframes | TP modes | Base combos |
|---|---|---|---|
| 1 | M5, M15, M30, H1 | rr1.5, rr2.0, rr3.0, next_level | 16 |
| 2b | M5, M15, M30, H1 | rr1.5, rr2.0, rr3.0, next_level | 16 |
| 3 | M5, M15, M30, H1 | rr1.5, rr2.0, rr3.0, owner | 16 |
| 2a | M5 (trigger M30 or H1) | ob, fvg | 4 |

52 base combinations x 2 (trend) x 2 (exit) = **208 variants tried in total**.
**64 are eligible** for selection and the test: every M5-entry variant (Setups 1, 2b, 3: 16 each; 2a: 16).
The other 144 (M15, M30, H1 entries) are **comparison only** ("would a slower entry do better?"): they are
labelled as such, never selected and never touch the test set. The report also shows cost as a share of the SL
distance per entry timeframe, since the fixed cost is a much bigger part of 1R on M5 than on H1.

### Metrics per variant (development period 2022-05-16 .. 2025-10-23)
Trades, win rate, expectancy in R with bootstrap 95% CI, total R, profit factor, max drawdown (R), longest
losing streak, trades per month, per year, per session, buy vs sell, and cost sensitivity at 0.50 / 0.75.

### Null baseline
Per variant: the same number of entries at random bars in the same sessions, with the same direction mix, SL
distances and TP rule, repeated 200 times. Reported as the share of random runs whose expectancy the variant
beats.

### Selection rule (development period only)
A variant is eligible for selection only if it is an M5-entry variant and all of these hold:
1. n >= 100 trades (one position at a time).
2. Expectancy > 0 in each of 2022 (partial), 2023, 2024 and 2025 (to Oct 23).
3. It beats >= 95% of its random runs.

Pick the eligible variant with the highest expectancy **per setup**: at most 4 (Setups 1, 2a, 2b, 3). A setup
with no eligible variant is reported FAIL and not tested. Because 64 variants are eligible, some may pass by
luck; the count is stated next to every result.

### Part C - test set (run once)
Run the selected variants (at most 4) on **2025-10-24 .. 2026-10-23, once**, with a bootstrap 95% CI.
**PASS** per variant: expectancy >= +0.10R, n >= 30, and lower CI bound > -0.10R. Whatever the result, no
re-tuning and no re-run; report it honestly, as Phase 4 did.

### Honesty notes
- (a) Part A (`reports/05_levels.md`, development period only) was completed before this pre-registration.
  None of its levels beat random, and no rule was changed because of it.
- (b) During engineering, one development-period smoke run was seen (variant `1|M5|rr2.0|notrend|hold`:
  n = 2754, expectancy -0.234R). No rule was changed because of it.
- (c) The Setup 3 owner-target rule was clarified with the owner's own words before any backtest result.
- (d) The test period 2025-10-24 .. 2026-10-23 has not been touched.

<!-- RESULTS BELOW: generated by src/xauai/research.py; everything above is the pre-registration -->
