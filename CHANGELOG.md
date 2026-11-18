# Changelog

## v1.0.0: Incident intelligence platform

**Rebuilt from v0.6.0.** See docs/ENGINEERING_ASSESSMENT.md for the review that drove the changes and docs/upgrading-from-0.6.md for migration notes.

### Added
- **Flight recorder**: background collection in three tiers (fast 15 s / standard 60 s / slow 300 s), per-instance non-overlapping runs, bounded global concurrency, persisted run history, coverage and collection-gap tracking, collector failures recorded as evidence.
- **Evidence model**: every noteworthy condition is stored with source, thresholds, limitations, first/last seen, peaks and a confidence label. Evidence is cleared only when its collector succeeded; otherwise it is marked *stale*.
- **Diagnostics**: root-vs-direct blocking graph with held/waited locks and cycle detection; connection saturation (with "possible symptom of blocking" context); wait-event pile-ups; idle-in-transaction and old-transaction findings; `pg_stat_statements` window deltas, regression and hotspot hypotheses; vacuum pressure with context (xmin holders, running vacuum, table options); XID and multixact age; prepared transactions; physical and logical slots (`wal_status`, retention); standby receiver/recovery state; archiver failures; checkpoint pressure; configuration change detection and observations; server-log classification (deadlocks, lock waits, connection refusals, restarts, authentication failures).
- **Incident workflow**: automatic candidates from critical evidence (never auto-opened), promote/dismiss, evidence linking (structural vs temporal), timeline notes, findings with confidence, Markdown/JSON reports separating facts from hypotheses.
- **Dashboard**: 11 pages (Overview, Live Activity, Query Performance, Locks and Blocking, Transactions and Vacuum, Replication and HA, Incidents, Evidence Explorer, Database Instances, Monitoring Settings, Audit and Diagnostics), dark/light themes, time-zone selector, global time range with chart and timeline zoom, evidence drawer with diagnostic cards, keyboard and screen-reader support.
- **Security**: token authentication with viewer/operator/admin roles, audit log, TLS modes with explicit "not verified" warnings, query-text modes (`redact` default / `full` / `none`), credential redaction in all modes, security headers and CSP, sanitized error responses, CSV formula-injection protection.
- **Operations**: multi-instance configuration (`INSTANCES_FILE`), retention with dry run, runtime-editable thresholds persisted in the store, `/api/health/live` and `/api/health/ready`, capability matrix that explains missing privileges.
- **Tests**: 198 automated tests including live PostgreSQL 16.15 integration (primary and standby) and Chromium end-to-end tests. See docs/VALIDATION.md.
- Scripts and docs: least-privilege role SQL, lab setup scripts, architecture/permissions/compatibility/validation/upgrade documents.

### Changed
- All timestamps are stored as UTC ISO-8601 (milliseconds, `Z`); the UI converts to the selected zone.
- Demo data lives in `demo_history.sqlite3`, separate from `incident_history.sqlite3`, and is labelled everywhere.
- Query text is redacted by default before storage and display.
- The v0.6 `data/incident_history.sqlite3` is upgraded in place on first start (legacy events become evidence; raw legacy payloads are dropped).
- Default collection interval is 15 s (was 30 s); `AUTO_MONITOR` defaults to true. Environment names `MONITOR_INTERVAL_SECONDS`, `LONG_QUERY_SECONDS`, `IDLE_TXN_SECONDS` still work.
- `uvicorn app.main:app` creates the application lazily; importing `app.main` has no side effects.

### Fixed
- v0.6 reported blocking as flat edges; v1.0 identifies root blockers and chains.
- v0.6 opened a new database connection per query and had no query timeouts; v1.0 uses a small pool with statement/lock timeouts and read-only sessions.
- v0.6 failed-queries endpoint required a privilege its documented role lacked; v1.0 reports the missing capability instead of failing.
- v0.6 stored raw SQL text and returned unsanitized database error messages; v1.0 redacts both.
- Dollar-quoted string literals are now masked in redaction (found by the v1.0 test suite).
- Statements embedded in log `DETAIL` lines are now redacted (found by the v1.0 test suite).

### Known limitations
- Verified on PostgreSQL 16.15 only. Logical replication, Patroni, Windows/macOS and the container image were not exercised.
- Server-log parsing supports `stderr` text format only.
- No SSO, rate limiting or tamper-evident audit log.

## v0.6.0 — Incident investigation workspace

- Added local incident register with severity, environment, ticket reference, status, findings, and action records.
- Added symptom runbooks for query regression, blocking/long transactions, connection exhaustion, replication/standby health, and backup/recovery issues.
- Added incident-linked diagnostic evidence capture and JSON incident reports.
- Added JSON export of local snapshots, incidents, and query-performance samples.
- Added supplied PostgreSQL log excerpt ingestion endpoint.
- Added optional `pg_stat_statements` observation capture and basic adjacent-interval comparison. At least three valid samples are required for an interval-to-interval ratio; counter resets and workload changes still require review.
- Added synthetic demo mode for evaluating UI and core workflow without a database connection.
- Added API tests for incidents, runbooks, log ingestion, query-statistics history, snapshots, and evidence export.
- Retained v0.4 sampling/incident events and v0.5 failed-SQL log-tail support.
- Explicitly documented limitations, least-privilege guidance, data sensitivity, and the absence of automatic remediation.

## v0.5.0 — Failed SQL log ingestion

- Added `GET /api/diagnostics/failed-queries` to discover the active PostgreSQL log and parse recent ERROR/FATAL/PANIC records with DETAIL, HINT, and STATEMENT lines when present.
- Added `PG_LOG_FILE` as an optional explicit log path and `PG_LOG_TAIL_BYTES` to bound scanning.
- Limitation: reads only the current log tail; rotated logs, multiline statements, custom formats, and permission differences may limit results.

## v0.4.0 — Automatic sampling and flight-recorder dashboard

- Added configurable background sampling of current sessions, blocking relationships, long-running queries, idle-in-transaction sessions, connection distribution, and selected database counters.
- Added seven-day retention for automatic samples in local SQLite storage.
- Added threshold-based incident events and additional evidence in manual snapshots.
- Added dashboard panels for activity, blocking, timeline, history, and database counters.
