"""Offline tests for ``timeclock_cli.client`` helpers and models."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from timeclock_cli.client import (
    LOCATION_TYPES,
    Entry,
    LunchBreak,
    TimeclockError,
    _parse_in_zone,
    _parse_naive,
    _parse_utc,
    _window_for_days,
    looks_like_clock_time,
    normalize_location,
    parse_clock_time,
)


# ---------------------------------------------------------------- normalize_location


@pytest.mark.parametrize(
    "given,expected",
    [
        ("office", "OFFICE"),
        ("OFFICE", "OFFICE"),
        ("Office", "OFFICE"),
        ("home", "HOME"),
        ("HOME", "HOME"),
        ("abroad", "ABROAD"),
        ("field", "FIELD"),
        ("  office  ", "OFFICE"),  # whitespace tolerated
        ("home-office", "HOME"),  # alias
        ("home office", "HOME"),  # space form of alias
        ("remote", "HOME"),       # alias
        ("work", "OFFICE"),       # alias
        ("travel", "ABROAD"),     # alias
        ("onsite", "FIELD"),      # alias
    ],
)
def test_normalize_location_accepts_user_friendly_inputs(given, expected):
    assert normalize_location(given) == expected


def test_normalize_location_none_returns_none():
    assert normalize_location(None) is None


def test_normalize_location_rejects_unknown():
    with pytest.raises(TimeclockError) as exc:
        normalize_location("moon")
    msg = str(exc.value)
    assert "moon" in msg
    for loc in LOCATION_TYPES:
        assert loc in msg, "error message should list the allowed values"


# ---------------------------------------------------------------- _window_for_days


def test_window_for_days_inclusive_of_today():
    end, start = _window_for_days(5)
    # 5 days inclusive: today + 4 prior days
    assert (end.date() - start.date()).days == 4
    assert end.hour == 23 and end.minute == 59 and end.second == 59
    assert start.hour == 0 and start.minute == 0 and start.second == 0


def test_window_for_days_clamps_minimum_to_one():
    end, start = _window_for_days(0)
    assert start.date() == end.date()


def test_window_for_days_large_value():
    end, start = _window_for_days(30)
    assert (end.date() - start.date()).days == 29


def test_window_for_days_is_timezone_aware():
    end, start = _window_for_days(3)
    assert end.tzinfo is not None
    assert start.tzinfo is not None


# ---------------------------------------------------------------- parsers


def test_parse_naive_server_format():
    assert _parse_naive("2026-05-19 05:38:39") == datetime(2026, 5, 19, 5, 38, 39)


def test_parse_naive_none_or_empty():
    assert _parse_naive(None) is None
    assert _parse_naive("") is None


def test_parse_naive_iso_fallback():
    assert _parse_naive("2026-05-19T05:38:39") == datetime(2026, 5, 19, 5, 38, 39)


def test_parse_naive_garbage_returns_none():
    assert _parse_naive("not a date at all") is None


def test_parse_utc_attaches_utc_tz():
    dt = _parse_utc("2026-05-19 05:38:39")
    assert dt is not None
    assert dt.tzinfo == timezone.utc
    assert dt.hour == 5  # still 05 UTC


def test_parse_in_zone_attaches_named_zone():
    dt = _parse_in_zone("2026-05-19 07:38:39", "Europe/Berlin")
    assert dt is not None
    assert dt.tzinfo is not None
    # Same wall-clock numbers, just labelled with the right tz.
    assert dt.hour == 7 and dt.minute == 38


def test_parse_in_zone_unknown_tz_falls_back_to_local():
    dt = _parse_in_zone("2026-05-19 07:38:39", "Not/A_Real_Zone")
    assert dt is not None
    assert dt.tzinfo is not None  # local tz, not None


# ---------------------------------------------------------------- parse_clock_time


def test_parse_clock_time_basic():
    # Use 00:00 — always either now or in the past today (so allow_future=False is fine
    # for any current minute).
    dt = parse_clock_time("00:00")
    assert dt.tzinfo is not None
    assert dt.hour == 0 and dt.minute == 0


@pytest.mark.parametrize("s", ["7:30", "07:30", "23:59"])
def test_looks_like_clock_time_yes(s):
    assert looks_like_clock_time(s) is True


@pytest.mark.parametrize("s", ["office", "home", "abc", "7:3", "7:300", ""])
def test_looks_like_clock_time_no(s):
    assert looks_like_clock_time(s) is False


def test_parse_clock_time_rejects_invalid_format():
    with pytest.raises(TimeclockError):
        parse_clock_time("not-a-time")


def test_parse_clock_time_rejects_out_of_range():
    with pytest.raises(TimeclockError):
        parse_clock_time("25:00")
    with pytest.raises(TimeclockError):
        parse_clock_time("12:99")


def test_parse_clock_time_refuses_future_by_default():
    # 23:59 is in the future unless we run this test at exactly that minute.
    # Skip the corner case where it's currently 23:59.
    now = datetime.now()
    if now.hour == 23 and now.minute == 59:
        pytest.skip("cannot reliably test 'future' near midnight")
    with pytest.raises(TimeclockError):
        parse_clock_time("23:59")


def test_parse_clock_time_allows_future_with_flag():
    now = datetime.now()
    if now.hour == 23 and now.minute == 59:
        pytest.skip("cannot reliably test 'future' near midnight")
    dt = parse_clock_time("23:59", allow_future=True)
    assert dt.hour == 23 and dt.minute == 59


# ---------------------------------------------------------------- Entry


def test_entry_closed_session_format():
    e = Entry(
        started_at=datetime(2026, 5, 18, 7, 42),
        ended_at=datetime(2026, 5, 18, 17, 45),
        location="OFFICE",
    )
    line = e.format_line()
    assert "07:42" in line and "17:45" in line
    assert "office" in line  # location is lowercased for display
    assert e.is_open is False
    assert e.is_absence is False
    assert e.duration == timedelta(hours=10, minutes=3)


def test_entry_open_session_uses_ellipsis_naive():
    e = Entry(
        started_at=datetime.now() - timedelta(hours=2),
        ended_at=None,
        location="HOME",
    )
    line = e.format_line()
    assert "…" in line
    assert e.is_open is True
    # duration ≈ 2h
    assert 1.9 < e.duration.total_seconds() / 3600 < 2.1


def test_entry_open_session_uses_ellipsis_tz_aware():
    """Real-world entries are tz-aware; duration should still work."""
    started = datetime.now(tz=timezone.utc) - timedelta(hours=2)
    e = Entry(started_at=started, ended_at=None, location="OFFICE")
    line = e.format_line()
    assert "…" in line
    assert e.is_open is True
    assert 1.9 < e.duration.total_seconds() / 3600 < 2.1


def test_entry_absence_format_marks_with_arrow():
    e = Entry(
        started_at=datetime(2026, 5, 15, 0, 0),
        ended_at=datetime(2026, 5, 15, 8, 0),
        location=None,
        absence_name="Vacation",
    )
    line = e.format_line()
    assert "Vacation" in line
    assert "↳" in line
    assert e.is_absence is True
    assert e.is_open is False


def test_entry_lunch_format_marks_as_lunch():
    e = Entry(
        started_at=datetime(2026, 5, 18, 12, 0),
        ended_at=datetime(2026, 5, 18, 12, 30),
        location=None,
        is_lunch=True,
    )
    line = e.format_line()
    assert "lunch break" in line
    assert "↳" in line
    assert e.is_open is False


# ---------------------------------------------------------------- LunchBreak


def test_lunch_break_duration_open_lunch():
    lunch = LunchBreak(
        started_at=datetime.now(tz=timezone.utc) - timedelta(minutes=15),
        ended_at=None,
    )
    assert lunch.is_open is True
    assert 14 < lunch.duration.total_seconds() / 60 < 16


def test_lunch_break_duration_closed_lunch():
    lunch = LunchBreak(
        started_at=datetime(2026, 5, 18, 12, 0, tzinfo=timezone.utc),
        ended_at=datetime(2026, 5, 18, 12, 30, tzinfo=timezone.utc),
    )
    assert lunch.is_open is False
    assert lunch.duration == timedelta(minutes=30)


# ---------------------------------------------------------------- CLI in-args splitter


def test_split_in_args_location_only():
    from timeclock_cli.cli import _split_in_args
    assert _split_in_args(["office"]) == ("office", None)


def test_split_in_args_time_only():
    from timeclock_cli.cli import _split_in_args
    assert _split_in_args(["7:30"]) == (None, "7:30")


def test_split_in_args_both_either_order():
    from timeclock_cli.cli import _split_in_args
    assert _split_in_args(["office", "7:30"]) == ("office", "7:30")
    assert _split_in_args(["7:30", "office"]) == ("office", "7:30")


def test_split_in_args_empty():
    from timeclock_cli.cli import _split_in_args
    assert _split_in_args([]) == (None, None)


def test_split_in_args_too_many():
    from timeclock_cli.cli import _split_in_args
    with pytest.raises(TimeclockError):
        _split_in_args(["office", "home", "7:30"])
    with pytest.raises(TimeclockError):
        _split_in_args(["7:30", "8:30"])
