# Permissions and least privilege

DB Incident Replay reads statistics views. It needs **no superuser, no table privileges and no write access**. Create the role with [`sql/00_monitoring_role.sql`](../sql/00_monitoring_role.sql).

## Recommended role

```sql
CREATE ROLE incident_reader LOGIN PASSWORD '<long random secret from your secret store>';
GRANT CONNECT ON DATABASE <db> TO incident_reader;     -- each database you monitor
GRANT pg_read_all_stats TO incident_reader;             -- required for complete diagnostics
GRANT pg_read_all_settings TO incident_reader;          -- optional: hidden parameter values
-- or simply: GRANT pg_monitor TO incident_reader;      -- includes the two above + pg_ls_waldir()
ALTER ROLE incident_reader SET default_transaction_read_only = on;
ALTER ROLE incident_reader SET statement_timeout = '5s';
ALTER ROLE incident_reader CONNECTION LIMIT 5;
```

The application *also* sets read-only sessions, a statement timeout (`DB_STATEMENT_TIMEOUT_MS`, default 5 s), a 2 s lock timeout and a 30 s idle-in-transaction timeout on every connection, so a misconfigured role cannot make it write.

## What each capability needs

| Capability | Source | Needs | Without it |
|---|---|---|---|
| Sessions, blocking, connections | `pg_stat_activity`, `pg_blocking_pids()`, `pg_locks` | `pg_read_all_stats` to see *other users'* sessions and query text | Only your own sessions are visible: blocking by other users is incomplete. The Instances page shows `pg_read_all_stats = false` |
| Database counters, checkpoints, WAL, archiver | `pg_stat_database`, `pg_stat_bgwriter` / `pg_stat_checkpointer`, `pg_stat_wal`, `pg_stat_archiver` | Readable by default | n/a |
| WAL directory size | `pg_ls_waldir()` | `pg_monitor` (or EXECUTE granted) | The collector reports **partial** (`wal_dir: PERMISSION_DENIED`); everything else works. This is what you see with `pg_read_all_stats` alone |
| Query performance | `pg_stat_statements` | Extension installed in the monitored database, in `shared_preload_libraries` (restart); `pg_read_all_stats` to see all statements' text | `NOT_INSTALLED` → "Query statistics unavailable" with the setup hint; foreign statements' text appears as hidden |
| Replication | `pg_stat_replication`, `pg_replication_slots`, `pg_stat_wal_receiver`, `pg_stat_subscription` | `pg_read_all_stats` or `pg_monitor` for full detail | Rows or columns are hidden; reported as partial |
| Vacuum, XID, table pressure | `pg_database`, `pg_stat_user_tables`, `pg_stat_progress_*`, `pg_prepared_xacts` | No table privilege is needed for statistics | n/a |
| Settings and change detection | `pg_settings` | `pg_read_all_settings` for superuser-only parameter values | Those values are not shown; change detection ignores them |
| Server log | A file on the *application host* | OS read permission for the app's user (`PG_LOG_FILE`) | `LOG_NOT_CONFIGURED` / `LOG_NOT_READABLE`; the rest of the standard tier is unaffected |

Verified on PostgreSQL 16.15 with a role holding only `pg_read_all_stats` and `CONNECT`: every collector ran; the only partial result was `wal_dir`; the role could not read `shared_preload_libraries` directly, and the application never needs to.

## What the app never does

No `INSERT`/`UPDATE`/`DELETE`/DDL, no `pg_cancel_backend`, `pg_terminate_backend`, `VACUUM`, `ALTER SYSTEM`, `COPY`, no functions with side effects. Suggested statements appear as text (with risks) in diagnostic cards and are never sent to the server.

## Network and TLS

Use `DB_SSLMODE=verify-full` with `DB_SSLROOTCERT` for any non-local server. With a weaker mode the Instances and Settings pages and the startup log state plainly that the certificate is **not verified**. Restrict the monitoring role in `pg_hba.conf` to the application's address.

## Application access

`AUTH_MODE=token` adds three roles: **viewer** (read everything), **operator** (also create/edit incidents, refresh, export, read snapshots and log excerpts) and **admin** (also settings, retention, instance toggles, audit log). See [SECURITY.md](../SECURITY.md).
