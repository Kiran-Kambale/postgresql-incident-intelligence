"""Live diagnostic views (served from the flight recorder), metrics and query performance."""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from ..errors import AppError
from ..security import audit, require
from ..services import collection_gaps, query_top, query_trend, series
from ..timeutil import iso, utcnow
from .deps import csv_list, get_ctx, live_meta, page_out, paging, resolve_instance, time_range

router = APIRouter()

METRICS = {
    "sessions_total": ("Client sessions", "count", "Sessions"), "sessions_active": ("Active sessions", "count", "Sessions"), "sessions_idle": ("Idle sessions", "count", "Sessions"),
    "sessions_idle_in_txn": ("Idle in transaction", "count", "Sessions"), "sessions_waiting_lock": ("Waiting on locks", "count", "Sessions"), "blocked_sessions": ("Blocked sessions", "count", "Locks"),
    "root_blockers": ("Root blockers", "count", "Locks"), "conn_utilization_pct": ("Connection utilization", "%", "Sessions"), "connection_slots_used": ("Connection slots used", "count", "Sessions"),
    "longest_query_s": ("Longest active query", "s", "Sessions"), "longest_xact_s": ("Longest open transaction", "s", "Sessions"), "longest_idle_txn_s": ("Longest idle in transaction", "s", "Sessions"),
    "autovacuum_workers": ("Autovacuum workers", "count", "Vacuum"),
    "db_xact_commit": ("Commits", "/s", "Database"), "db_xact_rollback": ("Rollbacks", "/s", "Database"), "db_deadlocks": ("Deadlocks", "/s", "Database"), "db_temp_bytes": ("Temp bytes written", "B/s", "Database"),
    "db_tup_returned": ("Rows returned", "/s", "Database"), "db_tup_inserted": ("Rows inserted", "/s", "Database"), "db_tup_updated": ("Rows updated", "/s", "Database"), "db_tup_deleted": ("Rows deleted", "/s", "Database"),
    "db_size_bytes": ("Database size", "B", "Database"), "bg_checkpoints_timed": ("Timed checkpoints", "/s", "Checkpoints"), "bg_checkpoints_req": ("Requested checkpoints", "/s", "Checkpoints"),
    "bg_buffers_checkpoint": ("Buffers written by checkpoints", "/s", "Checkpoints"), "wal_bytes": ("WAL generated (pg_stat_wal)", "B/s", "WAL"), "wal_lsn_bytes": ("WAL position advance", "B/s", "WAL"),
    "wal_dir_bytes": ("WAL directory size", "B", "WAL"), "repl_standbys": ("Connected standbys", "count", "Replication"), "repl_max_replay_lag_s": ("Max replay lag", "s", "Replication"),
    "repl_max_lag_bytes": ("Max standby lag", "B", "Replication"), "slot_max_retained_bytes": ("Max WAL retained by a slot", "B", "Replication"), "slots_inactive": ("Inactive slots", "count", "Replication"),
    "xid_age_max": ("Max database XID age", "xids", "Vacuum"), "mxid_age_max": ("Max multixact age", "mxids", "Vacuum"), "oldest_xmin_age": ("Oldest xmin holder age", "xids", "Vacuum"),
    "dead_tuples_max": ("Most dead tuples in one table", "tuples", "Vacuum"), "tables_vacuum_pressure": ("Tables with vacuum pressure", "count", "Vacuum"), "prepared_xacts": ("Prepared transactions", "count", "Vacuum"),
    "standby_replay_delay_s": ("Standby replay delay", "s", "Replication"),
}


def entry(ctx, iid: str, tier: str, name: str) -> dict[str, Any] | None:
    return ctx.recorder.ensure_fresh(iid, tier, name)


def _data(e: dict[str, Any] | None) -> dict[str, Any]:
    return (e or {}).get("data") or {}


@router.get("/api/metrics/catalog", tags=["metrics"])
def metric_catalog(p=require("viewer")) -> dict[str, Any]:
    return {"items": [{"name": k, "label": v[0], "unit": v[1], "group": v[2]} for k, v in METRICS.items()]}


@router.get("/api/metrics/series", tags=["metrics"])
def metric_series(request: Request, names: str = Query(..., max_length=600), t_from: str | None = Query(None, alias="from"), t_to: str | None = Query(None, alias="to"),
                  max_points: int = Query(240, ge=20, le=1000), iid: str = Depends(resolve_instance), p=require("viewer")) -> dict[str, Any]:
    ctx = get_ctx(request)
    wanted = csv_list(names, set(METRICS), limit=12) or []
    start, end = time_range(t_from, t_to, 1.0)
    out = series(ctx.store, iid, wanted, start, end, max_points, ctx.demo, float(ctx.settings.t("fast_interval_s")))
    cov = collection_gaps(ctx.store, iid, start, end, float(ctx.settings.t("fast_interval_s")), ctx.demo)
    out["meta"] = {"instance": iid, "demo_mode": ctx.demo, "units": {n: METRICS[n][1] for n in wanted}, "labels": {n: METRICS[n][0] for n in wanted}}
    out["gaps"], out["coverage_pct"] = cov["gaps"], cov["coverage_pct"]
    out["note"] = "Points exist only where samples were recorded; gaps are listed explicitly and are never interpolated."
    return out


