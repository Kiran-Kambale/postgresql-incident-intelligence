"""v0.6-compatible endpoints, now served from the flight recorder. They keep their paths and main fields; the dashboard no longer uses them."""
from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from ..errors import NotFound
from ..pg.logs import parse_log_text, recent_errors, redact_detail
from ..security import audit, redact_log_message, redact_sql, require
from ..timeutil import iso, utcnow
from .deps import get_ctx, page_out, paging, resolve_instance

router = APIRouter(tags=["legacy (v0.6 compatible)"])
LEGACY_KEY = {"blocking": "blocking", "long_query": "long_queries", "idle_in_transaction": "idle_transactions", "connection_saturation": "connections_high"}


def _act(request: Request, iid: str) -> tuple[Any, dict[str, Any]]:
    ctx = get_ctx(request)
    e = ctx.recorder.ensure_fresh(iid, "fast", "activity")
    return ctx, (e or {}).get("data") or {}


def _findings(ctx, iid: str) -> list[dict[str, Any]]:
    items, _n = ctx.store.evidence_list(instance_id=iid, status="active", severities=["critical", "warning"], limit=50, is_demo=ctx.demo)
    return [{"key": LEGACY_KEY.get(e["category"], e["category"]), "severity": e["severity"], "title": e["title"], "evidence_id": e["id"], "first_seen": e["first_seen"]} for e in items]


@router.get("/api/monitor/status")
def monitor_status(request: Request, p=require("viewer")) -> dict[str, Any]:
    ctx = get_ctx(request)
    st = ctx.recorder.status()
    return {"enabled": ctx.settings.auto_monitor, "running": st["running"], "interval_seconds": ctx.settings.t("fast_interval_s"), "demo_mode": ctx.demo, "recorder": st,
            "deprecated": "Use /api/system/status."}


@router.get("/api/monitor/latest")
def monitor_latest(request: Request, iid: str = Depends(resolve_instance), p=require("viewer")) -> dict[str, Any]:
    ctx, d = _act(request, iid)
    if not d:
        raise NotFound("No monitoring sample has been collected yet")
    rows, conn, graph = d["rows"], d["connections"], d["blocking"]
    ts = ctx.recorder.latest(iid, "activity")["ts"]
    return {"status": "ok", "captured_at": ts, "payload": {"demo_mode": ctx.demo, "checked_at": ts, "connections": conn, "locks": {"count": graph["blocked_sessions"], "roots": graph["roots"]},
                                                            "long_queries": [r for r in rows if r["state"] == "active" and (r["query_age_s"] or 0) >= ctx.settings.t("long_query_s")][:20],
                                                            "idle_transactions": [r for r in rows if (r["state"] or "").startswith("idle in transaction")][:20], "findings": _findings(ctx, iid)}}


@router.get("/api/monitor/history")
def monitor_history(request: Request, hours: float = Query(1.0, gt=0, le=168), iid: str = Depends(resolve_instance), p=require("viewer")) -> dict[str, Any]:
    ctx = get_ctx(request)
    now = utcnow()
    raw = ctx.store.metrics_series(iid, ["sessions_total", "blocked_sessions", "conn_utilization_pct"], iso(now - timedelta(hours=hours)), iso(now), max(15, int(hours * 3600 / 240)), ctx.demo)
    return {"hours": hours, "series": {k: [{"ts": x["ts"], "value": x["avg"]} for x in v] for k, v in raw.items()}, "deprecated": "Use /api/metrics/series."}


@router.get("/api/events")
def events(request: Request, limit: int = Query(100, ge=1, le=500), iid: str = Depends(resolve_instance), p=require("viewer")) -> dict[str, Any]:
    ctx = get_ctx(request)
    items, total = ctx.store.evidence_list(instance_id=iid, limit=limit, is_demo=ctx.demo)
    return {"items": [{"event_key": LEGACY_KEY.get(e["category"], e["category"]), "started_at": e["first_seen"], "ended_at": e.get("cleared_at"), "severity": e["severity"], "title": e["title"],
                       "active": e["status"] == "active", "evidence_id": e["id"]} for e in items], "total": total, "deprecated": "Use /api/evidence."}


@router.get("/api/diagnostics/activity")
def d_activity(request: Request, limit: int = Query(100, ge=1, le=500), iid: str = Depends(resolve_instance), p=require("viewer")) -> dict[str, Any]:
    ctx, d = _act(request, iid)
    rows = [r for r in d.get("rows", []) if r["backend_type"] == "client backend"][:limit]
    return {"count": len(rows), "items": rows, "demo_mode": ctx.demo}


