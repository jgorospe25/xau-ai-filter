"""Markdown helpers for reports."""

from __future__ import annotations

import pandas as pd


def table(df: pd.DataFrame, floatfmt: str = ".3f") -> str:
    def fmt(v: object) -> str:
        if isinstance(v, float):
            return "—" if pd.isna(v) else format(v, floatfmt)
        return str(v)

    cols = [str(c) for c in df.columns]
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for row in df.itertuples(index=False):
        lines.append("| " + " | ".join(fmt(v) for v in row) + " |")
    return "\n".join(lines)
