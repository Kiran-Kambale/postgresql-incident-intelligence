"""Version-aware SQL. Every statement here is read-only and bounded. v is server_version_num (e.g. 160015).

Support matrix (see docs/compatibility.md): PostgreSQL 12-17 are targeted; only 16 has been executed in this repository's test run.
"""
from __future__ import annotations

MIN_SUPPORTED = 120000


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def major(v: int) -> int:
    return v // 10000


ACTIVITY_TYPES = "(a.backend_type IN ('client backend','autovacuum worker','walsender','parallel worker') OR position('apply worker' in a.backend_type) > 0)"


def activity_sql(v: int) -> str:
    qid = "a.query_id::text" if v >= 140000 else "NULL::text"
    return f"""
SELECT a.pid, a.datname, a.usename, a.application_name, a.client_addr::text AS client_addr, a.backend_type, a.state,
       a.wait_event_type, a.wait_event, a.backend_start, a.xact_start, a.query_start, a.state_change,
       EXTRACT(EPOCH FROM (now() - a.xact_start))::float8 AS xact_age_s,
       EXTRACT(EPOCH FROM (now() - a.query_start))::float8 AS query_age_s,
       EXTRACT(EPOCH FROM (now() - a.state_change))::float8 AS state_age_s,
       age(a.backend_xid)::bigint AS xid_age, age(a.backend_xmin)::bigint AS xmin_age,
       {qid} AS query_id, left(a.query, %(qlen)s) AS query,
       CASE WHEN a.wait_event_type = 'Lock' THEN pg_blocking_pids(a.pid) ELSE '{{}}'::int[] END AS blocked_by
FROM pg_stat_activity a
WHERE a.pid <> pg_backend_pid() AND {ACTIVITY_TYPES}
ORDER BY a.xact_start NULLS LAST, a.query_start NULLS LAST
LIMIT %(limit)s"""


CONNECTIONS_SQL = """
SELECT coalesce(datname,'') AS datname, coalesce(usename,'') AS usename, coalesce(application_name,'') AS application_name,
       coalesce(state,'') AS state, backend_type, count(*)::int AS sessions
FROM pg_stat_activity WHERE backend_type IN ('client backend','walsender','autovacuum worker')
GROUP BY 1,2,3,4,5 ORDER BY sessions DESC LIMIT 500"""

WAITS_SQL = """
SELECT coalesce(wait_event_type,'(running)') AS wait_event_type, coalesce(wait_event,'') AS wait_event, state, count(*)::int AS sessions
FROM pg_stat_activity
WHERE backend_type = 'client backend' AND state IN ('active','idle in transaction','idle in transaction (aborted)','fastpath function call')
GROUP BY 1,2,3 ORDER BY sessions DESC LIMIT 100"""

LOCKS_SQL = """
SELECT l.pid, l.locktype, l.mode, l.granted,
       CASE WHEN l.locktype IN ('relation','tuple','extend') AND l.database = (SELECT oid FROM pg_database WHERE datname = current_database())
            THEN l.relation::regclass::text END AS relation_name,
       l.transactionid::text AS transactionid, l.page, l.tuple
FROM pg_locks l WHERE l.pid = ANY(%(pids)s::int[]) ORDER BY l.granted, l.pid LIMIT 2000"""

SETTINGS_KEYS = ("max_connections", "superuser_reserved_connections", "reserved_connections", "autovacuum_freeze_max_age",
                 "autovacuum_multixact_freeze_max_age", "autovacuum_max_workers", "autovacuum_naptime", "max_wal_senders",
                 "max_replication_slots", "max_slot_wal_keep_size", "wal_keep_size", "deadlock_timeout", "track_activity_query_size",
                 "idle_in_transaction_session_timeout", "statement_timeout", "lock_timeout", "log_lock_waits", "compute_query_id")
SETTINGS_SQL = "SELECT name, setting, unit FROM pg_settings WHERE name = ANY(%(names)s)"


def server_sql() -> str:
    return """
SELECT current_setting('server_version_num')::int AS version_num, version() AS version_string, pg_is_in_recovery() AS in_recovery,
       current_user AS current_user, current_database() AS database,
       coalesce((SELECT rolsuper FROM pg_roles WHERE rolname = current_user), false) AS is_superuser,
       pg_has_role(current_user, 'pg_monitor', 'member') AS pg_monitor,
       pg_has_role(current_user, 'pg_read_all_stats', 'member') AS pg_read_all_stats,
       pg_has_role(current_user, 'pg_read_all_settings', 'member') AS pg_read_all_settings,
       pg_postmaster_start_time() AS started_at, now() AS server_time,
       (SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()) AS ssl_in_use,
       (SELECT version FROM pg_stat_ssl WHERE pid = pg_backend_pid()) AS ssl_version"""


