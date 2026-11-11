from __future__ import annotations

import pandas as pd
import pytest

from conftest import make_bars
from xauai.levels import LEVEL_KINDS, level_table, merge_levels, refresh_times

T = pd.Timestamp


def _d1(highs: list[float], start: str = "2024-01-01") -> pd.DataFrame:
    rows = [(h - 1, h, h - 2, h - 1) for h in highs]
    return make_bars(rows, start, "1D")


def _flat(start: str, periods: int, freq: str) -> pd.DataFrame:
    return make_bars([(100.0, 100.5, 99.5, 100.0)] * periods, start, freq)


def _windows(*starts: str, hours: int = 2) -> pd.DataFrame:
    s = pd.to_datetime(list(starts))
    return pd.DataFrame({"session": "X", "start": s, "end": s + pd.Timedelta(hours=hours)})


def _bars(d1: pd.DataFrame, days: int) -> dict[str, pd.DataFrame]:
    return {"M1": _flat("2024-01-01", days * 1440, "1min"), "M30": _flat("2024-01-01", days * 48, "30min"), "D1": d1}


def _get(tab: pd.DataFrame, vf: str, kind: str) -> float | None:
    r = tab[(tab["valid_from"] == T(vf)) & (tab["kind"] == kind)]
    return None if r.empty else float(r["price"].iloc[0])


def test_refresh_times_union_sorted_unique() -> None:
    r = refresh_times(_windows("2024-01-02 10:00", "2024-01-02 00:00"), _d1([1, 2, 3]))
    assert list(r) == [T("2024-01-01"), T("2024-01-02"), T("2024-01-02 10:00"), T("2024-01-03")]


def test_level_table_uses_only_finished_session_and_day() -> None:
    d1 = _d1([101, 102, 103])  # Jan1, Jan2, Jan3 highs
    b = _bars(d1, 3)
    m1 = b["M1"]
    m1.loc[m1["time"] == T("2024-01-02 10:30"), "high"] = 105.0  # previous-session spike
    m1.loc[m1["time"] == T("2024-01-03 10:30"), "high"] = 120.0  # today's spike, must not leak
    tab = level_table(b, _windows("2024-01-02 10:00", "2024-01-03 10:00"))
    assert _get(tab, "2024-01-03 10:00", "pdh") == 102  # yesterday (Jan2), never Jan3's 103
    assert _get(tab, "2024-01-03 10:00", "psh") == 105
    assert _get(tab, "2024-01-03 00:00", "pdh") == 102  # Jan2 closes exactly at Jan3 00:00
    assert _get(tab, "2024-01-02 10:00", "pdh") == 101
    assert not tab[tab["kind"] == "pdh"]["price"].eq(103).any()
    # valid_to is the next refresh; last is NaT
    assert tab[tab["valid_from"] == T("2024-01-03 00:00")]["valid_to"].iloc[0] == T("2024-01-03 10:00")
    assert tab["valid_to"].iloc[-1] is pd.NaT or pd.isna(tab["valid_to"].iloc[-1])


def test_weekly_levels_from_previous_server_week() -> None:
    d1 = _d1([101, 110, 103, 104, 105, 99, 99, 150, 90])  # Jan1(Mon)..Jan9; week2 starts Jan8
    # weekly high from week1 (Jan1-7) = 110; week2's 150 must not leak into week2 itself
    tab = level_table(_bars(d1, 9), _windows("2024-01-05 10:00", "2024-01-09 10:00"))
    assert _get(tab, "2024-01-05 10:00", "pwh") is None  # first week: no completed week
    assert _get(tab, "2024-01-09 10:00", "pwh") == 110
    wk1 = d1[d1["time"] < T("2024-01-08")]
    assert _get(tab, "2024-01-09 10:00", "pwl") == wk1["low"].min()
    assert _get(tab, "2024-01-08 00:00", "pwh") == 110


def test_swing_anchor_falls_back_to_session_extreme() -> None:
    b = _bars(_d1([101, 102, 103]), 3)
    m1 = b["M1"]
    m1.loc[m1["time"] == T("2024-01-02 10:30"), "high"] = 105.0
    m1.loc[m1["time"] == T("2024-01-02 11:00"), "low"] = 95.0
    tab = level_table(b, _windows("2024-01-02 10:00", "2024-01-03 10:00"))
    vf = "2024-01-03 10:00"
    assert (_get(tab, vf, "swh"), _get(tab, vf, "swl")) == (105.0, 95.0)  # no M30 swing -> psh/psl
    assert _get(tab, vf, "poc") is not None and _get(tab, vf, "val") <= _get(tab, vf, "poc") <= _get(tab, vf, "vah")


def test_swing_anchor_uses_confirmed_m30_swing() -> None:
    b = _bars(_d1([101, 102, 103]), 3)
    m1, m30 = b["M1"], b["M30"]
    m1.loc[m1["time"] == T("2024-01-02 10:45"), "high"] = 105.0
    m30.loc[m30["time"] == T("2024-01-02 10:30"), "high"] = 104.0  # swing high, known 11:30
    tab = level_table(b, _windows("2024-01-02 10:00", "2024-01-03 10:00"))
    vf = "2024-01-03 10:00"
    assert _get(tab, vf, "psh") == 105.0
    assert _get(tab, vf, "swh") == 104.0


def test_start_of_data_produces_no_nan_levels() -> None:
    b = _bars(_d1([101, 102, 103]), 3)
    tab = level_table(b, _windows("2024-01-01 10:00", "2024-01-02 10:00"))
    assert not tab["price"].isna().any()
    first = tab[tab["valid_from"] == T("2024-01-01")]
    assert first.empty  # nothing known yet: no day, week or session
    at_first_session = set(tab[tab["valid_from"] == T("2024-01-01 10:00")]["kind"])
    assert not {"pdh", "pdl", "psh", "psl"} & at_first_session
    assert set(tab["kind"]) <= set(LEVEL_KINDS)


def test_merge_takes_outer_edge_per_side() -> None:
    lv = pd.DataFrame(
        {
            "valid_from": T("2024-01-02"),
            "valid_to": pd.NaT,
            "kind": ["pdh", "psh", "pdl", "psl", "pwl"],
            "price": [100.0, 100.2, 98.0, 97.9, 95.0],
        }
    )
    m = merge_levels(lv, ref_price=99.0, h1_atr=1.0, merge=0.25).sort_values("price").reset_index(drop=True)
    assert list(m["side"]) == [-1, -1, 1]
    assert list(m["price"]) == [95.0, 97.9, 100.2]  # above: max of cluster; below: min of cluster
    assert list(m["kinds"]) == ["pwl", "psl,pdl", "pdh,psh"]


def test_merge_level_equal_to_ref_is_above() -> None:
    lv = pd.DataFrame({"valid_from": T("2024-01-02"), "valid_to": pd.NaT, "kind": ["pdh"], "price": [99.0]})
    assert list(merge_levels(lv, 99.0, 1.0)["side"]) == [1]
