-- Least-privilege monitoring role for DB Incident Replay. Run as a superuser on the monitored instance.
-- The application only reads statistics views. It needs NO superuser, NO table privileges and NO write access.
--
-- Choose ONE of the two options, then see docs/permissions.md for what each privilege unlocks.

-- Option A (recommended): the built-in monitoring role. Includes everything below plus pg_ls_waldir() for WAL directory size.
--   CREATE ROLE incident_reader LOGIN PASSWORD 'use-a-long-random-secret-from-your-secret-store';
--   GRANT pg_monitor TO incident_reader;

-- Option B (narrower): only what the diagnostics need.
CREATE ROLE incident_reader LOGIN PASSWORD 'use-a-long-random-secret-from-your-secret-store';
GRANT CONNECT ON DATABASE incident_lab TO incident_reader;      -- repeat for each database you monitor
GRANT pg_read_all_stats TO incident_reader;                      -- all sessions' activity and query text, pg_stat_statements text, replication views
GRANT pg_read_all_settings TO incident_reader;                   -- optional: full pg_settings values (without it some values are hidden)

-- Safety nets that apply to this role only (the application also sets read-only sessions and statement timeouts itself):
ALTER ROLE incident_reader SET default_transaction_read_only = on;
ALTER ROLE incident_reader SET statement_timeout = '5s';
ALTER ROLE incident_reader SET idle_in_transaction_session_timeout = '30s';
ALTER ROLE incident_reader CONNECTION LIMIT 5;

-- pg_stat_statements (query performance) requires the extension in the monitored database and in shared_preload_libraries (restart):
--   CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