# --------------------------------------------------------------------------- live views
@router.get("/api/live/overview", tags=["live"])
def overview(request: Request, iid: str = Depends(resolve_instance), p=require("viewer")) -> dict[str, Any]:
    ctx = get_ctx(request)
    rt = ctx.recorder.runtime(iid)
    act = ctx.recorder.latest(iid, "activity")
    if act is None and not ctx.recorder.running:
        act = entry(ctx, iid, "fast", "activity")
    d = _data(act)
    conn, graph = d.get("connections") or {}, d.get("blocking") or {}
    rep, vac, dbs = _data(rt.latest.get("replication")), _data(rt.latest.get("vacuum")), _data(rt.latest.get("dbstats"))
    rows = d.get("rows") or []
    clients = [r for r in rows if r["backend_type"] == "client backend"]
    kp = {"sessions_total": conn.get("client_backends"), "sessions_active": sum(1 for r in clients if r["state"] == "active") if act else None,
          "sessions_idle_in_txn": sum(1 for r in clients if (r["state"] or "").startswith("idle in transaction")) if act else None, "waiting_on_locks": conn.get("waiting_on_locks"),
          "blocked_sessions": graph.get("blocked_sessions"), "root_blockers": graph.get("root_blockers"), "utilization_percent": conn.get("utilization_percent"),
          "max_connections": conn.get("max_connections"), "longest_query_s": max((r["query_age_s"] or 0 for r in clients if r["state"] == "active"), default=0) if act else None,
          "longest_xact_s": max((r["xact_age_s"] or 0 for r in clients), default=0) if act else None,
          "standbys": len(rep.get("standbys", [])) if rep else None, "max_replay_lag_s": max((s.get("replay_lag_s") or 0 for s in rep.get("standbys", [])), default=None) if rep else None,
          "inactive_slots": sum(1 for s in rep.get("slots", []) if not s["active"]) if rep else None,
          "xid_age_max": max((x["xid_age"] for x in vac.get("databases_xid", [])), default=None) if vac else None, "deadlocks_total": (dbs.get("current") or {}).get("deadlocks") if dbs else None}
    counts = ctx.store.evidence_counts(iid, ctx.demo)
    active_hi, _n = ctx.store.evidence_list(instance_id=iid, status="active", severities=["critical", "warning"], limit=12, sort="severity", order="desc", is_demo=ctx.demo)
    recent, _n2 = ctx.store.evidence_list(instance_id=iid, severities=["critical", "warning"], limit=8, sort="first_seen", order="desc", is_demo=ctx.demo)
    incs, _n3 = ctx.store.incident_list(instance_id=iid, status="candidate,open,investigating,mitigated", limit=6, is_demo=ctx.demo)
    now = utcnow()
    cov = collection_gaps(ctx.store, iid, now - timedelta(hours=1), now, float(ctx.settings.t("fast_interval_s")), ctx.demo)
    failures = [{"collector": n, "status": v.get("status"), "error_code": v.get("error_code"), "message": v.get("message"), "failed_at": v.get("failed_at")}
                for n, v in rt.latest.items() if v.get("status") in ("error",) or v.get("stale")]
    fast = rt.tiers["fast"]
    monitoring = "demo" if ctx.demo else ("not_started" if fast.last_run_at is None else "healthy" if fast.last_status in ("ok", "partial") and not failures else "degraded")
    return {"meta": live_meta(ctx, iid, "activity", act), "monitoring": {"status": monitoring, "recorder_running": ctx.recorder.running, "tiers": {t: {"last_run_at": s.last_run_at, "last_status": s.last_status,
            "consecutive_failures": s.consecutive_failures, "duration_ms": s.last_duration_ms} for t, s in rt.tiers.items()}, "coverage_1h_pct": cov["coverage_pct"], "gaps_1h": len(cov["gaps"])},
            "kpis": kp, "availability": {n: v.get("status") for n, v in rt.latest.items()}, "failures": failures, "evidence": {"counts": counts, "active": active_hi, "recent": recent},
            "incidents": {"counts": ctx.store.incident_counts(ctx.demo), "open": incs}, "server": {k: rt.info.get(k) for k in ("version_string", "in_recovery", "started_at")} if rt.info else None,
            "blocking_headline": graph.get("roots", [])[:1]}


