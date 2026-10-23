import numpy as np
import pandas as pd
import pytest

from xauai import config
from xauai.features import FEATURES, compute_features, d1_ema_at
from xauai.signals import compute_signals


def test_d1_uses_only_completed_days():
    d1 = pd.DataFrame({"time": pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03"]), "close": [10.0, 20.0, 30.0]})
    # D1 EMA50 seeded at 10: after day 2 -> 10 + (20-10)*2/51
    after2 = 10 + 10 * 2 / 51
    t = pd.Series(pd.to_datetime(["2024-01-02 10:00", "2024-01-03 00:00", "2024-01-03 23:00"]))
    out = d1_ema_at(t, d1)
    assert out[0] == pytest.approx(10.0)  # Jan 2 still forming -> only Jan 1
    assert out[1] == pytest.approx(after2)  # Jan 2 ended exactly at 00:00 Jan 3
    assert out[2] == pytest.approx(after2)  # Jan 3 still forming


def test_d1_before_any_completed_day_is_nan():
    d1 = pd.DataFrame({"time": pd.to_datetime(["2024-01-01"]), "close": [10.0]})
    assert np.isnan(d1_ema_at(pd.Series(pd.to_datetime(["2024-01-01 05:00"])), d1)[0])


@pytest.mark.skipif(not (config.RAW_H1_PATH.exists() and config.RAW_D1_PATH.exists()), reason="needs raw data")
def test_no_lookahead_on_real_data():
    """Scrambling every bar after a signal must not change that signal's features."""
    raw = pd.read_parquet(config.RAW_H1_PATH)
    d1 = pd.read_parquet(config.RAW_D1_PATH)
    full = compute_features(compute_signals(raw), d1)
    rng = np.random.default_rng(0)
    for st in full["signal_time"].iloc[[10, 150, 300]]:
        i = int(raw.index[raw["time"] == st][0])
        cut = raw.copy()
        future = cut.index > i
        noise = rng.normal(0, 50, future.sum())
        for c in ("open", "high", "low", "close"):
            cut.loc[future, c] = cut.loc[future, c] + noise
        d1_cut = d1.copy()
        d1_cut.loc[d1_cut["time"] + pd.Timedelta(days=1) > st + pd.Timedelta(hours=1), "close"] += 999.0
        sig_cut = compute_signals(cut)
        sig_cut.loc[i, "signal"] = compute_signals(raw).loc[i, "signal"]  # keep the row present
        got = compute_features(sig_cut, d1_cut)
        a = full[full["signal_time"] == st][FEATURES].to_numpy(float)
        b = got[got["signal_time"] == st][FEATURES].to_numpy(float)
        np.testing.assert_allclose(a, b, rtol=0, atol=1e-12)


@pytest.mark.skipif(not (config.RAW_H1_PATH.exists() and config.RAW_D1_PATH.exists()), reason="needs raw data")
def test_features_are_finite_and_oriented():
    raw = pd.read_parquet(config.RAW_H1_PATH)
    f = compute_features(compute_signals(raw), pd.read_parquet(config.RAW_D1_PATH))
    assert len(f) == 426 and list(f.columns) == ["signal_time", *FEATURES]
    num = f[FEATURES].drop(columns=["d1_trend_pct"])
    assert np.isfinite(num.to_numpy(float)).all()
    # signal rules guarantee these are positive once oriented
    assert (f["dist_ema200_atr"] > 0).all() and (f["rsi_mid"] > 0).all() and (f["ema_gap_atr"] > 0).all()
