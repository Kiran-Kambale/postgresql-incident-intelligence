from __future__ import annotations

import ipaddress
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from .timeutil import iso


def jsonable(v: Any) -> Any:
    """Recursively convert database values to JSON-safe values (UTC ISO timestamps, float seconds, plain numbers)."""
    if isinstance(v, datetime):
        return iso(v)
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, timedelta):
        return v.total_seconds()
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (ipaddress.IPv4Address, ipaddress.IPv6Address)):
        return str(v)
    if isinstance(v, (bytes, memoryview)):
        return "<binary>"
    if isinstance(v, dict):
        return {k: jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple, set)):
        return [jsonable(x) for x in v]
    return v


def fmt_dur(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    s = int(round(seconds))
    if s < 60:
        return f"{s}s"
    m, sec = divmod(s, 60)
    if m < 60:
        return f"{m}m {sec}s"
    h, m = divmod(m, 60)
    if h < 48:
        return f"{h}h {m}m"
    d, h = divmod(h, 24)
    return f"{d}d {h}h"


def fmt_bytes(n: float | None) -> str:
    if n is None:
        return "unknown"
    n = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024 or unit == "TiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TiB"


def pct(a: float, b: float) -> float | None:
    return round(100.0 * a / b, 2) if b else None
