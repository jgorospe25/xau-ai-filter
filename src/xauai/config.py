"""Single source of project parameters. Mirrored as inputs in the MQL5 EA.

Signal values match the "Auto H1" preset of mql5/XAU_EMA_RSI_Signal.mq5 (v1.1),
which is the source of truth for signal rules.
"""

from pathlib import Path

# --- Market -----------------------------------------------------------------
SYMBOL: str = "XAUUSD.sc"
TIMEFRAME: str = "H1"  # resolved to mt5.TIMEFRAME_H1 in export.py

# --- Paths ------------------------------------------------------------------
ROOT: Path = Path(__file__).resolve().parents[2]
DATA_DIR: Path = ROOT / "data"
RAW_DIR: Path = DATA_DIR / "raw"
REPORTS_DIR: Path = ROOT / "reports"
RAW_H1_PATH: Path = RAW_DIR / "xauusd_h1.parquet"
RAW_D1_PATH: Path = RAW_DIR / "xauusd_d1.parquet"  # broker D1 bars, for the D1 trend feature
SYMBOL_INFO_PATH: Path = RAW_DIR / "symbol_info.json"
MT5_DIR: Path = DATA_DIR / "mt5"  # CSVs copied from the terminal (DumpSignals etc.)
LABELS_PATH: Path = DATA_DIR / "labels" / "trades_h1.parquet"  # base-case labels (Phase 3)

# MT5 terminal data folder (File -> Open Data Folder)
MT5_DATA_PATH: Path = Path(r"C:\Users\joshua\AppData\Roaming\MetaQuotes\Terminal\D0E8209F77C8CF37AD8BF550E51FF075")
MT5_FILES_DIR: Path = MT5_DATA_PATH / "MQL5" / "Files"

# --- Signal rules (closed bars only) ----------------------------------------
# BUY:  EMA_FAST crosses above EMA_SLOW, RSI_MID < RSI < RSI_UPPER, close > EMA_TREND
# SELL: EMA_FAST crosses below EMA_SLOW, RSI_LOWER < RSI < RSI_MID, close < EMA_TREND
EMA_FAST: int = 9
EMA_SLOW: int = 21
EMA_TREND: int = 200
USE_TREND_FILTER: bool = True

RSI_PERIOD: int = 14
RSI_MID: float = 50.0
RSI_UPPER: float = 70.0  # no buys at/above
RSI_LOWER: float = 30.0  # no sells at/below

# Volatility floor: ATR[i] >= SMA(ATR, ATR_AVG_PERIOD)[i] * ATR_FLOOR
ATR_PERIOD: int = 14  # MT5 iATR = SMA of True Range (not Wilder)
ATR_AVG_PERIOD: int = 50
ATR_FLOOR: float = 0.8

USE_SESSION_FILTER: bool = False  # off on H1
SESSION_START_HOUR: int = 9  # server time; only used if the filter is on
SESSION_END_HOUR: int = 22

# --- Trade levels / risk ----------------------------------------------------
SL_ATR_MULT: float = 1.5  # SL distance = ATR at signal bar * this
RR: float = 2.0  # TP distance = SL distance * this
RISK_PCT: float = 1.0  # % of balance risked per trade

# MT5 indicator starts evaluating signals at this bar index (g_minBars in the .mq5)
MIN_BARS: int = max(EMA_TREND, ATR_AVG_PERIOD + ATR_PERIOD) + 5

# --- Costs (price units) ----------------------------------------------------
SPREAD_FLOOR: float = 0.30  # cost spread = max(bar spread * point, floor); zero spreads are artifacts
# Sensitivity runs: TOTAL per-trade cost (spread floor + slippage); base case = 0.30 + 0.05 = 0.35
SENSITIVITY_TOTAL_COSTS: tuple[float, ...] = (0.50, 0.75)
SLIPPAGE: float = 0.05
# Swap is read from data/raw/symbol_info.json (swap_mode, swap_long, swap_short, swap_rollover3days)
ROLLOVER_HOUR: int = 0  # server hour at which swap is charged

# --- Labels -----------------------------------------------------------------
TIMEOUT_BARS: int = 48
FRIDAY_EXIT_HOUR: int = 22  # force exit at the open of the Friday 22:00 server bar
HOLIDAY_GAP_BARS: int = 24  # force exit before any gap of more than this many missing bars

# --- Splits (inclusive dates, server time) ----------------------------------
TRAIN_START: str = "2022-05-16"
TRAIN_END: str = "2024-12-31"
VAL_START: str = "2025-01-01"
VAL_END: str = "2025-10-23"
TEST_START: str = "2025-10-24"
TEST_END: str = "2026-10-23"

# --- Research (project 1): session-liquidity setups ---------------------------
RESEARCH_RAW_DIR: Path = RAW_DIR / "research"
RESEARCH_SYMBOL_INFO_PATH: Path = RESEARCH_RAW_DIR / "symbol_info.json"


def research_path(tf: str) -> Path:
    """Raw bar parquet for a research timeframe, e.g. research_path("M1")."""
    return RESEARCH_RAW_DIR / f"xauusd_{tf.lower()}.parquet"


# Server time = UTC + 2 h, +1 h while America/New_York is in DST (verified in timeutil tests / data).
SERVER_BASE_OFFSET_H: int = 2
# name -> (IANA zone, local start hour, local end hour)
SESSIONS: dict[str, tuple[str, int, int]] = {
    "asia": ("Asia/Tokyo", 9, 17),
    "london": ("Europe/London", 8, 17),
    "ny": ("America/New_York", 8, 17),
}
HOLIDAY_GAP_HOURS: int = HOLIDAY_GAP_BARS + 1  # wall-clock gap (h) above which trades are force-closed

DEV_START: str = "2022-05-16"
DEV_END: str = "2025-10-23"

# Fixed, pre-registered parameters (reported, never tuned)
SWING_N: int = 2  # bars on each side of a swing
RETEST_BARS: int = 12  # retest window K, entry-TF bars
TOL_ATR: float = 0.10  # touch tolerance, x ATR14(entry TF)
MERGE_ATR: float = 0.25  # level merge distance, x ATR14(H1)
VP_BIN: float = 0.50  # volume-profile bin, price units
VALUE_AREA: float = 0.70
REJECT_WICK: float = 0.50  # rejection wick >= this fraction of bar range
OB_RESET_ATR: float = 1.0  # 2b episode reset = zone top + this x ATR14(H1)
FVG_BODY_ATR: float = 1.5  # FVG middle-bar body >= this x ATR14(H1)
MIN_TP_R: float = 1.0  # minimum TP distance in R
RR_MODES: tuple[float, ...] = (1.5, 2.0, 3.0)
NULL_REPEATS: int = 200
NULL_BEAT: float = 0.95
SELECT_MIN_N: int = 100
TEST_MIN_N: int = 30
TEST_MIN_EXP: float = 0.10
TEST_MIN_CI_LO: float = -0.10
