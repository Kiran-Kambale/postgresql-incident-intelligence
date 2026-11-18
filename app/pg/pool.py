"""Connection management for one monitored PostgreSQL instance: pooled, read-only, time-bounded, error-classified."""
from __future__ import annotations

import logging
import threading
from typing import Any

import psycopg
from psycopg import errors as pgerr
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool, PoolTimeout

from ..config import InstanceConfig, Settings
from . import versions as V

log = logging.getLogger("dir.pg")


class PgError(Exception):
    """A classified database failure. `code` is stable for the API/UI; `message` is already sanitized."""

    def __init__(self, code: str, message: str, hint: str | None = None, sqlstate: str | None = None):
        super().__init__(message)
        self.code, self.message, self.hint, self.sqlstate = code, message, hint, sqlstate

    @property
    def status(self) -> str:   # collector status: unavailable = a capability is missing, error = something broke
        return "unavailable" if self.code in ("PERMISSION_DENIED", "NOT_INSTALLED", "UNSUPPORTED_VERSION", "LOG_NOT_CONFIGURED", "LOG_NOT_READABLE") else "error"


def classify(exc: BaseException) -> PgError:
    if isinstance(exc, PgError):
        return exc
    if isinstance(exc, pgerr.InsufficientPrivilege):
        return PgError("PERMISSION_DENIED", "The monitoring role lacks a privilege required for this check.",
                       "See docs/permissions.md: grant pg_monitor (or the specific role listed there) to the monitoring user.", "42501")
    if isinstance(exc, (pgerr.UndefinedTable, pgerr.UndefinedFunction, pgerr.UndefinedObject)):
        return PgError("NOT_INSTALLED", "A view, function or extension required for this check is not available on this server.",
                       "The feature may need an extension (for example pg_stat_statements) or a newer PostgreSQL version.", getattr(exc, "sqlstate", None))
    if isinstance(exc, pgerr.UndefinedColumn):
        return PgError("UNSUPPORTED_VERSION", "A column used by this check does not exist on this PostgreSQL version.",
                       "Report the server version; the check has not been verified on it.", "42703")
    if isinstance(exc, pgerr.QueryCanceled):
        return PgError("QUERY_TIMEOUT", "The diagnostic query exceeded its time limit and was cancelled.",
                       "The server may be overloaded. Raise DB_STATEMENT_TIMEOUT_MS only if you accept the added load.", "57014")
    if isinstance(exc, pgerr.LockNotAvailable):
        return PgError("LOCK_TIMEOUT", "A diagnostic query could not take a lock quickly and gave up.", None, "55P03")
    if isinstance(exc, (PoolTimeout, psycopg.OperationalError)):
        text = str(exc).lower()      # used only for matching, never displayed
        if "password authentication" in text or "authentication failed" in text:
            return PgError("AUTH_FAILED", "Authentication to the database failed.", "Check DB_USER / DB_PASSWORD for this instance.")
        if "ssl" in text or "certificate" in text:
            return PgError("TLS_FAILED", "The TLS handshake or certificate verification failed.", "Check sslmode and DB_SSLROOTCERT.")
        return PgError("DB_UNAVAILABLE", "The database could not be reached.", "Check host, port, firewall and that PostgreSQL is running.")
    if isinstance(exc, psycopg.Error):
        return PgError("DB_ERROR", "The database returned an error for a diagnostic query.", None, getattr(exc, "sqlstate", None))
    return PgError("COLLECTOR_ERROR", "A collector failed unexpectedly.", None)


class PgInstance:
    def __init__(self, cfg: InstanceConfig, settings: Settings):
        self.cfg = cfg
        self.settings = settings
        self._pool: ConnectionPool | None = None
        self._lock = threading.Lock()
        self.info: dict[str, Any] = {}      # last successful server probe

    # ------------------------------------------------------------------ pool
    def _configure(self, conn: psycopg.Connection) -> None:
        conn.autocommit = True
        conn.execute("SET default_transaction_read_only = on")
        conn.execute(f"SET statement_timeout = {int(self.settings.statement_timeout_ms)}")
        conn.execute("SET lock_timeout = '2s'")
        conn.execute("SET idle_in_transaction_session_timeout = '30s'")

    def pool(self) -> ConnectionPool:
        with self._lock:
            if self._pool is None:
                kw: dict[str, Any] = dict(host=self.cfg.host, port=self.cfg.port, dbname=self.cfg.dbname, user=self.cfg.user,
                                          password=self.cfg.password, connect_timeout=self.settings.connect_timeout_s,
                                          sslmode=self.cfg.sslmode, application_name="db-incident-replay", row_factory=dict_row)
                if self.cfg.sslrootcert:
                    kw["sslrootcert"] = self.cfg.sslrootcert
                self._pool = ConnectionPool(conninfo="", kwargs=kw, min_size=1, max_size=self.settings.pool_max, open=False,
                                            configure=self._configure, timeout=self.settings.connect_timeout_s + 2, max_lifetime=1800,
                                            reconnect_timeout=20, check=ConnectionPool.check_connection, name=f"dir-{self.cfg.id}")
                self._pool.open(wait=False)
            return self._pool

    def close(self) -> None:
        with self._lock:
            if self._pool is not None:
                try:
                    self._pool.close(timeout=3)
                except Exception:  # pragma: no cover
                    log.warning("pool close failed for %s", self.cfg.id)
                self._pool = None

    # ------------------------------------------------------------------ queries
    def query(self, sql: str, params: dict | tuple | None = None, *, timeout_ms: int | None = None) -> list[dict[str, Any]]:
        try:
            with self.pool().connection() as conn:
                with conn.transaction():
                    if timeout_ms:
                        conn.execute("SELECT set_config('statement_timeout', %s, true)", (str(int(timeout_ms)),))
                    cur = conn.execute(sql, params)
                    return list(cur.fetchall()) if cur.description else []
        except Exception as exc:
            err = classify(exc)
            log.info("query failed instance=%s code=%s", self.cfg.id, err.code)
            raise err from None

    def one(self, sql: str, params: dict | tuple | None = None, **kw) -> dict[str, Any] | None:
        rows = self.query(sql, params, **kw)
        return rows[0] if rows else None

    # ------------------------------------------------------------------ probe
    def probe(self) -> dict[str, Any]:
        """Connectivity, version, role privileges, extension and TLS state. Raises PgError on failure."""
        row = self.one(V.server_sql())
        ext = self.query(V.EXTENSION_SQL)
        info = dict(row or {})
        info["supported"] = info["version_num"] >= V.MIN_SUPPORTED
        info["major"] = V.major(info["version_num"])
        info["pg_stat_statements"] = ext[0] if ext else None
        info["tls_verified"] = self.cfg.tls_verified
        self.info = info
        return info
