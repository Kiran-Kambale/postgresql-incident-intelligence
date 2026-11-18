# Security

## Reporting a vulnerability

Open a private security advisory on the repository (preferred) or contact the maintainer directly. Do not post exploit details, credentials, hostnames or SQL text containing real data in public issues.

## Threat model in one paragraph

DB Incident Replay holds credentials to production databases and stores statements, session metadata and configuration from them. The goals are: it can never change the database; it exposes as little sensitive text as possible; access is controlled and auditable; and the stored history is treated as sensitive data.

## What the application does

- **Read-only by construction.** Sessions use `default_transaction_read_only = on`, statement/lock/idle timeouts, and a small pool. The code issues no write statements to the monitored database and never executes suggested SQL.
- **Least privilege.** `pg_read_all_stats` (or `pg_monitor`) is enough; no superuser, no table access. See docs/permissions.md. The Instances page warns if the role is a superuser.
- **TLS.** `DB_SSLMODE` supports `verify-full` with `DB_SSLROOTCERT`. With a mode that does not verify the certificate, the UI and logs say so explicitly.
- **Secrets.** Database passwords come from the environment (or per-instance `password_env` variables), never from the JSON instances file; they are excluded from `repr()`, API responses, audit entries and logs. Connection errors are classified and sanitized; hostnames, users and SQL are not echoed to the browser.
- **Query text.** `QUERY_TEXT_MODE=redact` (default) masks string and numeric literals, dollar-quoted strings and e-mail addresses before text is stored or returned; `full` keeps literals but still masks credentials (passwords in `ALTER ROLE`, URLs with credentials, bearer/JWT/AWS keys, `password=` style pairs); `none` hides text entirely. `REDACT_PATTERNS` adds custom regular expressions. Server-log messages, constraint-violation details and `DETAIL` statements are redacted the same way.
- **Authentication and authorization.** `AUTH_MODE=token` requires a bearer token; tokens map to **viewer**, **operator** or **admin**. Tokens are compared in constant time, must be at least 16 characters, are never logged, and are kept only in the browser tab's `sessionStorage`. Authorization failures are audited.
- **Audit log.** Incident changes, exports, snapshot reads, settings, retention, instance toggles, manual collections, log reads and authorization denials are recorded (actor, role, action, target, outcome, client address, redacted details).
- **Web hardening.** `Content-Security-Policy` (no inline script, same-origin only), `X-Frame-Options: DENY`, `X-Content-Type-Options`, `Referrer-Policy: no-referrer`, `Cache-Control: no-store`; the UI builds DOM from data without `innerHTML`; CSV export neutralizes spreadsheet formulas; error responses carry a request id instead of stack traces; request validation bounds every parameter.

## What it does not do (known limitations)

- No SSO/OIDC, no per-user accounts, no token rotation or revocation UI, no rate limiting or lockout. Use a reverse proxy for rate limiting and TLS.
- The app does not terminate TLS. **With `AUTH_MODE=none` bind to `127.0.0.1` only.** Anything reachable from a network needs `AUTH_MODE=token` behind HTTPS.
- The SQLite history file is not encrypted and the audit log is not tamper-evident. Protect the `data/` directory with file permissions and disk encryption; it contains evidence and (redacted) statements.
- Redaction is pattern-based. It reduces exposure; it does not guarantee that no sensitive value survives (for example, a secret in an identifier or comment). Use `QUERY_TEXT_MODE=none` where statements must never be stored.
- Anyone with the operator role can export evidence and read snapshots; treat those roles accordingly.
- The optional Patroni integration uses unauthenticated GET requests to the REST API with TLS verification on by default (`PATRONI_VERIFY_TLS`); it has not been tested against a real cluster.

## Deployment checklist

1. Dedicated least-privilege role; `verify-full` TLS; `pg_hba.conf` limited to the app host.
2. `AUTH_MODE=token`, long random tokens, HTTPS reverse proxy, bind the app to localhost or a private interface.
3. `QUERY_TEXT_MODE=redact` (or `none`), plus `REDACT_PATTERNS` for organization-specific identifiers.
4. File permissions (`chmod 700 data`), disk encryption, backups with the same protection.
5. Review the audit log periodically; set retention to what policy allows.
