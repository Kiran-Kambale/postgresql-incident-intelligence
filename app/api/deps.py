"""Shared request helpers: context access, instance resolution, pagination, time-range parsing."""
from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any

from fastapi import Query, Request

from ..context import AppContext
from ..errors import AppError, NotFound, Unavailable
from ..timeutil import iso, parse_ts, utcnow


def get_ctx(request: Request) -> AppContext:
    return request.app.state.ctx


def resolve_instance(request: Request, instance: str | None = Query(None, max_length=32, pattern=r"^[a-z0-9][a-z0-9_-]*$")) -> str:
    ctx = get_ctx(request)
    ids = [i.id for i in ctx.settings.instances]
    if not ids:
        raise Unavailable("No database instance is configured.", code="NOT_CONFIGURED",
                          hint="Set DB_HOST, DB_PORT, DB_NAME, DB_USER and DB_PASSWORD (or INSTANCES_FILE) in .env, or start with DEMO_MODE=true.")
    if instance is None:
        return ids[0]
    if instance not in ids:
        raise NotFound(f"Unknown instance '{instance}'")
    return instance


def paging(page: int = Query(1, ge=1, le=100000), page_size: int = Query(50, ge=1, le=500)) -> tuple[int, int]:
    return page, page_size


def page_out(items: list[Any], total: int, page: int, page_size: int, **extra: Any) -> dict[str, Any]:
    return {"items": items, "total": total, "page": page, "page_size": page_size, "pages": max(1, math.ceil(total / page_size)), **extra}


def time_range(t_from: str | None, t_to: str | None, default_hours: float = 1.0, max_hours: float = 24 * 31) -> tuple[datetime, datetime]:
    end = parse_ts(t_to) if t_to else utcnow()
    if t_to and end is None:
        raise AppError("'to' must be an ISO-8601 timestamp", code="VALIDATION_ERROR")
    start = parse_ts(t_from) if t_from else end - timedelta(hours=default_hours)
    if t_from and start is None:
        raise AppError("'from' must be an ISO-8601 timestamp", code="VALIDATION_ERROR")
    if start >= end:
        raise AppError("'from' must be earlier than 'to'", code="VALIDATION_ERROR")
    if (end - start) > timedelta(hours=max_hours):
        raise AppError(f"The time range may not exceed {max_hours:g} hours", code="VALIDATION_ERROR")
    return start, end


def csv_list(value: str | None, allowed: set[str] | None = None, limit: int = 20) -> list[str] | None:
    if not value:
        return None
    items = [v.strip() for v in value.split(",") if v.strip()][:limit]
    if allowed is not None and any(i not in allowed for i in items):
        raise AppError(f"Unsupported value; allowed: {', '.join(sorted(allowed))}", code="VALIDATION_ERROR")
    return items


def live_meta(ctx: AppContext, iid: str, name: str, entry: dict[str, Any] | None) -> dict[str, Any]:
    """Describe how fresh and trustworthy the data behind a live view is."""
    rt = ctx.recorder.runtime(iid)
    now = utcnow()
    meta: dict[str, Any] = {"instance": iid, "collector": name, "demo_mode": ctx.demo, "recorder_running": ctx.recorder.running}
    if entry is None:
        meta.update({"status": "pending", "collected_at": None, "age_s": None, "message": "No data collected yet. The recorder collects shortly after start."})
        return meta
    ts = parse_ts(entry.get("ts"))
    meta.update({"status": entry["status"], "collected_at": entry.get("ts"), "age_s": round((now - ts).total_seconds(), 1) if ts else None, "stale": bool(entry.get("stale")),
                 "message": entry.get("message"), "error_code": entry.get("error_code"), "hint": entry.get("hint"), "failed_at": entry.get("failed_at")})
    meta["server"] = {k: rt.info.get(k) for k in ("version_num", "in_recovery")} if rt.info else None
    return meta
