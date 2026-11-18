"""SQLite persistence core: connections, migrations, metrics, collection runs, query statistics, audit, retention.

Design notes
- One SQLite file per mode (live vs demo) so synthetic data can never mix with real observations.
- WAL mode; a process-wide write lock; short-lived connections (safe across threads).
- All timestamps are UTC ISO-8601 strings with a trailing Z, so lexical order equals time order.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from ..security import redact_sql, sql_fingerprint
from ..timeutil import iso, normalize_ts, parse_ts, utcnow

SCHEMA_VERSION = 2
MAX_DETAILS_BYTES = 48_000


def jdump(obj: Any, cap: int = MAX_DETAILS_BYTES) -> str:
    s = json.dumps(obj, default=str, separators=(",", ":"))
    if len(s) <= cap:
        return s
    keys = list(obj.keys())[:20] if isinstance(obj, dict) else []
    return json.dumps({"_truncated": True, "_note": f"details exceeded {cap} bytes and were truncated", "keys": keys})


def jload(s: str | None, default: Any = None) -> Any:
    if not s:
        return default
    try:
        return json.loads(s)
    except ValueError:
        return default


def like_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


LEGACY_DDL = [
    """CREATE TABLE IF NOT EXISTS snapshots (id INTEGER PRIMARY KEY AUTOINCREMENT, captured_at TEXT NOT NULL,
        database_name TEXT NOT NULL, title TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '', payload_json TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS monitor_samples (id INTEGER PRIMARY KEY AUTOINCREMENT, captured_at TEXT NOT NULL,
        database_name TEXT NOT NULL, status TEXT NOT NULL, payload_json TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS incident_events (id INTEGER PRIMARY KEY AUTOINCREMENT, event_key TEXT NOT NULL,
        started_at TEXT NOT NULL, ended_at TEXT, severity TEXT NOT NULL, title TEXT NOT NULL, details_json TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 1)""",
    """CREATE TABLE IF NOT EXISTS incidents (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        title TEXT NOT NULL, symptom TEXT NOT NULL, severity TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open',
        environment TEXT NOT NULL DEFAULT '', ticket_ref TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '',
        findings_json TEXT NOT NULL DEFAULT '[]', actions_json TEXT NOT NULL DEFAULT '[]')""",
    """CREATE TABLE IF NOT EXISTS query_performance_samples (id INTEGER PRIMARY KEY AUTOINCREMENT, captured_at TEXT NOT NULL,
        database_name TEXT NOT NULL, queryid TEXT NOT NULL, query_text TEXT NOT NULL, calls REAL NOT NULL,
        total_exec_time REAL NOT NULL, mean_exec_time REAL NOT NULL, rows_returned REAL NOT NULL,
        source TEXT NOT NULL DEFAULT 'pg_stat_statements')""",
]

V2_DDL = [
    """CREATE TABLE IF NOT EXISTS collection_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, instance_id TEXT NOT NULL,
        tier TEXT NOT NULL, trigger TEXT NOT NULL DEFAULT 'scheduled', started_at TEXT NOT NULL, finished_at TEXT,
        duration_ms INTEGER, status TEXT NOT NULL, collectors_json TEXT NOT NULL DEFAULT '{}', error TEXT,
        is_demo INTEGER NOT NULL DEFAULT 0)""",
    "CREATE INDEX IF NOT EXISTS idx_runs_inst ON collection_runs(instance_id, tier, started_at)",
    """CREATE TABLE IF NOT EXISTS metrics (ts TEXT NOT NULL, instance_id TEXT NOT NULL, name TEXT NOT NULL,
        value REAL NOT NULL, is_demo INTEGER NOT NULL DEFAULT 0)""",
    "CREATE INDEX IF NOT EXISTS idx_metrics ON metrics(instance_id, name, ts)",
    """CREATE TABLE IF NOT EXISTS evidence (id TEXT PRIMARY KEY, instance_id TEXT NOT NULL, category TEXT NOT NULL,
        severity TEXT NOT NULL, title TEXT NOT NULL, fingerprint TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
        first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, cleared_at TEXT, observation_count INTEGER NOT NULL DEFAULT 1,
        source TEXT NOT NULL, collector TEXT NOT NULL DEFAULT '', pid INTEGER, query_fingerprint TEXT,
        collection_status TEXT NOT NULL DEFAULT 'ok', confidence TEXT NOT NULL DEFAULT 'observed',
        limitations TEXT NOT NULL DEFAULT '', details_json TEXT NOT NULL DEFAULT '{}',
        is_demo INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS idx_ev_time ON evidence(instance_id, first_seen)",
    "CREATE INDEX IF NOT EXISTS idx_ev_fp ON evidence(instance_id, fingerprint, status)",
    "CREATE INDEX IF NOT EXISTS idx_ev_cat ON evidence(category, severity)",
    """CREATE TABLE IF NOT EXISTS incident_evidence (incident_id TEXT NOT NULL, evidence_id TEXT NOT NULL,
        link_type TEXT NOT NULL DEFAULT 'manual', linked_at TEXT NOT NULL, linked_by TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (incident_id, evidence_id))""",
    """CREATE TABLE IF NOT EXISTS incident_notes (id INTEGER PRIMARY KEY AUTOINCREMENT, incident_id TEXT NOT NULL,
        ts TEXT NOT NULL, kind TEXT NOT NULL, author TEXT NOT NULL DEFAULT '', text TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS idx_notes_inc ON incident_notes(incident_id, ts)",
    "CREATE TABLE IF NOT EXISTS query_texts (fingerprint TEXT PRIMARY KEY, text TEXT NOT NULL, first_seen TEXT NOT NULL)",
    """CREATE TABLE IF NOT EXISTS query_stat_samples (ts TEXT NOT NULL, instance_id TEXT NOT NULL, dbid TEXT NOT NULL DEFAULT '',
        userid TEXT NOT NULL DEFAULT '', queryid TEXT NOT NULL, fingerprint TEXT, calls REAL NOT NULL,
        total_exec_ms REAL NOT NULL, max_exec_ms REAL, rows REAL, shared_hit REAL, shared_read REAL, temp_written REAL,
        stats_reset TEXT, source TEXT NOT NULL DEFAULT 'pg_stat_statements', is_demo INTEGER NOT NULL DEFAULT 0)""",
    "CREATE INDEX IF NOT EXISTS idx_qss ON query_stat_samples(instance_id, queryid, ts)",
    "CREATE INDEX IF NOT EXISTS idx_qss_ts ON query_stat_samples(instance_id, ts)",
    """CREATE TABLE IF NOT EXISTS table_samples (ts TEXT NOT NULL, instance_id TEXT NOT NULL, relation TEXT NOT NULL,
        size_bytes REAL, n_live REAL, n_dead REAL, is_demo INTEGER NOT NULL DEFAULT 0)""",
    "CREATE INDEX IF NOT EXISTS idx_tbl ON table_samples(instance_id, relation, ts)",
    """CREATE TABLE IF NOT EXISTS audit_log (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, actor TEXT NOT NULL,
        role TEXT NOT NULL, action TEXT NOT NULL, target TEXT NOT NULL DEFAULT '', outcome TEXT NOT NULL DEFAULT 'ok',
        ip TEXT NOT NULL DEFAULT '', details_json TEXT NOT NULL DEFAULT '{}')""",
    "CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts)",
    "CREATE TABLE IF NOT EXISTS kv_state (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS settings_store (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)",
]

INCIDENT_COLUMNS = {
    "instance_id": "TEXT NOT NULL DEFAULT 'default'", "started_at": "TEXT", "ended_at": "TEXT",
    "stage": "TEXT NOT NULL DEFAULT 'collect'", "suspected_root_cause": "TEXT NOT NULL DEFAULT ''",
    "confirmed_root_cause": "TEXT NOT NULL DEFAULT ''", "resolution": "TEXT NOT NULL DEFAULT ''",
    "review_notes": "TEXT NOT NULL DEFAULT ''", "source": "TEXT NOT NULL DEFAULT 'manual'",
    "candidate_key": "TEXT", "is_demo": "INTEGER NOT NULL DEFAULT 0",
}
SNAPSHOT_COLUMNS = {"instance_id": "TEXT NOT NULL DEFAULT 'default'", "is_demo": "INTEGER NOT NULL DEFAULT 0", "incident_id": "TEXT"}
CATEGORY_MAP = {"blocking": "blocking", "long_queries": "long_query", "idle_transactions": "idle_in_transaction",
                "connections_high": "connection_saturation"}


def _columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}


class CoreStore:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._wlock = threading.RLock()
        self.migrated_from: int = 0
        self._migrate()

    # ------------------------------------------------------------------ connections
    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=30000")
        con.execute("PRAGMA foreign_keys=ON")
        return con

    @contextmanager
    def tx(self):
        with self._wlock:
            con = self._connect()
            try:
                con.execute("BEGIN IMMEDIATE")
                yield con
                con.execute("COMMIT")
            except BaseException:
                try:
                    con.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            finally:
                con.close()

    def rows(self, sql: str, params: Iterable[Any] | dict = ()) -> list[dict[str, Any]]:
        con = self._connect()
        try:
            return [dict(r) for r in con.execute(sql, params).fetchall()]
        finally:
            con.close()

    def one(self, sql: str, params: Iterable[Any] | dict = ()) -> dict[str, Any] | None:
        r = self.rows(sql, params)
        return r[0] if r else None

    def execute(self, sql: str, params: Iterable[Any] | dict = ()) -> int:
        with self.tx() as con:
            return con.execute(sql, params).rowcount

    def ping(self) -> bool:
        try:
            return self.one("SELECT 1 AS ok")["ok"] == 1
        except sqlite3.Error:
            return False

    # ------------------------------------------------------------------ migrations
    def _migrate(self) -> None:
        with self._wlock:
            con = self._connect()
            try:
                con.execute("PRAGMA journal_mode=WAL")
                version = con.execute("PRAGMA user_version").fetchone()[0]
                self.migrated_from = version
                if version >= SCHEMA_VERSION:
                    return
                con.execute("BEGIN IMMEDIATE")
                try:
                    if version < 1:
                        for ddl in LEGACY_DDL:
                            con.execute(ddl)
                        con.execute("CREATE INDEX IF NOT EXISTS idx_samples_captured ON monitor_samples(captured_at)")
                        con.execute("CREATE INDEX IF NOT EXISTS idx_events_started ON incident_events(started_at)")
                        con.execute("CREATE INDEX IF NOT EXISTS idx_incidents_created ON incidents(created_at)")
                        con.execute("CREATE INDEX IF NOT EXISTS idx_qps_query_time ON query_performance_samples(queryid, captured_at)")
                    if version < 2:
                        self._migrate_v2(con)
                    con.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                    con.execute("COMMIT")
                except BaseException:
                    con.execute("ROLLBACK")
                    raise
            finally:
                con.close()

    def _migrate_v2(self, con: sqlite3.Connection) -> None:
        for ddl in V2_DDL:
            con.execute(ddl)
        for col, decl in INCIDENT_COLUMNS.items():
            if col not in _columns(con, "incidents"):
                con.execute(f"ALTER TABLE incidents ADD COLUMN {col} {decl}")
        for col, decl in SNAPSHOT_COLUMNS.items():
            if col not in _columns(con, "snapshots"):
                con.execute(f"ALTER TABLE snapshots ADD COLUMN {col} {decl}")
        # --- legacy data: normalize timestamps, flag demo rows, link snapshots to incidents
        for r in con.execute("SELECT id, created_at, updated_at, findings_json FROM incidents").fetchall():
            demo = 1 if '"demo_mode": true' in (r["findings_json"] or "") or '"demo_mode":true' in (r["findings_json"] or "") else 0
            ca, ua = normalize_ts(r["created_at"]) or r["created_at"], normalize_ts(r["updated_at"]) or r["updated_at"]
            con.execute("UPDATE incidents SET created_at=?, updated_at=?, started_at=COALESCE(started_at, ?), is_demo=?, "
                        "instance_id=CASE WHEN ?=1 THEN 'demo' ELSE instance_id END WHERE id=?", (ca, ua, ca, demo, demo, r["id"]))
        for r in con.execute("SELECT id, captured_at, title, payload_json FROM snapshots").fetchall():
            demo = 1 if '"demo_mode": true' in (r["payload_json"] or "") or '"demo_mode":true' in (r["payload_json"] or "") else 0
            inc = r["title"].split(" ")[0] if r["title"].startswith("DIR-") and r["title"].endswith("evidence capture") else None
            con.execute("UPDATE snapshots SET captured_at=?, is_demo=?, instance_id=CASE WHEN ?=1 THEN 'demo' ELSE instance_id END, "
                        "incident_id=? WHERE id=?", (normalize_ts(r["captured_at"]) or r["captured_at"], demo, demo, inc, r["id"]))
        for r in con.execute("SELECT * FROM incident_events").fetchall():
            cat = CATEGORY_MAP.get(r["event_key"], r["event_key"])
            first = normalize_ts(r["started_at"]) or r["started_at"]
            end = normalize_ts(r["ended_at"])
            details = jload(r["details_json"], {})
            if isinstance(details, dict):
                details = {k: v for k, v in details.items() if k != "details"}  # drop raw v0.6 row payloads (may hold raw SQL)
            demo = 1 if isinstance(details, dict) and details.get("demo_mode") else 0
            con.execute(
                "INSERT OR IGNORE INTO evidence(id, instance_id, category, severity, title, fingerprint, status, first_seen, last_seen, "
                "cleared_at, observation_count, source, collector, collection_status, confidence, limitations, details_json, is_demo, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"EV-LEG-{r['id']:06d}", "demo" if demo else "default", cat, r["severity"] if r["severity"] in ("info", "warning", "critical") else "warning",
                 r["title"], f"legacy:{r['event_key']}:{r['id']}", "cleared", first, end or first, end, 1,
                 "migrated from v0.6 monitor event", "", "ok", "observed",
                 "Migrated from v0.6: sampled observation, end time may be approximate.", jdump(details), demo, first))
        for r in con.execute("SELECT * FROM query_performance_samples").fetchall():
            demo = 1 if r["source"] != "pg_stat_statements" else 0
            text = redact_sql(r["query_text"], "redact") or ""
            fp = sql_fingerprint(text) or r["queryid"]
            con.execute("INSERT OR IGNORE INTO query_texts(fingerprint, text, first_seen) VALUES (?,?,?)", (fp, text, normalize_ts(r["captured_at"]) or r["captured_at"]))
            con.execute("INSERT INTO query_stat_samples(ts, instance_id, queryid, fingerprint, calls, total_exec_ms, rows, source, is_demo) VALUES (?,?,?,?,?,?,?,?,?)",
                        (normalize_ts(r["captured_at"]) or r["captured_at"], "demo" if demo else "default", r["queryid"], fp, r["calls"],
                         r["total_exec_time"], r["rows_returned"], r["source"], demo))

    # ------------------------------------------------------------------ kv + settings
    def kv_get(self, key: str, default: str | None = None) -> str | None:
        r = self.one("SELECT value FROM kv_state WHERE key=?", (key,))
        return r["value"] if r else default

    def kv_set(self, key: str, value: str) -> None:
        self.execute("INSERT INTO kv_state(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                     (key, value, iso()))

    def settings_load(self) -> dict[str, str]:
        return {r["key"]: r["value"] for r in self.rows("SELECT key, value FROM settings_store")}

    def settings_save(self, key: str, value: str) -> None:
        self.execute("INSERT INTO settings_store(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                     (key, value, iso()))

    def settings_delete(self, key: str) -> None:
        self.execute("DELETE FROM settings_store WHERE key=?", (key,))

    # ------------------------------------------------------------------ audit
    def audit_add(self, actor: str, role: str, action: str, target: str, outcome: str, ip: str, details: dict) -> None:
        self.execute("INSERT INTO audit_log(ts,actor,role,action,target,outcome,ip,details_json) VALUES(?,?,?,?,?,?,?,?)",
                     (iso(), actor, role, action, target, outcome, ip, jdump(details, 8000)))

    def audit_list(self, *, action: str | None = None, outcome: str | None = None, q: str | None = None,
                   t_from: str | None = None, t_to: str | None = None, limit: int = 50, offset: int = 0) -> tuple[list[dict], int]:
        where, p = ["1=1"], []
        if action:
            where.append("action LIKE ? ESCAPE '\\'"); p.append(like_escape(action) + "%")
        if outcome:
            where.append("outcome=?"); p.append(outcome)
        if q:
            where.append("instr(lower(actor||' '||action||' '||target||' '||details_json), ?) > 0"); p.append(q.lower())
        if t_from:
            where.append("ts >= ?"); p.append(t_from)
        if t_to:
            where.append("ts <= ?"); p.append(t_to)
        w = " AND ".join(where)
        total = self.one(f"SELECT count(*) AS n FROM audit_log WHERE {w}", p)["n"]
        items = self.rows(f"SELECT * FROM audit_log WHERE {w} ORDER BY id DESC LIMIT ? OFFSET ?", [*p, limit, offset])
        for it in items:
            it["details"] = jload(it.pop("details_json"), {})
        return items, total

    # ------------------------------------------------------------------ metrics
    def metrics_add(self, instance_id: str, ts: str, values: dict[str, float | None], is_demo: bool = False) -> int:
        rows = [(ts, instance_id, k, float(v), int(is_demo)) for k, v in values.items() if v is not None]
        if rows:
            with self.tx() as con:
                con.executemany("INSERT INTO metrics(ts,instance_id,name,value,is_demo) VALUES (?,?,?,?,?)", rows)
        return len(rows)

    def metrics_series(self, instance_id: str, names: list[str], t_from: str, t_to: str, step_s: int,
                       is_demo: bool = False) -> dict[str, list[dict[str, Any]]]:
        if not names:
            return {}
        step = max(1, int(step_s))
        ph = ",".join("?" for _ in names)
        rows = self.rows(
            f"SELECT name, (CAST(strftime('%s', ts) AS INTEGER) / {step}) * {step} AS b, avg(value) AS avg, max(value) AS max, "
            f"min(value) AS min, count(*) AS n FROM metrics WHERE instance_id=? AND is_demo=? AND name IN ({ph}) AND ts>=? AND ts<=? "
            f"GROUP BY name, b ORDER BY b", [instance_id, int(is_demo), *names, t_from, t_to])
        out: dict[str, list[dict[str, Any]]] = {n: [] for n in names}
        for r in rows:
            out[r["name"]].append({"ts": iso(datetime.fromtimestamp(r["b"], tz=utcnow().tzinfo)), "avg": r["avg"], "max": r["max"], "min": r["min"], "n": r["n"]})
        return out

    def metrics_latest(self, instance_id: str, names: list[str], is_demo: bool = False) -> dict[str, dict[str, Any]]:
        out = {}
        for n in names:
            r = self.one("SELECT ts, value FROM metrics WHERE instance_id=? AND is_demo=? AND name=? ORDER BY ts DESC LIMIT 1", (instance_id, int(is_demo), n))
            if r:
                out[n] = r
        return out

    # ------------------------------------------------------------------ collection runs
    def run_add(self, instance_id: str, tier: str, trigger: str, started_at: str, finished_at: str | None, duration_ms: int,
                status: str, collectors: dict[str, Any], error: str | None, is_demo: bool = False) -> int:
        with self.tx() as con:
            cur = con.execute("INSERT INTO collection_runs(instance_id,tier,trigger,started_at,finished_at,duration_ms,status,collectors_json,error,is_demo) "
                              "VALUES (?,?,?,?,?,?,?,?,?,?)", (instance_id, tier, trigger, started_at, finished_at, duration_ms, status, jdump(collectors, 16000), error, int(is_demo)))
            return cur.lastrowid

    def runs_list(self, instance_id: str | None = None, tier: str | None = None, status: str | None = None,
                  t_from: str | None = None, t_to: str | None = None, limit: int = 100, offset: int = 0,
                  is_demo: bool = False) -> tuple[list[dict], int]:
        where, p = ["is_demo=?"], [int(is_demo)]
        for col, val in (("instance_id", instance_id), ("tier", tier), ("status", status)):
            if val:
                where.append(f"{col}=?"); p.append(val)
        if t_from:
            where.append("started_at>=?"); p.append(t_from)
        if t_to:
            where.append("started_at<=?"); p.append(t_to)
        w = " AND ".join(where)
        total = self.one(f"SELECT count(*) AS n FROM collection_runs WHERE {w}", p)["n"]
        items = self.rows(f"SELECT * FROM collection_runs WHERE {w} ORDER BY started_at DESC, id DESC LIMIT ? OFFSET ?", [*p, limit, offset])
        for it in items:
            it["collectors"] = jload(it.pop("collectors_json"), {})
        return items, total

    def run_times(self, instance_id: str, tier: str, t_from: str, t_to: str, is_demo: bool = False, only_ok: bool = True) -> list[dict]:
        st = "AND status IN ('ok','partial')" if only_ok else ""
        return self.rows(f"SELECT started_at, status FROM collection_runs WHERE instance_id=? AND tier=? AND is_demo=? AND started_at>=? AND started_at<=? {st} ORDER BY started_at",
                         (instance_id, tier, int(is_demo), t_from, t_to))

    # ------------------------------------------------------------------ query statistics
    def qstats_add(self, instance_id: str, ts: str, rows: list[dict[str, Any]], is_demo: bool = False) -> int:
        if not rows:
            return 0
        with self.tx() as con:
            for r in rows:
                if r.get("fingerprint") and r.get("text"):
                    con.execute("INSERT OR IGNORE INTO query_texts(fingerprint,text,first_seen) VALUES (?,?,?)", (r["fingerprint"], r["text"][:2000], ts))
            con.executemany(
                "INSERT INTO query_stat_samples(ts,instance_id,dbid,userid,queryid,fingerprint,calls,total_exec_ms,max_exec_ms,rows,shared_hit,shared_read,temp_written,stats_reset,source,is_demo) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [(ts, instance_id, str(r.get("dbid", "")), str(r.get("userid", "")), str(r["queryid"]), r.get("fingerprint"), float(r["calls"]),
                  float(r["total_exec_ms"]), r.get("max_exec_ms"), r.get("rows"), r.get("shared_hit"), r.get("shared_read"), r.get("temp_written"),
                  r.get("stats_reset"), r.get("source", "pg_stat_statements"), int(is_demo)) for r in rows])
        return len(rows)

    def qstats_samples(self, instance_id: str, t_from: str, t_to: str, is_demo: bool = False, queryid: str | None = None) -> list[dict]:
        q = "SELECT s.*, t.text FROM query_stat_samples s LEFT JOIN query_texts t ON t.fingerprint = s.fingerprint WHERE s.instance_id=? AND s.is_demo=? AND s.ts>=? AND s.ts<=?"
        p: list[Any] = [instance_id, int(is_demo), t_from, t_to]
        if queryid:
            q += " AND s.queryid=?"; p.append(queryid)
        return self.rows(q + " ORDER BY s.ts", p)

    def table_samples_add(self, instance_id: str, ts: str, rows: list[dict[str, Any]], is_demo: bool = False) -> None:
        if rows:
            with self.tx() as con:
                con.executemany("INSERT INTO table_samples(ts,instance_id,relation,size_bytes,n_live,n_dead,is_demo) VALUES (?,?,?,?,?,?,?)",
                                [(ts, instance_id, r["relation"], r.get("size_bytes"), r.get("n_live"), r.get("n_dead"), int(is_demo)) for r in rows])

    def table_growth(self, instance_id: str, t_from: str, t_to: str, is_demo: bool = False) -> list[dict]:
        rows = self.rows("SELECT relation, ts, size_bytes FROM table_samples WHERE instance_id=? AND is_demo=? AND ts>=? AND ts<=? ORDER BY ts", (instance_id, int(is_demo), t_from, t_to))
        by: dict[str, list[dict]] = {}
        for r in rows:
            by.setdefault(r["relation"], []).append(r)
        out = []
        for rel, pts in by.items():
            if len(pts) < 2 or pts[0]["size_bytes"] is None or pts[-1]["size_bytes"] is None:
                continue
            span = (parse_ts(pts[-1]["ts"]) - parse_ts(pts[0]["ts"])).total_seconds()
            out.append({"relation": rel, "first_ts": pts[0]["ts"], "last_ts": pts[-1]["ts"], "first_bytes": pts[0]["size_bytes"],
                        "last_bytes": pts[-1]["size_bytes"], "growth_bytes": pts[-1]["size_bytes"] - pts[0]["size_bytes"],
                        "growth_per_day_bytes": (pts[-1]["size_bytes"] - pts[0]["size_bytes"]) / span * 86400 if span > 0 else None, "samples": len(pts)})
        return sorted(out, key=lambda r: -(r["growth_bytes"] or 0))

    # ------------------------------------------------------------------ retention + stats
    def retention_run(self, days: dict[str, float], dry_run: bool = False, now: datetime | None = None) -> dict[str, int]:
        now = now or utcnow()
        cut = lambda key: iso(now - timedelta(days=days[key]))
        plan = {
            "metrics": ("DELETE FROM metrics WHERE ts < ?", "SELECT count(*) FROM metrics WHERE ts < ?", cut("retention_metrics_days")),
            "collection_runs": ("DELETE FROM collection_runs WHERE started_at < ?", "SELECT count(*) FROM collection_runs WHERE started_at < ?", cut("retention_runs_days")),
            "query_stat_samples": ("DELETE FROM query_stat_samples WHERE ts < ?", "SELECT count(*) FROM query_stat_samples WHERE ts < ?", cut("retention_query_stats_days")),
            "table_samples": ("DELETE FROM table_samples WHERE ts < ?", "SELECT count(*) FROM table_samples WHERE ts < ?", cut("retention_metrics_days")),
            "evidence": ("DELETE FROM evidence WHERE status='cleared' AND COALESCE(cleared_at,last_seen) < ? AND id NOT IN (SELECT evidence_id FROM incident_evidence)",
                         "SELECT count(*) FROM evidence WHERE status='cleared' AND COALESCE(cleared_at,last_seen) < ? AND id NOT IN (SELECT evidence_id FROM incident_evidence)", cut("retention_evidence_days")),
            "snapshots": ("DELETE FROM snapshots WHERE captured_at < ? AND incident_id IS NULL", "SELECT count(*) FROM snapshots WHERE captured_at < ? AND incident_id IS NULL", cut("retention_snapshots_days")),
            "audit_log": ("DELETE FROM audit_log WHERE ts < ?", "SELECT count(*) FROM audit_log WHERE ts < ?", cut("retention_audit_days")),
            "legacy_monitor_samples": ("DELETE FROM monitor_samples WHERE captured_at < ?", "SELECT count(*) FROM monitor_samples WHERE captured_at < ?", cut("retention_metrics_days")),
        }
        out: dict[str, int] = {}
        with self.tx() as con:
            for name, (delete_sql, count_sql, cutoff) in plan.items():
                out[name] = con.execute(count_sql, (cutoff,)).fetchone()[0] if dry_run else con.execute(delete_sql, (cutoff,)).rowcount
            if not dry_run:
                con.execute("DELETE FROM query_texts WHERE fingerprint NOT IN (SELECT DISTINCT fingerprint FROM query_stat_samples WHERE fingerprint IS NOT NULL)")
        return out

    def stats(self) -> dict[str, Any]:
        tables = ["metrics", "collection_runs", "evidence", "incidents", "incident_notes", "query_stat_samples", "table_samples", "snapshots", "audit_log"]
        counts = {t: self.one(f"SELECT count(*) AS n FROM {t}")["n"] for t in tables}
        size = self.path.stat().st_size if self.path.exists() else 0
        wal = Path(str(self.path) + "-wal")
        return {"path_name": self.path.name, "size_bytes": size + (wal.stat().st_size if wal.exists() else 0), "rows": counts,
                "schema_version": SCHEMA_VERSION, "migrated_from_version": self.migrated_from}
