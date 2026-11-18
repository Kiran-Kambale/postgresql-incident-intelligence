"""UTC timestamp helpers. Everything persisted is UTC ISO-8601 with millisecond precision and a trailing Z."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

UTC = timezone.utc
_TS = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?\s*(Z|[+-]\d{2}(?::?\d{2})?)?$"
)


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime | None = None) -> str:
    """Format a datetime as UTC ISO-8601 (naive datetimes are assumed to be UTC)."""
    dt = dt or utcnow()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def parse_ts(value: Any) -> datetime | None:
    """Parse ISO-like timestamps (Z, +00, +00:00, space separator, naive=UTC). Returns None if unparseable."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    m = _TS.match(str(value).strip())
    if not m:
        return None
    y, mo, d, h, mi, s, frac, tz = m.groups()
    micro = int((frac or "0").ljust(6, "0")[:6])
    try:
        dt = datetime(int(y), int(mo), int(d), int(h), int(mi), int(s), micro)
    except ValueError:
        return None
    if tz in (None, "Z"):
        off = 0
    else:
        sign = 1 if tz[0] == "+" else -1
        digits = tz[1:].replace(":", "")
        off = sign * (int(digits[:2]) * 60 + (int(digits[2:4]) if len(digits) >= 4 else 0))
    return (dt - timedelta(minutes=off)).replace(tzinfo=UTC)


def normalize_ts(value: Any) -> str | None:
    dt = parse_ts(value)
    return iso(dt) if dt else None


def ago(seconds: float, now: datetime | None = None) -> str:
    return iso((now or utcnow()) - timedelta(seconds=seconds))
