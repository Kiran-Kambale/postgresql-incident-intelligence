# Lab incident scenarios

Run only against a disposable lab database (see local-postgres-setup.md). Lower the thresholds on the Monitoring Settings page if you do not want to wait (for example `block_warn_s` 5, `idle_txn_s` 10).

## 1. Blocking transaction (root blocker)
Session A: `BEGIN; UPDATE demo_accounts SET balance = balance - 1 WHERE account_id = 1;` (leave open). Session B and C: `UPDATE demo_accounts SET balance = balance + 1 WHERE account_id = 1;`.
Expected: within one fast interval, **Locks and Blocking** shows session A as the root blocker, B and C blocked; blocking evidence is created; with enough blocked sessions or wait time a candidate incident appears. For a chain, have session B first update `account_id = 2`, then `1`, and session D update `2`: B becomes an *intermediate* node. End with `ROLLBACK;` and confirm the evidence is cleared with its peak values.

## 2. Long query
`SELECT pg_sleep(35);` in a separate session with `long_query_s` below the duration. Expect long-query evidence while it runs and cleared afterwards.

## 3. Deadlock
Two sessions updating rows 1 and 2 in opposite order. Expect a `deadlock` evidence from the counter; with `PG_LOG_FILE` configured, the evidence names the PIDs and (redacted) statements.

## 4. Query regression
Enable `pg_stat_statements`, run a stable workload for 15+ minutes, then run a slower variant. Expect a `query_regression` hypothesis. Do not read it as a diagnosis.

## 5. Connection pressure
Open a bounded number of idle sessions on a lab server with a small `max_connections`. Compare *by application* and *by state* before concluding the cause.

## 6. Replication
With `scripts/lab_standby.sh`: `SELECT pg_wal_replay_pause();` on the standby, write on the primary; expect "WAL replay is paused" evidence on the standby instance and replay lag in bytes on the primary.

Record manual investigation time against tool-assisted time, false positives, missing evidence and whether each finding was verified. Do not claim a time saving from a demo.