EXTENSION_SQL = """
SELECT e.extname, e.extversion, n.nspname AS schema FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace
WHERE e.extname = 'pg_stat_statements'"""


def dbstats_sql(v: int) -> str:
    sess = ", d.sessions, d.sessions_abandoned, d.sessions_fatal, d.sessions_killed" if v >= 140000 else ""
    return f"""
SELECT d.datname, d.numbackends, d.xact_commit, d.xact_rollback, d.blks_read, d.blks_hit, d.tup_returned, d.tup_fetched,
       d.tup_inserted, d.tup_updated, d.tup_deleted, d.conflicts, d.temp_files, d.temp_bytes, d.deadlocks, d.checksum_failures,
       d.blk_read_time, d.blk_write_time, d.stats_reset,
       CASE WHEN d.datname = current_database() THEN pg_database_size(d.datname) END::bigint AS size_bytes,
       (d.datname = current_database()) AS is_current{sess}
FROM pg_stat_database d WHERE d.datname IS NOT NULL ORDER BY (d.datname = current_database()) DESC, d.datname LIMIT 50"""


def statements_sql(v: int, schema: str) -> str:
    sc = quote_ident(schema)
    if v >= 130000:
        t = "s.total_exec_time::float8 AS total_exec_ms, s.mean_exec_time::float8 AS mean_exec_ms, s.max_exec_time::float8 AS max_exec_ms"
        order = "s.total_exec_time"
    else:
        t = "s.total_time::float8 AS total_exec_ms, s.mean_time::float8 AS mean_exec_ms, s.max_time::float8 AS max_exec_ms"
        order = "s.total_time"
    return f"""
SELECT s.queryid::text AS queryid, s.dbid::text AS dbid, s.userid::text AS userid, s.calls::float8 AS calls, {t},
       s.rows::float8 AS rows, s.shared_blks_hit::float8 AS shared_hit, s.shared_blks_read::float8 AS shared_read,
       s.temp_blks_written::float8 AS temp_written, left(s.query, %(qlen)s) AS query
FROM {sc}.pg_stat_statements s
WHERE s.queryid IS NOT NULL AND s.dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
ORDER BY {order} DESC LIMIT %(limit)s"""


def statements_info_sql(v: int, schema: str) -> str | None:
    return f"SELECT dealloc, stats_reset FROM {quote_ident(schema)}.pg_stat_statements_info" if v >= 140000 else None


def bgwriter_sql(v: int) -> str:
    if v >= 170000:
        return """SELECT c.num_timed AS checkpoints_timed, c.num_requested AS checkpoints_req, c.write_time AS checkpoint_write_ms,
                         c.sync_time AS checkpoint_sync_ms, c.buffers_written AS buffers_checkpoint, b.buffers_clean, b.maxwritten_clean,
                         b.buffers_alloc, NULL::bigint AS buffers_backend, c.stats_reset
                  FROM pg_stat_checkpointer c, pg_stat_bgwriter b"""
    return """SELECT checkpoints_timed, checkpoints_req, checkpoint_write_time AS checkpoint_write_ms, checkpoint_sync_time AS checkpoint_sync_ms,
                     buffers_checkpoint, buffers_clean, maxwritten_clean, buffers_alloc, buffers_backend, stats_reset FROM pg_stat_bgwriter"""


def wal_sql(v: int) -> str:
    stat = ("(SELECT row_to_json(w) FROM (SELECT wal_records, wal_fpi, wal_bytes::float8 AS wal_bytes, wal_buffers_full, stats_reset FROM pg_stat_wal) w) AS wal_stats,"
            if v >= 140000 else "NULL::json AS wal_stats,")
    return f"""SELECT {stat}
       CASE WHEN pg_is_in_recovery() THEN NULL ELSE pg_wal_lsn_diff(pg_current_wal_lsn(), '0/0')::float8 END AS wal_lsn_bytes"""


