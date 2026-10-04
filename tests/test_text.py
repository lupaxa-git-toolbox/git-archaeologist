"""Date and size parsing."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from lupaxa.git_archaeologist.text import format_iec, parse_size, parse_user_time


def test_date_only_until_covers_the_whole_day() -> None:
    since = parse_user_time("2025-01-01", until=False)
    until = parse_user_time("2025-01-01", until=True)
    assert since == datetime(2025, 1, 1, tzinfo=timezone.utc)
    assert until == datetime(2025, 1, 2, tzinfo=timezone.utc)
    assert since < until


def test_rfc3339_requires_an_offset() -> None:
    parsed = parse_user_time("2025-01-01T00:30:00Z", until=False)
    assert parsed == datetime(2025, 1, 1, 0, 30, tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        parse_user_time("yesterday", until=False)
    with pytest.raises(ValueError):
        parse_user_time("2025-01-01T00:00:00", until=False)


def test_size_suffixes_and_rejection() -> None:
    assert parse_size("10") == 10
    assert parse_size("10MiB") == 10 * 1024 * 1024
    assert parse_size("1KB") == 1000
    assert format_iec(10 * 1024 * 1024) == "10.0 MiB"
    with pytest.raises(ValueError):
        parse_size("-1")
    with pytest.raises(ValueError):
        parse_size("1.5MiB")
    with pytest.raises(ValueError):
        parse_size("10XB")
