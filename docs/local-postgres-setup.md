# Local PostgreSQL lab

Use a disposable instance. Do not run destructive scenarios against a shared or production database.

**Quick path (Debian/Ubuntu, root):** `bash scripts/lab_setup.sh` creates `incident_lab`, the least-privilege role `incident_reader` (`pg_read_all_stats` only) and `pg_stat_statements`. `bash scripts/lab_standby.sh` adds a physical standby on port 5433. Both are meant for the test suite and contain known lab passwords.

**Manual path:**
1. Create a lab database and run `sql/01_setup.sql` (lab objects) or `sql/00_monitoring_role.sql` (role only).
2. Set `DEMO_MODE=false` and the `DB_*` values in `.env`; keep `DB_SSLMODE=prefer` locally.
3. Start the app on `127.0.0.1` and open **Database Instances**: "Test connection" shows the version, role privileges, `pg_stat_statements` and TLS state; the capability matrix shows what each diagnostic needs.

`pg_stat_statements` needs `shared_preload_libraries` and a restart. Log access (`PG_LOG_FILE`) is an OS permission, separate from database privileges. Make configuration changes under a change plan; the app never makes them for you.
