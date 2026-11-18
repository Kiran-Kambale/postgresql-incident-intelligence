"""Read-only collectors. Each returns plain data; failures are classified and never hide other collectors' results.

Tiers: fast (sessions, blocking, connections) / standard (counters, replication, query statistics) / slow (vacuum, XID age, settings, server info).
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from ..config import Settings
from ..security import redact_sql, sql_fingerprint
from ..timeutil import iso
from ..util import jsonable
from . import versions as V
from .graph import build_blocking_graph
from .logs import collect_errorlog
from .pool import PgError, PgInstance, classify

log = logging.getLogger("dir.collect")
QUERY_LEN = 1500
ACTIVITY_LIMIT = 1500


@dataclass
class CResult:
    name: str
    status: str                       # ok | partial | unavailable | error
    data: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None
    message: str | None = None
    hint: str | None = None
    duration_ms: int = 0
    parts: dict[str, str] = field(default_factory=dict)   # sub-query -> ok | error code

    def summary(self) -> dict[str, Any]:
        return {"status": self.status, "error_code": self.error_code, "message": self.message, "hint": self.hint,
                "duration_ms": self.duration_ms, "parts": self.parts or None}


class Ctx:
    """Per-run context shared by the collectors of one tier."""

    def __init__(self, inst: PgInstance, settings: Settings, info: dict[str, Any], now: datetime, store: Any = None):
        self.inst, self.settings, self.info, self.now, self.store = inst, settings, info, now, store
        self.v: int = info.get("version_num", 0)
        self.partials: list[tuple[str, str]] = []
        self._kv: dict[str, str] | None = None

    def soft(self, part: str, sql: str, params: Any = None, default: Any = None) -> Any:
        """Run an optional sub-query; a failure is recorded as a partial result instead of failing the collector."""
        try:
            return self.inst.query(sql, params)
        except PgError as e:
            self.partials.append((part, e.code))
            return default if default is not None else []

    def kv(self) -> dict[str, str]:
        if self._kv is None:
            rows = self.inst.query(V.SETTINGS_SQL, {"names": list(V.SETTINGS_KEYS)})
            self._kv = {r["name"]: r["setting"] for r in rows}
        return self._kv

    def num(self, name: str, default: float | None = None) -> float | None:
        try:
            return float(self.kv()[name])
        except (KeyError, ValueError):
            return default

    def text(self, sql: str | None) -> str | None:
        return redact_sql(sql, self.settings.query_text_mode, self.settings.redact_patterns)


# --------------------------------------------------------------------------- helpers
def _clean_activity(ctx: Ctx, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        r = jsonable(r)
        raw = r.get("query")
        r["query_fingerprint"] = sql_fingerprint(raw)
        r["query"] = ctx.text(raw)
        r["blocked_by"] = list(r.get("blocked_by") or [])
        out.append(r)
    return out


def summarize_connections(conn_rows: list[dict[str, Any]], max_conn: float | None, reserved: float, activity: list[dict[str, Any]]) -> dict[str, Any]:
    by_state: dict[str, int] = {}
    by_db: dict[str, int] = {}
    by_user: dict[str, int] = {}
    by_app: dict[str, int] = {}
    client = walsenders = autovac = 0
    for r in conn_rows:
        n = r["sessions"]
        if r["backend_type"] == "client backend":
            client += n
            by_state[r["state"] or "unknown"] = by_state.get(r["state"] or "unknown", 0) + n
            by_db[r["datname"] or "(none)"] = by_db.get(r["datname"] or "(none)", 0) + n
            by_user[r["usename"] or "(none)"] = by_user.get(r["usename"] or "(none)", 0) + n
            by_app[r["application_name"] or "(unset)"] = by_app.get(r["application_name"] or "(unset)", 0) + n
        elif r["backend_type"] == "walsender":
            walsenders += n
        elif r["backend_type"] == "autovacuum worker":
            autovac += n
    used = client + walsenders
    top = lambda d: [{"name": k, "sessions": v} for k, v in sorted(d.items(), key=lambda kv: -kv[1])[:10]]
    return {
        "client_backends": client, "walsenders": walsenders, "autovacuum_workers": autovac, "slots_used": used,
        "max_connections": int(max_conn) if max_conn else None, "reserved_connections": int(reserved),
        "utilization_percent": round(100.0 * used / max_conn, 1) if max_conn else None,
        "available_for_regular_users": int(max_conn - reserved - used) if max_conn else None,
        "by_state": [{"state": k, "sessions": v} for k, v in sorted(by_state.items(), key=lambda kv: -kv[1])],
        "by_database": top(by_db), "by_user": top(by_user), "by_application": top(by_app),
        "waiting_on_locks": sum(1 for r in activity if r.get("blocked_by")),
        "note": "Slots used = client backends + walsenders (both count against max_connections). Autovacuum workers have a separate limit.",
    }


# --------------------------------------------------------------------------- collectors
def collect_activity(ctx: Ctx) -> dict[str, Any]:
    rows = ctx.inst.query(V.activity_sql(ctx.v), {"qlen": QUERY_LEN, "limit": ACTIVITY_LIMIT})
    activity = _clean_activity(ctx, rows)
    conn_rows = ctx.inst.query(V.CONNECTIONS_SQL)
    waits = ctx.soft("waits", V.WAITS_SQL)
    pids = sorted({p for r in activity for p in r["blocked_by"]} | {r["pid"] for r in activity if r["blocked_by"]})[:300]
    lock_rows = jsonable(ctx.soft("pg_locks", V.LOCKS_SQL, {"pids": pids})) if pids else []
    graph = build_blocking_graph(activity, lock_rows)
    maxc = ctx.num("max_connections")
    reserved = (ctx.num("superuser_reserved_connections", 3) or 0) + (ctx.num("reserved_connections", 0) or 0)
    return {"rows": activity, "truncated": len(rows) >= ACTIVITY_LIMIT, "connections": summarize_connections(conn_rows, maxc, reserved, activity),
            "waits": jsonable(waits), "blocking": graph}


def collect_dbstats(ctx: Ctx) -> dict[str, Any]:
    rows = jsonable(ctx.inst.query(V.dbstats_sql(ctx.v)))
    for r in rows:
        tot = (r["blks_hit"] or 0) + (r["blks_read"] or 0)
        r["cache_hit_pct"] = round(100.0 * r["blks_hit"] / tot, 2) if tot else None
    bg = jsonable(ctx.soft("bgwriter", V.bgwriter_sql(ctx.v)))
    wal = ctx.soft("wal", V.wal_sql(ctx.v))
    wal_dir = ctx.soft("wal_dir", V.WAL_DIR_SQL)       # needs pg_monitor: optional
    arch = ctx.soft("archiver", V.ARCHIVER_SQL)
    return {"databases": rows, "current": next((r for r in rows if r.get("is_current")), rows[0] if rows else None),
            "bgwriter": bg[0] if bg else None, "wal": jsonable(wal[0]) if wal else None, "wal_dir": jsonable(wal_dir[0]) if wal_dir else None,
            "archiver": jsonable(arch[0]) if arch else None,
            "note": "All counters are cumulative since stats_reset; rates are derived from consecutive samples and skipped across resets."}


def collect_statements(ctx: Ctx) -> dict[str, Any]:
    ext = ctx.info.get("pg_stat_statements")
    if not ext:
        raise PgError("NOT_INSTALLED", "pg_stat_statements is not installed in this database.",
                      "Add it to shared_preload_libraries (restart required) and run CREATE EXTENSION pg_stat_statements. See docs/query-performance.md.")
    schema = ext["schema"]
    rows = ctx.inst.query(V.statements_sql(ctx.v, schema), {"qlen": QUERY_LEN, "limit": 200})
    info_sql = V.statements_info_sql(ctx.v, schema)
    info = ctx.soft("statements_info", info_sql) if info_sql else []
    out, hidden = [], 0
    for r in rows:
        raw = r.pop("query") or ""
        if raw.startswith("<insufficient privilege>"):
            hidden += 1
        r["fingerprint"] = sql_fingerprint(raw) or f"qid-{r['queryid']}"
        r["text"] = ctx.text(raw)
        out.append(jsonable(r))
    return {"queries": out, "info": jsonable(info[0]) if info else None, "hidden_text": hidden, "schema": schema,
            "stats_reset": (jsonable(info[0]).get("stats_reset") if info else None),
            "note": "Cumulative since the last reset, current database only. Query text is normalized by PostgreSQL and redacted here."}


def collect_replication(ctx: Ctx) -> dict[str, Any]:
    rec = bool(ctx.info.get("in_recovery"))
    senders = jsonable(ctx.inst.query(V.replication_sql(ctx.v, rec)))
    slots = jsonable(ctx.soft("slots", V.slots_sql(ctx.v, rec)))
    receiver = recovery = None
    if rec:
        r = ctx.soft("wal_receiver", V.receiver_sql(ctx.v))
        receiver = jsonable(r[0]) if r else None
        if receiver and receiver.get("sender_host"):
            receiver["sender_host"] = "<hidden>"
        rv = ctx.soft("recovery", V.RECOVERY_SQL)
        recovery = jsonable(rv[0]) if rv else None
    subs = jsonable(ctx.soft("subscriptions", V.SUBSCRIPTIONS_SQL))
    kv = ctx.kv()
    return {"role": "standby" if rec else "primary", "in_recovery": rec, "standbys": senders, "slots": slots, "receiver": receiver,
            "recovery": recovery, "logical_subscriptions": subs,
            "settings": {k: kv.get(k) for k in ("max_wal_senders", "max_replication_slots", "max_slot_wal_keep_size", "wal_keep_size")},
            "note": "Physical replication (pg_stat_replication, physical slots) and logical replication (subscriptions, logical slots) are reported separately."}


def collect_vacuum(ctx: Ctx) -> dict[str, Any]:
    xid = jsonable(ctx.inst.query(V.XID_SQL))
    tables = jsonable(ctx.soft("tables", V.vacuum_tables_sql(ctx.v), {"limit": 50}))
    sizes = jsonable(ctx.soft("table_sizes", V.TABLE_SIZES_SQL))
    prog = jsonable(ctx.soft("progress_vacuum", V.PROGRESS_VACUUM_SQL))
    for p in prog:
        p["query"] = ctx.text(p.get("query"))
    idx = jsonable(ctx.soft("progress_index", V.PROGRESS_INDEX_SQL))
    clu = jsonable(ctx.soft("progress_cluster", V.PROGRESS_CLUSTER_SQL))
    prepared = jsonable(ctx.soft("prepared_xacts", V.PREPARED_SQL))
    xmin_b = jsonable(ctx.soft("xmin_backends", V.XMIN_BACKENDS_SQL))
    xmin_s = jsonable(ctx.soft("xmin_slots", V.XMIN_SLOTS_SQL))
    for t in tables:
        live, dead = t.get("n_live_tup") or 0, t.get("n_dead_tup") or 0
        t["dead_ratio_pct"] = round(100.0 * dead / (live + dead), 1) if (live + dead) else 0.0
    kv = ctx.kv()
    return {"databases_xid": xid, "tables": tables, "table_sizes": sizes, "vacuum_progress": prog, "maintenance": {"create_index": idx, "cluster": clu},
            "prepared_xacts": prepared, "xmin_holders": {"backends": xmin_b, "slots": xmin_s, "prepared": prepared[:5]},
            "settings": {k: kv.get(k) for k in ("autovacuum_freeze_max_age", "autovacuum_multixact_freeze_max_age", "autovacuum_max_workers", "autovacuum_naptime")},
            "note": "Table statistics are for the monitored database only. n_dead_tup is an estimate maintained by the statistics system."}


def collect_settings(ctx: Ctx) -> dict[str, Any]:
    rows = ctx.inst.query(V.ALL_SETTINGS_SQL)
    settings = {r["name"]: {"setting": r["setting"], "unit": r["unit"], "source": r["source"], "pending_restart": r["pending_restart"], "context": r["context"]} for r in rows}
    digest = hashlib.sha256(json.dumps({k: v["setting"] for k, v in sorted(settings.items())}, sort_keys=True).encode()).hexdigest()[:16]
    return {"settings": settings, "hash": digest, "count": len(settings)}


def collect_server(ctx: Ctx) -> dict[str, Any]:
    info = ctx.inst.probe()
    ctx.info.update(info)
    return jsonable({k: info.get(k) for k in ("version_num", "version_string", "major", "supported", "in_recovery", "current_user", "database", "is_superuser",
                                              "pg_monitor", "pg_read_all_stats", "pg_read_all_settings", "started_at", "server_time", "ssl_in_use",
                                              "ssl_version", "tls_verified", "pg_stat_statements")})


TIERS: dict[str, list[tuple[str, Callable[[Ctx], dict[str, Any]]]]] = {
    "fast": [("activity", collect_activity)],
    "standard": [("dbstats", collect_dbstats), ("replication", collect_replication), ("statements", collect_statements)],
    "slow": [("server", collect_server), ("vacuum", collect_vacuum), ("settings", collect_settings)],
}


def run_collector(name: str, fn: Callable[[Ctx], dict[str, Any]], ctx: Ctx) -> CResult:
    t0 = time.monotonic()
    ctx.partials = []
    try:
        data = fn(ctx)
        status = "partial" if ctx.partials else "ok"
        res = CResult(name, status, data, parts={p: c for p, c in ctx.partials})
        if ctx.partials:
            res.message = "Some sub-queries were unavailable: " + ", ".join(f"{p} ({c})" for p, c in ctx.partials)
    except PgError as e:
        res = CResult(name, e.status, {}, e.code, e.message, e.hint)
    except Exception as exc:   # a collector bug must never take the recorder down
        e = classify(exc)
        log.exception("collector %s crashed", name)
        res = CResult(name, "error", {}, e.code, e.message, e.hint)
    res.duration_ms = int((time.monotonic() - t0) * 1000)
    return res


def run_tier(inst: PgInstance, settings: Settings, tier: str, info: dict[str, Any], now: datetime, store: Any = None) -> dict[str, CResult]:
    ctx = Ctx(inst, settings, info, now, store)
    results: dict[str, CResult] = {}
    if tier != "slow" and not info.get("version_num"):          # first contact: learn version/privileges before version-gated SQL
        results["server"] = run_collector("server", collect_server, ctx)
        if results["server"].status != "ok":
            return results
        ctx.v = ctx.info.get("version_num", 0)
    if info.get("version_num") and info["version_num"] < V.MIN_SUPPORTED:
        return {"server": CResult("server", "unavailable", {}, "UNSUPPORTED_VERSION",
                                  f"PostgreSQL {info['version_num'] // 10000} is older than the supported minimum ({V.MIN_SUPPORTED // 10000}).")}
    plan = list(TIERS[tier]) + ([("errorlog", collect_errorlog)] if tier == "standard" and inst.cfg.log_file else [])
    for name, fn in plan:
        res = run_collector(name, fn, ctx)
        results[name] = res
        if name == "server" and res.status == "ok":
            ctx.v = ctx.info.get("version_num", 0)
        elif res.error_code in ("DB_UNAVAILABLE", "AUTH_FAILED", "TLS_FAILED"):   # no point hammering a down database
            for rest, _fn in plan[len(results):]:
                results[rest] = CResult(rest, "error", {}, res.error_code, "Skipped because the database is unreachable.")
            break
    return results
