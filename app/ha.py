"""Optional Patroni REST integration boundary (read-only). Not exercised against a real Patroni cluster in this repository's tests."""
from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request
from typing import Any

from .config import Settings


def patroni_status(settings: Settings, timeout: float = 3.0) -> dict[str, Any]:
    url = settings.patroni_url
    if not url.startswith(("http://", "https://")):
        return {"status": "misconfigured", "note": "PATRONI_URL must start with http:// or https://"}
    ctx = ssl.create_default_context() if settings.patroni_verify_tls else ssl._create_unverified_context()   # verification is on unless explicitly disabled
    try:
        with urllib.request.urlopen(urllib.request.Request(url + "/cluster", headers={"Accept": "application/json"}), timeout=timeout, context=ctx if url.startswith("https") else None) as r:
            data = json.loads(r.read(1_000_000))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return {"status": "unreachable", "note": f"Patroni REST API could not be read ({type(exc).__name__}).", "tls_verified": settings.patroni_verify_tls}
    members = [{"name": m.get("name"), "role": m.get("role"), "state": m.get("state"), "lag": m.get("lag"), "timeline": m.get("timeline")} for m in data.get("members", [])]
    leaders = [m for m in members if m["role"] in ("leader", "master", "primary")]
    return {"status": "ok" if len(leaders) == 1 else "attention", "scope": data.get("scope"), "members": members, "leaders": len(leaders), "tls_verified": settings.patroni_verify_tls,
            "note": "Reported by the Patroni REST API, not inferred from SQL. Failover history is not read."}
