# PostgreSQL server log

The server log gives what SQL views cannot: deadlock participants and statements, lock-wait messages (with the holder), connection refusals, authentication failures, crashes and restarts, cancelled statements. Without it the app still detects the *effects* (deadlock counter, blocking, saturation).

## Configure (on the PostgreSQL server)

```
logging_collector = on            # or use your distribution's default stderr logging
log_destination = 'stderr'        # csvlog / jsonlog are NOT parsed
log_line_prefix = '%m [%p] %q%u@%d '        # or: '%m [%p] user=%u db=%d app=%a client=%h '
log_lock_waits = on               # logs waits longer than deadlock_timeout (default 1 s)
log_min_error_statement = error   # include the failing statement
```
The prefix must contain a timestamp (`%m` or `%t`) and `[%p]`. `log_lock_waits` increases log volume in proportion to lock waits.

## Configure (in the app)

`PG_LOG_FILE=/path/to/postgresql.log` per instance (or `log_file` in `INSTANCES_FILE`). The app's OS user needs read permission on the file; this is an OS permission, not a database privilege. If the log uses a local time zone without an offset, set `PG_LOG_UTC_OFFSET_MINUTES` (for example `330` for IST) so events line up with UTC evidence.

## Behaviour and limits

- Each standard collection reads only **new complete lines** since the last offset (stored per file), bounded by `PG_LOG_TAIL_BYTES` (default 2 MiB). A partial last line is read next time. Rotation and truncation are detected and the new file is read from its start. If more than the bound was written between collections, the oldest part is skipped and reported.
- Messages, `DETAIL` statements, constraint values and e-mail addresses are redacted before storage (see SECURITY.md). Statements follow `QUERY_TEXT_MODE`.
- Multi-line messages are joined; continuation lines must be indented as PostgreSQL writes them.
- Recognized events: deadlocks, lock waits (holder PID where logged), connection slot exhaustion, authentication failures, statement/lock timeouts, server restarts and signals, checkpoint-frequency warnings, cancelled autovacuum, and other `ERROR`/`FATAL`/`PANIC` messages (de-duplicated by normalized message per minute).
- Not supported: csvlog, jsonlog, syslog, files on other hosts, compressed rotated logs.

## Paste-in analysis

`POST /api/diagnostics/failed-queries/ingest` parses a pasted excerpt in memory and returns redacted ERROR/FATAL/PANIC records. Nothing is stored.
