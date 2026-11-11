from __future__ import annotations

import pandas as pd
from conftest import make_bars

from xauai import export
from xauai.timeutil import TF_LEN


def test_drop_forming_bar_m5() -> None:
    df = make_bars([(1, 2, 0, 1)] * 3, "2025-01-06 10:00", "5min")  # 10:00, 10:05, 10:10
    out = export.drop_forming_bar(df, pd.Timestamp("2025-01-06 10:13"), TF_LEN["M5"])
    assert list(out["time"]) == list(df["time"].iloc[:2])
    kept = export.drop_forming_bar(df, pd.Timestamp("2025-01-06 10:15"), TF_LEN["M5"])
    assert len(kept) == 3


def test_cli_routes_research(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(export, "export_research", lambda *a, **k: calls.append("research") or {})
    monkeypatch.setattr(export, "main", lambda: calls.append("main"))
    export.cli(["--research"])
    export.cli([])
    assert calls == ["research", "main"]
