#!/usr/bin/env bash
# Creates a DISPOSABLE PostgreSQL lab for the live integration tests (tests/test_live_pg.py). Never run this against a real server.
# Assumes a Debian/Ubuntu host with the `postgresql` package installed and root/sudo access. Starts cluster 16/main if needed.
set -euo pipefail
VER="${PGVER:-16}"; CLUSTER="${PGCLUSTER:-main}"; DB="${LAB_DB:-incident_lab}"
pg_lsclusters | grep -q "$VER *$CLUSTER .*online" || pg_ctlcluster "$VER" "$CLUSTER" start
for i in $(seq 1 30); do su postgres -c "psql -Atqc 'select 1'" >/dev/null 2>&1 && break; sleep 1; done
CONF="/etc/postgresql/$VER/$CLUSTER/postgresql.conf"
if ! grep -q "pg_stat_statements" "$CONF"; then
  echo "shared_preload_libraries = 'pg_stat_statements'" >> "$CONF"
  echo "compute_query_id = on" >> "$CONF"
  pg_ctlcluster "$VER" "$CLUSTER" restart
  for i in $(seq 1 30); do su postgres -c "psql -Atqc 'select 1'" >/dev/null 2>&1 && break; sleep 1; done
fi
su postgres -c "psql -v ON_ERROR_STOP=1 -Atq" <<SQL
ALTER USER postgres PASSWORD 'lab_admin_pw';
SELECT 'CREATE DATABASE $DB' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = '$DB')\gexec
DO \$\$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'incident_reader') THEN CREATE ROLE incident_reader LOGIN PASSWORD 'lab_pw'; END IF; END \$\$;
GRANT pg_read_all_stats TO incident_reader;
GRANT CONNECT ON DATABASE $DB TO incident_reader;
SQL
su postgres -c "psql -d $DB -v ON_ERROR_STOP=1 -Atq -c 'CREATE EXTENSION IF NOT EXISTS pg_stat_statements'"
echo "lab ready: db=$DB role=incident_reader (pg_read_all_stats, least privilege); setup superuser password is for the lab only"
