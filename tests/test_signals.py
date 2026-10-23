import numpy as np
import pandas as pd
import pytest

from xauai import config, signals


def _frame(close: np.ndarray, spread: float = 1.0) -> pd.DataFrame:
    n = len(close)
    return pd.DataFrame(
        {
            "time": pd.date_range("2024-01-01", periods=n, freq="h"),
            "open": close,
            "high": close + spread,
            "low": close - spread,
            "close": close,
        }
    )


@pytest.fixture
def patched(monkeypatch):
    """Override indicator outputs so the rule logic can be tested on chosen values."""

    def apply(fast, slow, trend, rsi_v, atr_v, atr_avg):
        def fake(df):
            out = df.copy()
            out["ema_fast"], out["ema_slow"], out["ema_trend"] = fast, slow, trend
            out["rsi"], out["atr"], out["atr_avg"] = rsi_v, atr_v, atr_avg
            return out

        monkeypatch.setattr(signals, "compute_indicators", fake)

    return apply


def _setup(n: int):
    i = config.MIN_BARS + 5
    fast = np.zeros(n)
    slow = np.zeros(n)
    return i, fast, slow


def test_buy_on_cross_up_with_filters(patched):
    n = config.MIN_BARS + 10
    i, fast, slow = _setup(n)
    fast[:i], slow[:i] = 1.0, 2.0  # below
    fast[i:], slow[i:] = 3.0, 2.0  # crosses above at i
    close = np.full(n, 100.0)
    patched(fast, slow, np.full(n, 90.0), np.full(n, 60.0), np.full(n, 1.0), np.full(n, 1.0))
    s = signals.compute_signals(_frame(close))["signal"].to_numpy()
    assert s[i] == 1 and (np.delete(s, i) == 0).all()


def test_equal_previous_counts_as_cross(patched):
    n = config.MIN_BARS + 10
    i, fast, slow = _setup(n)
    fast[:i], slow[:i] = 2.0, 2.0  # equal before -> fast[i-1] <= slow[i-1]
    fast[i:], slow[i:] = 3.0, 2.0
    patched(fast, slow, np.full(n, 90.0), np.full(n, 60.0), np.full(n, 1.0), np.full(n, 1.0))
    s = signals.compute_signals(_frame(np.full(n, 100.0)))["signal"].to_numpy()
    assert s[i] == 1


@pytest.mark.parametrize(
    "rsi_v, trend, atr_v, ok",
    [
        (60.0, 90.0, 1.0, True),
        (50.0, 90.0, 1.0, False),  # RSI must be strictly > 50
        (70.0, 90.0, 1.0, False),  # and strictly < 70
        (60.0, 110.0, 1.0, False),  # close below trend EMA
        (60.0, 90.0, 0.79, False),  # ATR below 0.8 * average
        (60.0, 90.0, 0.8, True),  # ATR exactly at floor passes (>=)
    ],
)
def test_buy_filters(patched, rsi_v, trend, atr_v, ok):
    n = config.MIN_BARS + 10
    i, fast, slow = _setup(n)
    fast[:i], slow[:i] = 1.0, 2.0
    fast[i:], slow[i:] = 3.0, 2.0
    patched(fast, slow, np.full(n, trend), np.full(n, rsi_v), np.full(n, atr_v), np.full(n, 1.0))
    s = signals.compute_signals(_frame(np.full(n, 100.0)))["signal"].to_numpy()
    assert bool(s[i] == 1) == ok


def test_sell_mirror(patched):
    n = config.MIN_BARS + 10
    i, fast, slow = _setup(n)
    fast[:i], slow[:i] = 3.0, 2.0
    fast[i:], slow[i:] = 1.0, 2.0
    patched(fast, slow, np.full(n, 110.0), np.full(n, 40.0), np.full(n, 1.0), np.full(n, 1.0))
    s = signals.compute_signals(_frame(np.full(n, 100.0)))["signal"].to_numpy()
    assert s[i] == -1


def test_no_signals_before_min_bars(patched):
    n = config.MIN_BARS + 10
    fast = np.where(np.arange(n) % 2 == 0, 3.0, 1.0)  # crosses every bar
    slow = np.full(n, 2.0)
    patched(fast, slow, np.full(n, 0.0), np.full(n, 60.0), np.full(n, 1.0), np.full(n, 1.0))
    s = signals.compute_signals(_frame(np.full(n, 100.0)))["signal"].to_numpy()
    assert (s[: config.MIN_BARS] == 0).all() and (s[config.MIN_BARS :] != 0).any()