@router.get("/api/live/activity", tags=["live"])
def activity(request: Request, iid: str = Depends(resolve_instance), state: str | None = Query(None, max_length=60), database: str | None = Query(None, max_length=64),
             user: str | None = Query(None, max_length=64), application: str | None = Query(None, max_length=64), q: str | None = Query(None, max_length=100),
             min_age_s: float = Query(0, ge=0), blocked_only: bool = False, wait_event: str | None = Query(None, max_length=64), pid: int | None = Query(None, ge=1),
             backend_type: str | None = Query("client backend", max_length=40), sort: str = Query("query_age_s", pattern="^(query_age_s|xact_age_s|state_age_s|pid|state)$"),
             order: str = Query("desc", pattern="^(asc|desc)$"), pg=Depends(paging), p=require("viewer")) -> dict[str, Any]:
    ctx = get_ctx(request)
    e = entry(ctx, iid, "fast", "activity")
    d = _data(e)
    rows = list(d.get("rows") or [])
    if backend_type and backend_type != "all":
        rows = [r for r in rows if r["backend_type"] == backend_type]
    f = lambda r: ((not state or r["state"] == state) and (not database or r["datname"] == database) and (not user or r["usename"] == user) and (not application or r["application_name"] == application)
                   and (not blocked_only or r["blocked_by"]) and (not wait_event or r["wait_event"] == wait_event) and (not pid or r["pid"] == pid)
                   and (max(r["query_age_s"] or 0, r["xact_age_s"] or 0) >= min_age_s) and (not q or q.lower() in f"{r['query']} {r['application_name']} {r['usename']} {r['datname']} {r['pid']}".lower()))
    rows = [r for r in rows if f(r)]
    key = (lambda r: (r[sort] is None, r[sort] or 0)) if sort != "state" else (lambda r: r["state"] or "")
    rows.sort(key=key, reverse=(order == "desc"))
    page, size = pg
    total = len(rows)
    return page_out(rows[(page - 1) * size: page * size], total, page, size, meta=live_meta(ctx, iid, "activity", e), connections=d.get("connections"), truncated=d.get("truncated", False),
                    query_text_mode=ctx.settings.query_text_mode, waits=d.get("waits") or [])


@router.get("/api/live/blocking", tags=["live"])
def blocking(request: Request, iid: str = Depends(resolve_instance), p=require("viewer")) -> dict[str, Any]:
    ctx = get_ctx(request)
    e = entry(ctx, iid, "fast", "activity")
    g = _data(e).get("blocking") or {"roots": [], "nodes": [], "edges": [], "cycles": [], "blocked_sessions": 0, "root_blockers": 0}
    active, _n = ctx.store.evidence_list(instance_id=iid, categories=["blocking"], status="active", limit=20, is_demo=ctx.demo)
    recent, _n2 = ctx.store.evidence_list(instance_id=iid, categories=["blocking"], limit=15, sort="first_seen", order="desc", is_demo=ctx.demo)
    return {"meta": live_meta(ctx, iid, "activity", e), "graph": g, "active_evidence": active, "recent_evidence": recent,
            "definitions": {"root_blocker": "Blocks other sessions and is not itself waiting for a lock. Resolving it unblocks the chain.",
                            "direct_blocker": "Holds (or is queued ahead for) a lock another session is waiting for. It may itself be blocked by a root blocker.",
                            "snapshot": "Lock inspection reflects the instant of collection. Historical blocking exists only where the recorder captured it (see Evidence)."}}


@router.get("/api/live/connections", tags=["live"])
def connections(request: Request, iid: str = Depends(resolve_instance), p=require("viewer")) -> dict[str, Any]:
    ctx = get_ctx(request)
    e = entry(ctx, iid, "fast", "activity")
    d = _data(e)
    return {"meta": live_meta(ctx, iid, "activity", e), "connections": d.get("connections"), "waits": d.get("waits") or []}


@router.get("/api/live/vacuum", tags=["live"])
def vacuum(request: Request, iid: str = Depends(resolve_instance), p=require("viewer")) -> dict[str, Any]:
    ctx = get_ctx(request)
    e = entry(ctx, iid, "slow", "vacuum")
    now = utcnow()
    growth = ctx.store.table_growth(iid, iso(now - timedelta(days=7)), iso(now), ctx.demo)[:15]
    return {"meta": live_meta(ctx, iid, "vacuum", e), **_data(e), "growth_7d": growth,
            "growth_note": "Growth is computed from stored size samples of the largest tables; it is empty until at least two samples exist."}


