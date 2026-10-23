"""Phase 4: expanding-window walk-forward for LR / LightGBM / rule filters, judged in R.

Usage:  uv run python -m xauai.train            # walk-forward + gate only (test set never read)
        uv run python -m xauai.train --final    # also evaluates the test set ONCE, only if the gate passed
Appends results to reports/04_model.md below the committed pre-registration; writes figures reports/04_*.png.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from lightgbm import LGBMClassifier  # noqa: E402
from sklearn.linear_model import LogisticRegression  # noqa: E402
from sklearn.metrics import roc_auc_score  # noqa: E402
from sklearn.pipeline import make_pipeline  # noqa: E402
from sklearn.preprocessing import StandardScaler  # noqa: E402

from xauai import config  # noqa: E402
from xauai.backtest import bootstrap_ci, metrics  # noqa: E402
from xauai.features import FEATURES, compute_features  # noqa: E402
from xauai.md import table  # noqa: E402
from xauai.signals import compute_signals  # noqa: E402

FOLD_STARTS = pd.to_datetime(["2023-01-01", "2023-07-01", "2024-01-01", "2024-07-01", "2025-01-01", "2025-07-01"])
OOS_END = pd.Timestamp(config.VAL_END) + pd.Timedelta(days=1)  # exclusive
TEST_START = pd.Timestamp(config.TEST_START)
TEST_END = pd.Timestamp(config.TEST_END) + pd.Timedelta(days=1)  # exclusive
THRESHOLDS = np.round(np.arange(0.300, 0.7001, 0.005), 3)
MIN_KEEP = 0.40
MIN_UPLIFT = 0.10
RULE_QUANTILES = np.arange(0.1, 0.91, 0.1)
MODELS = ("lr", "lgbm")
MODEL_NAME = {"lr": "Logistic regression", "lgbm": "LightGBM", "rule": "Rule baseline"}
MARKER = "<!-- RESULTS BELOW"
LGBM_PARAMS = dict(
    num_leaves=8, max_depth=3, min_child_samples=20, learning_rate=0.05, n_estimators=100,
    random_state=0, deterministic=True, n_jobs=1, verbose=-1,
)
# palette (dataviz reference instance, light surface)
SURFACE, INK, MUTED, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
COLOR = {"lr": "#2a78d6", "lgbm": "#eb6834"}


# --- Data -------------------------------------------------------------------
def load_dataset() -> pd.DataFrame:
    trades = pd.read_parquet(config.LABELS_PATH)
    feats = compute_features(compute_signals(pd.read_parquet(config.RAW_H1_PATH)), pd.read_parquet(config.RAW_D1_PATH))
    assert (trades.set_index("signal_time")["direction"] == feats.set_index("signal_time").loc[trades["signal_time"], "direction"]).all()
    data = trades.merge(feats.drop(columns="direction"), on="signal_time", how="inner", validate="one_to_one")
    assert len(data) == len(trades), "every labeled trade needs features"
    data["y"] = (data["r"] > 0).astype(int)
    return data.sort_values("signal_time").reset_index(drop=True)


def lr_matrix(df: pd.DataFrame) -> np.ndarray:
    x = df[[f for f in FEATURES if f not in ("hour", "dow")]].copy()
    x["hour_sin"], x["hour_cos"] = np.sin(2 * np.pi * df["hour"] / 24), np.cos(2 * np.pi * df["hour"] / 24)
    x["dow_sin"], x["dow_cos"] = np.sin(2 * np.pi * df["dow"] / 5), np.cos(2 * np.pi * df["dow"] / 5)
    return x.to_numpy(float)


LR_COLUMNS = [f for f in FEATURES if f not in ("hour", "dow")] + ["hour_sin", "hour_cos", "dow_sin", "dow_cos"]


def fit_predict(kind: str, train: pd.DataFrame, test: pd.DataFrame) -> tuple[np.ndarray, pd.Series]:
    """Return (P(win) for test, importance per feature)."""
    if kind == "lr":
        model = make_pipeline(StandardScaler(), LogisticRegression(C=1.0, max_iter=5000))
        model.fit(lr_matrix(train), train["y"])
        coef = pd.Series(np.abs(model[-1].coef_[0]), index=LR_COLUMNS)
        return model.predict_proba(lr_matrix(test))[:, 1], coef
    model = LGBMClassifier(**LGBM_PARAMS, importance_type="gain")
    model.fit(train[FEATURES], train["y"])
    return model.predict_proba(test[FEATURES])[:, 1], pd.Series(model.feature_importances_, index=FEATURES)


@dataclass(frozen=True)
class Rule:
    feature: str
    op: str  # ">=" or "<="
    threshold: float

    def keep(self, df: pd.DataFrame) -> np.ndarray:
        v = df[self.feature].to_numpy(float)
        return v >= self.threshold if self.op == ">=" else v <= self.threshold

    def __str__(self) -> str:
        return f"{self.feature} {self.op} {self.threshold:.3g}"


def fit_rule(train: pd.DataFrame) -> Rule:
    """Best single-feature threshold by mean R on train, keeping >= MIN_KEEP of train trades."""
    best, best_score = None, -np.inf
    r = train["r"].to_numpy()
    for f in FEATURES:
        for thr in np.unique(np.quantile(train[f], RULE_QUANTILES)):
            for op in (">=", "<="):
                rule = Rule(f, op, float(thr))
                k = rule.keep(train)
                if k.mean() >= MIN_KEEP and r[k].mean() > best_score:
                    best, best_score = rule, r[k].mean()
    assert best is not None
    return best


# --- Walk-forward -----------------------------------------------------------
def walk_forward(data: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, pd.DataFrame]]:
    ends = [*FOLD_STARTS[1:], OOS_END]
    preds, folds = [], []
    imps: dict[str, list[pd.Series]] = {k: [] for k in MODELS}
    for k, (start, end) in enumerate(zip(FOLD_STARTS, ends), 1):
        before = data[data["signal_time"] < start]
        train = before[before["exit_time"] < start]  # purge labels that end after the fold start
        test = data[(data["signal_time"] >= start) & (data["signal_time"] < end)].copy()
        test["fold"] = k
        for kind in MODELS:
            p, imp = fit_predict(kind, train, test)
            test[f"p_{kind}"] = p
            imps[kind].append(imp / imp.sum())
        rule = fit_rule(train)
        test["keep_rule"] = rule.keep(test)
        preds.append(test)
        folds.append({"fold": k, "start": start.date(), "end": (end - pd.Timedelta(days=1)).date(),
                      "train_n": len(train), "purged": len(before) - len(train), "train_win_rate": train["y"].mean(),
                      "oos_n": len(test), "rule": str(rule)})
    return pd.concat(preds, ignore_index=True), pd.DataFrame(folds), {k: pd.concat(v, axis=1) for k, v in imps.items()}


def threshold_curve(p: np.ndarray, r: np.ndarray) -> pd.DataFrame:
    rows = []
    for t in THRESHOLDS:
        k = p >= t
        rows.append({"threshold": t, "kept": int(k.sum()), "keep_frac": k.mean(),
                     "expectancy": r[k].mean() if k.any() else np.nan, "total_R": r[k].sum()})
    return pd.DataFrame(rows)


def choose_threshold(curve: pd.DataFrame) -> float:
    ok = curve[curve["keep_frac"] >= MIN_KEEP]
    best = ok["expectancy"].max()
    return float(ok.loc[ok["expectancy"] == best, "threshold"].min())


def yearly(oos: pd.DataFrame, keep: np.ndarray) -> pd.DataFrame:
    rows = []
    for y, g in oos.groupby(oos["signal_time"].dt.year):
        k = keep[g.index]
        rows.append({"year": int(y), "base_n": len(g), "base_exp": g["r"].mean(), "base_total": g["r"].sum(),
                     "filt_n": int(k.sum()), "filt_exp": g["r"][k].mean() if k.any() else np.nan,
                     "filt_total": g["r"][k].sum()})
    return pd.DataFrame(rows)


def criteria(oos: pd.DataFrame, keep: np.ndarray, rule_exp: float) -> dict[str, tuple[str, bool]]:
    r = oos["r"].to_numpy()
    base_exp, base_tot = r.mean(), r.sum()
    f_exp = r[keep].mean() if keep.any() else -np.inf
    yr = yearly(oos, keep)
    not_worse = int((yr["filt_n"].gt(0) & (yr["filt_exp"] >= yr["base_exp"])).sum())
    return {
        "1. expectancy ≥ baseline + 0.10R": (f"{f_exp:+.3f} vs {base_exp + MIN_UPLIFT:+.3f}", f_exp >= base_exp + MIN_UPLIFT),
        "2. keeps ≥ 40% of trades": (f"{keep.mean():.1%}", keep.mean() >= MIN_KEEP),
        "3. total R ≥ baseline total R": (f"{r[keep].sum():+.2f} vs {base_tot:+.2f}", r[keep].sum() >= base_tot),
        "4. not worse in all 3 years": (f"{not_worse} of {len(yr)}", not_worse >= 3),
        "5. beats rule baseline": (f"{f_exp:+.3f} vs {rule_exp:+.3f}", f_exp > rule_exp),
    }


# --- Figures ----------------------------------------------------------------
def _style(ax) -> None:
    ax.set_facecolor(SURFACE)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color("#c9c8c2")
    ax.tick_params(colors=MUTED)


def plot_importance(imps: dict[str, pd.DataFrame], path) -> None:
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.8), facecolor=SURFACE)
    titles = {"lr": "Logistic regression — mean |standardized coef| share", "lgbm": "LightGBM — mean gain share"}
    for ax, kind in zip(axes, MODELS):
        s = imps[kind].mean(axis=1).sort_values()
        _style(ax)
        ax.grid(axis="y", visible=False)
        ax.grid(axis="x", color=GRID, linewidth=0.8)
        ax.barh(s.index, s.to_numpy(), color=COLOR[kind], height=0.7, edgecolor=SURFACE)
        ax.set_title(titles[kind], color=INK, fontsize=10, loc="left")
        ax.tick_params(axis="y", labelsize=8.5)
    fig.suptitle("Feature importance, averaged over the 6 walk-forward folds", color=INK, fontsize=11, x=0.01, ha="left")
    fig.tight_layout()
    fig.savefig(path, dpi=150, facecolor=SURFACE)
    plt.close(fig)


def plot_calibration(oos: pd.DataFrame, path) -> None:
    fig, ax = plt.subplots(figsize=(6, 5.2), facecolor=SURFACE)
    _style(ax)
    ax.plot([0.15, 0.65], [0.15, 0.65], color=MUTED, linewidth=1, linestyle=(0, (4, 3)), label="perfect calibration")
    for kind in MODELS:
        bins = pd.qcut(oos[f"p_{kind}"], 5, duplicates="drop")
        g = oos.groupby(bins, observed=True).agg(p=(f"p_{kind}", "mean"), w=("y", "mean"), n=("y", "size"))
        ax.plot(g["p"], g["w"], color=COLOR[kind], linewidth=2, marker="o", markersize=8,
                markeredgecolor=SURFACE, markeredgewidth=2, label=MODEL_NAME[kind])
    ax.axhline(oos["y"].mean(), color=MUTED, linewidth=1, linestyle=":")
    ax.text(0.155, oos["y"].mean() + 0.008, f"OOS win rate {oos['y'].mean():.1%}", color=MUTED, fontsize=8.5)
    ax.set_xlabel("predicted P(R > 0), quintile mean", color=MUTED)
    ax.set_ylabel("observed share with R > 0", color=MUTED)
    ax.set_title("Calibration — pooled walk-forward OOS", color=INK, fontsize=11, loc="left")
    ax.legend(frameon=False, fontsize=9, labelcolor=INK)
    fig.tight_layout()
    fig.savefig(path, dpi=150, facecolor=SURFACE)
    plt.close(fig)


def plot_thresholds(curves: dict[str, pd.DataFrame], chosen: dict[str, float], base_exp: float, path) -> None:
    fig, (a1, a2) = plt.subplots(2, 1, figsize=(9, 6.5), sharex=True, facecolor=SURFACE,
                                 gridspec_kw={"height_ratios": [3, 2]})
    for ax in (a1, a2):
        _style(ax)
    for kind in MODELS:
        c = curves[kind]
        a1.plot(c["threshold"], c["expectancy"], color=COLOR[kind], linewidth=2, label=MODEL_NAME[kind])
        a2.plot(c["threshold"], c["keep_frac"] * 100, color=COLOR[kind], linewidth=2)
        for ax in (a1, a2):
            ax.axvline(chosen[kind], color=COLOR[kind], linewidth=1, linestyle=":")
    a1.axhline(base_exp, color=MUTED, linewidth=1, linestyle=(0, (4, 3)))
    a1.text(THRESHOLDS[0], base_exp, " baseline", color=MUTED, fontsize=8.5, va="bottom")
    a1.axhline(base_exp + MIN_UPLIFT, color=INK, linewidth=1, linestyle=(0, (1, 2)))
    a1.text(THRESHOLDS[0], base_exp + MIN_UPLIFT, " pass line (+0.10R)", color=INK, fontsize=8.5, va="bottom")
    a2.axhline(MIN_KEEP * 100, color=INK, linewidth=1, linestyle=(0, (1, 2)))
    a2.text(THRESHOLDS[-1], MIN_KEEP * 100, "40% minimum ", color=INK, fontsize=8.5, va="bottom", ha="right")
    a1.set_ylabel("filtered expectancy (R)", color=MUTED)
    a2.set_ylabel("trades kept (%)", color=MUTED)
    a2.set_xlabel("probability threshold (dotted = chosen)", color=MUTED)
    a1.set_title("Threshold vs expectancy and trade count — pooled walk-forward OOS", color=INK, fontsize=11, loc="left")
    a1.legend(frameon=False, fontsize=9, labelcolor=INK, loc="lower left")
    fig.tight_layout()
    fig.savefig(path, dpi=150, facecolor=SURFACE)
    plt.close(fig)


# --- Report -----------------------------------------------------------------
def run_test_set(data: pd.DataFrame, kind: str, thr: float) -> list[str]:
    before = data[data["signal_time"] < TEST_START]
    train = before[before["exit_time"] < TEST_START]
    test = data[(data["signal_time"] >= TEST_START) & (data["signal_time"] < TEST_END)].copy()
    p, _ = fit_predict(kind, train, test)
    keep = p >= thr
    r = test.sort_values("signal_time")["r"].to_numpy()
    keep = keep[np.argsort(test["signal_time"].to_numpy(), kind="stable")]
    months = ((TEST_END - TEST_START) / pd.Timedelta(days=1)) / 30.4375
    rows = []
    for name, rr in (("baseline (all)", r), (f"filtered ({MODEL_NAME[kind]}, p ≥ {thr:.3f})", r[keep])):
        m = metrics(rr, months)
        lo, hi = bootstrap_ci(rr)
        rows.append({"group": name, "n": m["n"], "win_rate": f"{m['win_rate']:.1%}", "expectancy_R": m["expectancy_R"],
                     "ci95_low": lo, "ci95_high": hi, "total_R": rr.sum(), "max_dd_R": m["max_dd_R"],
                     "trades_per_month": m["trades_per_month"]})
    return [
        "## Test set (run once)\n",
        f"Final {MODEL_NAME[kind]} trained on {len(train)} trades before {TEST_START.date()} "
        f"({len(before) - len(train)} purged); applied once to {len(test)} test trades.\n",
        table(pd.DataFrame(rows)),
        "",
    ]


def build_results(data: pd.DataFrame, oos: pd.DataFrame, folds: pd.DataFrame, curves, chosen, crit, verdicts,
                  rule_exp: float, test_lines: list[str] | None) -> str:
    out: list[str] = []
    w = out.append
    r = oos["r"].to_numpy()
    passed = [k for k in MODELS if verdicts[k]]
    w(f"\n## Results\n\nGenerated by `src/xauai/train.py` at {datetime.now():%Y-%m-%d %H:%M} (local).\n")
    w(f"Pooled OOS: {len(oos)} trades, {oos['signal_time'].min().date()} .. {oos['signal_time'].max().date()}; "
      f"baseline expectancy {r.mean():+.3f}R, total {r.sum():+.2f}R, win rate {oos['y'].mean():.1%}.\n")
    if passed:
        best = max(passed, key=lambda k: r[oos[f"p_{k}"].to_numpy() >= chosen[k]].mean())
        w(f"**Gate: PASS** — chosen model: {MODEL_NAME[best]} at threshold {chosen[best]:.3f}.\n")
    else:
        w("**Gate: FAIL** — neither model meets all five pre-registered criteria. "
          "The test set was not touched.\n")

    w("### Pre-registered criteria (pooled OOS)\n")
    rows = []
    for name in crit["lr"]:
        row = {"criterion": name}
        for k in MODELS:
            v, ok = crit[k][name]
            row[MODEL_NAME[k]] = f"{v} {'✅' if ok else '❌'}"
        rows.append(row)
    rows.append({"criterion": "**all five**", **{MODEL_NAME[k]: "**PASS**" if verdicts[k] else "**FAIL**" for k in MODELS}})
    w(table(pd.DataFrame(rows)))
    w("")

    w("### Pooled OOS summary\n")
    months = ((OOS_END - FOLD_STARTS[0]) / pd.Timedelta(days=1)) / 30.4375
    rows = []
    keeps = {"baseline": np.ones(len(oos), bool), "rule": oos["keep_rule"].to_numpy(),
             **{k: oos[f"p_{k}"].to_numpy() >= chosen[k] for k in MODELS}}
    order = oos["signal_time"].argsort(kind="stable").to_numpy()
    for name, k in keeps.items():
        rr = r[order][k[order]]
        m = metrics(rr, months)
        lo, hi = bootstrap_ci(rr)
        rows.append({"filter": "Baseline (all)" if name == "baseline" else MODEL_NAME[name],
                     "threshold": f"{chosen[name]:.3f}" if name in chosen else "—",
                     "AUC": f"{roc_auc_score(oos['y'], oos[f'p_{name}']):.3f}" if name in MODELS else "—",
                     "n": m["n"], "kept": f"{k.mean():.0%}", "win_rate": f"{m['win_rate']:.1%}",
                     "expectancy_R": m["expectancy_R"], "ci95": f"[{lo:+.3f}, {hi:+.3f}]", "total_R": rr.sum(),
                     "PF": m["profit_factor"], "max_dd_R": m["max_dd_R"]})
    w(table(pd.DataFrame(rows)))
    w("")
    w("The rule baseline is refit per fold; its rules were: "
      + "; ".join(f"fold {f.fold}: `{f.rule}`" for f in folds.itertuples()) + ".\n")

    w("### Per fold\n")
    rows = []
    for f in folds.itertuples():
        g = oos[oos["fold"] == f.fold]
        row = {"fold": f.fold, "period": f"{f.start} .. {f.end}", "train_n": f.train_n, "purged": f.purged,
               "oos_n": len(g), "base_exp": g["r"].mean()}
        for k in MODELS:
            kk = g[f"p_{k}"] >= chosen[k]
            row[f"{k}_n"] = int(kk.sum())
            row[f"{k}_exp"] = g["r"][kk].mean() if kk.any() else np.nan
        kk = g["keep_rule"]
        row["rule_n"], row["rule_exp"] = int(kk.sum()), g["r"][kk].mean() if kk.any() else np.nan
        rows.append(row)
    w(table(pd.DataFrame(rows)))
    w("")

    w("### Per year: baseline vs filtered\n")
    rows = []
    for k in ("rule", *MODELS):
        yr = yearly(oos, keeps[k])
        for y in yr.itertuples():
            rows.append({"filter": MODEL_NAME[k], "year": y.year, "base_n": y.base_n, "base_exp": y.base_exp,
                         "filt_n": y.filt_n, "filt_exp": y.filt_exp, "base_total": y.base_total,
                         "filt_total": y.filt_total})
    w(table(pd.DataFrame(rows)))
    w("")

    w("### Feature importance\n\n![Feature importance](04_importance.png)\n")
    w("### Calibration\n\n![Calibration](04_calibration.png)\n")
    w("### Threshold vs expectancy / trade count\n\n![Threshold curve](04_threshold.png)\n")
    rows = []
    for t in (0.35, 0.40, 0.45, 0.50, 0.55):
        row = {"threshold": f"{t:.2f}"}
        for k in MODELS:
            c = curves[k].set_index("threshold").loc[t]
            row[f"{k}_kept"] = f"{int(c['kept'])} ({c['keep_frac']:.0%})"
            row[f"{k}_exp"] = c["expectancy"]
        rows.append(row)
    w(table(pd.DataFrame(rows)))
    w("")

    if test_lines:
        out.extend(test_lines)
    else:
        w("## Test set\n\nNot run (gate failed). Per the pre-registration: no tuning and no rerun against the test set.\n")
    return "\n".join(out) + "\n"


def main(final: bool = False) -> None:
    data = load_dataset()
    oos, folds, imps = walk_forward(data)
    r = oos["r"].to_numpy()
    curves = {k: threshold_curve(oos[f"p_{k}"].to_numpy(), r) for k in MODELS}
    chosen = {k: choose_threshold(curves[k]) for k in MODELS}
    rule_keep = oos["keep_rule"].to_numpy()
    rule_exp = r[rule_keep].mean()
    crit = {k: criteria(oos, oos[f"p_{k}"].to_numpy() >= chosen[k], rule_exp) for k in MODELS}
    verdicts = {k: all(ok for _, ok in crit[k].values()) for k in MODELS}

    test_lines = None
    passed = [k for k in MODELS if verdicts[k]]
    if passed and final:
        best = max(passed, key=lambda k: r[oos[f"p_{k}"].to_numpy() >= chosen[k]].mean())
        test_lines = run_test_set(data, best, chosen[best])

    plot_importance(imps, config.REPORTS_DIR / "04_importance.png")
    plot_calibration(oos, config.REPORTS_DIR / "04_calibration.png")
    plot_thresholds(curves, chosen, r.mean(), config.REPORTS_DIR / "04_threshold.png")

    report = config.REPORTS_DIR / "04_model.md"
    prereg = report.read_text(encoding="utf-8")
    cut = prereg.index(MARKER)
    prereg = prereg[: prereg.index("\n", cut) + 1]  # keep pre-registration + marker line verbatim
    report.write_text(prereg + build_results(data, oos, folds, curves, chosen, crit, verdicts, rule_exp, test_lines),
                      encoding="utf-8")
    print({k: ("PASS" if v else "FAIL") for k, v in verdicts.items()}, "thresholds", chosen, "->", report)


if __name__ == "__main__":
    main(final="--final" in sys.argv[1:])
