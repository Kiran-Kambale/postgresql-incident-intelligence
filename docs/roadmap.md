# Roadmap

Ordered by value to an on-call DBA. None of this is promised; it is the next honest steps.

## Verify what exists (before adding more)
1. Run the live suite on PostgreSQL 12-15 and 17 (a CI matrix is provided in `.github/workflows/ci.yml`; only 16 was executed).
2. Exercise logical replication (publisher slots, subscriber state) and a real Patroni cluster.
3. Soak test: days of collection against a busy server; measure monitored-database overhead and SQLite growth; tune retention defaults from data.
4. Build and scan the container image; run on macOS and Windows.

## Next features
- **Plan capture**: opt-in `auto_explain` log parsing and plan-change detection for regressed statements.
- **Host metrics**: pluggable CPU/memory/disk/IO inputs (Prometheus or node_exporter) correlated on the same timeline; disk-full risk for the WAL volume.
- **csvlog/jsonlog** parsing and log shipping from remote hosts.
- **Sub-interval blocking capture**: optionally listen to `log_lock_waits` entries to cover blocking shorter than the sampling interval.
- **Alert routing**: webhook/Slack/ticket export of candidates (with the same human-triage rule).
- **Stronger access control**: OIDC, per-user tokens with expiry and revocation, rate limiting, tamper-evident audit.
- **Multi-instance views**: fleet overview and cross-instance incident correlation; replica set topology.
- **PostgreSQL store** for the history database when SQLite's single-writer model is limiting.
- **Runbook automation (dry-run only)**: generate a reviewed change plan for a suggested action, still executed by a human.

## Explicitly out of scope
Unattended remediation (killing sessions, vacuuming, reindexing, configuration changes, failover).
