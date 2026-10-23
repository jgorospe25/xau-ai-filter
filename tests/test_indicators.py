import numpy as np
import pytest

from xauai.indicators import atr, ema, rsi, true_range


def test_ema_seeds_with_first_price_and_recurses():
    x = np.array([10.0, 11.0, 12.0, 11.0])
    a = 2 / (3 + 1)
    expected = [10.0]
    for v in x[1:]:
        expected.append(a * v + (1 - a) * expected[-1])
    np.testing.assert_allclose(ema(x, 3), expected)


def test_true_range_uses_previous_close():
    high = np.array([10.0, 12.0, 11.0])
    low = np.array([9.0, 11.0, 8.0])
    close = np.array([9.5, 11.5, 9.0])
    # bar0: no prev close -> high-low; bar1: max(12,9.5)-min(11,9.5)=2.5; bar2: max(11,11.5)-min(8,11.5)=3.5
    np.testing.assert_allclose(true_range(high, low, close), [1.0, 2.5, 3.5])


def test_atr_is_simple_average_of_true_range_not_wilder():
    rng = np.random.default_rng(0)
    close = 100 + rng.normal(0, 1, 60).cumsum()
    high = close + rng.uniform(0.1, 1, 60)
    low = close - rng.uniform(0.1, 1, 60)
    n = 14
    out = atr(high, low, close, n)
    tr = true_range(high, low, close)
    assert np.isnan(out[:n]).all()
    for i in range(n, 60):
        assert out[i] == pytest.approx(tr[i - n + 1 : i + 1].mean(), rel=1e-12)


def test_rsi_sma_seed_then_wilder():
    close = np.array([1.0, 2.0, 1.5, 2.5, 3.0, 2.0, 2.2])
    n = 3
    out = rsi(close, n)
    d = np.diff(close)
    up, dn = np.clip(d, 0, None), np.clip(-d, 0, None)
    pos, neg = up[:n].mean(), dn[:n].mean()
    assert np.isnan(out[:n]).all()
    assert out[n] == pytest.approx(100 - 100 / (1 + pos / neg))
    for i in range(n + 1, len(close)):
        pos = (pos * (n - 1) + up[i - 1]) / n
        neg = (neg * (n - 1) + dn[i - 1]) / n
        assert out[i] == pytest.approx(100 - 100 / (1 + pos / neg))


def test_rsi_edge_cases_all_up_and_flat():
    assert rsi(np.arange(10.0), 3)[-1] == 100.0
    assert rsi(np.full(10, 5.0), 3)[-1] == 50.0
