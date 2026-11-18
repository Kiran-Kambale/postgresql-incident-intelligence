#!/usr/bin/env bash
# Adds a physical STANDBY (port 5433) to the disposable lab created by lab_setup.sh, for tests/test_live_standby.py.
# Debian/Ubuntu, root/sudo. Disposable lab only: it edits pg_hba.conf and creates a replication role with a known password.
set -euo pipefail
VER="${PGVER:-16}"
bash "$(dirname "$0")/lab_setup.sh" >/dev/null
HBA="/etc/postgresql/$VER/main/pg_hba.conf"
su postgres -c "psql -Atq" <<'SQL'
DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'replicator') THEN CREATE ROLE replicator REPLICATION LOGIN PASSWORD 'repl_pw'; END IF; END $$;
SQL
grep -q "replicator" "$HBA" || { echo "host replication replicator 127.0.0.1/32 scram-sha-256" >> "$HBA"; pg_ctlcluster "$VER" main reload; }
if ! pg_lsclusters | grep -q "^$VER *standby"; then
  pg_createcluster "$VER" standby --port 5433 >/dev/null
  rm -rf "/var/lib/postgresql/$VER/standby" && mkdir -p "/var/lib/postgresql/$VER/standby" && chown postgres:postgres "/var/lib/postgresql/$VER/standby" && chmod 700 "/var/lib/postgresql/$VER/standby"
  su postgres -c "psql -Atqc \"SELECT pg_drop_replication_slot('standby_slot') FROM pg_replication_slots WHERE slot_name='standby_slot'\"" || true
  su postgres -c "PGPASSWORD=repl_pw pg_basebackup -h 127.0.0.1 -p 5432 -U replicator -D /var/lib/postgresql/$VER/standby -R -X stream -S standby_slot -C"
fi
pg_lsclusters | grep -q "^$VER *standby .*online" || pg_ctlcluster "$VER" standby start
for i in $(seq 1 30); do su postgres -c "psql -p 5433 -Atqc 'select pg_is_in_recovery()'" 2>/dev/null | grep -q t && break; sleep 1; done
echo "standby ready on port 5433 (in recovery: $(su postgres -c "psql -p 5433 -Atqc 'select pg_is_in_recovery()'"))"
