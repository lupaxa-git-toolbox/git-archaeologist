"""Byte-safe display, dates, sizes, and durations."""

from __future__ import annotations

import base64
import calendar
import re
from datetime import datetime, timedelta, timezone

_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_RFC3339 = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(\.\d+)?(Z|[+-]\d{2}:\d{2})$"
)
_SIZE = re.compile(r"^(\d+)([A-Za-z]+)?$")
_SUFFIX = {
    "b": 1,
    "kb": 1_000,
    "mb": 1_000**2,
    "gb": 1_000**3,
    "tb": 1_000**4,
    "kib": 1024,
    "mib": 1024**2,
    "gib": 1024**3,
    "tib": 1024**4,
}
_MAX_I64 = 2**63 - 1
_LANG = {
    ".py": "Python",
    ".sh": "Shell",
    ".yml": "YAML",
    ".yaml": "YAML",
    ".md": "Markdown",
    ".toml": "TOML",
    ".json": "JSON",
    ".js": "JavaScript",
    ".mjs": "JavaScript",
    ".cjs": "JavaScript",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".html": "HTML",
    ".css": "CSS",
    ".c": "C",
    ".h": "C",
    ".cpp": "C++",
    ".cc": "C++",
    ".hpp": "C++",
    ".rs": "Rust",
    ".go": "Go",
    ".java": "Java",
    ".rb": "Ruby",
    ".sql": "SQL",
}


def display_path(data: bytes) -> str:
    """Return a reversible-looking display string. Control bytes are escaped."""
    out: list[str] = []
    index = 0
    while index < len(data):
        byte = data[index]
        if byte < 32 or byte == 127 or byte == 92:
            out.append(f"\\x{byte:02x}")
            index += 1
            continue
        decoded = False
        for length in (4, 3, 2, 1):
            chunk = data[index : index + length]
            try:
                text = chunk.decode("utf-8")
            except UnicodeDecodeError:
                continue
            out.append(text)
            index += len(chunk)
            decoded = True
            break
        if not decoded:
            out.append(f"\\x{byte:02x}")
            index += 1
    return "".join(out)


def path_record(data: bytes) -> dict[str, str]:
    """JSON path object. ``path_bytes_base64`` is the authoritative value."""
    return {
        "path": display_path(data),
        "path_bytes_base64": base64.b64encode(data).decode("ascii"),
    }


def escape_cell(data: bytes) -> str:
    """Escape a path for a Markdown table cell or console line."""
    text = display_path(data)
    return text.replace("|", "\\|").replace("`", "\\`").replace("<", "\\<").replace(">", "\\>")


def parse_size(text: str) -> int:
    """Parse a nonnegative size. Bare integers are bytes."""
    match = _SIZE.fullmatch(text.strip())
    if match is None:
        raise ValueError(f"invalid size: {text}")
    number = int(match.group(1))
    suffix = (match.group(2) or "b").lower()
    if suffix not in _SUFFIX:
        raise ValueError(f"invalid size suffix: {text}")
    value = number * _SUFFIX[suffix]
    if value > _MAX_I64:
        raise ValueError(f"size exceeds signed 64-bit range: {text}")
    return value


def format_iec(size: int) -> str:
    """Format a byte count with IEC units."""
    value = float(size)
    unit = "B"
    for name in ("KiB", "MiB", "GiB", "TiB", "PiB"):
        if value < 1024:
            break
        value /= 1024
        unit = name
    if unit == "B":
        return f"{size} B"
    return f"{value:.1f} {unit}"


def parse_user_time(text: str, *, until: bool) -> datetime:
    """Parse a date or RFC 3339 timestamp into an aware UTC instant.

    Date-only ``--since`` is midnight UTC. Date-only ``--until`` is midnight
    UTC on the next day. Timestamp ``--until`` stays exclusive as written.
    """
    date_match = _DATE.fullmatch(text)
    if date_match:
        year, month, day = (int(part) for part in date_match.groups())
        _validate_date(year, month, day)
        instant = datetime(year, month, day, tzinfo=timezone.utc)
        if until:
            instant += timedelta(days=1)
        return instant
    stamp = _RFC3339.fullmatch(text)
    if stamp is None:
        raise ValueError(f"invalid timestamp: {text}")
    year, month, day, hour, minute, second = (int(stamp.group(i)) for i in range(1, 7))
    _validate_date(year, month, day)
    if hour > 23 or minute > 59 or second > 60:
        raise ValueError(f"invalid timestamp: {text}")
    fraction = stamp.group(7)
    micro = 0
    if fraction:
        micro = int(float(fraction) * 1_000_000)
    offset = stamp.group(8)
    if offset == "Z":
        tz = timezone.utc
    else:
        sign = 1 if offset[0] == "+" else -1
        oh, om = int(offset[1:3]), int(offset[4:6])
        if oh > 23 or om > 59:
            raise ValueError(f"invalid timestamp: {text}")
        tz = timezone(sign * timedelta(hours=oh, minutes=om))
    return datetime(year, month, day, hour, minute, second, micro, tzinfo=tz).astimezone(
        timezone.utc
    )


