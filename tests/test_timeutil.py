import pandas as pd

from conftest import make_bars
from xauai.timeutil import TF_LEN, assign_session, previous_session, server_to_utc, session_windows, utc_to_server


def test_server_offset_winter_and_us_dst():
    utc = server_to_utc(pd.Series(pd.to_datetime(["2024-01-15 10:00", "2024-07-15 10:00", "2024-03-20 10:00"])))
    # winter +2, summer +3, US-DST-only week (US changed 2024-03-10, EU 2024-03-31) +3
    assert list(utc) == list(pd.to_datetime(["2024-01-15 08:00", "2024-07-15 07:00", "2024-03-20 07:00"]))


def test_utc_server_roundtrip():
    # transition hours themselves are ambiguous/nonexistent in server time; test around them
    s = pd.Series(pd.date_range("2024-03-01", "2024-03-09", freq="h").append(pd.date_range("2024-03-12", "2024-04-05", freq="h")))
    assert utc_to_server(server_to_utc(s)).equals(s)


def test_london_session_follows_uk_clock_in_us_only_dst_week():
    w = session_windows("2024-03-20", "2024-03-20")
    lon = w[w.session == "london"].iloc[0]
    # London 08:00 GMT (UK not yet on BST) = 08:00 UTC = 11:00 server (+3 during US DST)
    assert lon.start == pd.Timestamp("2024-03-20 11:00") and lon.end == pd.Timestamp("2024-03-20 20:00")


def test_assign_session_overlap_and_outside():
    w = session_windows("2024-01-15", "2024-01-16")
    out = assign_session(pd.Series(pd.to_datetime(["2024-01-15 16:00", "2024-01-16 00:30"])), w)
    # winter (+2): London 10-19, NY 15-24 server -> 16:00 is overlap -> NY (started last);
    # 00:30 is after NY, before Asia (02:00 server) -> next session = Asia
    assert out.session.tolist() == ["ny", "asia"]
    assert out.session_end.iloc[0] == pd.Timestamp("2024-01-16 00:00")


def test_previous_session_excludes_running_session():
    w = session_windows("2024-01-15", "2024-01-16")
    p = previous_session(pd.Timestamp("2024-01-15 11:00"), w)  # London running, Asia ended 10:00 server
    assert p.session == "asia"
    assert previous_session(pd.Timestamp("2024-01-15 00:00"), w) is None


def test_windows_weekdays_only_sorted():
    w = session_windows("2024-01-12", "2024-01-15")  # Fri .. Mon
    assert len(w) == 6
    assert w.start.is_monotonic_increasing


def test_tf_len_and_make_bars():
    assert TF_LEN["H1"] == pd.Timedelta(hours=1) and TF_LEN["M1"] == pd.Timedelta(minutes=1)
    b = make_bars([(1, 2, 0, 1)] * 3, "2024-01-01", "h")
    assert list(b.columns) == ["time", "open", "high", "low", "close", "tick_volume", "spread"]
