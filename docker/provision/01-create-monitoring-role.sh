#!/usr/bin/env bash

# ==========================================================
# PostgreSQL Incident Intelligence
# Automatic PostgreSQL Monitoring Role Provisioning
# PostgreSQL 17
# ==========================================================

set -Eeuo pipefail

# ----------------------------------------------------------
# Validate required environment variables
# ----------------------------------------------------------

: "${PGHOST:?PGHOST is required}"
: "${PGPORT:?PGPORT is required}"
: "${PGDATABASE:?PGDATABASE is required}"
: "${PGUSER:?PGUSER is required}"
: "${PGPASSWORD:?PGPASSWORD is required}"

: "${DB_NAME:?DB_NAME is required}"
: "${DB_USER:?DB_USER is required}"
: "${DB_PASSWORD:?DB_PASSWORD is required}"

export PGHOST PGPORT PGDATABASE PGUSER PGPASSWORD

echo "=============================================="
echo "PostgreSQL monitoring role provisioning"
echo "=============================================="

echo "PostgreSQL host: ${PGHOST}"
echo "Target database: ${DB_NAME}"
echo "Monitoring user: ${DB_USER}"

# Ensure extension creation targets the configured database.
if [[ "$PGDATABASE" != "$DB_NAME" ]]; then
    echo "ERROR: PGDATABASE and DB_NAME must match."
    exit 1
fi

# ----------------------------------------------------------
# Provision the least-privilege monitoring role
# ----------------------------------------------------------

psql -X \
  --no-psqlrc \
  -v ON_ERROR_STOP=1 \
  -v db_name="$DB_NAME" \
  -v db_user="$DB_USER" \
  -v db_password="$DB_PASSWORD" <<'SQL'

-- 1. Create the monitoring role if it does not exist.
SELECT format(
    'CREATE ROLE %I LOGIN',
    :'db_user'
)
WHERE NOT EXISTS (
    SELECT 1
    FROM pg_roles
    WHERE rolname = :'db_user'
)
\gexec


-- 2. Enforce restricted role attributes and update password.
ALTER ROLE :"db_user"
    WITH LOGIN
    PASSWORD :'db_password'
    NOSUPERUSER
    NOCREATEDB
    NOCREATEROLE
    NOREPLICATION
    NOBYPASSRLS
    INHERIT;


-- 3. Allow connection to the configured database.
GRANT CONNECT ON DATABASE :"db_name"
TO :"db_user";


-- 4. Grant monitoring permissions (Option B).
GRANT pg_read_all_stats
TO :"db_user";

GRANT pg_read_all_settings
TO :"db_user";


-- 5. Configure session safety defaults.
ALTER ROLE :"db_user"
    SET default_transaction_read_only = on;

ALTER ROLE :"db_user"
    SET statement_timeout = '5s';

ALTER ROLE :"db_user"
    SET idle_in_transaction_session_timeout = '30s';

ALTER ROLE :"db_user"
    CONNECTION LIMIT 5;


-- 6. Enable query-performance statistics.
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

SQL

echo "=============================================="
echo "Monitoring role provisioning completed."
echo "=============================================="

