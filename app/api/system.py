"""System, health, instances, settings, audit and capability endpoints."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from ..config import TUNABLES, VERSION
from ..errors import AppError, NotFound
from ..pg.pool import PgError
from ..runbooks import RUNBOOKS
from ..security import audit, require
from ..timeutil import iso
from .deps import get_ctx, page_out, paging, resolve_instance

router = APIRouter()
CAPABILITIES = [
    ("activity", "Live sessions, wait events, connection utilization", "pg_stat_activity", "pg_read_all_stats (without it other users' query text and some columns are hidden)"),
    ("blocking", "Blocking graph and lock details", "pg_blocking_pids(), pg_locks", "No special role for own sessions; pg_read_all_stats to see all sessions"),
    ("dbstats", "Database counters, checkpoints, WAL, archiver", "pg_stat_database, pg_stat_bgwriter / pg_stat_checkpointer, pg_stat_wal, pg_stat_archiver", "pg_monitor for pg_ls_waldir() (WAL directory size, optional)"),
    ("statements", "Query performance", "pg_stat_statements", "Extension installed and in shared_preload_libraries; pg_read_all_stats to see all statements' text"),
    ("replication", "Replication state, lag, slots, standby recovery", "pg_stat_replication, pg_replication_slots, pg_stat_wal_receiver, pg_stat_subscription", "pg_monitor or pg_read_all_stats"),
    ("vacuum", "XID age, dead tuples, vacuum progress, xmin holders", "pg_database, pg_stat_user_tables, pg_stat_progress_*, pg_prepared_xacts", "pg_read_all_stats; SELECT-level access is not needed for statistics"),
    ("settings", "Configuration and change detection", "pg_settings", "pg_read_all_settings to see every parameter value"),
    ("errorlog", "Server log errors, deadlocks, lock waits", "PG_LOG_FILE on the application host", "OS read permission on the log file (not a database privilege)"),
]


@router.get("/api/health/live", tags=["system"])
def live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/api/health/ready", tags=["system"])
def ready(request: Request) -> Any:
    ctx = get_ctx(request)
    problems = []
    if not ctx.store.ping():
        problems.append("history store is not writable")
    if not ctx.settings.instances:
        problems.append("no database instance configured")
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=200 if not problems else 503, content={"status": "ready" if not problems else "not_ready", "problems": problems})


@router.get("/api/auth/status", tags=["system"])
def auth_status(request: Request) -> dict[str, Any]:
    s = get_ctx(request).settings
    return {"auth_mode": s.auth_mode, "authentication_required": s.auth_mode == "token", "roles": ["viewer", "operator", "admin"]}


@router.get("/api/health", tags=["system"])
def health(request: Request, p=require("viewer")) -> dict[str, Any]:
    ctx = get_ctx(request)
    insts = []
    for cfg in ctx.settings.instances:
        rt = ctx.recorder.runtime(cfg.id)
        fast = rt.tiers["fast"]
        status = "demo" if ctx.demo else "unknown" if fast.last_status is None else ("ok" if fast.last_status in ("ok", "partial") else "unreachable")
        insts.append({"id": cfg.id, "status": status, "last_collection": fast.last_run_at, "version": rt.info.get("version_string")})
    overall = "unconfigured" if not insts else "ok" if all(i["status"] in ("ok", "demo", "unknown") for i in insts) else "degraded"
    return {"status": overall, "version": VERSION, "demo_mode": ctx.demo, "checked_at": iso(), "instances": insts, "history_store": ctx.store.ping(),
            "recorder_running": ctx.recorder.running}


@router.get("/api/system/status", tags=["system"])
def system_status(request: Request, p=require("viewer")) -> dict[str, Any]:
    ctx = get_ctx(request)
    return {"version": VERSION, "demo_mode": ctx.demo, "started_at": ctx.started_at, "config": ctx.settings.public(), "recorder": ctx.recorder.status(), "store": ctx.store.stats(),
            "timezone_note": "All persisted timestamps are UTC; the dashboard converts them to the selected timezone."}


@router.get("/api/system/capabilities", tags=["system"])
def capabilities(request: Request, iid: str = Depends(resolve_instance), p=require("viewer")) -> dict[str, Any]:
    ctx = get_ctx(request)
    rt = ctx.recorder.runtime(iid)
    rows = []
    for name, purpose, views, needs in CAPABILITIES:
        latest = rt.latest.get(name)
        status = "not_collected_yet" if latest is None and name != "blocking" else None
        if name == "blocking":
            latest = rt.latest.get("activity")
        if latest:
            status = latest["status"] if not latest.get("stale") else f"{latest['status']} (stale)"
        if name == "errorlog" and not rt.cfg.log_file:
            status = "not_configured"
        rows.append({"capability": name, "purpose": purpose, "uses": views, "requires": needs, "status": status or "unknown",
                     "detail": (latest or {}).get("message"), "error_code": (latest or {}).get("error_code")})
    ha = {"patroni": {"status": "not_configured", "note": "Cluster health from Patroni/repmgr cannot be determined from SQL. Set PATRONI_URL to enable the optional REST integration."}}
    if ctx.settings.patroni_url:
        from ..ha import patroni_status
        ha = {"patroni": patroni_status(ctx.settings)}
    return {"instance": iid, "server": {k: rt.info.get(k) for k in ("version_num", "version_string", "current_user", "is_superuser", "pg_monitor", "pg_read_all_stats", "pg_read_all_settings",
                                                                 "ssl_in_use", "ssl_version", "tls_verified", "in_recovery")} if rt.info else None, "capabilities": rows, "integrations": ha,
            "supported_versions": "PostgreSQL 12-17 (see docs/compatibility.md for what was executed)"}


# --------------------------------------------------------------------------- instances
@router.get("/api/instances", tags=["instances"])
def instances(request: Request, p=require("viewer")) -> dict[str, Any]:
    ctx = get_ctx(request)
    out = []
    for cfg in ctx.settings.instances:
        rt = ctx.recorder.runtime(cfg.id)
        counts = ctx.store.evidence_counts(cfg.id, ctx.demo)
        out.append({**cfg.public(), "enabled": rt.enabled, "version": rt.info.get("version_string"), "role": "standby" if rt.info.get("in_recovery") else "primary" if rt.info else None,
                    "tls_in_use": rt.info.get("ssl_in_use"), "tiers": {t: vars(s) for t, s in rt.tiers.items()}, "active_evidence": counts["active_by_severity"],
                    "collector_status": {n: {"status": v.get("status"), "ts": v.get("ts"), "stale": v.get("stale", False)} for n, v in rt.latest.items()}, "demo": ctx.demo})
    return {"items": out, "total": len(out), "warnings": ctx.settings.config_warnings}


class ToggleBody(BaseModel):
    enabled: bool


@router.post("/api/instances/{iid}/test", tags=["instances"])
def test_instance(iid: str, request: Request, p=require("operator")) -> dict[str, Any]:
    ctx = get_ctx(request)
    if iid not in ctx.recorder.rt:
        raise NotFound("Unknown instance")
    rt = ctx.recorder.rt[iid]
    audit(request, "instance.test", iid)
    if ctx.demo:
        return {"ok": True, "demo": True, "message": "Demo instance: no database connection is made."}
    try:
        info = rt.pg.probe()
    except PgError as e:
        return {"ok": False, "error_code": e.code, "message": e.message, "hint": e.hint}
    rt.info.update(info)
    return {"ok": True, "version": info["version_string"], "supported": info["supported"], "in_recovery": info["in_recovery"], "current_user": info["current_user"],
            "privileges": {k: info[k] for k in ("is_superuser", "pg_monitor", "pg_read_all_stats", "pg_read_all_settings")}, "tls": {"in_use": info["ssl_in_use"], "version": info["ssl_version"],
            "certificate_verified": rt.cfg.tls_verified}, "pg_stat_statements": bool(info["pg_stat_statements"]),
            "warnings": (["The monitoring role is a superuser; use a least-privilege role (see docs/permissions.md)."] if info["is_superuser"] else []) + ([] if rt.cfg.tls_verified else ["TLS certificate is not verified for this connection."])}


@router.post("/api/instances/{iid}/monitoring", tags=["instances"])
def toggle_monitoring(iid: str, body: ToggleBody, request: Request, p=require("admin")) -> dict[str, Any]:
    ctx = get_ctx(request)
    if iid not in ctx.recorder.rt:
        raise NotFound("Unknown instance")
    ctx.recorder.rt[iid].enabled = body.enabled
    audit(request, "instance.monitoring", iid, details={"enabled": body.enabled})
    return {"instance": iid, "enabled": body.enabled, "note": "Runtime toggle; resets to the configured value on restart."}


# --------------------------------------------------------------------------- settings
class SettingsBody(BaseModel):
    values: dict[str, float] = Field(..., min_length=1, max_length=40)


@router.get("/api/settings", tags=["settings"])
def get_settings(request: Request, p=require("viewer")) -> dict[str, Any]:
    ctx = get_ctx(request)
    return {"items": ctx.settings.tunable_rows(), "security": {"auth_mode": ctx.settings.auth_mode, "query_text_mode": ctx.settings.query_text_mode,
                                                                 "statement_timeout_ms": ctx.settings.statement_timeout_ms, "connect_timeout_s": ctx.settings.connect_timeout_s,
                                                                 "max_concurrent_collections": ctx.settings.max_concurrent, "tls": {i.id: {"sslmode": i.sslmode, "verified": i.tls_verified} for i in ctx.settings.instances}},
            "note": "Thresholds, intervals and retention can be changed here (admin). Security settings are environment-only by design."}


@router.put("/api/settings", tags=["settings"])
def put_settings(body: SettingsBody, request: Request, p=require("admin")) -> dict[str, Any]:
    ctx = get_ctx(request)
    unknown = [k for k in body.values if k not in TUNABLES]
    if unknown:
        raise AppError(f"Unknown setting(s): {', '.join(unknown)}", code="VALIDATION_ERROR")
    old = {k: ctx.settings.t(k) for k in body.values}
    errors = {}
    new = {}
    for k, v in body.values.items():
        try:
            new[k] = ctx.settings.set_override(k, v)
        except ValueError as e:
            errors[k] = str(e)
    if errors:
        for k in new:                       # roll back partial application
            ctx.settings.overrides.pop(k, None)
            if k in old and old[k] != ctx.settings.defaults.get(k):
                ctx.settings.overrides[k] = old[k]
        raise AppError("Some values are out of range", code="VALIDATION_ERROR", details=errors)
    for k, v in new.items():
        ctx.store.settings_save(k, str(v))
    audit(request, "settings.update", "tunables", details={"changed": {k: {"old": old[k], "new": new[k]} for k in new}})
    return {"updated": new, "items": ctx.settings.tunable_rows()}


@router.delete("/api/settings/{name}", tags=["settings"])
def reset_setting(name: str, request: Request, p=require("admin")) -> dict[str, Any]:
    ctx = get_ctx(request)
    if name not in TUNABLES:
        raise NotFound("Unknown setting")
    ctx.settings.overrides.pop(name, None)
    ctx.store.settings_delete(name)
    audit(request, "settings.reset", name)
    return {"name": name, "value": ctx.settings.t(name), "overridden": False}


@router.post("/api/maintenance/retention", tags=["settings"])
def run_retention(request: Request, dry_run: bool = Query(True), p=require("admin")) -> dict[str, Any]:
    ctx = get_ctx(request)
    out = ctx.recorder.run_retention(dry_run=dry_run)
    audit(request, "retention.run", "store", details={"dry_run": dry_run, "rows": out})
    return {"dry_run": dry_run, "rows_affected": out, "note": "Evidence linked to incidents and open incidents are never removed by retention."}


# --------------------------------------------------------------------------- audit + runs
@router.get("/api/audit", tags=["audit"])
def audit_list(request: Request, action: str | None = Query(None, max_length=60), outcome: str | None = Query(None, max_length=20), q: str | None = Query(None, max_length=100),
               t_from: str | None = Query(None, alias="from"), t_to: str | None = Query(None, alias="to"), pg=Depends(paging), p=require("admin")) -> dict[str, Any]:
    ctx = get_ctx(request)
    page, size = pg
    items, total = ctx.store.audit_list(action=action, outcome=outcome, q=q, t_from=t_from, t_to=t_to, limit=size, offset=(page - 1) * size)
    return page_out(items, total, page, size)


@router.get("/api/runs", tags=["audit"])
def runs(request: Request, iid: str = Depends(resolve_instance), tier: str | None = Query(None, pattern="^(fast|standard|slow)$"), status: str | None = Query(None, pattern="^(ok|partial|error)$"),
         pg=Depends(paging), p=require("viewer")) -> dict[str, Any]:
    ctx = get_ctx(request)
    page, size = pg
    items, total = ctx.store.runs_list(iid, tier, status, limit=size, offset=(page - 1) * size, is_demo=ctx.demo)
    return page_out(items, total, page, size)


# --------------------------------------------------------------------------- runbooks (v0.6, read-only guidance)
@router.get("/api/runbooks", tags=["runbooks"])
def runbooks(p=require("viewer")) -> dict[str, Any]:
    items = [{k: v for k, v in rb.items() if k != "checks"} | {"check_count": len(rb["checks"])} for rb in RUNBOOKS.values()]
    return {"items": items, "total": len(items)}


@router.get("/api/runbooks/{runbook_id}", tags=["runbooks"])
def runbook(runbook_id: str, p=require("viewer")) -> dict[str, Any]:
    if runbook_id not in RUNBOOKS:
        raise NotFound("Runbook not found")
    return RUNBOOKS[runbook_id]
