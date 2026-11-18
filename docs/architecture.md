# Architecture

```
 browser (static ES modules, no build step, no CDN)
    │  HTTPS via your reverse proxy; Bearer token when AUTH_MODE=token
    ▼
 FastAPI app ──────────────────────────────────────────────────────────────┐
  api/system · api/diagnostics · api/investigation · api/legacy (v0.6)      │
  security.py (auth, audit, redaction, headers) · errors.py (one error shape)│
    │ reads                                  │ triggers (manual refresh)    │
    ▼                                        ▼                              │
 SQLite store (WAL)  ◄──── writes ────  Flight recorder (recorder.py)      │
  evidence · metrics · runs ·            per instance × tier loops,        │
  query samples · incidents ·            non-overlapping, bounded          │
  snapshots · audit · settings           concurrency, retention task       │
                                              │ read-only                   │
                                              ▼                             │
                                  pg/ collectors → analysis → evidence      │
                                  pooled connections, timeouts, classified  │
                                  errors, version-gated SQL (pg/versions.py)│
                                              │                             │
                                              ▼                             │
                         monitored PostgreSQL instance(s) (+ optional log file, Patroni REST)
```

## Modules

- `app/config.py`: environment parsing and validation; secrets never appear in `repr()` or public dictionaries; runtime-editable *tunables* with bounds.
- `app/pg/`: `versions.py` (all SQL, gated by `server_version_num`), `pool.py` (connections, error classification), `collectors.py` (what is read), `graph.py` (blocking analysis), `logs.py` (log parsing/tailing), `analysis.py` (rules that turn data into metrics and evidence candidates).
- `app/recorder.py`: schedules three tiers per instance (fast 15 s: sessions/blocking/connections; standard 60 s: counters, replication, statements, log; slow 300 s: vacuum/XID/settings/server info), runs a pipeline per collection, and keeps the latest data in memory for the dashboard.
- `app/store/`: SQLite persistence split into core (schema, migrations, metrics, runs, retention), evidence lifecycle and incidents.
- `app/services.py`: query-statistics windows, correlation, timeline and gaps, incident candidates, reports.
- `app/recommend.py`: the diagnostic card for each evidence category.
- `app/demo.py`: synthetic data source with the same shapes as real collectors; flows through the same analysis code.
- `static/`: hash-routed single page app. DOM is built with a small hyperscript helper; data is never injected as HTML (CSP forbids inline script).

## Data flow of one collection

1. The tier loop asks the recorder to run (skipped if the previous run of that tier is still active).
2. Collectors run against a pooled, read-only connection. A failure in one collector does not stop the others; unreachable-database errors short-circuit the rest of the tier.
3. Analyzers turn data into **metrics** (numbers for charts) and **evidence candidates** (observations with thresholds and limitations).
4. In one SQLite transaction the store upserts evidence by fingerprint: new / updated / escalated / cleared. Evidence is cleared only if its collector succeeded in this run; if the collector failed it is marked `stale`.
5. Metrics, query samples, table sizes and the run record are persisted; the latest data is cached in memory.
6. Critical evidence may create an **incident candidate** (once per condition, with a cool-down, and linked to siblings that share a PID).

## Key decisions

- **SQLite, one process.** Zero operational dependencies and enough for the intended scale (a handful of instances). Trade-off: no horizontal scaling and no multi-writer. The store boundary (`app/store`) is the place to swap in PostgreSQL later.
- **Evidence, not alerts.** Everything has a fingerprint, first/last seen and limitations so incidents can be reconstructed after the fact.
- **Observed vs inferred is part of the data.** `confidence` is `observed`, `likely` or `hypothesis` on evidence; findings can additionally be `confirmed` by a human.
- **Gaps are data.** Coverage and collection gaps are computed from run history and shown in charts and the timeline; charts never connect points across a gap.
- **Time.** Everything stored is UTC. The browser converts for display. Log timestamps without a zone are converted using `PG_LOG_UTC_OFFSET_MINUTES`.
- **No build step for the UI.** Fewer moving parts, auditable source, strict CSP. Trade-off: no TypeScript or component library; a small custom toolkit (`static/js/ui.js`).
- **Read-only by construction.** Sessions are `default_transaction_read_only=on`; the code contains no write statement against the monitored database; suggested SQL is returned as text.

## Storage schema (SQLite, `PRAGMA user_version = 2`)

`evidence`, `incident_evidence`, `incidents`, `incident_notes`, `snapshots`, `metrics`, `collection_runs`, `query_stat_samples`, `query_texts`, `table_samples`, `audit_log`, `kv_state`, `settings_store`, plus the v0.6 tables (kept for migration). Live and demo data use different files; rows are additionally flagged `is_demo`.
