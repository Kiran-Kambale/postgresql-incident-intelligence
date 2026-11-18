"""Environment-based configuration. Secrets are never included in repr() or public dictionaries."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import __version__

ROOT = Path(__file__).resolve().parent.parent
VERSION = __version__
TRUE = ("1", "true", "yes", "on")
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
SSL_MODES = ("disable", "allow", "prefer", "require", "verify-ca", "verify-full")

# name -> (type, default, min, max, group, description). Editable at runtime from Monitoring Settings.
TUNABLES: dict[str, tuple[type, float, float, float, str, str]] = {
    "fast_interval_s": (int, 15, 5, 3600, "Collection", "Interval for sessions, blocking, connections (seconds)"),
    "standard_interval_s": (int, 60, 15, 3600, "Collection", "Interval for database counters, replication, query statistics (seconds)"),
    "slow_interval_s": (int, 300, 60, 86400, "Collection", "Interval for vacuum, XID age, slots, table sizes, settings (seconds)"),
    "long_query_s": (int, 30, 1, 86400, "Thresholds", "Active query older than this is recorded as a long-running query"),
    "idle_txn_s": (int, 60, 1, 86400, "Thresholds", "Idle-in-transaction session older than this is recorded"),
    "old_xact_s": (int, 3600, 60, 604800, "Thresholds", "Open transaction older than this is recorded"),
    "block_min_s": (int, 3, 0, 3600, "Thresholds", "Lock waits shorter than this are ignored as normal contention"),
    "block_warn_s": (int, 15, 1, 86400, "Thresholds", "Blocking wait duration for a warning"),
    "block_crit_s": (int, 60, 1, 86400, "Thresholds", "Blocking wait duration for a critical finding"),
    "block_crit_sessions": (int, 5, 1, 10000, "Thresholds", "Transitively blocked sessions for a critical finding"),
    "conn_warn_pct": (float, 80, 1, 100, "Thresholds", "Connection utilization warning (% of max_connections)"),
    "conn_crit_pct": (float, 90, 1, 100, "Thresholds", "Connection utilization critical (% of max_connections)"),
    "dead_tup_min": (int, 10000, 100, 10**9, "Thresholds", "Minimum dead tuples before a table is considered for vacuum pressure"),
    "dead_tup_ratio_pct": (float, 20, 1, 100, "Thresholds", "Dead tuples as % of live tuples for vacuum pressure"),
    "xid_warn_age": (int, 1_000_000_000, 1_000_000, 2_100_000_000, "Thresholds", "Database XID age warning"),
    "xid_crit_age": (int, 1_500_000_000, 1_000_000, 2_100_000_000, "Thresholds", "Database XID age critical"),
    "repl_lag_warn_s": (int, 30, 1, 86400, "Thresholds", "Replay lag warning (seconds)"),
    "repl_lag_crit_s": (int, 300, 1, 86400, "Thresholds", "Replay lag critical (seconds)"),
    "slot_retained_warn_mb": (int, 1024, 1, 10**7, "Thresholds", "WAL retained by a replication slot warning (MiB)"),
    "candidate_cooldown_min": (int, 30, 0, 10080, "Incidents", "Minimum minutes before another automatic incident candidate for the same condition"),
    "retention_metrics_days": (int, 14, 1, 3650, "Retention", "Time-series metrics"),
    "retention_runs_days": (int, 14, 1, 3650, "Retention", "Collection run history"),
    "retention_query_stats_days": (int, 14, 1, 3650, "Retention", "Query statistics samples"),
    "retention_evidence_days": (int, 90, 1, 3650, "Retention", "Cleared evidence not linked to an incident"),
    "retention_snapshots_days": (int, 90, 1, 3650, "Retention", "Manual evidence snapshots not linked to an incident"),
    "retention_audit_days": (int, 365, 7, 3650, "Retention", "Audit log"),
}
ENV_ALIASES = {"fast_interval_s": "MONITOR_INTERVAL_SECONDS", "long_query_s": "LONG_QUERY_SECONDS", "idle_txn_s": "IDLE_TXN_SECONDS"}


def _flag(v: str | None, default: bool = False) -> bool:
    return default if v is None or v == "" else v.strip().lower() in TRUE


def _num(v: Any, typ: type, lo: float, hi: float, default: float):
    try:
        x = typ(float(v)) if typ is int else float(v)
    except (TypeError, ValueError):
        return default
    return min(max(x, lo), hi)


@dataclass(frozen=True)
class InstanceConfig:
    id: str
    name: str
    host: str
    port: int
    dbname: str
    user: str
    password: str = field(default="", repr=False)
    sslmode: str = "prefer"
    sslrootcert: str = ""
    environment: str = ""
    log_file: str = ""
    log_utc_offset_minutes: int = 0
    enabled: bool = True

    @property
    def tls_verified(self) -> bool:
        return self.sslmode in ("verify-ca", "verify-full")

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "host": self.host, "port": self.port, "dbname": self.dbname,
                "user": self.user, "sslmode": self.sslmode, "tls_verified": self.tls_verified,
                "environment": self.environment, "log_file_configured": bool(self.log_file), "enabled": self.enabled}


@dataclass
class Settings:
    demo_mode: bool = False
    auto_monitor: bool = True
    data_dir: Path = ROOT / "data"
    instances: list[InstanceConfig] = field(default_factory=list)
    defaults: dict[str, float] = field(default_factory=dict)
    overrides: dict[str, float] = field(default_factory=dict)
    auth_mode: str = "none"
    tokens: dict[str, str] = field(default_factory=dict, repr=False)   # token -> role
    query_text_mode: str = "redact"                                      # redact | full | none
    redact_patterns: list[str] = field(default_factory=list)
    log_tail_bytes: int = 2 * 1024 * 1024
    max_concurrent: int = 2
    connect_timeout_s: int = 5
    statement_timeout_ms: int = 5000
    pool_max: int = 3
    patroni_url: str = ""
    patroni_verify_tls: bool = True
    config_warnings: list[str] = field(default_factory=list)

    # ----------------------------------------------------------------- tunables
    def t(self, name: str):
        return self.overrides.get(name, self.defaults.get(name, TUNABLES[name][1]))

    def tunable_rows(self) -> list[dict[str, Any]]:
        rows = []
        for name, (typ, default, lo, hi, group, desc) in TUNABLES.items():
            rows.append({"name": name, "group": group, "description": desc, "type": typ.__name__, "min": lo, "max": hi,
                         "default": self.defaults.get(name, default), "value": self.t(name),
                         "overridden": name in self.overrides})
        return rows

    def set_override(self, name: str, value: Any) -> float:
        if name not in TUNABLES:
            raise KeyError(name)
        typ, _d, lo, hi, _g, _desc = TUNABLES[name]
        try:
            x = typ(float(value)) if typ is int else float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{name} must be a number") from None
        if not (lo <= x <= hi):
            raise ValueError(f"{name} must be between {lo:g} and {hi:g}")
        self.overrides[name] = x
        return x

    def public(self) -> dict[str, Any]:
        return {"demo_mode": self.demo_mode, "auto_monitor": self.auto_monitor, "auth_mode": self.auth_mode,
                "query_text_mode": self.query_text_mode, "instances": [i.public() for i in self.instances],
                "max_concurrent_collections": self.max_concurrent, "statement_timeout_ms": self.statement_timeout_ms,
                "connect_timeout_s": self.connect_timeout_s, "patroni_configured": bool(self.patroni_url),
                "log_tail_bytes": self.log_tail_bytes, "warnings": list(self.config_warnings)}

    # ----------------------------------------------------------------- loading
    @classmethod
    def from_env(cls, env: dict[str, str] | None = None, data_dir: Path | None = None) -> "Settings":
        e = dict(os.environ if env is None else env)
        s = cls()
        s.demo_mode = _flag(e.get("DEMO_MODE"), False)
        s.auto_monitor = _flag(e.get("AUTO_MONITOR"), True)
        s.data_dir = Path(data_dir or e.get("DATA_DIR") or ROOT / "data")
        s.auth_mode = (e.get("AUTH_MODE") or "none").strip().lower()
        if s.auth_mode not in ("none", "token"):
            raise ValueError("AUTH_MODE must be 'none' or 'token'")
        for pair in filter(None, (p.strip() for p in (e.get("API_TOKENS") or "").split(","))):
            role, _, tok = pair.partition(":")
            if role not in ("viewer", "operator", "admin") or len(tok) < 16:
                raise ValueError("API_TOKENS entries must look like role:token (roles viewer/operator/admin, token >= 16 characters)")
            s.tokens[tok] = role
        if e.get("API_TOKEN"):
            if len(e["API_TOKEN"]) < 16:
                raise ValueError("API_TOKEN must be at least 16 characters")
            s.tokens[e["API_TOKEN"]] = "admin"
        if s.auth_mode == "token" and not s.tokens:
            raise ValueError("AUTH_MODE=token requires API_TOKEN or API_TOKENS")
        s.query_text_mode = (e.get("QUERY_TEXT_MODE") or "redact").strip().lower()
        if s.query_text_mode not in ("redact", "full", "none"):
            raise ValueError("QUERY_TEXT_MODE must be redact, full or none")
        s.redact_patterns = [p for p in (e.get("REDACT_PATTERNS") or "").split("||") if p.strip()]
        s.log_tail_bytes = int(_num(e.get("PG_LOG_TAIL_BYTES"), int, 65536, 10485760, 2097152))
        s.max_concurrent = int(_num(e.get("MAX_CONCURRENT_COLLECTIONS"), int, 1, 8, 2))
        s.connect_timeout_s = int(_num(e.get("DB_CONNECT_TIMEOUT"), int, 1, 60, 5))
        s.statement_timeout_ms = int(_num(e.get("DB_STATEMENT_TIMEOUT_MS"), int, 500, 120000, 5000))
        s.pool_max = int(_num(e.get("DB_POOL_MAX"), int, 1, 10, 3))
        s.patroni_url = (e.get("PATRONI_URL") or "").strip().rstrip("/")
        s.patroni_verify_tls = _flag(e.get("PATRONI_VERIFY_TLS"), True)
        for name, (typ, default, lo, hi, _g, _d) in TUNABLES.items():
            raw = e.get(ENV_ALIASES.get(name, "")) or e.get("DIR_" + name.upper())
            s.defaults[name] = _num(raw, typ, lo, hi, default) if raw not in (None, "") else default
        s.instances = [] if s.demo_mode else s._load_instances(e)
        if s.demo_mode:
            s.instances = [InstanceConfig(id="demo", name="Synthetic demo instance", host="demo.invalid", port=5432,
                                          dbname="demo", user="demo", sslmode="disable", environment="DEMO (synthetic)")]
        for inst in s.instances:
            if not s.demo_mode and not inst.tls_verified:
                s.config_warnings.append(f"Instance '{inst.id}': TLS certificate is not verified (sslmode={inst.sslmode}). "
                                         "Use sslmode=verify-full with DB_SSLROOTCERT for remote databases.")
        return s

    def _load_instances(self, e: dict[str, str]) -> list[InstanceConfig]:
        out: list[InstanceConfig] = []
        if all(e.get(k) for k in ("DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD")):
            out.append(self._mk(e.get("DB_INSTANCE_ID") or "default", {
                "name": e.get("DB_INSTANCE_NAME") or "Primary instance", "host": e["DB_HOST"], "port": e["DB_PORT"],
                "dbname": e["DB_NAME"], "user": e["DB_USER"], "password": e["DB_PASSWORD"],
                "sslmode": e.get("DB_SSLMODE", "prefer"), "sslrootcert": e.get("DB_SSLROOTCERT", ""),
                "environment": e.get("DB_ENVIRONMENT", ""), "log_file": e.get("PG_LOG_FILE", ""),
                "log_utc_offset_minutes": int(_num(e.get("PG_LOG_UTC_OFFSET_MINUTES"), int, -840, 840, 0))}))
        path = e.get("INSTANCES_FILE")
        if path:
            try:
                items = json.loads(Path(path).read_text())
            except (OSError, ValueError) as exc:
                raise ValueError(f"INSTANCES_FILE could not be read: {type(exc).__name__}") from None
            for item in items if isinstance(items, list) else []:
                pw = e.get(str(item.get("password_env", ""))) or ""
                out.append(self._mk(str(item.get("id", "")), {**item, "password": pw}))
        ids = [i.id for i in out]
        if len(ids) != len(set(ids)):
            raise ValueError("Instance ids must be unique")
        return out

    def _mk(self, iid: str, d: dict[str, Any]) -> InstanceConfig:
        if not ID_RE.match(iid):
            raise ValueError(f"Invalid instance id {iid!r}: use lowercase letters, digits, - and _ (max 32)")
        ssl = str(d.get("sslmode", "prefer"))
        if ssl not in SSL_MODES:
            raise ValueError(f"Instance {iid}: sslmode must be one of {', '.join(SSL_MODES)}")
        return InstanceConfig(id=iid, name=str(d.get("name") or iid), host=str(d["host"]), port=int(d.get("port", 5432)),
                              dbname=str(d["dbname"]), user=str(d["user"]), password=str(d.get("password", "")),
                              sslmode=ssl, sslrootcert=str(d.get("sslrootcert", "")), environment=str(d.get("environment", "")),
                              log_file=str(d.get("log_file", "")), log_utc_offset_minutes=int(d.get("log_utc_offset_minutes", 0)),
                              enabled=bool(d.get("enabled", True)))
