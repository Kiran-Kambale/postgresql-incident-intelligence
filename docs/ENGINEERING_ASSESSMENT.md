# Engineering assessment of v0.6.0

This is the review of the uploaded v0.6.0 codebase that preceded the v1.0 work. It is written from reading the code and running it (7 original tests, demo mode, and a real PostgreSQL 16 lab). Nothing here is speculation about intent.

## What v0.6.0 was

A single-file FastAPI application (`app/main.py`, 907 lines) with one inline-JS page (`static/index.html`, ~29 KB), SQLite persistence, 7 API tests, and documentation. It could capture point-in-time snapshots, sample a few metrics, keep an incident register with runbooks, parse log excerpts and record `pg_stat_statements` samples manually. It worked against a real PostgreSQL 16 server.

## Weaknesses found (all verified in the code)

| # | Finding | Why it matters | v1.0 change |
|---|---|---|---|
| 1 | Blocking was reported as flat blocker→blocked edges with no root blocker, chain or depth | The engineer has to reconstruct who to talk to; an intermediate session looks like a culprit | Graph with root / intermediate / leaf roles, chains, depth, cycles, held/waited locks (`app/pg/graph.py`) |
| 2 | A new database connection per query; no statement timeout | Diagnostics could add load to an already struggling server, and one slow catalog query could hang the app | Small pool, read-only sessions, `statement_timeout`, `lock_timeout`, classified errors |
| 3 | No retention for query text, payloads or snapshots | Unbounded SQLite growth; stored sensitive text forever | Retention for every table with dry run; evidence linked to incidents is protected |
| 4 | Raw SQL text stored and returned unredacted | Literals can contain personal data and secrets | Redaction on by default (`QUERY_TEXT_MODE`), credential masking in every mode, redaction of log `DETAIL` lines |
| 5 | No TLS options for the PostgreSQL connection | Credentials and data over the wire without verification | `sslmode` incl. `verify-full`; an explicit "certificate not verified" warning |
| 6 | Database error text returned to the browser | Leaks hosts, users, internals | Classified, sanitized errors with stable codes |
| 7 | No authentication, no audit | Anyone who can reach the port can export evidence | Token auth with roles; audit log of sensitive operations |
| 8 | Mixed timestamp formats (`str(datetime)`, `isoformat`, with/without offset) | Sorting and cross-source correlation break | One UTC format everywhere; legacy data normalized on migration |
| 9 | Events keyed without an instance id | Cannot monitor more than one database | Every record carries `instance_id` |
| 10 | Demo and live data could share one SQLite file | Synthetic data could be mistaken for evidence | Separate files plus labels everywhere |
| 11 | The failed-queries endpoint failed for the documented least-privilege role (`data_directory` needs `pg_read_all_settings`) | The shipped setup did not work for a feature it advertised | Capability model: missing privileges are reported as "unavailable" with the reason |
| 12 | Defaults differed between code and `.env.example` | Surprising behaviour | One config module; documented defaults |
| 13 | Collection failures were not persisted | A silent gap looked like "nothing happened" | Failures are recorded as evidence; coverage and gaps are first-class |
| 14 | No vacuum/XID, replication/slot or configuration diagnostics | The common incident classes outside locks and long queries were invisible | New collectors and analyzers |
| 15 | Thin tests (7, all API happy paths) | Refactoring was unsafe | 197 tests; see VALIDATION.md |
| 16 | `@app.on_event` (deprecated) and import-time side effects | Warnings; surprising file creation on import | Lifespan context; lazy `app` object |

## What was kept

The incident register, runbooks, log-excerpt ingestion, snapshot capture and history, evidence export, demo mode, and the v0.6 API paths. Existing history files are migrated in place (docs/upgrading-from-0.6.md).

## What was deliberately not built

Automatic remediation (cancel/terminate/vacuum/create index), execution of suggested SQL, OS/cloud metrics, plan capture, SSO, and an agent that edits configuration. The product's value is trustworthy evidence and clear next steps; the risk of an unattended write path in a tool that points at production is not justified.