@router.get("/api/live/replication", tags=["live"])
def replication(request: Request, iid: str = Depends(resolve_instance), p=require("viewer")) -> dict[str, Any]:
    ctx = get_ctx(request)
    e = entry(ctx, iid, "standard", "replication")
    d = _data(e)
    ha = {"patroni": {"status": "not_configured", "note": "Cluster manager state cannot be derived from SQL. Set PATRONI_URL to read it from the Patroni REST API."}}
    if ctx.settings.patroni_url:
        from ..ha import patroni_status
        ha = {"patroni": patroni_status(ctx.settings)}
    return {"meta": live_meta(ctx, iid, "replication", e), **d, "integrations": ha}


@router.get("/api/live/database", tags=["live"])
def database(request: Request, iid: str = Depends(resolve_instance), p=require("viewer")) -> dict[str, Any]:
    ctx = get_ctx(request)
    e = entry(ctx, iid, "standard", "dbstats")
    return {"meta": live_meta(ctx, iid, "dbstats", e), **_data(e)}


@router.get("/api/live/config", tags=["live"])
def config_view(request: Request, iid: str = Depends(resolve_instance), q: str | None = Query(None, max_length=80), changed_only: bool = False,
                pg=Depends(paging), p=require("viewer")) -> dict[str, Any]:
    ctx = get_ctx(request)
    e = entry(ctx, iid, "slow", "settings")
    d = _data(e)
    items = [{"name": k, **v} for k, v in sorted((d.get("settings") or {}).items())]
    if q:
        items = [i for i in items if q.lower() in i["name"].lower()]
    if changed_only:
        items = [i for i in items if i["source"] not in ("default", "override")]
    page, size = pg
    changes, _n = ctx.store.evidence_list(instance_id=iid, categories=["config_change"], limit=10, sort="first_seen", order="desc", is_demo=ctx.demo)
    obs, _n2 = ctx.store.evidence_list(instance_id=iid, categories=["config_observation"], status="active", limit=30, is_demo=ctx.demo)
    return page_out(items[(page - 1) * size: page * size], len(items), page, size, meta=live_meta(ctx, iid, "settings", e), settings_hash=d.get("hash"), recent_changes=changes, observations=obs,
                    note="Observations explain how a value affects diagnostics or risk; they are not recommendations to change it.")


class RefreshBody(BaseModel):
    tiers: list[str] = Field(default_factory=lambda: ["fast"], max_length=3)


@router.post("/api/live/refresh", tags=["live"])
def refresh(body: RefreshBody, request: Request, iid: str = Depends(resolve_instance), p=require("operator")) -> dict[str, Any]:
    ctx = get_ctx(request)
    bad = [t for t in body.tiers if t not in ("fast", "standard", "slow")]
    if bad:
        raise AppError("tiers must be fast, standard or slow", code="VALIDATION_ERROR")
    audit(request, "collection.manual", iid, details={"tiers": body.tiers})
    return {"results": {t: ctx.recorder.run_tier_sync(iid, t, "manual") for t in body.tiers}}


# --------------------------------------------------------------------------- query performance
@router.get("/api/queries/top", tags=["queries"])
def queries_top(request: Request, iid: str = Depends(resolve_instance), window_min: int = Query(60, ge=5, le=1440), sort: str = Query("total", pattern="^(total|mean|calls|rows|temp)$"),
                limit: int = Query(25, ge=1, le=100), p=require("viewer")) -> dict[str, Any]:
    ctx = get_ctx(request)
    e = entry(ctx, iid, "standard", "statements")
    top = query_top(ctx.store, iid, window_min * 60, utcnow(), ctx.demo, limit, sort)
    reg, _n = ctx.store.evidence_list(instance_id=iid, categories=["query_regression", "query_hotspot"], limit=15, sort="first_seen", order="desc", is_demo=ctx.demo)
    return {"meta": live_meta(ctx, iid, "statements", e), **top, "regressions": reg, "extension": (ctx.recorder.runtime(iid).info or {}).get("pg_stat_statements"),
            "stats_reset": _data(e).get("stats_reset"), "hidden_text": _data(e).get("hidden_text")}


@router.get("/api/queries/trend", tags=["queries"])
def queries_trend(request: Request, iid: str = Depends(resolve_instance), queryid: str | None = Query(None, max_length=40), fingerprint: str | None = Query(None, max_length=40),
                  t_from: str | None = Query(None, alias="from"), t_to: str | None = Query(None, alias="to"), p=require("viewer")) -> dict[str, Any]:
    ctx = get_ctx(request)
    if not queryid and not fingerprint:
        raise AppError("Provide queryid or fingerprint", code="VALIDATION_ERROR")
    start, end = time_range(t_from, t_to, 6.0)
    return {"instance": iid, **query_trend(ctx.store, iid, queryid, fingerprint, start, end, ctx.demo),
            "note": "Per-interval means derived from cumulative counters; intervals spanning a statistics reset are omitted."}
