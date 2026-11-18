# Validation report (v1.0.0)

Everything below was executed on **2026-11-16**. Where something was *not* executed it says so. Nothing here is an estimate.

## Environment

| Component | Version |
|---|---|
| OS | Ubuntu 24.04 (x86-64) container |
| Python | 3.12 (3.11 is the stated minimum but was **not** run) |
| Key libraries | FastAPI 0.162.1, psycopg 3.3.8, psycopg-pool 3.3.3, pytest 9.2.0 |
| PostgreSQL | **16.15** (Ubuntu build): one primary and one streaming physical standby, both on localhost |
| Browser | Chromium 153 via Playwright 1.63.0 |

The monitoring role in the live tests was `incident_reader` with **only** `CONNECT` and `pg_read_all_stats` (no superuser, no table privileges). A separate superuser connection was used by the tests solely to create and drop disposable objects.

## Result

```
198 passed, 0 failed, 0 skipped (162.9 s) — pytest -q, primary and standby labs running
```

| File | Tests | What it covers |
|---|---|---|
| `tests/test_core.py` | 48 | UTC parsing, redaction (literals, dollar quotes, passwords, URLs, tokens, log constraint values), fingerprints, configuration validation and secret hygiene, blocking graph (root/intermediate/leaf, cycles, unknown blocker, node cap), version-gated SQL for PG 12-17, error classification and message sanitization |
| `tests/test_store.py` | 19 | Evidence lifecycle (new/escalate/clear/stale/instant/dedupe), demo isolation, filters/search/pagination, incident workflow and validation, candidate cool-down, automatic links not advancing the workflow, metrics bucketing, retention (dry run, protected evidence), audit, **migration of a v0.6 database** including idempotence |
| `tests/test_analysis.py` | 39 | Every analyzer rule: blocking severity thresholds, long query exclusions, idle-in-transaction, saturation context, wait pile-ups, deadlock counter, replication lag/slots/paused replay, checkpoint and archiver deltas, vacuum pressure with context, XID thresholds, settings diffs and observations, failure escalation, query window deltas and counter resets |
| `tests/test_logs_recorder.py` | 16 | Log parsing (two prefix styles, continuations, time zones), classification and redaction, tailing with partial lines/rotation/size bounds; recorder success path, failure → stale → recovery, overlap skipping, concurrency bound, crash containment, unreachable database without mocks, clean start/stop, on-demand collection |
| `tests/test_api.py` | 33 | Contracts and workflows in demo mode: security headers, one error shape, every live view, pagination/filters/validation, metrics with rates and gaps, timeline gaps (including a simulated 10-minute outage), evidence cards (six questions, graded actions), related evidence, CSV formula safety, incident candidate → promote, full incident workflow and reports, settings validation/persistence/audit, retention, capabilities, v0.6 compatibility endpoints |
| `tests/test_auth.py` | 6 | 401 vs 403, viewer/operator/admin boundaries, audit of denials, tokens never in responses or audit |
| `tests/test_live_pg.py` | 10 | **Real PostgreSQL 16.15**: probe and privileges; all collectors with a least-privilege role; read-only enforcement; statement timeout; wrong password and unreachable host (classified, nothing leaked); missing privilege; real blocking with two waiters → evidence → incident candidate → cleared with peaks; **real three-level chain** with the middle session classified *intermediate* and the leaf directly blocked by it; vacuum/XID views; physical slot retention |
| `tests/test_live_standby.py` | 3 | **Real primary + standby**: primary sees the streaming standby and its slot; standby-side receiver/recovery; paused replay detected end to end (evidence "WAL replay is paused", lag bytes on the primary) and recovery after resume |
| `tests/test_e2e_browser.py` | 24 | **Chromium**: all 11 pages without console errors or failing API calls; KPI navigation; blocking graph selection; evidence drawer (six answers, Escape, focus); filters/search/JSON tab; theme, time zone, range; `/` shortcut and keyboard activation; create → investigate → resolve an incident with validation; candidate dismissal requires confirmation; copy-SQL confirmation (never executed); settings save/reset; responsive navigation; no remote resources |

Reproduce: `bash scripts/lab_standby.sh && playwright install chromium && pytest` (Debian/Ubuntu, root). Without the labs, 9 live tests skip themselves and the other 189 run.

## Defects the tests found (and that are fixed)

1. **Redaction leak:** `$$secret$$` dollar-quoted literals were not masked (regex back-reference to an absent group). Found by a unit test.
2. **Log redaction leak:** statements embedded in deadlock `DETAIL` lines (`Process 101: UPDATE … 'value'`) were stored unmasked. Found by a unit test.
3. **Wrong error class:** authentication failures were classified as "unavailable" because message redaction had already rewritten "password authentication failed". Found by a unit test.
4. **Demo counters went backwards** across scenario cycles, making regression history meaningless. Found by an API test.
5. **Demo seeding raced live collection** and mis-ordered time (evidence cleared and reopened incorrectly). Found while inspecting a long "ongoing" blocking episode in a screenshot.
6. **UI crash on every chart page:** the SVG helper treated a child element as an attribute map. Found by the first browser smoke test.
7. **Keyboard bug:** pressing Enter on a table row opened the details panel and immediately closed it (focus moved to the close button and the browser's follow-up key event activated it). Found by the browser tests; fixed in six components and covered by a regression assertion.
8. Blocking graph was scaled down to unreadable size and showed "blocks undefined sessions". Found by looking at the screenshot.
9. Test hygiene: a search test collided with random hex ids (flaky 1 in ~10 runs); fixed.

## Observations from the real PostgreSQL 16.15 runs

- With `pg_read_all_stats` alone every collector succeeded; `pg_ls_waldir()` (WAL directory size) is the single partial result and needs `pg_monitor`. The same role cannot read `shared_preload_libraries`; the application never needs it.
- Collector durations on the (tiny, idle) lab: server 1 ms, activity 4 ms, dbstats 3 ms, replication 3 ms, vacuum 8 ms, statements 18 ms, settings 2 ms. **These say nothing about a large production server**; no load or soak test was run.
- A lock chain of three sessions was classified correctly (root → intermediate → leaf, depth 2, chain of three PIDs) and the leaf's *direct* blocker was the middle session, not the root.

## Not validated (be careful before relying on these)

- PostgreSQL 12, 13, 14, 15, 17 (SQL selection is unit-tested; nothing was executed).
- Logical replication (real logical slots, subscriptions) and `wal_level = logical` setups.
- Patroni/repmgr, PgBouncer-fronted connections, RDS/Cloud SQL/Azure privilege models.
- Server-log parsing against real PostgreSQL log files (parsing is unit-tested against representative text I wrote; the live tests did not configure `PG_LOG_FILE`).
- Load and soak behaviour: large `pg_stat_activity` (the collector caps at 1,500 rows), thousands of tables, days of retention, SQLite growth, monitored-database overhead.
- TLS with real certificates (`verify-full` path is configuration-validated only; the lab used `prefer`).
- Token authentication behind a real reverse proxy; browser behaviour other than Chromium.
- Windows, macOS, Python 3.11, the Docker image and `docker-compose.yml`, the CI workflow file (never executed).
- Accessibility with a screen reader (keyboard operation and ARIA roles were tested; no assistive technology was run).
- Security review by a third party; no penetration test.

## A note on process

Midway through this work the sandbox was reset and the working copy was lost. The project was rebuilt from the recorded design and code, written directly to persistent storage, and **all numbers above come from the rebuilt code**, not from the earlier build. Several defects in the list above were found again or newly during re-verification.