WAL_DIR_SQL = "SELECT count(*)::int AS wal_files, coalesce(sum(size), 0)::float8 AS wal_dir_bytes FROM pg_ls_waldir()"   # needs pg_monitor

XMIN_BACKENDS_SQL = """
SELECT pid, usename, application_name, state, backend_type, age(backend_xmin)::bigint AS xmin_age,
       EXTRACT(EPOCH FROM (now() - xact_start))::float8 AS xact_age_s
FROM pg_stat_activity WHERE backend_xmin IS NOT NULL AND pid <> pg_backend_pid() ORDER BY age(backend_xmin) DESC LIMIT 5"""

XMIN_SLOTS_SQL = """
SELECT slot_name, slot_type, active, age(xmin)::bigint AS xmin_age, age(catalog_xmin)::bigint AS catalog_xmin_age
FROM pg_replication_slots WHERE xmin IS NOT NULL OR catalog_xmin IS NOT NULL ORDER BY greatest(age(xmin), age(catalog_xmin)) DESC NULLS LAST LIMIT 5"""

ARCHIVER_SQL = "SELECT archived_count, failed_count, last_archived_wal, last_archived_time, last_failed_wal, last_failed_time, stats_reset FROM pg_stat_archiver"


def replication_sql(v: int, in_recovery: bool) -> str:
    ref = "pg_last_wal_receive_lsn()" if in_recovery else "pg_current_wal_lsn()"
    return f"""
SELECT pid, usename, application_name, client_addr::text AS client_addr, state, sync_state, sync_priority,
       sent_lsn::text AS sent_lsn, write_lsn::text AS write_lsn, flush_lsn::text AS flush_lsn, replay_lsn::text AS replay_lsn,
       pg_wal_lsn_diff({ref}, sent_lsn)::float8 AS sent_lag_bytes, pg_wal_lsn_diff(sent_lsn, write_lsn)::float8 AS write_lag_bytes,
       pg_wal_lsn_diff(sent_lsn, flush_lsn)::float8 AS flush_lag_bytes, pg_wal_lsn_diff(sent_lsn, replay_lsn)::float8 AS replay_lag_bytes,
       pg_wal_lsn_diff({ref}, replay_lsn)::float8 AS total_lag_bytes,
       EXTRACT(EPOCH FROM write_lag)::float8 AS write_lag_s, EXTRACT(EPOCH FROM flush_lag)::float8 AS flush_lag_s,
       EXTRACT(EPOCH FROM replay_lag)::float8 AS replay_lag_s, EXTRACT(EPOCH FROM (now() - backend_start))::float8 AS connected_s,
       reply_time, age(backend_xmin)::bigint AS xmin_age
FROM pg_stat_replication ORDER BY application_name, pid LIMIT 100"""


def slots_sql(v: int, in_recovery: bool) -> str:
    ref = "pg_last_wal_replay_lsn()" if in_recovery else "pg_current_wal_lsn()"
    extra = ""
    if v >= 130000:
        extra += ", wal_status, safe_wal_size::float8 AS safe_wal_size"
    if v >= 160000:
        extra += ", conflicting"
    if v >= 170000:
        extra += ", invalidation_reason, inactive_since"
    return f"""
SELECT slot_name, plugin, slot_type, database, temporary, active, active_pid, xmin::text AS xmin, catalog_xmin::text AS catalog_xmin,
       age(xmin)::bigint AS xmin_age, age(catalog_xmin)::bigint AS catalog_xmin_age, restart_lsn::text AS restart_lsn,
       confirmed_flush_lsn::text AS confirmed_flush_lsn,
       CASE WHEN restart_lsn IS NOT NULL THEN pg_wal_lsn_diff({ref}, restart_lsn)::float8 END AS retained_bytes{extra}
FROM pg_replication_slots ORDER BY slot_name LIMIT 100"""


def receiver_sql(v: int) -> str:
    flushed = "flushed_lsn::text" if v >= 130000 else "received_lsn::text"
    return f"""
SELECT status, receive_start_lsn::text AS receive_start_lsn, {flushed} AS received_lsn, last_msg_send_time, last_msg_receipt_time,
       latest_end_lsn::text AS latest_end_lsn, latest_end_time, slot_name, sender_host, sender_port FROM pg_stat_wal_receiver"""


