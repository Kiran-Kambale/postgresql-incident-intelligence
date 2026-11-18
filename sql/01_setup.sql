-- Local LAB setup (disposable database for the demo scenarios in docs/incident-scenarios.md). Run as a PostgreSQL administrator.
-- For real servers use sql/00_monitoring_role.sql instead: it creates only the least-privilege role.
CREATE DATABASE incident_lab;
-- Connect to incident_lab before running the remainder of this file.
-- In psql: \c incident_lab
CREATE ROLE incident_reader LOGIN PASSWORD 'change_this_local_password';
GRANT CONNECT ON DATABASE incident_lab TO incident_reader;
GRANT pg_read_all_stats TO incident_reader;
-- Optional: let the app discover the active log file (the app's OS user must also be able to read it).
GRANT EXECUTE ON FUNCTION pg_catalog.pg_current_logfile() TO incident_reader;

CREATE EXTENSION IF NOT EXISTS pg_stat_statements;   -- needs shared_preload_libraries = 'pg_stat_statements' and a restart

CREATE TABLE IF NOT EXISTS public.demo_accounts (
  account_id integer PRIMARY KEY,
  owner_name text NOT NULL,
  balance numeric(12,2) NOT NULL DEFAULT 1000
);
INSERT INTO public.demo_accounts(account_id, owner_name, balance)
VALUES (1, 'Demo Alice', 1000), (2, 'Demo Bob', 1000)
ON CONFLICT (account_id) DO NOTHING;
-- Statistics views do not need table privileges; the monitoring role gets none on purpose.
