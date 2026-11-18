# Query performance (pg_stat_statements)

## Enable (manual, needs a restart and an administrator)

```
# postgresql.conf
shared_preload_libraries = 'pg_stat_statements'     # restart required
compute_query_id = on                               # PostgreSQL 14+ (default auto)
```
```sql
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;  -- in each monitored database
```
Make this change under your normal change process. The extension adds a small overhead; consult the PostgreSQL documentation.

## What the app does

Every standard collection (60 s) it reads the top 200 statements of the current database by cumulative execution time, stores their counters with a normalized, redacted text, and detects statistics resets. The Query Performance page and `/api/queries/top` compute **window deltas between stored samples** (calls, total time, mean, rows per call, cache hit ratio, share of sampled time). `/api/queries/trend` shows per-interval means. Window comparisons raise `query_regression` and `query_hotspot` evidence.

## How to read it

- A **mean** is a window average (delta total time ÷ delta calls). It is not the duration of one execution.
- A reset (`pg_stat_statements_reset()`, restart, or eviction when the hash table is full) makes counters go backwards. Intervals spanning a reset are skipped and counted ("counter reset(s) skipped"), which can understate activity around it.
- A regression is a **hypothesis**: a different parameter mix, data growth, contention (look at blocking at the same time), a plan change or a deployment can each produce it. Compare plans (`EXPLAIN`, `auto_explain`) before concluding.
- Only the top 200 statements are sampled, so "share" is relative to sampled statements and rare expensive statements may be missed.
- Statements of other users show as hidden unless the role has `pg_read_all_stats`.
- Plans, per-execution latencies and wait events per statement are not collected.

## v0.6 endpoints

`POST /api/query-performance/capture` now triggers one standard collection; `GET /api/query-performance/history` summarizes interval means per statement. Both are served from the recorder's samples.