def _validate_date(year: int, month: int, day: int) -> None:
    if month < 1 or month > 12 or day < 1 or day > calendar.monthrange(year, month)[1]:
        raise ValueError(f"invalid date: {year:04d}-{month:02d}-{day:02d}")


def to_rfc3339(instant: datetime) -> str:
    """Format an aware datetime as UTC RFC 3339."""
    utc = instant.astimezone(timezone.utc).replace(microsecond=0)
    return utc.strftime("%Y-%m-%dT%H:%M:%SZ")


def from_unix(timestamp: int) -> str:
    """Format a Unix timestamp as UTC RFC 3339."""
    return to_rfc3339(datetime.fromtimestamp(timestamp, timezone.utc))


def percentage(part: int, whole: int) -> float:
    """Round a percentage to two decimal places. A zero denominator is 0."""
    if whole == 0:
        return 0.0
    return round(part * 100 / whole, 2)


def elapsed_seconds(start: int | None, end: int | None) -> int | None:
    """Return a nonnegative elapsed span, or None when chronology is inconsistent."""
    if start is None or end is None:
        return None
    if end < start:
        return None
    return end - start


def format_duration(seconds: int | None) -> str:
    """Format an elapsed duration as days or ``Ny Nd`` using 365-day years."""
    if seconds is None:
        return "duration unavailable"
    days = seconds // 86400
    if days < 365:
        unit = "day" if days == 1 else "days"
        return f"{days} {unit}"
    years, rest = divmod(days, 365)
    return f"{years}y {rest}d"


def auto_group(span_seconds: int | None) -> str:
    """Choose day, month, or year from an elapsed activity span."""
    if span_seconds is None:
        return "month"
    days = span_seconds / 86400
    if days <= 90:
        return "day"
    if days <= 3 * 365:
        return "month"
    return "year"


def bucket_key(timestamp: int, group: str) -> str:
    """Return the UTC bucket key for a committer timestamp."""
    moment = datetime.fromtimestamp(timestamp, timezone.utc)
    if group == "day":
        return moment.strftime("%Y-%m-%d")
    if group == "month":
        return moment.strftime("%Y-%m")
    if group == "year":
        return moment.strftime("%Y")
    raise ValueError(f"unknown group: {group}")


def bucket_range(start: int, end: int, group: str) -> list[str]:
    """Inclusive UTC bucket keys from the first selected instant through the last."""
    if group == "day":
        cursor = datetime.fromtimestamp(start, timezone.utc).date()
        last = datetime.fromtimestamp(end, timezone.utc).date()
        keys: list[str] = []
        while cursor <= last:
            keys.append(cursor.isoformat())
            cursor += timedelta(days=1)
        return keys
    if group == "month":
        moment = datetime.fromtimestamp(start, timezone.utc)
        last = datetime.fromtimestamp(end, timezone.utc)
        year, month = moment.year, moment.month
        keys = []
        while (year, month) <= (last.year, last.month):
            keys.append(f"{year:04d}-{month:02d}")
            month += 1
            if month == 13:
                month = 1
                year += 1
        return keys
    moment = datetime.fromtimestamp(start, timezone.utc)
    last = datetime.fromtimestamp(end, timezone.utc)
    return [f"{year:04d}" for year in range(moment.year, last.year + 1)]


def exclusive_bucket_end(key: str, group: str) -> int:
    """Unix instant at the exclusive end of a bucket key."""
    if group == "day":
        year, month, day = (int(part) for part in key.split("-"))
        moment = datetime(year, month, day, tzinfo=timezone.utc) + timedelta(days=1)
    elif group == "month":
        year, month = (int(part) for part in key.split("-"))
        if month == 12:
            moment = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
        else:
            moment = datetime(year, month + 1, 1, tzinfo=timezone.utc)
    else:
        year = int(key)
        moment = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    return int(moment.timestamp())


def extension_of(path: bytes) -> str:
    """Return the classification suffix, or ``no_extension``."""
    name = path.rsplit(b"/", 1)[-1]
    if name.startswith(b".") and b"." not in name[1:]:
        return "no_extension"
    if b"." not in name:
        return "no_extension"
    suffix = name.rsplit(b".", 1)[-1]
    try:
        text = suffix.decode("utf-8").lower()
    except UnicodeDecodeError:
        return "no_extension"
    if not text or text == name.decode("utf-8", "replace").lower():
        return "no_extension"
    return f".{text}"


def language_of(path: bytes) -> str:
    """Classify a regular file by its final suffix. This is a filename heuristic."""
    suffix = extension_of(path)
    if suffix == "no_extension":
        return "No extension"
    return _LANG.get(suffix, "Unknown")


def mode_kind(mode: str) -> str:
    """Map a Git mode to file, symlink, or gitlink."""
    if mode == "120000":
        return "symlink"
    if mode == "160000":
        return "gitlink"
    return "file"


def abbreviate(oid: str, peers: set[str]) -> str:
    """Abbreviate an object ID to at least 12 characters, extended for uniqueness."""
    width = 12
    while width < len(oid):
        prefix = oid[:width]
        clashes = [other for other in peers if other.startswith(prefix) and other != oid]
        if not clashes:
            return prefix
        width += 1
    return oid