@router.get("/api/diagnostics/locks")
def d_locks(request: Request, iid: str = Depends(resolve_instance), p=require("viewer")) -> dict[str, Any]:
    ctx, d = _act(request, iid)
    g = d.get("blocking") or {"edges": [], "roots": [], "nodes": []}
    return {"count": len(g["edges"]), "blocked_sessions": g.get("blocked_sessions", 0), "root_blockers": g["roots"], "items": g["edges"], "nodes": g["nodes"], "demo_mode": ctx.demo, "note": g.get("note")}


@router.get("/api/diagnostics/connections")
def d_connections(request: Request, iid: str = Depends(resolve_instance), p=require("viewer")) -> dict[str, Any]:
    ctx, d = _act(request, iid)
    return {**(d.get("connections") or {}), "demo_mode": ctx.demo}


@router.get("/api/diagnostics/long-queries")
def d_long(request: Request, iid: str = Depends(resolve_instance), min_seconds: float | None = Query(None, ge=0), p=require("viewer")) -> dict[str, Any]:
    ctx, d = _act(request, iid)
    th = min_seconds if min_seconds is not None else ctx.settings.t("long_query_s")
    rows = [r for r in d.get("rows", []) if r["state"] == "active" and (r["query_age_s"] or 0) >= th and not r["blocked_by"]]
    return {"threshold_seconds": th, "count": len(rows), "items": rows, "demo_mode": ctx.demo}


@router.get("/api/diagnostics/idle-transactions")
def d_idle(request: Request, iid: str = Depends(resolve_instance), min_seconds: float | None = Query(None, ge=0), p=require("viewer")) -> dict[str, Any]:
    ctx, d = _act(request, iid)
    th = min_seconds if min_seconds is not None else ctx.settings.t("idle_txn_s")
    rows = [r for r in d.get("rows", []) if (r["state"] or "").startswith("idle in transaction") and (r["state_age_s"] or 0) >= th]
    return {"threshold_seconds": th, "count": len(rows), "items": rows, "demo_mode": ctx.demo}


@router.get("/api/diagnostics/summary")
def d_summary(request: Request, iid: str = Depends(resolve_instance), p=require("viewer")) -> dict[str, Any]:
    ctx, d = _act(request, iid)
    rows, conn, g = d.get("rows", []), d.get("connections") or {}, d.get("blocking") or {}
    return {"demo_mode": ctx.demo, "locks": g.get("blocked_sessions", 0), "root_blockers": g.get("root_blockers", 0), "connections": conn,
            "long_queries": sum(1 for r in rows if r["state"] == "active" and (r["query_age_s"] or 0) >= ctx.settings.t("long_query_s") and not r["blocked_by"]),
            "idle_transactions": sum(1 for r in rows if (r["state"] or "").startswith("idle in transaction") and (r["state_age_s"] or 0) >= ctx.settings.t("idle_txn_s")),
            "findings": _findings(ctx, iid), "deprecated": "Use /api/live/overview."}


@router.get("/api/diagnostics/failed-queries")
def failed_queries(request: Request, limit: int = Query(50, ge=1, le=200), iid: str = Depends(resolve_instance), p=require("operator")) -> dict[str, Any]:
    ctx = get_ctx(request)
    cfg = ctx.recorder.runtime(iid).cfg
    if ctx.demo or not cfg.log_file:
        return {"available": False, "items": [], "count": 0, "reason": "No server log file is configured for this instance (set PG_LOG_FILE). Demo mode has no log."}
    audit(request, "log.read", iid)
    try:
        items = recent_errors(cfg.log_file, ctx.settings.log_tail_bytes, limit, cfg.log_utc_offset_minutes, ctx.settings.query_text_mode, ctx.settings.redact_patterns)
    except Exception as exc:
        code = getattr(exc, "code", "LOG_NOT_READABLE")
        return {"available": False, "items": [], "count": 0, "reason": getattr(exc, "message", "The log could not be read."), "error_code": code}
    return {"available": True, "items": items, "count": len(items)}


class IngestIn(BaseModel):
    source_name: str = Field("pasted log", max_length=100)
    log_text: str = Field(..., min_length=1, max_length=2_000_000)


