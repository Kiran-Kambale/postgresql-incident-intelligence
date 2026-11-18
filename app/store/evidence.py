"""Evidence lifecycle: observations are upserted by fingerprint, cleared only when their collector succeeded."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from ..timeutil import iso, parse_ts, utcnow
from .core import jdump, jload, like_escape

SEV = {"info": 0, "warning": 1, "critical": 2}
SORTS = {
    "first_seen": "first_seen", "last_seen": "last_seen", "category": "category", "title": "title",
    "observations": "observation_count",
    "severity": "CASE severity WHEN 'critical' THEN 2 WHEN 'warning' THEN 1 ELSE 0 END",
}
_INSERT = ("INSERT INTO evidence(id,instance_id,category,severity,title,fingerprint,status,first_seen,last_seen,cleared_at,observation_count,source,collector,"
           "pid,query_fingerprint,collection_status,confidence,limitations,details_json,is_demo,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)")


def new_evidence_id(ts: datetime | None = None) -> str:
    return f"EV-{(ts or utcnow()).strftime('%Y%m%d')}-{uuid.uuid4().hex[:8].upper()}"


def ev_out(r: dict[str, Any]) -> dict[str, Any]:
    d = dict(r)
    details = jload(d.pop("details_json", None), {}) or {}
    d["peaks"] = details.pop("_peaks", {}) if isinstance(details, dict) else {}
    d["details"] = details
    end = parse_ts(d.get("cleared_at") or d.get("last_seen"))
    start = parse_ts(d.get("first_seen"))
    d["duration_s"] = round((end - start).total_seconds(), 1) if start and end else None
    d["is_demo"] = bool(d.get("is_demo"))
    return d


class EvidenceMixin:
    def evidence_apply(self, instance_id: str, ts: datetime, candidates: list[dict[str, Any]], ok_collectors: set[str],
                       failed_collectors: set[str], is_demo: bool = False) -> dict[str, list[dict[str, Any]]]:
        """Upsert candidate observations; clear active evidence whose collector succeeded but no longer observes it."""
        ts_iso = iso(ts)
        changes: dict[str, list] = {"new": [], "updated": [], "escalated": [], "cleared": []}
        with self.tx() as con:
            active = {r["fingerprint"]: dict(r) for r in con.execute(
                "SELECT * FROM evidence WHERE instance_id=? AND is_demo=? AND status='active'", (instance_id, int(is_demo))).fetchall()}
            seen: set[str] = set()
            for c in candidates:
                fp = c["fingerprint"]
                if fp in seen:
                    continue
                seen.add(fp)
                if c.get("instant"):        # a point-in-time event (log line, counter increase): stored as already cleared
                    if con.execute("SELECT 1 FROM evidence WHERE instance_id=? AND fingerprint=? LIMIT 1", (instance_id, fp)).fetchone():
                        continue
                    first = iso(c["ts"]) if c.get("ts") else ts_iso
                    eid = new_evidence_id(c.get("ts") or ts)
                    con.execute(_INSERT, (eid, instance_id, c["category"], c["severity"], c["title"], fp, "cleared", first, first, first, 1, c.get("source", ""),
                                          c.get("collector", ""), c.get("pid"), c.get("query_fingerprint"), "ok", c.get("confidence", "observed"),
                                          c.get("limitations", ""), jdump(c.get("details") or {}), int(is_demo), ts_iso))
                    changes["new"].append(dict(con.execute("SELECT * FROM evidence WHERE id=?", (eid,)).fetchone()))
                    continue
                details = dict(c.get("details") or {})
                peaks = dict(c.get("peaks") or {})
                if fp in active:
                    old = active[fp]
                    oldpeaks = (jload(old["details_json"], {}) or {}).get("_peaks", {})
                    merged = {k: max(v, oldpeaks.get(k, v)) for k, v in peaks.items()}
                    for k, v in oldpeaks.items():
                        merged.setdefault(k, v)
                    if merged:
                        details["_peaks"] = merged
                    sev = c["severity"] if SEV[c["severity"]] > SEV[old["severity"]] else old["severity"]
                    con.execute("UPDATE evidence SET last_seen=?, observation_count=observation_count+1, severity=?, title=?, details_json=?, "
                                "collection_status='ok', pid=COALESCE(?, pid), confidence=? WHERE id=?",
                                (ts_iso, sev, c["title"], jdump(details), c.get("pid"), c.get("confidence", old["confidence"]), old["id"]))
                    row = dict(con.execute("SELECT * FROM evidence WHERE id=?", (old["id"],)).fetchone())
                    changes["updated"].append(row)
                    if SEV[sev] > SEV[old["severity"]]:
                        changes["escalated"].append(row)
                else:
                    if peaks:
                        details["_peaks"] = peaks
                    eid = new_evidence_id(ts)
                    con.execute(_INSERT, (eid, instance_id, c["category"], c["severity"], c["title"], fp, "active", ts_iso, ts_iso, None, 1, c.get("source", ""),
                                          c.get("collector", ""), c.get("pid"), c.get("query_fingerprint"), "ok", c.get("confidence", "observed"),
                                          c.get("limitations", ""), jdump(details), int(is_demo), ts_iso))
                    changes["new"].append(dict(con.execute("SELECT * FROM evidence WHERE id=?", (eid,)).fetchone()))
            for fp, old in active.items():
                if fp in seen:
                    continue
                if old["collector"] in ok_collectors:
                    con.execute("UPDATE evidence SET status='cleared', cleared_at=?, collection_status='ok' WHERE id=?", (ts_iso, old["id"]))
                    changes["cleared"].append({**old, "status": "cleared", "cleared_at": ts_iso})
                elif old["collector"] in failed_collectors:
                    con.execute("UPDATE evidence SET collection_status='stale' WHERE id=?", (old["id"],))
        return changes

    def evidence_get(self, evidence_id: str) -> dict[str, Any] | None:
        r = self.one("SELECT * FROM evidence WHERE id=?", (evidence_id,))
        return ev_out(r) if r else None

    def evidence_list(self, *, instance_id: str | None = None, categories: list[str] | None = None, severities: list[str] | None = None,
                      status: str | None = None, t_from: str | None = None, t_to: str | None = None, q: str | None = None,
                      incident_id: str | None = None, sort: str = "first_seen", order: str = "desc", limit: int = 50, offset: int = 0,
                      is_demo: bool = False) -> tuple[list[dict[str, Any]], int]:
        where, p = ["e.is_demo=?"], [int(is_demo)]
        if instance_id:
            where.append("e.instance_id=?"); p.append(instance_id)
        if categories:
            where.append(f"e.category IN ({','.join('?' for _ in categories)})"); p += categories
        if severities:
            where.append(f"e.severity IN ({','.join('?' for _ in severities)})"); p += severities
        if status in ("active", "cleared"):
            where.append("e.status=?"); p.append(status)
        if t_from:   # overlap semantics: still-active evidence overlaps any later window
            where.append("(e.status='active' OR COALESCE(e.cleared_at, e.last_seen) >= ?)"); p.append(t_from)
        if t_to:
            where.append("e.first_seen <= ?"); p.append(t_to)
        if q:
            where.append("instr(lower(e.id||' '||e.title||' '||e.category||' '||COALESCE(e.pid,'')||' '||e.source||' '||e.details_json), ?) > 0"); p.append(q.lower())
        join = ""
        if incident_id:
            join = " JOIN incident_evidence ie ON ie.evidence_id = e.id AND ie.incident_id = ?"
            p.insert(0, incident_id)
        key = SORTS.get(sort, SORTS["first_seen"])
        direction = "ASC" if order.lower() == "asc" else "DESC"
        w = " AND ".join(where)
        total = self.one(f"SELECT count(*) AS n FROM evidence e{join} WHERE {w}", p)["n"]
        rows = self.rows(f"SELECT e.* FROM evidence e{join} WHERE {w} ORDER BY {key} {direction}, e.id DESC LIMIT ? OFFSET ?", [*p, limit, offset])
        return [ev_out(r) for r in rows], total

    def evidence_window(self, instance_id: str, t_from: str, t_to: str, is_demo: bool = False, exclude_id: str | None = None, limit: int = 300) -> list[dict[str, Any]]:
        rows = self.rows(
            "SELECT * FROM evidence WHERE instance_id=? AND is_demo=? AND first_seen <= ? AND (status='active' OR COALESCE(cleared_at,last_seen) >= ?) "
            "AND id != ? ORDER BY first_seen LIMIT ?", (instance_id, int(is_demo), t_to, t_from, exclude_id or "", limit))
        return [ev_out(r) for r in rows]

    def evidence_counts(self, instance_id: str | None, is_demo: bool = False) -> dict[str, Any]:
        base, p = "is_demo=?", [int(is_demo)]
        if instance_id:
            base += " AND instance_id=?"; p.append(instance_id)
        by_sev = {r["severity"]: r["n"] for r in self.rows(f"SELECT severity, count(*) AS n FROM evidence WHERE {base} AND status='active' GROUP BY severity", p)}
        by_cat = {r["category"]: r["n"] for r in self.rows(f"SELECT category, count(*) AS n FROM evidence WHERE {base} AND status='active' GROUP BY category", p)}
        total = self.one(f"SELECT count(*) AS n FROM evidence WHERE {base}", p)["n"]
        return {"active_by_severity": by_sev, "active_by_category": by_cat, "total": total, "active": sum(by_sev.values())}

    def evidence_categories(self, is_demo: bool = False) -> list[str]:
        return [r["category"] for r in self.rows("SELECT DISTINCT category FROM evidence WHERE is_demo=? ORDER BY category", (int(is_demo),))]

    def evidence_active_for_collector(self, instance_id: str, collector: str, is_demo: bool = False) -> list[dict[str, Any]]:
        return [ev_out(r) for r in self.rows("SELECT * FROM evidence WHERE instance_id=? AND is_demo=? AND collector=? AND status='active'", (instance_id, int(is_demo), collector))]
