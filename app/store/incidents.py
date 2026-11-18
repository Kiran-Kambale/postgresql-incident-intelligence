"""Incident register: manual incidents, automatic candidates, notes, evidence links and v0.6-compatible findings/actions."""
from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

from ..errors import AppError, NotFound
from ..timeutil import iso, parse_ts, utcnow
from .core import jdump, jload, like_escape
from .evidence import ev_out

STATUSES = ("candidate", "open", "investigating", "mitigated", "resolved", "closed", "dismissed")
OPEN_STATUSES = ("candidate", "open", "investigating", "mitigated")
SEVERITIES = ("low", "medium", "high", "critical")
STAGES = ("detect", "collect", "correlate", "investigate", "recommend", "resolve", "review")
CLASSIFICATIONS = ("observed", "likely", "hypothesis", "confirmed")
STATUS_STAGE = {"candidate": "detect", "open": "collect", "investigating": "investigate", "mitigated": "recommend", "resolved": "resolve", "closed": "review"}
EDITABLE = ("title", "symptom", "severity", "environment", "ticket_ref", "notes", "suspected_root_cause", "confirmed_root_cause",
            "resolution", "review_notes", "started_at", "ended_at")


class IncidentMixin:
    def _inc_id(self) -> str:
        return "DIR-" + utcnow().strftime("%Y%m%d-") + uuid.uuid4().hex[:8].upper()

    def incident_get(self, incident_id: str) -> dict[str, Any] | None:
        r = self.one("SELECT * FROM incidents WHERE id=?", (incident_id,))
        if not r:
            return None
        r["findings"] = jload(r.pop("findings_json"), [])
        r["actions"] = jload(r.pop("actions_json"), [])
        r["is_demo"] = bool(r.get("is_demo"))
        r["timeline_notes"] = self.rows("SELECT id, ts, kind, author, text FROM incident_notes WHERE incident_id=? ORDER BY ts, id", (incident_id,))
        links = self.rows("SELECT evidence_id, link_type, linked_at, linked_by FROM incident_evidence WHERE incident_id=? ORDER BY linked_at", (incident_id,))
        r["evidence_links"] = links
        r["evidence_ids"] = [x["evidence_id"] for x in links]
        r["snapshot_ids"] = [x["id"] for x in self.rows("SELECT id FROM snapshots WHERE incident_id=? ORDER BY id", (incident_id,))]
        return r

    def incident_create(self, data: dict[str, Any], *, source: str = "manual", is_demo: bool = False, instance_id: str = "default",
                        status: str | None = None, candidate_key: str | None = None, evidence_ids: list[str] | None = None,
                        actor: str = "", started_at: str | None = None) -> dict[str, Any]:
        now = iso()
        status = status or ("candidate" if source == "auto" else "open")
        iid = self._inc_id()
        stage = STATUS_STAGE.get(status, "collect")
        with self.tx() as con:
            con.execute(
                "INSERT INTO incidents(id,created_at,updated_at,title,symptom,severity,status,environment,ticket_ref,notes,findings_json,actions_json,"
                "instance_id,started_at,stage,source,candidate_key,is_demo) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (iid, now, now, data["title"].strip(), data.get("symptom", "").strip() or data["title"].strip(), data.get("severity", "medium"), status,
                 data.get("environment", "").strip(), data.get("ticket_ref", "").strip(), data.get("notes", "").strip(), "[]", "[]",
                 instance_id, started_at or data.get("started_at") or now, stage, source, candidate_key, int(is_demo)))
            for eid in evidence_ids or []:
                con.execute("INSERT OR IGNORE INTO incident_evidence(incident_id,evidence_id,link_type,linked_at,linked_by) VALUES (?,?,?,?,?)",
                            (iid, eid, "auto" if source == "auto" else "manual", now, actor or source))
            con.execute("INSERT INTO incident_notes(incident_id,ts,kind,author,text) VALUES (?,?,?,?,?)",
                        (iid, now, "system", actor or source, "Incident candidate generated from diagnostic evidence." if source == "auto" else "Incident created."))
        return self.incident_get(iid)

    def incident_list(self, *, status: str | None = None, severity: str | None = None, instance_id: str | None = None, q: str | None = None,
                      source: str | None = None, limit: int = 100, offset: int = 0, is_demo: bool | None = None,
                      include_dismissed: bool = True) -> tuple[list[dict[str, Any]], int]:
        where, p = ["1=1"], []
        if status:
            sts = [s for s in status.split(",") if s]
            where.append(f"status IN ({','.join('?' for _ in sts)})"); p += sts
        elif not include_dismissed:
            where.append("status != 'dismissed'")
        if severity:
            where.append("severity=?"); p.append(severity)
        if instance_id:
            where.append("instance_id=?"); p.append(instance_id)
        if source:
            where.append("source=?"); p.append(source)
        if is_demo is not None:
            where.append("is_demo=?"); p.append(int(is_demo))
        if q:
            where.append("instr(lower(id||' '||title||' '||symptom||' '||ticket_ref||' '||environment), ?) > 0"); p.append(q.lower())
        w = " AND ".join(where)
        total = self.one(f"SELECT count(*) AS n FROM incidents WHERE {w}", p)["n"]
        rows = self.rows(
            "SELECT id,created_at,updated_at,title,symptom,severity,status,stage,environment,ticket_ref,notes,instance_id,started_at,ended_at,source,is_demo,"
            f"suspected_root_cause,confirmed_root_cause,(SELECT count(*) FROM incident_evidence ie WHERE ie.incident_id=incidents.id) AS evidence_count FROM incidents WHERE {w} "
            "ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?", [*p, limit, offset])
        for r in rows:
            r["is_demo"] = bool(r["is_demo"])
        return rows, total

    def incident_counts(self, is_demo: bool = False) -> dict[str, int]:
        return {r["status"]: r["n"] for r in self.rows("SELECT status, count(*) AS n FROM incidents WHERE is_demo=? GROUP BY status", (int(is_demo),))}

    def incident_add_note(self, incident_id: str, kind: str, text: str, author: str = "") -> dict[str, Any]:
        if not self.one("SELECT 1 AS x FROM incidents WHERE id=?", (incident_id,)):
            raise NotFound("Incident not found")
        now = iso()
        with self.tx() as con:
            cur = con.execute("INSERT INTO incident_notes(incident_id,ts,kind,author,text) VALUES (?,?,?,?,?)", (incident_id, now, kind, author, text.strip()))
            con.execute("UPDATE incidents SET updated_at=? WHERE id=?", (now, incident_id))
            return {"id": cur.lastrowid, "ts": now, "kind": kind, "author": author, "text": text.strip()}

    def incident_link_evidence(self, incident_id: str, evidence_id: str, link_type: str = "manual", actor: str = "") -> None:
        if not self.one("SELECT 1 AS x FROM incidents WHERE id=?", (incident_id,)):
            raise NotFound("Incident not found")
        if not self.one("SELECT 1 AS x FROM evidence WHERE id=?", (evidence_id,)):
            raise NotFound("Evidence not found")
        now = iso()
        with self.tx() as con:
            con.execute("INSERT OR IGNORE INTO incident_evidence(incident_id,evidence_id,link_type,linked_at,linked_by) VALUES (?,?,?,?,?)", (incident_id, evidence_id, link_type, now, actor))
            con.execute("UPDATE incidents SET updated_at=?, stage=CASE WHEN ?='manual' AND stage IN ('detect','collect') THEN 'correlate' ELSE stage END WHERE id=?",
                        (now, link_type, incident_id))          # automatic links never advance the workflow: a human decides

    def incident_unlink_evidence(self, incident_id: str, evidence_id: str) -> bool:
        with self.tx() as con:
            n = con.execute("DELETE FROM incident_evidence WHERE incident_id=? AND evidence_id=?", (incident_id, evidence_id)).rowcount
            con.execute("UPDATE incidents SET updated_at=? WHERE id=?", (iso(), incident_id))
        return n > 0

    def incident_evidence(self, incident_id: str) -> list[dict[str, Any]]:
        rows = self.rows("SELECT e.*, ie.link_type FROM evidence e JOIN incident_evidence ie ON ie.evidence_id=e.id WHERE ie.incident_id=? ORDER BY e.first_seen", (incident_id,))
        out = []
        for r in rows:
            link = r.pop("link_type")
            d = ev_out(r)
            d["link_type"] = link
            out.append(d)
        return out

    def incident_update(self, incident_id: str, patch: dict[str, Any], actor: str = "") -> dict[str, Any]:
        inc = self.incident_get(incident_id)
        if not inc:
            raise NotFound("Incident not found")
        now = iso()
        notes: list[tuple[str, str]] = []
        sets: dict[str, Any] = {}
        for k in EDITABLE:
            if patch.get(k) is not None and patch[k] != inc.get(k):
                sets[k] = patch[k].strip() if isinstance(patch[k], str) else patch[k]
        if "severity" in sets and sets["severity"] not in SEVERITIES:
            raise AppError("Invalid severity", code="VALIDATION_ERROR")
        for k in ("started_at", "ended_at"):
            if k in sets:
                if sets[k] == "":
                    sets[k] = None
                elif parse_ts(sets[k]) is None:
                    raise AppError(f"{k} must be an ISO-8601 timestamp", code="VALIDATION_ERROR")
                else:
                    sets[k] = iso(parse_ts(sets[k]))
        new_status = patch.get("status")
        if new_status and new_status != inc["status"]:
            if new_status not in STATUSES:
                raise AppError("Invalid status", code="VALIDATION_ERROR")
            sets["status"] = new_status
            notes.append(("status", f"Status changed from {inc['status']} to {new_status}."))
            if new_status in ("resolved", "closed") and not (sets.get("ended_at") or inc.get("ended_at")):
                sets["ended_at"] = now
            if new_status in ("open", "investigating", "mitigated") and inc["status"] in ("resolved", "closed") and "ended_at" not in sets:
                sets["ended_at"] = None
            if "stage" not in patch and new_status in STATUS_STAGE:
                sets["stage"] = STATUS_STAGE[new_status]
        if patch.get("stage"):
            if patch["stage"] not in STAGES:
                raise AppError("Invalid stage", code="VALIDATION_ERROR")
            sets["stage"] = patch["stage"]
        if "suspected_root_cause" in sets and sets["suspected_root_cause"]:
            notes.append(("hypothesis", "Suspected root cause recorded (hypothesis, not confirmed): " + sets["suspected_root_cause"][:500]))
        if "confirmed_root_cause" in sets and sets["confirmed_root_cause"]:
            notes.append(("confirmed", "Root cause marked as confirmed: " + sets["confirmed_root_cause"][:500]))
        findings, actions = list(inc["findings"]), list(inc["actions"])
        if patch.get("finding"):
            f = dict(patch["finding"])
            cls = f.get("classification") or f.get("confidence")
            if cls and cls not in CLASSIFICATIONS:
                raise AppError(f"classification must be one of {', '.join(CLASSIFICATIONS)}", code="VALIDATION_ERROR")
            f.setdefault("classification", cls or "hypothesis")
            findings.append({**f, "recorded_at": now, "recorded_by": actor})
        if patch.get("action"):
            actions.append({**patch["action"], "recorded_at": now, "recorded_by": actor, "execution": "recorded_only"})
        with self.tx() as con:
            cols = {**sets, "updated_at": now, "findings_json": jdump(findings), "actions_json": jdump(actions)}
            con.execute(f"UPDATE incidents SET {', '.join(f'{c}=?' for c in cols)} WHERE id=?", [*cols.values(), incident_id])
            for kind, text in notes:
                con.execute("INSERT INTO incident_notes(incident_id,ts,kind,author,text) VALUES (?,?,?,?,?)", (incident_id, now, kind, actor, text))
        return self.incident_get(incident_id)

    def incident_candidate_blocked(self, candidate_key: str, cooldown_min: float, is_demo: bool = False) -> bool:
        """True if an open/candidate incident exists for this key, or one was created within the cooldown."""
        r = self.one(f"SELECT 1 AS x FROM incidents WHERE candidate_key=? AND is_demo=? AND (status IN ({','.join('?' for _ in OPEN_STATUSES)}) OR created_at >= ? OR "
                     "(ended_at IS NOT NULL AND ended_at >= ?)) LIMIT 1",
                     [candidate_key, int(is_demo), *OPEN_STATUSES, iso(utcnow() - timedelta(minutes=cooldown_min)), iso(utcnow() - timedelta(minutes=cooldown_min))])
        return r is not None

    # ------------------------------------------------------------------ manual snapshots (v0.6 compatible)
    def snapshot_add(self, instance_id: str, database_name: str, title: str, notes: str, payload: dict, incident_id: str | None = None, is_demo: bool = False) -> dict:
        now = iso()
        with self.tx() as con:
            cur = con.execute("INSERT INTO snapshots(captured_at,database_name,title,notes,payload_json,instance_id,is_demo,incident_id) VALUES (?,?,?,?,?,?,?,?)",
                              (now, database_name, title, notes, jdump(payload, 1_500_000), instance_id, int(is_demo), incident_id))
            return {"id": cur.lastrowid, "captured_at": now}

    def snapshot_list(self, limit: int = 100, offset: int = 0, is_demo: bool = False, incident_id: str | None = None) -> tuple[list[dict], int]:
        where, p = "is_demo=?", [int(is_demo)]
        if incident_id:
            where += " AND incident_id=?"; p.append(incident_id)
        total = self.one(f"SELECT count(*) AS n FROM snapshots WHERE {where}", p)["n"]
        return self.rows(f"SELECT id,captured_at,database_name,title,notes,instance_id,incident_id FROM snapshots WHERE {where} ORDER BY id DESC LIMIT ? OFFSET ?", [*p, limit, offset]), total

    def snapshot_get(self, snapshot_id: int) -> dict | None:
        r = self.one("SELECT id,captured_at,database_name,title,notes,payload_json,instance_id,incident_id,is_demo FROM snapshots WHERE id=?", (snapshot_id,))
        if not r:
            return None
        r["payload"] = jload(r.pop("payload_json"), {})
        r["is_demo"] = bool(r["is_demo"])
        return r
