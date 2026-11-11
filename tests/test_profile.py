from __future__ import annotations

import pandas as pd
import pytest

from conftest import make_bars
from xauai.profile import Profile, volume_profile


def _fixture() -> pd.DataFrame:
    df = make_bars([(100.0, 100.5, 100.0, 100.5), (100.0, 101.0, 100.0, 100.5), (102.0, 102.5, 102.0, 102.5)], "2024-01-02", "1min")
    df["tick_volume"] = [10, 4, 2]
    return df


# Hand computation (bin 0.5, bin k = [0.5k, 0.5k+0.5), centre 0.5k+0.25):
#   bar1 [100.0,100.5] -> bins 200,201 -> 10/2 = 5 each
#   bar2 [100.0,101.0] -> bins 200,201,202 -> 4/3 = 1.333 each
#   bar3 [102.0,102.5] -> bins 204,205 -> 2/2 = 1 each
#   totals: 200 = 6.333, 201 = 6.333, 202 = 1.333, 204 = 1, 205 = 1; sum = 16
#   POC: 200 and 201 tie -> lower -> centre 100.25
#   value area target 0.70 * 16 = 11.2; start 6.333 (bin 200).
#   neighbours: 199 = 0, 201 = 6.333 -> add 201 -> 12.667 >= 11.2. VAL = 100.25, VAH = 100.75.
def test_profile_poc_and_value_area_on_fixture() -> None:
    p = volume_profile(_fixture(), bin_size=0.5, va=0.70)
    assert p is not None
    assert p.poc == pytest.approx(100.25)
    assert (p.val, p.vah) == (pytest.approx(100.25), pytest.approx(100.75))


def test_profile_empty_is_none() -> None:
    assert volume_profile(_fixture().iloc[0:0]) is None


def test_profile_value_area_tie_goes_upper() -> None:
    # single-bin POC at bin 200 (vol 10); bins 199 and 201 both 3 -> tie -> upper (201) first.
    rows = [(100.1, 100.2, 100.1, 100.1), (99.6, 99.7, 99.6, 99.6), (100.6, 100.7, 100.6, 100.6)]
    df = make_bars(rows, "2024-01-02", "1min")
    df["tick_volume"] = [10, 3, 3]
    p = volume_profile(df, bin_size=0.5, va=0.70)  # total 16, target 11.2: 10 -> 13 (upper) done
    assert p == Profile(poc=pytest.approx(100.25), vah=pytest.approx(100.75), val=pytest.approx(100.25))


def test_profile_value_area_crosses_empty_gap_bins() -> None:
    # bins 200-202 get 1/3 each from bar 1; bin 200 +10 (bar 2); bin 210 gets 6 (bar 3). Total 17, target 11.9.
    # 200 = 10.333, 201 = 202 = 0.333, 203..209 = 0 (empty gap), 210 = 6. Growth must walk the gap
    # (ties between empty bins go upper) until it reaches bin 210: VAL = 100.25, VAH = 105.25.
    rows = [(100.0, 101.0, 100.0, 100.5), (100.0, 100.2, 100.0, 100.1), (105.0, 105.2, 105.0, 105.1)]
    df = make_bars(rows, "2024-01-02", "1min")
    df["tick_volume"] = [1, 10, 6]
    p = volume_profile(df, bin_size=0.5, va=0.70)
    assert p == Profile(poc=pytest.approx(100.25), vah=pytest.approx(105.25), val=pytest.approx(100.25))


def test_profile_value_area_when_one_side_exhausted() -> None:
    # POC is the lowest bin (200, vol 10), bin 202 has 5, bin 201 is empty. Total 15, target 10.5.
    # Down side is exhausted at the histogram edge; growth must continue upward through the gap.
    rows = [(100.0, 100.2, 100.0, 100.1), (101.0, 101.2, 101.0, 101.1)]
    df = make_bars(rows, "2024-01-02", "1min")
    df["tick_volume"] = [10, 5]
    p = volume_profile(df, bin_size=0.5, va=0.70)
    assert p == Profile(poc=pytest.approx(100.25), vah=pytest.approx(101.25), val=pytest.approx(100.25))
