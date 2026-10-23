import numpy as np
import pandas as pd

from xauai import train
from xauai.features import FEATURES


def _data(n=400, seed=0):
    rng = np.random.default_rng(seed)
    t = pd.date_range("2022-06-01", periods=n, freq="3D") + pd.Timedelta(hours=10)
    df = pd.DataFrame({f: rng.normal(size=n) for f in FEATURES})
    df["hour"] = rng.integers(1, 24, n)
    df["dow"] = rng.integers(0, 5, n)
    df["direction"] = rng.choice([-1, 1], n)
    df["signal_time"] = t
    df["exit_time"] = t + pd.Timedelta(days=2)
    df["r"] = np.where(rng.random(n) < 0.4, 2.0, -1.0)
    df["y"] = (df["r"] > 0).astype(int)
    return df


def test_walk_forward_purges_labels_crossing_fold_start():
    df = _data()
    start = train.FOLD_STARTS[0]
    straddle = (df["signal_time"] < start) & (df["exit_time"] >= start)
    assert straddle.sum() > 0
    _, folds, _ = train.walk_forward(df)
    before = (df["signal_time"] < start).sum()
    assert folds.loc[0, "train_n"] == before - straddle.sum()
    assert folds.loc[0, "purged"] == straddle.sum()


def test_walk_forward_oos_covers_period_once_and_never_test():
    df = _data(n=600)
    oos, _, _ = train.walk_forward(df)
    assert oos["signal_time"].min() >= train.FOLD_STARTS[0]
    assert oos["signal_time"].max() < train.OOS_END
    assert oos["signal_time"].is_unique
    expected = ((df["signal_time"] >= train.FOLD_STARTS[0]) & (df["signal_time"] < train.OOS_END)).sum()
    assert len(oos) == expected


def test_choose_threshold_respects_min_keep_and_ties_low():
    p = np.array([0.1, 0.2, 0.6, 0.7, 0.8])
    r = np.array([-1.0, -1.0, 2.0, 2.0, 2.0])
    c = train.threshold_curve(p, r)
    t = train.choose_threshold(c)
    assert t == 0.3  # >= 0.3 keeps 60% with expectancy 2.0; lowest threshold among the ties
    assert (c.loc[c["threshold"] == t, "keep_frac"] >= train.MIN_KEEP).all()


def test_fit_rule_finds_informative_feature():
    df = _data()
    df["rsi_mid"] = np.where(df["r"] > 0, 5.0, -5.0) + np.random.default_rng(1).normal(0, 0.1, len(df))
    rule = train.fit_rule(df)
    assert rule.feature == "rsi_mid" and rule.op == ">="
    assert rule.keep(df).mean() >= train.MIN_KEEP


def test_yearly_criterion_counts_empty_year_as_worse():
    oos = pd.DataFrame({"signal_time": pd.to_datetime(["2023-03-01", "2024-03-01", "2025-03-01"]),
                        "r": [1.0, 1.0, -1.0]})
    keep = np.array([True, True, False])
    crit = train.criteria(oos, keep, rule_exp=0.0)
    assert crit["4. not worse in all 3 years"] == ("2 of 3", False)
