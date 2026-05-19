"""Offline tests for ``timeclock_cli.client`` helpers and models."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from timeclock_cli.client import (
    LOCATION_TYPES,
    Entry,
    TimeclockError,
    _parse_dt,
    _window_for_days,
    normalize_location,
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


# ---------------------------------------------------------------- _parse_dt


def test_parse_dt_server_format():
    assert _parse_dt("2026-05-19 05:38:39") == datetime(2026, 5, 19, 5, 38, 39)


def test_parse_dt_none_or_empty():
    assert _parse_dt(None) is None
    assert _parse_dt("") is None


def test_parse_dt_iso_fallback():
    assert _parse_dt("2026-05-19T05:38:39") == datetime(2026, 5, 19, 5, 38, 39)


def test_parse_dt_garbage_returns_none():
    assert _parse_dt("not a date at all") is None


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


def test_entry_open_session_uses_ellipsis():
    e = Entry(
        started_at=datetime.now() - timedelta(hours=2),
        ended_at=None,
        location="HOME",
    )
    line = e.format_line()
    assert "…" in line
    assert e.is_open is True


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
