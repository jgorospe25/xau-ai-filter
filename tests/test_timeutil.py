import pandas as pd

from conftest import make_bars
from xauai.timeutil import (
    TF_LEN,
    assign_session,
    in_windows,
    local_windows,
    previous_session,
    server_to_utc,
    session_windows,
    time_filter_mask,
    utc_to_server,
)


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


def test_asia_box_window_crosses_midnight_winter():
    w = local_windows("2025-01-06", "2025-01-06", "America/New_York", 20, 24)
    assert len(w) == 1
    assert w.iloc[0].start == pd.Timestamp("2025-01-07 03:00") and w.iloc[0].end == pd.Timestamp("2025-01-07 07:00")


def test_asia_box_window_summer():
    w = local_windows("2025-07-07", "2025-07-07", "America/New_York", 20, 24)
    assert len(w) == 1
    assert w.iloc[0].start == pd.Timestamp("2025-07-08 03:00") and w.iloc[0].end == pd.Timestamp("2025-07-08 07:00")


def test_killzone_us_only_dst_week():
    for day in ("2025-03-10", "2025-03-03"):  # server and NY shift together
        w = local_windows(day, day, "America/New_York", 7, 10)
        assert w.iloc[0].start == pd.Timestamp(f"{day} 14:00") and w.iloc[0].end == pd.Timestamp(f"{day} 17:00")


def test_owner_hours_manila_fixed_offset():
    w = local_windows("2025-01-06", "2025-01-06", "Asia/Manila", 8, 18)
    assert w.iloc[0].start == pd.Timestamp("2025-01-06 02:00") and w.iloc[0].end == pd.Timestamp("2025-01-06 12:00")
    w = local_windows("2025-07-07", "2025-07-07", "Asia/Manila", 8, 18)
    assert w.iloc[0].start == pd.Timestamp("2025-07-07 03:00") and w.iloc[0].end == pd.Timestamp("2025-07-07 13:00")


def test_in_windows_half_open():
    w = pd.DataFrame({"start": [pd.Timestamp("2025-01-06 02:00")], "end": [pd.Timestamp("2025-01-06 12:00")]})
    t = pd.Series(pd.to_datetime(["2025-01-06 01:59", "2025-01-06 02:00", "2025-01-06 11:59", "2025-01-06 12:00"]))
    assert list(in_windows(t, w)) == [False, True, True, False]


def test_time_filter_none_all_true():
    t = pd.Series(pd.date_range("2025-01-06", periods=48, freq="h"))
    assert time_filter_mask(t, "none").all()
    m = time_filter_mask(t, "owner")
    assert m.dtype == bool and 0 < m.sum() < len(t)


def test_time_filter_empty_and_nat():
    empty = pd.Series(pd.to_datetime([]))
    for mode in ("none", "killzone", "owner"):
        m = time_filter_mask(empty, mode)
        assert m.dtype == bool and len(m) == 0
    # 2025-01-06 (NY winter): kill zone 02-05 NY = 09-12 server
    t = pd.Series(pd.to_datetime(["2025-01-06 10:00", None, "2025-01-06 13:00"]))
    assert list(time_filter_mask(t, "killzone")) == [True, False, False]
    assert list(time_filter_mask(t, "none")) == [True, False, True]
    assert not time_filter_mask(pd.Series([pd.NaT, pd.NaT]), "owner").any()
