# API reference

Interactive documentation is served at `/docs` (OpenAPI at `/openapi.json`). Every error has the same shape: `{"detail": "...", "error": {"code", "message", "request_id", "hint?", "details?"}}`. Validation failures are HTTP 422 with `VALIDATION_ERROR`. List endpoints accept `page` (1-based) and `page_size` (max 500) and return `items, total, page, page_size, pages`. Time parameters are ISO-8601 (`from`, `to`).

Roles (when `AUTH_MODE=token`): **V** viewer, **O** operator, **A** admin. Open without a token: `GET /api/health/live`, `GET /api/auth/status`, static files.

## System
| Endpoint | Role | Purpose |
|---|---|---|
| `GET /api/health/live`, `/api/health/ready` | open | Liveness; readiness (store writable, an instance configured) |
| `GET /api/health` | V | Instance status summary, version, demo flag |
| `GET /api/system/status` | V | Recorder state, store statistics, configuration (no secrets) |
| `GET /api/system/capabilities` | V | What the monitoring role can see and why something is unavailable |
| `GET /api/instances` · `POST /api/instances/{id}/test` (O) · `POST /api/instances/{id}/monitoring` (A) | V | Instances, connection test with privilege report, pause/resume |
| `GET /api/settings` · `PUT /api/settings` (A) · `DELETE /api/settings/{name}` (A) | V | Thresholds, intervals, retention (bounded) |
| `POST /api/maintenance/retention?dry_run=true` | A | Retention (dry run by default) |
| `GET /api/audit` (A) · `GET /api/runs` | | Audit log; collection run history |

## Live views (served from the recorder)
`GET /api/live/overview`, `/activity` (filters: state, database, user, application, q, min_age_s, blocked_only, wait_event, pid, sort, order), `/blocking`, `/connections`, `/vacuum`, `/replication`, `/database`, `/config`; `POST /api/live/refresh` (O). Each response has `meta` with collection time, age, status and demo flag.

## History
`GET /api/metrics/catalog`, `GET /api/metrics/series?names=a,b&from&to&max_points` (gauges and per-second rates; gaps and coverage included), `GET /api/timeline?from&to&category&severity`, `GET /api/queries/top`, `GET /api/queries/trend`.

## Evidence
`GET /api/evidence` (filters: instance, category, severity, status, from, to, q, incident_id, sort, order), `GET /api/evidence/{id}` (evidence + diagnostic card), `GET /api/evidence/{id}/related`, `GET /api/evidence/export?format=json|csv` (O).

## Incidents
`POST /api/incidents` (O) · `GET /api/incidents` · `GET|PATCH /api/incidents/{id}` · `POST /api/incidents/{id}/notes` (O) · `GET|POST /api/incidents/{id}/evidence` · `DELETE /api/incidents/{id}/evidence/{evidence_id}` · `POST /api/incidents/{id}/capture` (O) · `GET /api/incidents/{id}/report?format=json|markdown` · `GET /api/snapshots/{id}` (O).

## v0.6-compatible (deprecated, still served)
`/api/monitor/status|latest|history`, `/api/events`, `/api/diagnostics/activity|locks|connections|long-queries|idle-transactions|summary|failed-queries`, `POST /api/diagnostics/failed-queries/ingest`, `/api/history*`, `/api/query-performance/capture|history`, `/api/runbooks*`. Response shapes keep the v0.6 field names where they existed; new fields were added.
