# Troubleshooting

Start with **Database Instances**: the capability matrix and "Test connection" explain most problems, and **Audit and Diagnostics → Collection runs** shows each collector's last status and error code.

| Symptom | Likely cause and what to do |
|---|---|
| `DB_UNAVAILABLE` ("The database could not be reached") | Service down, wrong host/port, firewall. Test with `psql` using the same values. After 3 failed runs a critical *collector failure* evidence appears; existing evidence turns *stale* rather than cleared |
| `AUTH_FAILED` | `DB_USER`/`DB_PASSWORD` mismatch or `pg_hba.conf`. Restart the app after editing `.env` |
| `TLS_FAILED` | `DB_SSLMODE`/`DB_SSLROOTCERT` do not match the server certificate |
| `PERMISSION_DENIED` on a collector | The role lacks a privilege listed in docs/permissions.md. The rest of the tier still runs ("partial") |
| `NOT_INSTALLED` for statements | `pg_stat_statements` is not installed in this database; see docs/query-performance.md |
| `QUERY_TIMEOUT` | The server is overloaded or catalogs are huge. Do not raise `DB_STATEMENT_TIMEOUT_MS` casually: collection adds load |
| Blocking is not shown although sessions waited | The wait was shorter than the 15 s sampling interval, or it ended before the next collection; or the role lacks `pg_read_all_stats` so other users' sessions are invisible. Check the Coverage figure |
| "Waiting for the first collection" | The recorder starts a few seconds after the app; fast tier first, slow tier after ~4 s |
| Charts show hatched bands | Collection did not run (before monitoring started, app stopped, or the database was unreachable). They are never interpolated |
| Evidence marked *stale* | Its collector is failing now; the observation could have ended, we cannot tell |
| `LOG_NOT_READABLE` | The app's OS user cannot read `PG_LOG_FILE` |
| Demo mode takes several seconds to start | First start generates two hours of synthetic history before serving |
| 401 in the browser | `AUTH_MODE=token`: use the Sign in button; tokens live in the tab's session only |
| `Address already in use` | Another process uses port 8000: `--port 8001` |
| `No module named psycopg_pool` | `pip install -r requirements.txt` (new dependency since v0.6) |

Never paste real passwords, hostnames, customer names or unredacted SQL into public issues. Evidence exports are sensitive; review them before sharing.
