"""Turn collector output into (a) compact metrics and (b) evidence candidates.

Rules are deliberately conservative and context-aware: each candidate carries observed facts, thresholds used and limitations.
Nothing here claims a root cause; hypotheses are labelled as such via `confidence`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..config import Settings
from ..util import fmt_bytes, fmt_dur
from .collectors import CResult

RISKY_SETTINGS = {"autovacuum", "fsync", "full_page_writes", "synchronous_commit", "max_connections", "wal_level", "archive_mode",
                  "idle_in_transaction_session_timeout", "statement_timeout", "lock_timeout", "track_counts", "track_activities",
                  "max_wal_size", "shared_buffers", "work_mem", "max_wal_senders", "max_replication_slots", "synchronous_standby_names"}
DOC = "https://www.postgresql.org/docs/current/"


@dataclass
class Analysis:
    candidates: list[dict[str, Any]] = field(default_factory=list)
    metrics: dict[str, float | None] = field(default_factory=dict)
    table_samples: list[dict[str, Any]] = field(default_factory=list)
    qstats: list[dict[str, Any]] = field(default_factory=list)
    state_updates: list[tuple[str, Any]] = field(default_factory=list)


def mk(category: str, severity: str, title: str, fingerprint: str, collector: str, source: str, details: dict[str, Any],
       *, pid: int | None = None, confidence: str = "observed", limitations: str = "", peaks: dict[str, float] | None = None,
       query_fingerprint: str | None = None, **extra: Any) -> dict[str, Any]:
    return {"category": category, "severity": severity, "title": title, "fingerprint": fingerprint, "collector": collector, "source": source,
            "details": details, "pid": pid, "confidence": confidence, "limitations": limitations, "peaks": peaks or {},
            "query_fingerprint": query_fingerprint, **extra}


def _brief(n: dict[str, Any]) -> dict[str, Any]:
    keys = ("pid", "datname", "usename", "application_name", "client_addr", "state", "wait_event_type", "wait_event", "xact_age_s", "query_age_s", "wait_s",
            "query", "query_fingerprint", "direct_blockers", "waiting_for", "holds", "role")
    return {k: n.get(k) for k in keys if k in n}


# --------------------------------------------------------------------------- fast tier
def analyze_activity(T: Settings, data: dict[str, Any], ts: datetime) -> tuple[list[dict], dict[str, float | None]]:
    rows, conn, graph = data["rows"], data["connections"], data["blocking"]
    clients = [r for r in rows if r["backend_type"] == "client backend"]
    active = [r for r in clients if r["state"] == "active"]
    idle_txn = [r for r in clients if (r["state"] or "").startswith("idle in transaction")]
    cands: list[dict] = []
    metrics = {
        "sessions_total": conn["client_backends"], "sessions_active": len(active), "sessions_idle": sum(1 for r in clients if r["state"] == "idle"),
        "sessions_idle_in_txn": len(idle_txn), "sessions_waiting_lock": conn["waiting_on_locks"], "blocked_sessions": graph["blocked_sessions"],
        "root_blockers": graph["root_blockers"], "connection_slots_used": conn["slots_used"], "conn_utilization_pct": conn["utilization_percent"],
        "autovacuum_workers": conn["autovacuum_workers"],
        "longest_query_s": max((r["query_age_s"] or 0 for r in active), default=0.0),
        "longest_xact_s": max((r["xact_age_s"] or 0 for r in clients), default=0.0),
        "longest_idle_txn_s": max((r["state_age_s"] or 0 for r in idle_txn), default=0.0),
    }
    by_pid = {r["pid"]: r for r in rows}
    src = "pg_stat_activity + pg_blocking_pids() + pg_locks"
    lim_block = ("pg_blocking_pids() and pg_locks are point-in-time snapshots of the lock manager. Wait duration is approximated by statement age "
                 "because PostgreSQL does not expose when a wait began. Blocking that ended between collections is not seen.")
    # --- blocking, one evidence record per root blocker
    for root in graph["roots"]:
        wait = root.get("max_wait_s") or 0
        n = root["blocked_count"]
        if wait < T.t("block_min_s"):
            continue
        sev = "critical" if (wait >= T.t("block_crit_s") or n >= T.t("block_crit_sessions")) else "warning" if wait >= T.t("block_warn_s") else "info"
        blocked_nodes = sorted((x for x in graph["nodes"] if x["role"] != "root_blocker"), key=lambda x: -(x.get("wait_s") or 0))
        r_row = by_pid.get(root["pid"], {})
        idle = (root.get("state") or "").startswith("idle in transaction")
        cands.append(mk("blocking", sev, f"PID {root['pid']} is blocking {n} session{'s' if n != 1 else ''} (longest wait ≥ {fmt_dur(wait)})",
                        f"blocking:{root.get('datname')}:{root['pid']}:{r_row.get('backend_start')}", "activity", src,
                        {"root": _brief(root), "blocked_count": n, "direct_blocked_count": root["direct_blocked_count"], "max_wait_s": wait, "depth": root["depth"],
                         "chain": root["chain"], "root_is_idle_in_transaction": idle,
                         "blocked_sessions": [_brief(x) for x in blocked_nodes[:15] if x["pid"] in set(root["blocked_pids"])],
                         "relations_waited_on": sorted({w["relation_name"] for x in blocked_nodes for w in (x.get("waiting_for") or []) if w.get("relation_name")})[:10],
                         "root_holds": root.get("holds", []), "thresholds": {"warn_s": T.t("block_warn_s"), "crit_s": T.t("block_crit_s"), "crit_sessions": T.t("block_crit_sessions")}},
                        pid=root["pid"], limitations=lim_block, peaks={"blocked_count": n, "max_wait_s": wait}, query_fingerprint=root.get("query_fingerprint")))
    for cyc in graph["cycles"]:
        cands.append(mk("blocking", "critical", f"Lock wait cycle between PIDs {', '.join(map(str, cyc))}", f"blocking-cycle:{'-'.join(map(str, cyc))}", "activity", src,
                        {"cycle": cyc, "note": "A cycle of sessions each waiting for another. PostgreSQL's deadlock detector normally resolves lock cycles after deadlock_timeout; "
                                               "a persisting cycle may involve non-heavyweight waits."}, pid=cyc[0], limitations=lim_block))
    # --- long-running active queries (sessions waiting on a lock are victims, represented by the blocking evidence)
    longq = sorted((r for r in active if (r["query_age_s"] or 0) >= T.t("long_query_s") and not r["blocked_by"] and not (r["query"] or "").startswith("autovacuum:")),
                   key=lambda r: -(r["query_age_s"] or 0))[:20]
    for r in longq:
        cands.append(mk("long_query", "warning", f"PID {r['pid']} has run a query for {fmt_dur(r['query_age_s'])}",
                        f"longq:{r['pid']}:{r['backend_start']}:{r['query_start']}", "activity", "pg_stat_activity",
                        {**_brief(r), "waiting_on_lock": bool(r["blocked_by"]), "blocked_by": r["blocked_by"], "threshold_s": T.t("long_query_s")},
                        pid=r["pid"], query_fingerprint=r["query_fingerprint"], peaks={"query_age_s": r["query_age_s"] or 0},
                        limitations="A long query is not necessarily a problem (reports, maintenance, batch jobs). Duration is the age of the current statement."))
    # --- idle in transaction
    for r in sorted(idle_txn, key=lambda r: -(r["state_age_s"] or 0))[:20]:
        idle_for = r["state_age_s"] or 0
        if idle_for < T.t("idle_txn_s"):
            continue
        roots = {x["pid"] for x in graph["roots"]}
        blocking = r["pid"] in roots
        sev = "critical" if idle_for >= 3600 else "warning"     # a blocking root is already covered by the blocking evidence
        cands.append(mk("idle_in_transaction", sev, f"PID {r['pid']} idle in transaction for {fmt_dur(idle_for)}"
                        + (" while blocking other sessions" if blocking else ""),
                        f"idle:{r['pid']}:{r['backend_start']}:{r['xact_start']}", "activity", "pg_stat_activity",
                        {**_brief(r), "idle_for_s": idle_for, "blocking_others": blocking, "threshold_s": T.t("idle_txn_s"),
                         "last_statement": r["query"]}, pid=r["pid"], query_fingerprint=r["query_fingerprint"], peaks={"idle_for_s": idle_for},
                        limitations="`query` shows the last statement executed in the session, not necessarily the statement that took the locks."))
    # --- old transactions that are active (not idle) - these also hold back vacuum
    for r in sorted((r for r in active if (r["xact_age_s"] or 0) >= T.t("old_xact_s")), key=lambda r: -(r["xact_age_s"] or 0))[:10]:
        cands.append(mk("long_transaction", "warning", f"PID {r['pid']} has held a transaction open for {fmt_dur(r['xact_age_s'])}",
                        f"xact:{r['pid']}:{r['backend_start']}:{r['xact_start']}", "activity", "pg_stat_activity",
                        {**_brief(r), "xact_age_s": r["xact_age_s"], "xmin_age": r.get("xmin_age"), "threshold_s": T.t("old_xact_s")}, pid=r["pid"],
                        query_fingerprint=r["query_fingerprint"], peaks={"xact_age_s": r["xact_age_s"] or 0},
                        limitations="An old transaction delays vacuum cleanup of rows newer than its snapshot; whether that matters depends on write churn."))
    # --- connection saturation
    util = conn["utilization_percent"]
    if util is not None and util >= T.t("conn_warn_pct"):
        full = conn["available_for_regular_users"] is not None and conn["available_for_regular_users"] <= 0
        sev = "critical" if util >= T.t("conn_crit_pct") or full else "warning"
        waiting_share = conn["waiting_on_locks"] / max(1, conn["client_backends"])
        cands.append(mk("connection_saturation", sev, f"Connection slots {util:.0f}% used ({conn['slots_used']} of {conn['max_connections']})",
                        "connsat", "activity", "pg_stat_activity + pg_settings",
                        {**{k: conn[k] for k in ("slots_used", "max_connections", "reserved_connections", "available_for_regular_users", "client_backends", "walsenders")},
                         "utilization_percent": util, "by_state": conn["by_state"], "by_application": conn["by_application"], "by_user": conn["by_user"],
                         "by_database": conn["by_database"], "waiting_on_locks": conn["waiting_on_locks"],
                         "possible_symptom_of_blocking": waiting_share >= 0.3 and conn["waiting_on_locks"] >= 3,
                         "thresholds": {"warn_pct": T.t("conn_warn_pct"), "crit_pct": T.t("conn_crit_pct")}},
                        peaks={"utilization_percent": util, "slots_used": conn["slots_used"]},
                        limitations="Saturation can be a cause (too many clients) or a symptom (sessions stuck behind locks or slow queries). The counts alone do not say which."))
    # --- wait event pile-ups (instantaneous samples)
    n_active = max(1, len(active))
    for w in data.get("waits", []):
        if w["wait_event_type"] in ("(running)", "Client", "Activity"):
            continue
        if w["sessions"] >= max(10, 0.3 * n_active):
            cands.append(mk("wait_event_pileup", "warning", f"{w['sessions']} sessions waiting on {w['wait_event_type']}:{w['wait_event']}",
                            f"waitpile:{w['wait_event_type']}:{w['wait_event']}", "activity", "pg_stat_activity wait events",
                            {"wait_event_type": w["wait_event_type"], "wait_event": w["wait_event"], "sessions": w["sessions"], "state": w["state"], "active_sessions": n_active},
                            peaks={"sessions": w["sessions"]}, confidence="hypothesis",
                            limitations="Wait events are instantaneous samples; many sessions in the same wait at one instant suggests contention but does not identify its cause."))
    return cands, metrics


# --------------------------------------------------------------------------- standard tier
def analyze_standard(T: Settings, results: dict[str, CResult], prev: dict[str, float], ts: datetime) -> Analysis:
    out = Analysis()
    m = out.metrics
    db = results.get("dbstats")
    if db and db.status in ("ok", "partial"):
        cur = db.data.get("current") or {}
        for k in ("xact_commit", "xact_rollback", "blks_hit", "blks_read", "deadlocks", "temp_bytes", "temp_files", "conflicts", "tup_returned", "tup_fetched",
                  "tup_inserted", "tup_updated", "tup_deleted", "size_bytes", "numbackends", "checksum_failures"):
            m["db_" + k] = cur.get(k)
        bg = db.data.get("bgwriter") or {}
        for k in ("checkpoints_timed", "checkpoints_req", "buffers_checkpoint", "buffers_clean", "buffers_backend", "checkpoint_write_ms", "checkpoint_sync_ms", "buffers_alloc"):
            m["bg_" + k] = bg.get(k)
        w = db.data.get("wal") or {}
        ws = w.get("wal_stats") or {}
        m["wal_bytes"] = ws.get("wal_bytes")
        m["wal_fpi"] = ws.get("wal_fpi")
        m["wal_lsn_bytes"] = w.get("wal_lsn_bytes")
        wd = db.data.get("wal_dir") or {}
        m["wal_dir_bytes"] = wd.get("wal_dir_bytes")
        ar = db.data.get("archiver") or {}
        m["archive_failed_count"], m["archive_count"] = ar.get("failed_count"), ar.get("archived_count")
        dl, pdl = cur.get("deadlocks"), prev.get("db_deadlocks")
        if dl is not None and pdl is not None and dl > pdl:
            out.candidates.append(mk("deadlock", "warning", f"{int(dl - pdl)} new deadlock(s) counted by PostgreSQL", f"deadlocks:{cur.get('datname')}:{int(dl)}", "dbstats",
                                     "pg_stat_database.deadlocks", {"database": cur.get("datname"), "increase": dl - pdl, "total_since_reset": dl, "previous_total": pdl,
                                                                   "stats_reset": cur.get("stats_reset")}, instant=True, ts=ts,
                                     limitations="Only the counter increase is observed here. Statements and PIDs are available only if the server log is configured (log_lock_waits and the default deadlock report)."))
        req, tim = bg.get("checkpoints_req"), bg.get("checkpoints_timed")
        preq, ptim = prev.get("bg_checkpoints_req"), prev.get("bg_checkpoints_timed")
        if None not in (req, tim, preq, ptim) and req >= preq and tim >= ptim:
            dreq, dtim = req - preq, tim - ptim
            if dreq >= 3 and dreq > 2 * max(1, dtim):
                out.candidates.append(mk("checkpoint_pressure", "info", f"{int(dreq)} of the last {int(dreq + dtim)} checkpoints were requested (WAL-triggered)",
                                         "ckptpressure", "dbstats", "pg_stat_bgwriter / pg_stat_checkpointer",
                                         {"requested_delta": dreq, "timed_delta": dtim, "since_last_sample": True}, confidence="hypothesis",
                                         limitations="Frequent requested checkpoints mean WAL volume reached max_wal_size between timed checkpoints. This is expected during bulk loads and "
                                                     "migrations; investigate write patterns before changing max_wal_size."))
        fc, pfc = ar.get("failed_count"), prev.get("archive_failed_count")
        if fc is not None and pfc is not None and fc > pfc:
            out.candidates.append(mk("archiver_failure", "warning", f"WAL archiving failed {int(fc - pfc)} time(s) since the last sample", "archiverfail", "dbstats", "pg_stat_archiver",
                                     {"failed_delta": fc - pfc, "failed_total": fc, "last_failed_wal": ar.get("last_failed_wal"), "last_failed_time": ar.get("last_failed_time"),
                                      "last_archived_time": ar.get("last_archived_time")}, peaks={"failed_total": fc},
                                     limitations="Failed archiving makes PostgreSQL keep WAL segments, which can fill the WAL volume. The cause is in archive_command output (server log)."))
    rep = results.get("replication")
    if rep and rep.status in ("ok", "partial"):
        d = rep.data
        stand = d["standbys"]
        m["repl_standbys"] = len(stand)
        lags = [s["replay_lag_s"] for s in stand if s.get("replay_lag_s") is not None]
        m["repl_max_replay_lag_s"] = max(lags) if lags else None
        m["repl_max_lag_bytes"] = max((s["total_lag_bytes"] or 0 for s in stand), default=None) if stand else None
        slots = d["slots"]
        m["slots_total"] = len(slots)
        m["slots_inactive"] = sum(1 for s in slots if not s["active"])
        m["slot_max_retained_bytes"] = max((s["retained_bytes"] or 0 for s in slots), default=None) if slots else None
        rl = lambda s: s.get("replay_lag_s")
        for s in stand:
            name = s.get("application_name") or s.get("client_addr") or f"pid {s['pid']}"
            lag = rl(s)
            if s["state"] != "streaming":
                out.candidates.append(mk("replication_lag", "warning", f"Standby {name} is in state '{s['state']}', not streaming", f"repl-state:{name}", "replication", "pg_stat_replication",
                                         {"standby": name, "state": s["state"], "total_lag_bytes": s["total_lag_bytes"], "sync_state": s["sync_state"]},
                                         limitations="'catchup' or 'startup' is normal right after a standby (re)connects; it is notable only if it persists."))
            if lag is not None and lag >= T.t("repl_lag_warn_s"):
                sev = "critical" if lag >= T.t("repl_lag_crit_s") else "warning"
                # decompose where the delay appears (transport vs flush vs replay) without assigning a cause
                parts = {"write_lag_s": s.get("write_lag_s"), "flush_lag_s": s.get("flush_lag_s"), "replay_lag_s": lag}
                stage = "replay" if (lag - (s.get("flush_lag_s") or 0)) > 0.5 * lag else "network/flush"
                out.candidates.append(mk("replication_lag", sev, f"Standby {name} replay lag {fmt_dur(lag)} ({fmt_bytes(s['total_lag_bytes'])} behind)", f"repl-lag:{name}", "replication",
                                         "pg_stat_replication", {"standby": name, "sync_state": s["sync_state"], "lags": parts, "total_lag_bytes": s["total_lag_bytes"],
                                                                 "sent_lag_bytes": s["sent_lag_bytes"], "write_lag_bytes": s["write_lag_bytes"], "flush_lag_bytes": s["flush_lag_bytes"],
                                                                 "replay_lag_bytes": s["replay_lag_bytes"], "delay_appears_mostly_in": stage,
                                                                 "thresholds": {"warn_s": T.t("repl_lag_warn_s"), "crit_s": T.t("repl_lag_crit_s")}},
                                         peaks={"replay_lag_s": lag, "total_lag_bytes": s["total_lag_bytes"] or 0}, confidence="observed",
                                         limitations="replay_lag is measured by PostgreSQL from commit-time acknowledgements and shows 0/NULL on an idle primary. The stage shown is where the "
                                                     "delay is visible, not a diagnosed cause."))
        warn_b = T.t("slot_retained_warn_mb") * 1024 * 1024
        for sl in slots:
            ret = sl.get("retained_bytes") or 0
            status = sl.get("wal_status")
            sev = None
            why = []
            if status == "lost":
                sev, why = "critical", ["wal_status=lost: required WAL has been removed; the consumer must be re-created"]
            elif status == "unreserved":
                sev, why = "warning", ["wal_status=unreserved: the slot is at risk of losing required WAL at the next checkpoint"]
            if ret >= warn_b:
                sev = "critical" if (ret >= 4 * warn_b and not sl["active"]) or sev == "critical" else (sev or "warning")
                why.append(f"{fmt_bytes(ret)} of WAL retained" + (" by an inactive slot" if not sl["active"] else ""))
            if sev:
                out.candidates.append(mk("replication_slot", sev, f"Replication slot '{sl['slot_name']}' retains {fmt_bytes(ret)} of WAL" + ("" if sl["active"] else " (inactive)"),
                                         f"slot:{sl['slot_name']}", "replication", "pg_replication_slots",
                                         {"slot": {k: sl.get(k) for k in ("slot_name", "slot_type", "active", "database", "plugin", "wal_status", "safe_wal_size", "retained_bytes", "restart_lsn")},
                                          "reasons": why, "max_slot_wal_keep_size": d["settings"].get("max_slot_wal_keep_size"), "retention_warn_bytes": warn_b,
                                          "kind": "logical" if sl["slot_type"] == "logical" else "physical"},
                                         peaks={"retained_bytes": ret},
                                         limitations="Retained WAL is measured from restart_lsn to the current WAL position. Whether it threatens disk space depends on the WAL volume's free space (not observable here)."))
        rec = d.get("recovery")
        if d["in_recovery"] and rec:
            delay = rec.get("replay_delay_s")
            m["standby_replay_delay_s"] = delay
            if rec.get("replay_paused"):
                out.candidates.append(mk("replication_lag", "warning", "WAL replay is paused on this standby", "repl-paused", "replication", "pg_is_wal_replay_paused()",
                                         {"replay_paused": True, "receive_replay_gap_bytes": rec.get("receive_replay_gap_bytes")}))
            elif delay is not None and delay >= T.t("repl_lag_warn_s"):
                out.candidates.append(mk("replication_lag", "warning", f"Standby last replayed a transaction {fmt_dur(delay)} ago", "repl-standby-delay", "replication",
                                         "pg_last_xact_replay_timestamp()", {"replay_delay_s": delay, "receive_replay_gap_bytes": rec.get("receive_replay_gap_bytes")},
                                         confidence="hypothesis", limitations="On a quiet primary this number grows without any lag; compare with receive/replay LSN gap."))
    return out


# --------------------------------------------------------------------------- slow tier
def _table_context(t: dict, running: set[str], holders: dict, T: Settings) -> tuple[str | None, list[str]]:
    ctx: list[str] = []
    rel = f"{t['schemaname']}.{t['relname']}"
    if rel in running:
        return None, [f"A vacuum is currently running on {rel}."]
    live, dead = t.get("n_live_tup") or 0, t.get("n_dead_tup") or 0
    ratio = 100.0 * dead / (live + dead) if (live + dead) else 0
    sev = "warning"
    if ratio >= 50 and dead >= 10 * T.t("dead_tup_min"):
        sev = "critical"
    last = t.get("last_autovacuum") or t.get("last_vacuum")
    ctx.append(f"Last vacuum/autovacuum: {last or 'never recorded since stats reset'}.")
    if t.get("reloptions"):
        ctx.append(f"Table-specific storage options are set: {t['reloptions']}.")
    if holders.get("oldest_xmin_age"):
        ctx.append(f"The oldest xmin holder is {holders['oldest_xmin_desc']} (age {holders['oldest_xmin_age']:,} transactions); vacuum cannot remove rows newer than it.")
    return sev, ctx


def analyze_vacuum(T: Settings, data: dict[str, Any], prev_metrics: dict[str, float]) -> Analysis:
    out = Analysis()
    m = out.metrics
    xids = data["databases_xid"]
    m["xid_age_max"] = max((x["xid_age"] for x in xids), default=None)
    m["mxid_age_max"] = max((x["mxid_age"] for x in xids), default=None)
    freeze = float(data["settings"].get("autovacuum_freeze_max_age") or 200_000_000)
    for x in xids:
        age = x["xid_age"] or 0
        sev = "critical" if age >= T.t("xid_crit_age") else "warning" if age >= T.t("xid_warn_age") else None
        if sev:
            out.candidates.append(mk("xid_age", sev, f"Database {x['datname']} XID age is {age:,} ({100 * age / 2_147_483_648:.0f}% of the wraparound limit)", f"xidage:{x['datname']}", "vacuum",
                                     "pg_database.datfrozenxid", {"database": x["datname"], "xid_age": age, "mxid_age": x["mxid_age"], "autovacuum_freeze_max_age": freeze,
                                                                  "wraparound_limit": 2_147_483_648, "thresholds": {"warn": T.t("xid_warn_age"), "crit": T.t("xid_crit_age")}},
                                     peaks={"xid_age": age}, limitations="XID age rises with transaction volume; the thresholds are absolute. Whether wraparound is imminent depends on the XID consumption rate (see trend) and on why freezing is not completing."))
        elif age >= freeze * 1.5:
            out.candidates.append(mk("xid_age", "info", f"Database {x['datname']} XID age {age:,} is above autovacuum_freeze_max_age ({int(freeze):,})", f"xidage:{x['datname']}", "vacuum",
                                     "pg_database.datfrozenxid", {"database": x["datname"], "xid_age": age, "autovacuum_freeze_max_age": freeze},
                                     limitations="Autovacuum is expected to be running anti-wraparound vacuums for this database; this is informational unless the age keeps rising."))
    hold = data["xmin_holders"]
    candidates_h = [(b["xmin_age"] or 0, f"backend PID {b['pid']} ({b.get('application_name') or b.get('usename')}, {b['state']})") for b in hold["backends"]] + \
                   [(max(s.get("xmin_age") or 0, s.get("catalog_xmin_age") or 0), f"replication slot '{s['slot_name']}'") for s in hold["slots"]] + \
                   [(p["xid_age"] or 0, f"prepared transaction '{p['gid']}'") for p in hold["prepared"]]
    candidates_h.sort(reverse=True)
    holders = {"oldest_xmin_age": candidates_h[0][0] if candidates_h else 0, "oldest_xmin_desc": candidates_h[0][1] if candidates_h else ""}
    m["oldest_xmin_age"] = holders["oldest_xmin_age"]
    if holders["oldest_xmin_age"] >= T.t("xid_warn_age") / 10:
        out.candidates.append(mk("old_xmin_holder", "warning", f"Oldest xmin holder is {holders['oldest_xmin_desc']} at age {holders['oldest_xmin_age']:,}", "oldxmin", "vacuum", "pg_stat_activity / pg_replication_slots / pg_prepared_xacts",
                                 {"holders": [{"age": a, "holder": d} for a, d in candidates_h[:5]], "threshold_age": T.t("xid_warn_age") / 10}, peaks={"xmin_age": holders["oldest_xmin_age"]},
                                 limitations="A holder's xmin limits what VACUUM may remove in every table. Impact depends on how much dead data accumulated since it began."))
    running = {p["relation"] for p in data["vacuum_progress"] if p.get("relation")}
    pressure = 0
    for t in data["tables"]:
        live, dead = t.get("n_live_tup") or 0, t.get("n_dead_tup") or 0
        if dead < T.t("dead_tup_min") or t.get("dead_ratio_pct", 0) < T.t("dead_tup_ratio_pct"):
            continue
        sev, context = _table_context(t, running, holders, T)
        if sev is None:
            continue
        pressure += 1
        rel = f"{t['schemaname']}.{t['relname']}"
        out.candidates.append(mk("vacuum_pressure", sev, f"{rel}: {int(dead):,} dead tuples ({t['dead_ratio_pct']}% of rows)", f"vacuum:{rel}", "vacuum", "pg_stat_user_tables",
                                 {"relation": rel, "n_live_tup": live, "n_dead_tup": dead, "dead_ratio_pct": t["dead_ratio_pct"], "total_bytes": t["total_bytes"], "last_vacuum": t["last_vacuum"],
                                  "last_autovacuum": t["last_autovacuum"], "autovacuum_count": t["autovacuum_count"], "n_mod_since_analyze": t["n_mod_since_analyze"],
                                  "reloptions": t["reloptions"], "context": context, "thresholds": {"min_dead": T.t("dead_tup_min"), "ratio_pct": T.t("dead_tup_ratio_pct")}},
                                 peaks={"n_dead_tup": dead, "dead_ratio_pct": t["dead_ratio_pct"]},
                                 limitations="n_dead_tup is a statistics-system estimate. High dead tuples are normal on churn-heavy tables between autovacuum runs; this fires only when both the absolute and relative thresholds are exceeded."))
    m["tables_vacuum_pressure"] = pressure
    m["dead_tuples_max"] = max((t.get("n_dead_tup") or 0 for t in data["tables"]), default=0)
    m["vacuum_workers_running"] = len(data["vacuum_progress"])
    m["prepared_xacts"] = len(data["prepared_xacts"])
    for p in data["prepared_xacts"]:
        if (p.get("xid_age") or 0) > 0:
            from ..timeutil import parse_ts, utcnow
            started = parse_ts(p["prepared"])
            age_s = (utcnow() - started).total_seconds() if started else 0
            if age_s >= T.t("old_xact_s"):
                out.candidates.append(mk("orphaned_prepared_xact", "warning", f"Prepared transaction '{p['gid']}' has been pending for {fmt_dur(age_s)}", f"prepared:{p['gid']}", "vacuum", "pg_prepared_xacts",
                                         {"gid": p["gid"], "owner": p["owner"], "database": p["database"], "prepared": p["prepared"], "age_s": age_s, "xid_age": p["xid_age"]}, peaks={"age_s": age_s},
                                         limitations="Prepared transactions survive restarts, keep their locks and hold back vacuum until committed or rolled back by their owner (often a transaction manager)."))
    for kind, rows in data["maintenance"].items():
        for r in rows:
            if (r.get("running_s") or 0) >= 3600:
                out.candidates.append(mk("long_maintenance", "info", f"{kind.replace('_', ' ')} PID {r['pid']} has run for {fmt_dur(r['running_s'])}", f"maint:{kind}:{r['pid']}", "vacuum", f"pg_stat_progress_{kind}",
                                         dict(r), pid=r["pid"], limitations="Long maintenance operations are usually expected on large objects; this is informational."))
    for r in data["vacuum_progress"]:
        if (r.get("running_s") or 0) >= 6 * 3600:
            out.candidates.append(mk("long_maintenance", "info", f"VACUUM PID {r['pid']} on {r.get('relation')} has run for {fmt_dur(r['running_s'])}", f"maint:vacuum:{r['pid']}", "vacuum",
                                     "pg_stat_progress_vacuum", dict(r), pid=r["pid"], limitations="Long vacuums on large tables can be normal; check progress and cost-based delay settings."))
    out.table_samples = [{"relation": s["relation"], "size_bytes": s["size_bytes"], "n_live": s["n_live"], "n_dead": s["n_dead"]} for s in data["table_sizes"]]
    return out


def analyze_settings(T: Settings, data: dict[str, Any], prev_map: dict[str, str] | None, server: dict[str, Any] | None) -> Analysis:
    out = Analysis()
    s = data["settings"]
    cur_map = {k: v["setting"] for k, v in s.items()}
    out.state_updates.append(("settings_map", cur_map))
    if prev_map:
        changed = [{"name": k, "old": prev_map[k], "new": cur_map[k], "unit": s[k]["unit"], "source": s[k]["source"]}
                   for k in sorted(cur_map) if k in prev_map and prev_map[k] != cur_map[k]]
        if changed:
            risky = any(c["name"] in RISKY_SETTINGS for c in changed)
            out.candidates.append(mk("config_change", "warning" if risky else "info", f"{len(changed)} setting(s) changed: " + ", ".join(c["name"] for c in changed[:5]) + ("…" if len(changed) > 5 else ""),
                                     f"cfgchange:{data['hash']}", "settings", "pg_settings", {"changed": changed[:60], "settings_hash": data["hash"]}, instant=True,
                                     limitations="Detected by comparing pg_settings between collections: the change happened between the two sample times, not at the instant shown. Who changed it is not observable here."))
    g = lambda k: s.get(k, {}).get("setting")
    obs = []
    if g("autovacuum") == "off":
        obs.append(("autovacuum_off", "warning", "autovacuum is disabled for the whole cluster", "Dead tuples and XID age are not handled automatically. This is occasionally intentional (bulk loads), but it must be compensated by manual vacuuming.", "runtime-config-autovacuum.html"))
    if g("fsync") == "off" or g("full_page_writes") == "off":
        which = "fsync" if g("fsync") == "off" else "full_page_writes"
        obs.append((f"{which}_off", "warning", f"{which} is off", "A crash or power loss can corrupt the cluster. Acceptable only for disposable instances.", "runtime-config-wal.html"))
    if g("track_counts") == "off" or g("track_activities") == "off":
        obs.append(("tracking_off", "warning", "Statistics tracking is partly disabled (track_counts or track_activities is off)", "Vacuum, table and activity diagnostics in this tool will be empty or misleading.", "runtime-config-statistics.html"))
    if g("log_lock_waits") == "off":
        obs.append(("log_lock_waits_off", "info", "log_lock_waits is off", "PostgreSQL will not log waits longer than deadlock_timeout. Blocking is then captured only by this tool's sampling. Enabling it adds log volume proportional to lock waits.", "runtime-config-logging.html"))
    if g("idle_in_transaction_session_timeout") == "0":
        obs.append(("idle_txn_timeout_unset", "info", "idle_in_transaction_session_timeout is 0 (no server-side limit)", "Sessions can stay idle in transaction indefinitely unless the application or pooler enforces its own limit. Whether to set one is workload-specific.", "runtime-config-client.html"))
    if server and not server.get("pg_stat_statements"):
        obs.append(("no_pg_stat_statements", "info", "pg_stat_statements is not installed", "Query-level performance analysis is unavailable.", "pgstatstatements.html"))
    pend = [k for k, v in s.items() if v.get("pending_restart")]
    if pend:
        obs.append(("pending_restart", "info", f"{len(pend)} setting(s) wait for a restart: " + ", ".join(sorted(pend)[:5]), "The running server still uses the old values.", "view-pg-settings.html"))
    for key, sev, title, why, doc in obs:
        out.candidates.append(mk("config_observation", sev, title, f"cfgobs:{key}", "settings", "pg_settings", {"observation": key, "why_it_matters": why, "documentation": DOC + doc},
                                 confidence="observed", limitations="A configuration observation, not a recommendation: the right value depends on workload and environment."))
    return out


# --------------------------------------------------------------------------- failures / capability gaps
def analyze_failures(results: dict[str, CResult], streaks: dict[str, int]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for name, r in results.items():
        if r.status == "error":
            streak = streaks.get(name, 1)
            sev = "critical" if streak >= 3 else "warning"
            out.append(mk("collector_failure", sev, f"Cannot collect {name}: {r.message or r.error_code}", f"collector:{name}:{r.error_code}", name, "flight recorder",
                          {"collector": name, "error_code": r.error_code, "message": r.message, "hint": r.hint, "consecutive_failures": streak},
                          peaks={"consecutive_failures": streak}, limitations="While this collector fails, evidence derived from it is marked stale and the timeline shows a collection gap."))
        elif r.status == "unavailable":
            out.append(mk("capability_gap", "info", f"{name} diagnostics unavailable: {r.message}", f"gap:{name}:{r.error_code}", name, "flight recorder",
                          {"collector": name, "error_code": r.error_code, "message": r.message, "hint": r.hint}, limitations="An optional capability is missing; related views show 'unavailable' instead of empty data."))
        elif r.status == "partial":
            for part, code in r.parts.items():
                out.append(mk("capability_gap", "info", f"{name}: '{part}' unavailable ({code})", f"gap:{name}:{part}:{code}", name, "flight recorder",
                              {"collector": name, "part": part, "error_code": code}, limitations="Part of this collector could not run (usually a privilege). The rest of its data is valid."))
    return out