@router.post("/api/diagnostics/failed-queries/ingest")
def ingest_log(body: IngestIn, request: Request, p=require("operator")) -> dict[str, Any]:
    ctx = get_ctx(request)
    entries = parse_log_text(body.log_text)
    items = []
    for e in entries:
        if e["level"] in ("ERROR", "FATAL", "PANIC"):
            items.append({"severity": e["level"], "time": iso(e["ts"]), "pid": e["pid"], "user": e.get("user"), "database": e.get("db"), "application": e.get("app"),
                          "message": redact_log_message(e["msg"].splitlines()[0])[:400], "statement": redact_sql(e.get("extra", {}).get("STATEMENT"), ctx.settings.query_text_mode, ctx.settings.redact_patterns),
                          "detail": redact_detail(e.get("extra", {}).get("DETAIL"), ctx.settings.query_text_mode, ctx.settings.redact_patterns)})
    audit(request, "log.ingest", body.source_name, details={"entries": len(entries), "errors": len(items)})
    return {"source": body.source_name, "parsed_entries": len(entries), "count": len(items), "items": items[:500],
            "note": "Parsed in memory and returned; nothing is stored. Query text follows QUERY_TEXT_MODE."}


# --------------------------------------------------------------------------- snapshots (history)
class CaptureIn(BaseModel):
    title: str = Field("Manual capture", max_length=200)
    notes: str = Field("", max_length=4000)


@router.post("/api/history/capture")
def history_capture(body: CaptureIn, request: Request, iid: str = Depends(resolve_instance), p=require("operator")) -> dict[str, Any]:
    ctx = get_ctx(request)
    ctx.recorder.run_tier_sync(iid, "fast", "capture", wait=True)
    rt = ctx.recorder.runtime(iid)
    snap = ctx.store.snapshot_add(iid, rt.cfg.dbname, body.title, body.notes, ctx.recorder.snapshot_payload(rt), None, ctx.demo)
    audit(request, "snapshot.capture", str(snap["id"]))
    return {**snap, "demo_mode": ctx.demo, "title": body.title}


@router.get("/api/history")
def history_list(request: Request, pg=Depends(paging), p=require("viewer")) -> dict[str, Any]:
    ctx = get_ctx(request)
    page, size = pg
    items, total = ctx.store.snapshot_list(size, (page - 1) * size, ctx.demo)
    return page_out(items, total, page, size)


@router.get("/api/history/{snapshot_id}")
def history_get(snapshot_id: int, request: Request, p=require("operator")) -> dict[str, Any]:
    snap = get_ctx(request).store.snapshot_get(snapshot_id)
    if not snap:
        raise NotFound("Snapshot not found")
    audit(request, "snapshot.read", str(snapshot_id))
    return snap


# --------------------------------------------------------------------------- query performance capture/history
@router.post("/api/query-performance/capture")
def qp_capture(request: Request, iid: str = Depends(resolve_instance), p=require("operator")) -> dict[str, Any]:
    ctx = get_ctx(request)
    res = ctx.recorder.run_tier_sync(iid, "standard", "manual", wait=True)
    audit(request, "queries.capture", iid)
    return {"captured": True, "run": res, "demo_mode": ctx.demo}


@router.get("/api/query-performance/history")
def qp_history(request: Request, hours: float = Query(24, gt=0, le=720), iid: str = Depends(resolve_instance), p=require("viewer")) -> dict[str, Any]:
    ctx = get_ctx(request)
    now = utcnow()
    samples = ctx.store.qstats_samples(iid, iso(now - timedelta(hours=hours)), iso(now), ctx.demo)
    by: dict[str, list[dict]] = defaultdict(list)
    for s in samples:
        by[s["queryid"]].append(s)
    items = []
    for qid, rows in by.items():
        means = []
        for a, b in zip(rows, rows[1:]):
            dc, dt = b["calls"] - a["calls"], b["total_exec_ms"] - a["total_exec_ms"]
            if dc > 0 and dt >= 0:
                means.append(dt / dc)
        items.append({"queryid": qid, "fingerprint": rows[-1]["fingerprint"], "query": rows[-1].get("text"), "samples": len(rows), "interval_means_ms": means[-12:],
                      "interval_mean_ratio": (round(means[-1] / means[0], 2) if len(means) >= 2 and means[0] > 0 else None), "last_calls": rows[-1]["calls"]})
    items.sort(key=lambda r: -(r["interval_mean_ratio"] or 0))
    return {"count": len(items), "items": items[:200], "demo_mode": ctx.demo,
            "limitations": ["interval_mean_ratio compares the latest interval's mean with the first interval's mean in the period; it is a hint, not a diagnosis.",
                            "Means derive from cumulative counters, not individual executions. Use /api/queries/top for windowed analysis."]}
