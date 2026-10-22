"""Tests for aristotlelib.date_utils module."""

from datetime import datetime, timedelta

import pydantic

from aristotlelib.date_utils import UTCDatetime, format_elapsed_time, format_relative_time


def test_format_relative_time_seconds_ago():
    """Test formatting a timestamp from seconds ago."""
    now = datetime.now()
    recent = now - timedelta(seconds=30)
    result = format_relative_time(recent)
    assert result == "30 secs ago"


def test_format_relative_time_one_second_ago():
    """Test formatting a timestamp from one second ago."""
    now = datetime.now()
    one_sec_ago = now - timedelta(seconds=1)
    result = format_relative_time(one_sec_ago)
    assert result == "1 sec ago"


def test_format_relative_time_minutes_ago():
    """Test formatting a timestamp from minutes ago."""
    now = datetime.now()
    five_mins_ago = now - timedelta(minutes=5)
    result = format_relative_time(five_mins_ago)
    assert result == "5 mins ago"


def test_format_relative_time_one_minute_ago():
    """Test formatting a timestamp from exactly one minute ago."""
    now = datetime.now()
    one_min_ago = now - timedelta(minutes=1)
    result = format_relative_time(one_min_ago)
    assert result == "1 min ago"


def test_format_relative_time_hours_ago():
    """Test formatting a timestamp from hours ago."""
    now = datetime.now()
    three_hours_ago = now - timedelta(hours=3)
    result = format_relative_time(three_hours_ago)
    assert result == "3 hours ago"


def test_format_relative_time_one_hour_ago():
    """Test formatting a timestamp from exactly one hour ago."""
    now = datetime.now()
    one_hour_ago = now - timedelta(hours=1)
    result = format_relative_time(one_hour_ago)
    assert result == "1 hour ago"


def test_format_relative_time_days_ago():
    """Test formatting a timestamp from days ago."""
    now = datetime.now()
    two_days_ago = now - timedelta(days=2)
    result = format_relative_time(two_days_ago)
    assert result == "2 days ago"


def test_format_relative_time_one_day_ago():
    """Test formatting a timestamp from exactly one day ago."""
    now = datetime.now()
    one_day_ago = now - timedelta(days=1)
    result = format_relative_time(one_day_ago)
    assert result == "1 day ago"


def test_format_relative_time_weeks_ago():
    """Test formatting a timestamp from weeks ago."""
    now = datetime.now()
    two_weeks_ago = now - timedelta(weeks=2)
    result = format_relative_time(two_weeks_ago)
    assert result == "2 weeks ago"


def test_format_relative_time_one_week_ago():
    """Test formatting a timestamp from exactly one week ago."""
    now = datetime.now()
    one_week_ago = now - timedelta(weeks=1)
    result = format_relative_time(one_week_ago)
    assert result == "1 week ago"


def test_format_elapsed_time_seconds():
    """Test formatting elapsed time in seconds."""
    result = format_elapsed_time(45)
    assert result == "started 45s ago"


def test_format_elapsed_time_minutes():
    """Test formatting elapsed time in minutes and seconds."""
    result = format_elapsed_time(125)  # 2 minutes 5 seconds
    assert result == "started 2m 5s ago"


def test_format_elapsed_time_hours():
    """Test formatting elapsed time in hours and minutes."""
    result = format_elapsed_time(3665)  # 1 hour 1 minute 5 seconds
    assert result == "started 1h 1m ago"


def test_format_elapsed_time_zero():
    """Test formatting zero elapsed time."""
    result = format_elapsed_time(0)
    assert result == "started 0s ago"


def test_format_elapsed_time_exact_minute():
    """Test formatting exactly one minute."""
    result = format_elapsed_time(60)
    assert result == "started 1m 0s ago"


def test_format_elapsed_time_exact_hour():
    """Test formatting exactly one hour."""
    result = format_elapsed_time(3600)
    assert result == "started 1h 0m ago"


def test_a_utc_datetime_keeps_its_date_time_format_in_both_schemas():
    """Code generated from the schema the CLI reference names types a timestamp as a time, not a string."""
    adapter = pydantic.TypeAdapter(UTCDatetime)

    for mode in ("validation", "serialization"):
        assert adapter.json_schema(mode=mode) == {"type": "string", "format": "date-time"}
    assert adapter.dump_python(datetime(2026, 10, 11, 9, 50), mode="json") == "2026-10-11T09:50:00+00:00"
