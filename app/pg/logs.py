"""PostgreSQL server log parsing (stderr text format) and incremental tailing.

Supported: `log_destination = stderr` with a prefix containing a timestamp and `[pid]`, e.g. `%m [%p] %q%u@%d ` or `%m [%p] user=%u db=%d app=%a client=%h `.
Not supported: csvlog / jsonlog (documented limitation). Messages are redacted before storage.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timedelta
from typing import Any

from ..security import redact_log_message, redact_sql, sql_fingerprint
from ..timeutil import UTC, iso
from .pool import PgError

LEVELS = r"(?:DEBUG[1-5]?|INFO|NOTICE|WARNING|ERROR|LOG|FATAL|PANIC|DETAIL|HINT|STATEMENT|CONTEXT|QUERY|LOCATION)"
HEAD = re.compile(r"^(?P<ts>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?)(?:\s+(?P<tz>UTC|GMT|Z|[+-]\d{2}(?::?\d{2})?|[A-Z]{2,5}))?\s+\[(?P<pid>\d+)\][: ]\s*(?P<rest>.*)$")
LEVEL = re.compile(rf"(?:^|\s)(?P<level>{LEVELS}):\s+(?P<msg>.*)$", re.S)
KV = re.compile(r"\b(user|db|app|client)=(\S*)")
USERDB = re.compile(r"^\s*(\S+)@(\S+)\s*$")
EXTRA = ("DETAIL", "HINT", "STATEMENT", "CONTEXT", "QUERY")
MAX_EVENTS_PER_RUN = 300


def _ts(head: re.Match, offset_min: int) -> datetime | None:
    from ..timeutil import parse_ts
    base = parse_ts(head.group("ts"))
    if base is None:
        return None
    tz = head.group("tz")
    if tz and tz not in ("UTC", "GMT", "Z") and re.match(r"^[+-]\d", tz):
        return parse_ts(f"{head.group('ts')}{tz}")
    return base - timedelta(minutes=offset_min)


def parse_log_text(text: str, offset_min: int = 0) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    last: dict[str, Any] | None = None
    field = "msg"
    for n, line in enumerate(text.splitlines(), 1):
        m = HEAD.match(line)
        if m:
            lv = LEVEL.search(m.group("rest"))
            ts = _ts(m, offset_min)
            if not lv or ts is None:
                continue
            level, msg = lv.group("level"), lv.group("msg")
            prefix = m.group("rest")[:lv.start()]
            pid = int(m.group("pid"))
            if level in EXTRA and last and last["pid"] == pid:
                last.setdefault("extra", {}).setdefault(level, "")
                last["extra"][level] = (last["extra"][level] + "\n" + msg).strip()
                field = level
                continue
            kv = dict(KV.findall(prefix))
            ud = USERDB.match(prefix)
            last = {"ts": ts, "pid": pid, "level": level, "msg": msg, "extra": {}, "line": n,
                    "user": kv.get("user") or (ud.group(1) if ud else None), "db": kv.get("db") or (ud.group(2) if ud else None),
                    "app": kv.get("app"), "client": kv.get("client")}
            entries.append(last)
            field = "msg"
        elif last is not None and line[:1] in ("\t", " "):
            if field == "msg":
                last["msg"] += "\n" + line.strip()
            else:
                last["extra"][field] += "\n" + line.strip()
    return entries


_P_WAIT = re.compile(r"Process (\d+) waits for (.+?) on (.+?); blocked by process (\d+)")
_P_STMT = re.compile(r"^Process (\d+): (.*)$", re.M)
_LOCKWAIT = re.compile(r"process (\d+) still waiting for (\w+) on (.+?) after ([\d.]+) ms")
_ACQ = re.compile(r"process (\d+) acquired (\w+) on (.+?) after ([\d.]+) ms")
_HOLDER = re.compile(r"Process holding the lock: (\d+)")
_NORM = re.compile(r"\d+")


def _redact_stmt(s: str | None, mode: str, extra: list[str]) -> str | None:
    return redact_sql(s, mode, extra) if s else s


def redact_detail(text: str | None, mode: str = "redact", extra: list[str] | None = None) -> str | None:
    """DETAIL lines can embed statements ("Process 12: UPDATE ... 'value'") and constraint values: mask both."""
    if not text:
        return text
    out = []
    for line in text.splitlines():
        m = _P_STMT.match(line.strip())
        out.append(f"Process {m.group(1)}: {redact_sql(m.group(2), mode, extra or [])}" if m else redact_log_message(line))
    return "\n".join(out)


def classify_entry(e: dict[str, Any], mode: str = "redact", extra_patterns: list[str] | None = None) -> dict[str, Any] | None:
    """Turn a parsed log entry into an evidence candidate (instant event), or None if it is not noteworthy."""
    msg, level, ts = e["msg"], e["level"], e["ts"]
    ex = e.get("extra", {})
    stmt = _redact_stmt(ex.get("STATEMENT"), mode, extra_patterns or [])
    base = {"instant": True, "ts": ts, "collector": "errorlog", "source": "PostgreSQL server log", "pid": e["pid"],
            "query_fingerprint": sql_fingerprint(ex.get("STATEMENT")), "confidence": "observed",
            "limitations": "Parsed from the server log; messages are redacted and only complete lines inside the configured read window are seen."}
    who = {"user": e.get("user"), "database": e.get("db"), "application": e.get("app")}
    fp = lambda *parts: "log:" + hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:16]
    first = msg.splitlines()[0]
    if level == "ERROR" and msg.startswith("deadlock detected"):
        detail = ex.get("DETAIL", "")
        procs = [{"pid": int(a), "waits_for": f"{lock} on {obj}", "blocked_by": int(b)} for a, lock, obj, b in _P_WAIT.findall(detail)]
        stmts = {int(p): _redact_stmt(t, mode, extra_patterns or []) for p, t in _P_STMT.findall(detail)}
        return {**base, "category": "deadlock", "severity": "critical", "title": f"Deadlock detected (PIDs {', '.join(str(p['pid']) for p in procs) or e['pid']})",
                "fingerprint": fp("deadlock", ts.isoformat(), e["pid"]), "details": {**who, "processes": procs, "statements": stmts, "victim_statement": stmt}}
    m = _LOCKWAIT.search(msg)
    if m and level == "LOG":
        holder = _HOLDER.search(ex.get("DETAIL", ""))
        return {**base, "category": "lock_wait_log", "severity": "info", "title": f"PID {m.group(1)} waited >{float(m.group(4)) / 1000:.1f}s for {m.group(2)}",
                "fingerprint": fp("lockwait", ts.isoformat(), m.group(1), m.group(3)), "pid": int(m.group(1)),
                "details": {**who, "waiter_pid": int(m.group(1)), "lock_mode": m.group(2), "lock_target": m.group(3), "waited_ms": float(m.group(4)),
                            "holder_pid": int(holder.group(1)) if holder else None, "statement": stmt}}
    if level in ("FATAL", "PANIC", "ERROR") and re.search(r"too many (connections|clients)|remaining connection slots are reserved", msg):
        return {**base, "category": "connection_refused", "severity": "critical", "title": "Connection refused: no free connection slots",
                "fingerprint": fp("connslots", ts.strftime("%Y%m%d%H%M%S"), who["user"]), "details": {**who, "message": redact_log_message(first)}}
    if level == "FATAL" and "password authentication failed" in msg:
        return {**base, "category": "auth_failure", "severity": "warning", "title": f"Authentication failed for {who['user'] or 'unknown user'}",
                "fingerprint": fp("auth", ts.strftime("%Y%m%d%H%M"), who["user"], e.get("client")), "details": {**who, "client": e.get("client"), "message": redact_log_message(first)}}
    if level == "ERROR" and re.search(r"canceling statement due to (statement|lock) timeout", msg):
        return {**base, "category": "statement_cancelled", "severity": "warning", "title": redact_log_message(first)[:120],
                "fingerprint": fp("cancel", ts.isoformat(), e["pid"]), "details": {**who, "message": redact_log_message(first), "statement": stmt}}
    if re.search(r"terminated by signal|not properly shut down|database system is ready to accept connections|received (fast|immediate|smart) shutdown request", msg):
        crash = bool(re.search(r"terminated by signal|not properly shut down", msg))
        return {**base, "category": "server_restart", "severity": "critical" if crash else "warning", "title": redact_log_message(first)[:140],
                "fingerprint": fp("restart", ts.isoformat(), first[:40]), "details": {"message": redact_log_message(first), "possible_crash": crash}}
    if "checkpoints are occurring too frequently" in msg:
        return {**base, "category": "checkpoint_warning", "severity": "warning", "title": "PostgreSQL warned: checkpoints are occurring too frequently",
                "fingerprint": fp("ckpt", ts.strftime("%Y%m%d%H%M")), "details": {"message": redact_log_message(first), "hint": redact_log_message(ex.get("HINT", ""))}}
    if "canceling autovacuum task" in msg:
        return {**base, "category": "autovacuum_cancelled", "severity": "info", "title": "An autovacuum task was cancelled",
                "fingerprint": fp("avcancel", ts.isoformat(), e["pid"]), "details": {"message": redact_log_message(first), "context": redact_log_message(ex.get("CONTEXT", ""))}}
    if level in ("ERROR", "FATAL", "PANIC"):
        norm = _NORM.sub("N", redact_log_message(first) or "")[:160]
        return {**base, "category": "error_log", "severity": "critical" if level == "PANIC" else "warning", "title": f"{level}: {(redact_log_message(first) or '')[:110]}",
                "fingerprint": fp("err", norm, ts.strftime("%Y%m%d%H%M")), "details": {**who, "level": level, "message": redact_log_message(msg)[:600],
                                                                                     "detail": (redact_detail(ex.get("DETAIL", ""), mode, extra_patterns) or "")[:400], "statement": stmt}}
    return None


def classify_all(entries: list[dict[str, Any]], mode: str, extra: list[str]) -> list[dict[str, Any]]:
    out = []
    for e in entries:
        c = classify_entry(e, mode, extra)
        if c:
            out.append(c)
        if len(out) >= MAX_EVENTS_PER_RUN:
            break
    return out


def read_window(path: str, offset: int | None, tail_bytes: int) -> tuple[str, dict[str, Any]]:
    """Read new complete lines since `offset`, bounded by tail_bytes. Detects rotation (inode change / truncation)."""
    try:
        st = os.stat(path)
    except FileNotFoundError:
        raise PgError("LOG_NOT_READABLE", "The configured server log file does not exist.", "Check PG_LOG_FILE and that the file is on this host.") from None
    except PermissionError:
        raise PgError("LOG_NOT_READABLE", "The application cannot read the configured server log file.", "Grant the app's OS user read access to the log file.") from None
    info: dict[str, Any] = {"inode": st.st_ino, "size": st.st_size, "rotated": False, "skipped_bytes": 0}
    start = offset or 0
    if start > st.st_size:
        start, info["rotated"] = 0, True
    if st.st_size - start > tail_bytes:
        info["skipped_bytes"] = st.st_size - start - tail_bytes
        start = st.st_size - tail_bytes
    try:
        with open(path, "rb") as f:
            f.seek(start)
            raw = f.read(tail_bytes)
    except PermissionError:
        raise PgError("LOG_NOT_READABLE", "The application cannot read the configured server log file.", "Grant the app's OS user read access to the log file.") from None
    cut = raw.rfind(b"\n")
    if cut == -1 and st.st_size - start > len(raw):
        cut = len(raw) - 1
    complete = raw[: cut + 1] if cut >= 0 else b""
    info["new_offset"] = start + len(complete)
    info["bytes_read"] = len(complete)
    return complete.decode("utf-8", "replace"), info


def collect_errorlog(ctx) -> dict[str, Any]:
    cfg = ctx.inst.cfg
    if not cfg.log_file:
        raise PgError("LOG_NOT_CONFIGURED", "No server log file is configured for this instance.", "Set PG_LOG_FILE (see docs/postgres-logging.md).")
    key = f"log_state:{cfg.id}"
    state = json.loads(ctx.store.kv_get(key) or "{}") if ctx.store else {}
    try:
        st_now = os.stat(cfg.log_file)
    except OSError:
        st_now = None
    offset = state.get("offset") if state.get("path") == cfg.log_file and st_now is not None and state.get("inode") == st_now.st_ino else None
    if offset is None and state.get("path") == cfg.log_file and st_now is not None and state.get("inode") not in (None, st_now.st_ino):
        offset = 0     # rotated: read the new file from its start
    text, info = read_window(cfg.log_file, offset, ctx.settings.log_tail_bytes)
    entries = parse_log_text(text, cfg.log_utc_offset_minutes)
    cands = classify_all(entries, ctx.settings.query_text_mode, ctx.settings.redact_patterns)
    return {"candidates": cands, "parsed_entries": len(entries), "bytes_read": info["bytes_read"], "skipped_bytes": info["skipped_bytes"],
            "rotated": info["rotated"], "state_update": {"key": key, "value": {"path": cfg.log_file, "inode": info["inode"], "offset": info["new_offset"]}},
            "note": "stderr text logs only (csvlog/jsonlog are not parsed). Only complete lines are consumed; a partial last line is read next run."}


def recent_errors(path: str, tail_bytes: int, limit: int, offset_min: int, mode: str, extra: list[str]) -> list[dict[str, Any]]:
    text, _info = read_window(path, 0, tail_bytes)
    out = []
    for e in reversed(parse_log_text(text, offset_min)):
        if e["level"] in ("ERROR", "FATAL", "PANIC"):
            out.append({"time": iso(e["ts"]), "pid": e["pid"], "level": e["level"], "user": e.get("user"), "database": e.get("db"),
                        "message": redact_log_message(e["msg"].splitlines()[0])[:300], "statement": _redact_stmt(e.get("extra", {}).get("STATEMENT"), mode, extra),
                        "detail": redact_detail(e.get("extra", {}).get("DETAIL"), mode, extra)})
        if len(out) >= limit:
            break
    return out
