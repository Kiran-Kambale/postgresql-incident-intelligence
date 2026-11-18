# Upgrading from v0.6.0

1. **Stop v0.6**, back up `data/incident_history.sqlite3`.
2. Copy `.env`; add `DB_SSLMODE` (and `DB_SSLROOTCERT` for remote servers). Old names (`MONITOR_INTERVAL_SECONDS`, `LONG_QUERY_SECONDS`, `IDLE_TXN_SECONDS`, `PG_LOG_FILE`, `PG_LOG_TAIL_BYTES`) still work. `AUTO_MONITOR` now defaults to true.
3. `pip install -r requirements.txt` (adds `psycopg-pool`).
4. Start the app. The history file is upgraded in place (SQLite `user_version` 0 → 2) in one transaction:
   - Incidents keep their ids, text, findings and actions; timestamps are normalized to UTC.
   - Legacy `incident_events` become **evidence** (categories mapped: `blocking`, `long_queries` → `long_query`, `idle_transactions` → `idle_in_transaction`, `connections_high` → `connection_saturation`), marked as migrated and cleared. Their raw row payloads are **dropped** (they could contain raw SQL).
   - Legacy `query_performance_samples` become query-statistics samples with redacted text.
   - Snapshots whose title is an incident capture are linked to that incident.
   - Rows that the demo mode had written are detected and flagged as demo.
5. Verify in **Audit and Diagnostics → Recorder and store**: schema version 2, "migrated from version 0".

The migration is not reversible; keep the backup. Re-opening an upgraded file is safe.

## API compatibility

v0.6 paths are still served (see docs/api.md). Notable differences: timestamps are UTC with `Z`; collection is continuous instead of manual; query text is redacted by default; some endpoints now require an operator or admin token when `AUTH_MODE=token`; validation errors are HTTP 422 (v0.6 used 400 for some).

## Behaviour changes to be aware of

- The recorder collects every 15 s by default and keeps data for 14 days (metrics) / 90 days (cleared evidence). Adjust on the Monitoring Settings page.
- Demo mode writes to `demo_history.sqlite3`; an existing live file is never touched by demo mode.
- The monitoring role no longer needs `GRANT EXECUTE ON pg_current_logfile()`; set `PG_LOG_FILE` explicitly instead.
