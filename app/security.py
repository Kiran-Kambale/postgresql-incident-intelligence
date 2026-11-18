"""Redaction, authentication/authorization, audit logging and security headers."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Callable

from fastapi import Depends, Request

from .errors import AppError

log = logging.getLogger("dir.security")

# --------------------------------------------------------------------------- redaction
_DOLLAR = re.compile(r"\$\$.*?\$\$|\$([A-Za-z_]\w*)\$.*?\$\1\$", re.S)
_STRING = re.compile(r"[eE]?'(?:[^']|'')*'")
_NUMBER = re.compile(r"(?<![\w$.])\d+(?:\.\d+)?(?:[eE][+-]?\d+)?(?![\w.])")
_PASSWORD_CLAUSE = re.compile(r"(?i)\b(PASSWORD|ENCRYPTED\s+PASSWORD)\s+('(?:[^']|'')*'|\S+)")
_KV_SECRET = re.compile(r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key)\b(\s*(?:=>|=|:)\s*)('(?:[^']|'')*'|\"[^\"]*\"|[^\s,;)]+)")
_URL_CRED = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)([^/\s:@]+):([^@\s/]+)@")
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]*")
_AWS = re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")
_LONGHEX = re.compile(r"\b[0-9a-fA-F]{32,}\b")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_KEY_DETAIL = re.compile(r"(?i)\bKey \(([^)]*)\)=\(([^)]*)\)")
_SPACE = re.compile(r"\s+")


def redact_secrets(text: str) -> str:
    """Always-on masking of credentials, applied in every query-text mode and to logs."""
    if not text:
        return text
    t = _PASSWORD_CLAUSE.sub(lambda m: f"{m.group(1)} '<redacted>'", text)
    t = _URL_CRED.sub(lambda m: f"{m.group(1)}{m.group(2)}:<redacted>@", t)
    t = _KV_SECRET.sub(lambda m: f"{m.group(1)}{m.group(2)}<redacted>", t)
    t = _BEARER.sub("Bearer <redacted>", t)
    t = _JWT.sub("<jwt-redacted>", t)
    t = _AWS.sub("<key-redacted>", t)
    return _LONGHEX.sub("<hex-redacted>", t)


def redact_sql(text: str | None, mode: str = "redact", extra: list[str] | None = None) -> str | None:
    """Prepare SQL text for display/storage. mode: redact (literals masked) | full (secrets only) | none (hidden)."""
    if text is None:
        return None
    if mode == "none":
        return "<query text hidden by QUERY_TEXT_MODE=none>" if text.strip() else text
    t = redact_secrets(text)
    if mode == "redact":
        t = _DOLLAR.sub("'?'", t)
        t = _STRING.sub("'?'", t)
        t = _EMAIL.sub("<email>", t)
        t = _NUMBER.sub("?", t)
    for pat in extra or []:
        try:
            t = re.sub(pat, "<redacted>", t)
        except re.error:
            pass
    return t


def redact_log_message(text: str | None) -> str | None:
    """Server log messages: mask credentials, constraint-detail values and e-mail addresses."""
    if not text:
        return text
    t = redact_secrets(text)
    t = _KEY_DETAIL.sub(lambda m: f"Key ({m.group(1)})=(<redacted>)", t)
    return _EMAIL.sub("<email>", t)


def sql_fingerprint(text: str | None) -> str | None:
    if not text:
        return None
    norm = _SPACE.sub(" ", redact_sql(text, "redact") or "").strip().lower()
    return hashlib.sha1(norm.encode("utf-8", "replace")).hexdigest()[:16]


_DSN_PW = re.compile(r"(?i)(password\s*=\s*)\S+")


def safe_text(text: Any, limit: int = 300) -> str:
    """Sanitize an exception message before it reaches an API response or log line."""
    t = redact_secrets(str(text))
    t = _DSN_PW.sub(r"\1<redacted>", t)
    return _SPACE.sub(" ", t).strip()[:limit]


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = safe_text(record.getMessage(), 2000)
            record.args = ()
        except Exception:
            pass
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        from .timeutil import iso
        payload = {"ts": iso(), "level": record.levelname, "logger": record.name, "msg": record.getMessage()}
        for k in ("request_id", "instance", "tier", "event"):
            if hasattr(record, k):
                payload[k] = getattr(record, k)
        if record.exc_info:
            payload["exc"] = safe_text(self.formatException(record.exc_info), 1500)
        return json.dumps(payload, default=str)


def setup_logging(level: str = "INFO") -> None:
    root = logging.getLogger("dir")
    if root.handlers:
        return
    h = logging.StreamHandler()
    h.setFormatter(JsonFormatter())
    h.addFilter(RedactingFilter())
    root.addHandler(h)
    root.setLevel(level)
    root.propagate = False


# --------------------------------------------------------------------------- auth
ROLE_ORDER = {"viewer": 1, "operator": 2, "admin": 3}


@dataclass(frozen=True)
class Principal:
    name: str
    role: str

    def allows(self, role: str) -> bool:
        return ROLE_ORDER[self.role] >= ROLE_ORDER[role]


def _principal(request: Request) -> Principal:
    s = request.app.state.ctx.settings
    if s.auth_mode == "none":
        return Principal("local", "admin")
    header = request.headers.get("authorization", "")
    token = header[7:].strip() if header.lower().startswith("bearer ") else ""
    for known, role in s.tokens.items():
        if token and hmac.compare_digest(token.encode(), known.encode()):
            return Principal(role, role)
    raise AppError("Authentication required", status=401, code="UNAUTHENTICATED",
                   hint="Send the API token as 'Authorization: Bearer <token>'.")


def require(role: str = "viewer") -> Callable:
    def dep(request: Request) -> Principal:
        p = _principal(request)
        if not p.allows(role):
            audit(request, "authz.denied", request.url.path, "denied", {"needed": role, "role": p.role}, principal=p)
            raise AppError(f"This action needs the {role} role", status=403, code="FORBIDDEN")
        request.state.principal = p
        return p
    return Depends(dep)


def audit(request: Request | None, action: str, target: str = "", outcome: str = "ok",
          details: dict[str, Any] | None = None, principal: Principal | None = None) -> None:
    """Record a sensitive operation. Details are redacted; failures to audit are logged, never raised."""
    try:
        ctx = request.app.state.ctx if request is not None else None
        if ctx is None:
            return
        p = principal or getattr(request.state, "principal", None)
        ip = request.client.host if request.client else ""
        clean = json.loads(redact_secrets(json.dumps(details or {}, default=str)))
        ctx.store.audit_add(p.name if p else "anonymous", p.role if p else "-", action, target[:200], outcome, ip, clean)
    except Exception:  # pragma: no cover - auditing must not break the request path
        log.warning("audit write failed for action=%s", action)


SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
                               "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
    "Cache-Control": "no-store",
}
