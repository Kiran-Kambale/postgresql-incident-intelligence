# Flight recorder and evidence model

## Collection tiers

| Tier | Default interval | Collects |
|---|---|---|
| fast | 15 s | `pg_stat_activity`, `pg_blocking_pids()`, `pg_locks` for involved PIDs, connection breakdown, wait events |
| standard | 60 s | `pg_stat_database`, bgwriter/checkpointer, WAL and archiver stats, replication (standbys, slots, receiver, recovery, subscriptions), `pg_stat_statements`, server log tail |
| slow | 300 s | XID/multixact age, table dead tuples and sizes, vacuum progress, xmin holders, prepared transactions, `pg_settings`, server version and privileges |

Each run is bounded by connect and statement timeouts and a global concurrency limit (`MAX_CONCURRENT_COLLECTIONS`, default 2). A tier never overlaps itself: if a run is still going, the next trigger is skipped and counted. A run has status `ok`, `partial` (some sub-queries unavailable, usually a privilege) or `error`.

## Evidence lifecycle

Evidence is identified by a **fingerprint** (for example `blocking:<db>:<root pid>:<backend start>`), so repeated observations update one record instead of creating a flood.

| Event | What happens |
|---|---|
| First observation | New evidence, `first_seen = collection time`, `status = active` |
| Observed again | `last_seen` and `observation_count` advance; severity can **escalate** (never silently downgrade); peaks (for example most blocked sessions) are kept |
| Not observed, collector succeeded | `status = cleared`, `cleared_at = collection time` |
| Not observed, collector **failed** | stays `active`, marked `collection_status = stale` (we do not know it ended) |
| Point-in-time events (log lines, counter increases, configuration changes) | stored as already-cleared *instant* evidence at their own timestamp, de-duplicated |

First and last seen are bounded by the sampling interval: **blocking that began and ended between two fast collections is not seen**, and a duration is accurate to roughly one interval. The UI labels durations accordingly and the diagnostic cards state each category's limitations.

## What becomes evidence (defaults; all editable on the Monitoring Settings page)

| Category | Rule (summary) |
|---|---|
| blocking | A root blocker whose longest dependent wait ≥ `block_min_s` (3 s). Info; warning ≥ 15 s; critical ≥ 60 s or ≥ 5 transitively blocked sessions. Cycles are critical |
| long_query | Active query older than `long_query_s` (30 s), not waiting on a lock (those are represented by blocking), not autovacuum |
| idle_in_transaction | Idle in transaction ≥ `idle_txn_s` (60 s); critical at 1 h |
| long_transaction | Open transaction older than `old_xact_s` (1 h) |
| connection_saturation | Slots used ≥ 80% (warning) / 90% or no regular slots left (critical); includes whether sessions waiting on locks might explain it |
| wait_event_pileup | At least 10 sessions *and* at least 30% of active sessions on the same wait event at one instant; labelled a hypothesis |
| deadlock | `pg_stat_database.deadlocks` increase; the server log adds statements and PIDs |
| replication_lag / replication_slot | Replay lag ≥ 30 s warning / 300 s critical with the stage where the delay is visible; slot `wal_status` unreserved/lost; slot WAL retention ≥ 1 GiB (critical at 4× when inactive); standby not streaming; replay paused |
| vacuum_pressure | Dead tuples ≥ 10,000 **and** ≥ 20% of rows, skipped while a vacuum is running on the table, with context (last autovacuum, table options, oldest xmin holder) |
| xid_age | Database XID age ≥ 1.0e9 warning / 1.5e9 critical (informational above 1.5× `autovacuum_freeze_max_age`) |
| old_xmin_holder, orphaned_prepared_xact, long_maintenance | Context for vacuum and wraparound |
| checkpoint_pressure, archiver_failure | Counter deltas between samples; checkpoint pressure is a hypothesis |
| query_regression, query_hotspot | Window-over-window mean time ratio ≥ 3 (≥ 20 calls each side), or one statement ≥ 50% of sampled time; hypotheses |
| config_change, config_observation | `pg_settings` diffs; observations such as `autovacuum=off` or `log_lock_waits=off`, phrased as context, never as recommendations |
| collector_failure, capability_gap | The recorder's own health: 3 consecutive failures = critical; a missing privilege/extension = info |

## Incident candidates

New or escalated **critical** evidence of a candidate category creates an incident candidate (status `candidate`, stage `detect`) with related evidence linked automatically. One open candidate per condition; further evidence is attached to it; evidence sharing a PID with an open candidate joins it instead of creating a duplicate; a dismissed or resolved candidate is not recreated for `candidate_cooldown_min` (30 min). Nobody is paged and nothing is opened as an incident without a human decision.

## Retention (defaults)

Metrics and run history 14 days; query-statistics samples 14 days; cleared evidence 90 days; manual snapshots 90 days; audit log 365 days. Evidence linked to an incident and snapshots linked to an incident are never deleted. The retention job runs hourly; `POST /api/maintenance/retention` defaults to a dry run.

## Demo isolation

`DEMO_MODE=true` uses `demo_history.sqlite3`, a synthetic data source with the same shape as real collectors, and a banner on every page. Demo evidence, incidents and snapshots carry `is_demo`. A demo instance never opens a database connection. The 40-minute scenario repeats; `DEMO_PHASE_OFFSET_S` shifts its clock (a test hook).
