"""Synthetic demo data source (DEMO_MODE=true). Never used for live monitoring and stored in a separate SQLite file.

It produces collector-shaped data for a repeating 40-minute scenario (normal -> idle-in-transaction blocking pile-up with connection pressure
and a slow query -> resolution) and flows through the same graph, analysis, evidence and incident code as real data.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Any

from .pg.collectors import CResult, summarize_connections
from .pg.graph import build_blocking_graph
from .security import sql_fingerprint
from .timeutil import iso, utcnow
from .util import jsonable

CYCLE_S = 2400
BLOCK_START, BLOCK_END = 840, 1800        # seconds into the cycle
EPOCH = datetime(2026, 1, 1, tzinfo=utcnow().tzinfo)


def phase(ts: datetime) -> tuple[float, int]:
    """Return (seconds into cycle, cycle index)."""
    s = (ts - EPOCH).total_seconds() + float(os.getenv("DEMO_PHASE_OFFSET_S", "0") or 0)    # test hook: shift the scenario clock
    return s % CYCLE_S, int(s // CYCLE_S)


class DemoSource:
    def info(self) -> dict[str, Any]:
        return {"version_num": 160004, "version_string": "PostgreSQL 16.4 (synthetic demo)", "major": 16, "supported": True, "in_recovery": False, "current_user": "demo",
                "database": "demo", "is_superuser": False, "pg_monitor": True, "pg_read_all_stats": True, "pg_read_all_settings": True, "ssl_in_use": True,
                "ssl_version": "TLSv1.3", "tls_verified": False, "pg_stat_statements": {"extname": "pg_stat_statements", "extversion": "1.10", "schema": "public"}}

    # ------------------------------------------------------------------ generators
    def generate(self, tier: str, ts: datetime, rt) -> dict[str, CResult]:
        fn = {"fast": self._fast, "standard": self._standard, "slow": self._slow}[tier]
        return fn(ts, rt)

    def _fast(self, ts: datetime, rt) -> dict[str, CResult]:
        sec, cyc = phase(ts)
        base = EPOCH + timedelta(seconds=cyc * CYCLE_S)     # backend start is stable within a scenario cycle, like a real backend
        rows: list[dict[str, Any]] = []

        def row(pid, app, user, state, query, *, xact=None, qage=0.0, blocked=None, wait=("Client", "ClientRead")):
            return {"pid": pid, "datname": "demo", "usename": user, "application_name": app, "client_addr": f"10.0.1.{pid % 200}", "backend_type": "client backend", "state": state,
                    "wait_event_type": wait[0], "wait_event": wait[1], "backend_start": iso(base), "xact_start": iso(ts - timedelta(seconds=xact)) if xact is not None else None,
                    "query_start": iso(ts - timedelta(seconds=qage)), "state_change": iso(ts - timedelta(seconds=qage)), "xact_age_s": xact, "query_age_s": qage, "state_age_s": qage,
                    "xid_age": None, "xmin_age": None, "query_id": None, "query": query, "query_fingerprint": sql_fingerprint(query), "blocked_by": blocked or []}
        for i in range(18):
            active = (int(sec) + i) % 7 == 0
            q = "SELECT * FROM orders WHERE customer_id = ?" if active else "COMMIT"
            rows.append(row(4000 + i, "shop-api", "shop_app", "active" if active else "idle", q, qage=0.2 if active else 5.0, wait=("", "") if active else ("Client", "ClientRead")))
        if BLOCK_START <= sec < BLOCK_END:
            t = sec - BLOCK_START
            rows.append(row(4242, "billing-job", "billing", "idle in transaction", "UPDATE accounts SET balance = balance - ? WHERE id = ?", xact=t + 40, qage=t + 38))
            n = min(60, int(t / 9))
            for i in range(n):
                rows.append(row(5000 + i, "shop-api", "shop_app", "active", "UPDATE accounts SET last_seen = now() WHERE id = ?", xact=t - i * 9 + 1, qage=max(1.0, t - i * 9),
                                blocked=[4242], wait=("Lock", "transactionid")))
            if t > 300:
                rows.append(row(6001, "reporting", "analyst", "active", "SELECT count(*) FROM events e JOIN sessions s ON s.id = e.session_id", xact=t - 300, qage=t - 300, wait=("IO", "DataFileRead")))
        graph_locks = []
        if graph_pids := [r["pid"] for r in rows if r["blocked_by"]]:
            graph_locks = [{"pid": 4242, "locktype": "transactionid", "mode": "ExclusiveLock", "granted": True, "relation_name": None, "transactionid": "918273", "page": None, "tuple": None}] + \
                          [{"pid": p, "locktype": "transactionid", "mode": "ShareLock", "granted": False, "relation_name": None, "transactionid": "918273", "page": None, "tuple": None} for p in graph_pids[:20]]
        graph = build_blocking_graph(rows, graph_locks)
        conn_rows = []
        for r in rows:
            conn_rows.append({"datname": r["datname"], "usename": r["usename"], "application_name": r["application_name"], "state": r["state"], "backend_type": "client backend", "sessions": 1})
        merged: dict[tuple, dict] = {}
        for c in conn_rows:
            k = (c["datname"], c["usename"], c["application_name"], c["state"])
            merged.setdefault(k, {**c, "sessions": 0})["sessions"] += 1
        conn = summarize_connections(list(merged.values()), 100, 3, rows)
        waits = [{"wait_event_type": "Lock", "wait_event": "transactionid", "state": "active", "sessions": sum(1 for r in rows if r["wait_event_type"] == "Lock")}] if graph_pids else []
        return {"activity": CResult("activity", "ok", {"rows": rows, "truncated": False, "connections": conn, "waits": waits, "blocking": graph}, duration_ms=4)}

    def _standard(self, ts: datetime, rt) -> dict[str, CResult]:
        sec, cyc = phase(ts)
        t = (ts - EPOCH).total_seconds()
        db = {"datname": "demo", "is_current": True, "numbackends": 24, "xact_commit": 5_000_000 + t * 120, "xact_rollback": 5000 + t * 0.4, "blks_read": 90000 + t * 3, "blks_hit": 9_000_000 + t * 800,
              "tup_returned": 2e8 + t * 9000, "tup_fetched": 5e7 + t * 2000, "tup_inserted": 1e6 + t * 14, "tup_updated": 6e5 + t * 9, "tup_deleted": 1e5 + t * 2, "conflicts": 0,
              "temp_files": 12 + int(t / 900), "temp_bytes": 5e7 + t * 2000, "deadlocks": 3 + (1 if cyc % 3 == 0 and sec > 1700 else 0) + cyc // 3, "checksum_failures": 0,
              "blk_read_time": 0, "blk_write_time": 0, "stats_reset": iso(EPOCH), "size_bytes": 4_200_000_000 + t * 300, "cache_hit_pct": 99.0}
        bg = {"checkpoints_timed": 400 + t / 300, "checkpoints_req": 12 + t / 5000, "checkpoint_write_ms": 1e6 + t * 50, "checkpoint_sync_ms": 3e4 + t, "buffers_checkpoint": 1e6 + t * 40,
              "buffers_clean": 5e4 + t, "maxwritten_clean": 0, "buffers_alloc": 2e6 + t * 60, "buffers_backend": 8e4 + t * 2, "stats_reset": iso(EPOCH)}
        dbres = CResult("dbstats", "ok", {"databases": [db], "current": db, "bgwriter": bg, "wal": {"wal_stats": {"wal_bytes": 3e10 + t * 40000, "wal_fpi": 1e6 + t * 5, "wal_records": 1e8 + t * 300}, "wal_lsn_bytes": 5e11 + t * 60000},
                                           "wal_dir": None, "archiver": {"archived_count": 90000 + t / 20, "failed_count": 0, "last_failed_wal": None, "last_failed_time": None, "last_archived_time": iso(ts)}, "note": "synthetic"}, duration_ms=6)
        lag_phase = 1000 <= sec < 1500
        lag = 0.4 + (min(45.0, (sec - 1000) / 8) if lag_phase else 0.0)
        stand = [{"pid": 7001, "usename": "replicator", "application_name": "replica-1", "client_addr": "10.0.5.12", "state": "streaming", "sync_state": "async", "sync_priority": 0,
                  "sent_lag_bytes": 2e3, "write_lag_bytes": 3e3, "flush_lag_bytes": 9e3, "replay_lag_bytes": lag * 2.5e6, "total_lag_bytes": lag * 2.6e6, "write_lag_s": 0.01, "flush_lag_s": 0.02,
                  "replay_lag_s": lag, "connected_s": 86400.0, "reply_time": iso(ts), "xmin_age": 120}]
        slots = [{"slot_name": "replica_1", "plugin": None, "slot_type": "physical", "database": None, "temporary": False, "active": True, "active_pid": 7001, "xmin": None, "catalog_xmin": None,
                  "xmin_age": None, "catalog_xmin_age": None, "restart_lsn": "5/AA000000", "confirmed_flush_lsn": None, "retained_bytes": 3e6 + lag * 1e6, "wal_status": "reserved", "safe_wal_size": None},
                 {"slot_name": "cdc_legacy", "plugin": "pgoutput", "slot_type": "logical", "database": "demo", "temporary": False, "active": False, "active_pid": None, "xmin": None, "catalog_xmin": "7300111",
                  "xmin_age": None, "catalog_xmin_age": 52_000_000, "restart_lsn": "4/10000000", "confirmed_flush_lsn": "4/10000000", "retained_bytes": 1.3e9 + sec * 60000, "wal_status": "extended", "safe_wal_size": None}]
        rep = CResult("replication", "ok", {"role": "primary", "in_recovery": False, "standbys": stand, "slots": slots, "receiver": None, "recovery": None, "logical_subscriptions": [],
                                            "settings": {"max_wal_senders": "10", "max_replication_slots": "10", "max_slot_wal_keep_size": "-1", "wal_keep_size": "0"}, "note": "synthetic"}, duration_ms=5)
        qs = []
        specs = [("1001", "SELECT o.id, o.total FROM orders o WHERE o.customer_id = ?", 350, 0.9), ("1002", "INSERT INTO orders (customer_id, total) VALUES (?, ?)", 60, 1.4),
                 ("1003", "UPDATE accounts SET last_seen = now() WHERE id = ?", 120, 0.5), ("1004", "SELECT count(*) FROM events e JOIN sessions s ON s.id = e.session_id", 4, 80.0)]
        blocking = BLOCK_START <= sec < BLOCK_END
        for qid, text, rate, mean in specs:
            m = mean * (6 if (blocking and qid == "1003") else 1)
            calls = rate * (t / 60)
            blocked_ms = 0.0
            if qid == "1003":       # time lost to the lock pile-up accrues only while it lasts, and is cumulative across cycles (counters never go backwards)
                per_cycle = (BLOCK_END - BLOCK_START) * 3000.0
                blocked_ms = cyc * per_cycle + max(0.0, min(1.0, (sec - BLOCK_START) / (BLOCK_END - BLOCK_START))) * per_cycle
            qs.append({"queryid": qid, "dbid": "16384", "userid": "10", "fingerprint": sql_fingerprint(text), "text": text, "calls": calls, "total_exec_ms": calls * mean + blocked_ms,
                       "mean_exec_ms": m, "max_exec_ms": m * 20, "rows": calls * 3, "shared_hit": calls * 40, "shared_read": calls * 0.5, "temp_written": 0})
        st = CResult("statements", "ok", {"queries": qs, "info": {"dealloc": 0, "stats_reset": iso(EPOCH)}, "hidden_text": 0, "schema": "public", "stats_reset": iso(EPOCH), "note": "synthetic"}, duration_ms=8)
        return {"dbstats": dbres, "replication": rep, "statements": st}

    def _slow(self, ts: datetime, rt) -> dict[str, CResult]:
        sec, cyc = phase(ts)
        t = (ts - EPOCH).total_seconds()
        dead = 40_000 + (t % 7200) * 55
        tables = [{"schemaname": "public", "relname": "events", "n_live_tup": 8e6, "n_dead_tup": dead, "n_mod_since_analyze": dead * 2, "n_ins_since_vacuum": 1e5, "last_vacuum": None,
                   "last_autovacuum": iso(ts - timedelta(hours=3)), "last_analyze": None, "last_autoanalyze": iso(ts - timedelta(hours=1)), "vacuum_count": 0, "autovacuum_count": 14, "total_bytes": 3.1e9 + t * 150,
                   "table_bytes": 2.4e9, "reloptions": None, "relfrozenxid_age": 80_000_000, "dead_ratio_pct": round(100 * dead / (8e6 + dead), 1)},
                  {"schemaname": "public", "relname": "accounts", "n_live_tup": 2e6, "n_dead_tup": 30_000 + (t % 1800) * 40, "n_mod_since_analyze": 1e5, "n_ins_since_vacuum": 100, "last_vacuum": None,
                   "last_autovacuum": iso(ts - timedelta(minutes=8)), "last_analyze": None, "last_autoanalyze": iso(ts - timedelta(minutes=8)), "vacuum_count": 0, "autovacuum_count": 220, "total_bytes": 7e8,
                   "table_bytes": 5e8, "reloptions": "{autovacuum_vacuum_scale_factor=0.02}", "relfrozenxid_age": 40_000_000, "dead_ratio_pct": 2.0}]
        vac = {"databases_xid": [{"datname": "demo", "xid_age": 180_000_000 + int(t * 12), "mxid_age": 1_200_000}], "tables": tables,
               "table_sizes": [{"relation": "public.events", "size_bytes": 3.1e9 + t * 150, "n_live": 8e6, "n_dead": dead}, {"relation": "public.accounts", "size_bytes": 7e8 + t * 20, "n_live": 2e6, "n_dead": 30000}],
               "vacuum_progress": [], "maintenance": {"create_index": [], "cluster": []}, "prepared_xacts": [],
               "xmin_holders": {"backends": [{"pid": 4242, "usename": "billing", "application_name": "billing-job", "state": "idle in transaction", "backend_type": "client backend", "xmin_age": 90_000_000, "xact_age_s": 900}] if BLOCK_START <= sec < BLOCK_END else [],
                                "slots": [{"slot_name": "cdc_legacy", "slot_type": "logical", "active": False, "xmin_age": None, "catalog_xmin_age": 52_000_000}], "prepared": []},
               "settings": {"autovacuum_freeze_max_age": "200000000", "autovacuum_multixact_freeze_max_age": "400000000", "autovacuum_max_workers": "3", "autovacuum_naptime": "60"}, "note": "synthetic"}
        settings = {"max_connections": ("100", None), "autovacuum": ("on", None), "log_lock_waits": ("off", None), "idle_in_transaction_session_timeout": ("0" if cyc >= 1 or sec > 600 else "900000", "ms"),
                    "deadlock_timeout": ("1000", "ms"), "work_mem": ("4096", "kB"), "shared_buffers": ("16384", "8kB"), "max_wal_size": ("1024", "MB"), "fsync": ("on", None)}
        sdata = {"settings": {k: {"setting": v[0], "unit": v[1], "source": "configuration file", "pending_restart": False, "context": "sighup"} for k, v in settings.items()},
                 "hash": f"demo{'1' if (cyc >= 1 or sec > 600) else '0'}", "count": len(settings)}
        server = self.info()
        return {"server": CResult("server", "ok", jsonable(server), duration_ms=3), "vacuum": CResult("vacuum", "ok", vac, duration_ms=9), "settings": CResult("settings", "ok", sdata, duration_ms=2)}

    # ------------------------------------------------------------------ history
    def seed(self, recorder, now: datetime | None = None, hours: float = 2.0) -> int:
        """Backfill labelled synthetic history so charts and the timeline are populated on first start."""
        now = (now or utcnow()).replace(microsecond=0)
        start = now - timedelta(hours=hours)
        steps = []
        for tier, step in (("slow", 600), ("standard", 120), ("fast", 30)):
            t = start
            while t < now:
                steps.append((t, tier))
                t += timedelta(seconds=step)
        order = {"slow": 0, "standard": 1, "fast": 2}
        steps.sort(key=lambda x: (x[0], order[x[1]]))
        iid = next(iter(recorder.rt))
        recorder.seeding = True
        try:
            for t, tier in steps:
                recorder.run_tier_sync(iid, tier, "seed", ts=t, wait=True)
            recorder.store.kv_set("demo_seeded_at", iso(now))
        finally:
            recorder.seeding = False
        return len(steps)