RECOVERY_SQL = """
SELECT pg_last_wal_receive_lsn()::text AS receive_lsn, pg_last_wal_replay_lsn()::text AS replay_lsn,
       pg_last_xact_replay_timestamp() AS last_replay_ts, EXTRACT(EPOCH FROM (now() - pg_last_xact_replay_timestamp()))::float8 AS replay_delay_s,
       pg_is_wal_replay_paused() AS replay_paused,
       pg_wal_lsn_diff(pg_last_wal_receive_lsn(), pg_last_wal_replay_lsn())::float8 AS receive_replay_gap_bytes"""

SUBSCRIPTIONS_SQL = """
SELECT subid, subname, pid, received_lsn::text AS received_lsn, latest_end_lsn::text AS latest_end_lsn, latest_end_time,
       last_msg_send_time, last_msg_receipt_time FROM pg_stat_subscription ORDER BY subname LIMIT 50"""

XID_SQL = """
SELECT datname, age(datfrozenxid)::bigint AS xid_age, mxid_age(datminmxid)::bigint AS mxid_age
FROM pg_database WHERE datallowconn ORDER BY xid_age DESC LIMIT 50"""

PREPARED_SQL = """
SELECT gid, prepared, owner, database, age(transaction)::bigint AS xid_age FROM pg_prepared_xacts ORDER BY prepared LIMIT 20"""


def vacuum_tables_sql(v: int) -> str:
    ins = "s.n_ins_since_vacuum" if v >= 130000 else "NULL::bigint AS n_ins_since_vacuum"
    return f"""
WITH t AS (
  SELECT s.relid, s.schemaname, s.relname, s.n_live_tup, s.n_dead_tup, s.n_mod_since_analyze, {ins},
         s.last_vacuum, s.last_autovacuum, s.last_analyze, s.last_autoanalyze, s.vacuum_count, s.autovacuum_count
  FROM pg_stat_user_tables s ORDER BY s.n_dead_tup DESC LIMIT %(limit)s)
SELECT t.schemaname, t.relname, t.n_live_tup::float8, t.n_dead_tup::float8, t.n_mod_since_analyze::float8, t.n_ins_since_vacuum::float8,
       t.last_vacuum, t.last_autovacuum, t.last_analyze, t.last_autoanalyze, t.vacuum_count, t.autovacuum_count,
       pg_total_relation_size(t.relid)::bigint AS total_bytes, pg_relation_size(t.relid)::bigint AS table_bytes,
       c.reloptions::text AS reloptions, age(c.relfrozenxid)::bigint AS relfrozenxid_age
FROM t JOIN pg_class c ON c.oid = t.relid ORDER BY t.n_dead_tup DESC"""


TABLE_SIZES_SQL = """
SELECT s.schemaname || '.' || s.relname AS relation, pg_total_relation_size(s.relid)::bigint AS size_bytes,
       s.n_live_tup::float8 AS n_live, s.n_dead_tup::float8 AS n_dead
FROM pg_stat_user_tables s JOIN pg_class c ON c.oid = s.relid ORDER BY c.relpages DESC LIMIT 20"""

PROGRESS_VACUUM_SQL = """
SELECT p.pid, p.datname, CASE WHEN p.datname = current_database() THEN p.relid::regclass::text END AS relation, p.phase,
       p.heap_blks_total::float8, p.heap_blks_scanned::float8, p.heap_blks_vacuumed::float8, p.index_vacuum_count,
       EXTRACT(EPOCH FROM (now() - a.xact_start))::float8 AS running_s, a.backend_type, left(a.query, 200) AS query
FROM pg_stat_progress_vacuum p LEFT JOIN pg_stat_activity a ON a.pid = p.pid ORDER BY a.xact_start LIMIT 20"""

PROGRESS_INDEX_SQL = """
SELECT p.pid, p.datname, p.command, p.phase, EXTRACT(EPOCH FROM (now() - a.xact_start))::float8 AS running_s
FROM pg_stat_progress_create_index p LEFT JOIN pg_stat_activity a ON a.pid = p.pid LIMIT 20"""

PROGRESS_CLUSTER_SQL = """
SELECT p.pid, p.datname, p.command, p.phase, EXTRACT(EPOCH FROM (now() - a.xact_start))::float8 AS running_s
FROM pg_stat_progress_cluster p LEFT JOIN pg_stat_activity a ON a.pid = p.pid LIMIT 20"""

ALL_SETTINGS_SQL = "SELECT name, setting, unit, source, pending_restart, context FROM pg_settings ORDER BY name"
