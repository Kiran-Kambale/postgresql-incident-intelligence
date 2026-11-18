# Compatibility

**Executed in this repository's validation: PostgreSQL 16.15 (Ubuntu build), primary and one streaming physical standby.** Everything else below is *supported by design and unit-tested for SQL selection, not run against a live server of that version.*

| Feature | 12 | 13 | 14 | 15 | 16 | 17 | How it is gated |
|---|---|---|---|---|---|---|---|
| Activity, blocking (`pg_blocking_pids`) | design | design | design | design | **executed** | design | n/a (available in 9.6+) |
| `query_id` on sessions | no | no | design | design | **executed** | design | `activity_sql(v >= 140000)` |
| `pg_stat_statements` time columns | `total_time` | `total_exec_time` | same | same | **executed** | same | `statements_sql(v >= 130000)`; `pg_stat_statements_info` only on 14+ |
| `pg_stat_wal`, session counters in `pg_stat_database` | no | no | design | design | **executed** | design | `wal_sql`, `dbstats_sql` (v >= 140000) |
| Checkpoint stats | `pg_stat_bgwriter` | same | same | same | **executed** | `pg_stat_checkpointer` | `bgwriter_sql(v >= 170000)` |
| Replication slots: `wal_status`, `safe_wal_size` | no | design | design | design | **executed** | design | `slots_sql(v >= 130000)`; `conflicting` on 16+; `invalidation_reason` on 17+ |
| WAL receiver `flushed_lsn` | `received_lsn` only | design | design | design | **executed** | design | `receiver_sql(v >= 130000)` |
| `n_ins_since_vacuum` | no | design | design | design | **executed** | design | `vacuum_tables_sql(v >= 130000)` |
| `reserved_connections` setting | absent | absent | absent | absent | **executed** | design | read as an optional setting (added in 16); treated as 0 when absent |

Versions below 12 are rejected with `UNSUPPORTED_VERSION`. If a column that the gating missed is absent on some minor version, the collector reports `UNSUPPORTED_VERSION` for that sub-query (partial result) rather than failing the tier.

## What was executed, concretely

- Primary: all collectors with a `pg_read_all_stats`-only role; real lock chains of two and three levels; physical replication slot retention; statement timeout; wrong password, unreachable host, missing privilege.
- Standby: receiver and recovery state, paused replay detected and reported as evidence, and replay lag in bytes observed on the primary.
- Not executed: logical replication objects, any other major version, Patroni or repmgr clusters, cloud-managed services (RDS, Cloud SQL, Azure) whose privilege models differ, PgBouncer-fronted connections (collect from PostgreSQL directly; pooler sessions show as the pooler's connections).

## Platforms

- Python 3.11+ (developed on 3.12). Linux executed. macOS and Windows are expected to work for the application itself but were not run; `scripts/lab_*.sh` are Debian/Ubuntu-specific.
- Browsers: Chromium (Playwright) executed. The UI uses standard ES modules, CSS variables, `<dialog>` and SVG.
- Container image: `Dockerfile` and `docker-compose.yml` are provided but were not built here.
