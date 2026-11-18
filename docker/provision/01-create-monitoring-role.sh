
#!/bin/bash

set -euo pipefail

# Validate required PostgreSQL connection settings.
: "${PGHOST:?PGHOST is required}"
: "${PGPORT:?PGPORT is required}"
: "${PGDATABASE:?PGDATABASE is required}"
: "${PGUSER:?PGUSER is required}"
: "${PGPASSWORD:?PGPASSWORD is required}"

# Password for the dedicated monitoring account.
: "${DB_PASSWORD:?DB_PASSWORD is required}"

echo "Starting DB Incident Replay PostgreSQL provisioning..."

# Create the monitoring role if it does not already exist.
psql -X \
    --no-password \
    --set=ON_ERROR_STOP=1 \
    --set=app_password="$DB_PASSWORD" <<'SQL'

SELECT format(
    'CREATE ROLE incident_reader LOGIN PASSWORD %L',
    :'app_password'
)
WHERE NOT EXISTS (
    SELECT 1
    FROM pg_roles
    WHERE rolname = 'incident_reader'
)
\gexec

-- Ensure the monitoring user's password is synchronized.
SELECT format(
    'ALTER ROLE incident_reader WITH LOGIN PASSWORD %L',
    :'app_password'
)
\gexec

-- Grant monitoring permissions.
SELECT format(
    'GRANT CONNECT ON DATABASE %I TO incident_reader',
    current_database()
)
\gexec
GRANT pg_monitor TO incident_reader;

-- Apply safety restrictions.
ALTER ROLE incident_reader SET default_transaction_read_only = on;
ALTER ROLE incident_reader SET statement_timeout = '5s';
ALTER ROLE incident_reader SET idle_in_transaction_session_timeout = '30s';
ALTER ROLE incident_reader CONNECTION LIMIT 5;

-- Enable query performance statistics.
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

SQL

echo "SUCCESS: incident_reader created or updated."
echo "SUCCESS: pg_monitor permissions configured."
echo "SUCCESS: pg_stat_statements enabled."
echo "DB Incident Replay provisioning completed."

