"""Evidence Explorer, timeline and incident workflow endpoints."""
from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from ..errors import AppError, NotFound
from ..recommend import build_card
from ..security import audit, require
from ..services import build_timeline, incident_report, related_evidence, report_markdown
from ..store.evidence import SORTS
from ..timeutil import iso, parse_ts
from .deps import csv_list, get_ctx, page_out, paging, resolve_instance, time_range

router = APIRouter()
SEV = {"info", "warning", "critical"}


# --------------------------------------------------------------------------- evidence
@router.get("/api/evidence", tags=["evidence"])
def evidence_list(request: Request, instance: str | None = Query(None, max_length=32), category: str | None = Query(None, max_length=400), severity: str | None = Query(None, max_length=60),
                  status: str | None = Query(None, pattern="^(active|cleared)$"), t_from: str | None = Query(None, alias="from"), t_to: str | None = Query(None, alias="to"),
                  q: str | None = Query(None, max_length=120), incident_id: str | None = Query(None, max_length=40), sort: str = Query("first_seen"),
                  order: str = Query("desc", pattern="^(asc|desc)$"), pg=Depends(paging), p=require("viewer")) -> dict[str, Any]:
    ctx = get_ctx(request)
    if sort not in SORTS:
        raise AppError(f"sort must be one of {', '.join(SORTS)}", code="VALIDATION_ERROR")
    if instance and instance not in ctx.recorder.rt:
        raise NotFound(f"Unknown instance '{instance}'")
    if t_from and not parse_ts(t_from) or t_to and not parse_ts(t_to):
        raise AppError("'from'/'to' must be ISO-8601 timestamps", code="VALIDATION_ERROR")
    page, size = pg
    items, total = ctx.store.evidence_list(instance_id=instance, categories=csv_list(category), severities=csv_list(severity, SEV), status=status, t_from=iso(parse_ts(t_from)) if t_from else None,
                                           t_to=iso(parse_ts(t_to)) if t_to else None, q=q, incident_id=incident_id, sort=sort, order=order, limit=size, offset=(page - 1) * size, is_demo=ctx.demo)
    return page_out(items, total, page, size, facets={"categories": ctx.store.evidence_categories(ctx.demo), "counts": ctx.store.evidence_counts(instance, ctx.demo)}, demo_mode=ctx.demo)


@router.get("/api/evidence/export", tags=["evidence"])
def evidence_export(request: Request, format: str = Query("json", pattern="^(json|csv)$"), instance: str | None = Query(None, max_length=32), limit: int = Query(200, ge=1, le=5000),
                    category: str | None = Query(None, max_length=400), severity: str | None = Query(None, max_length=60), t_from: str | None = Query(None, alias="from"),
                    t_to: str | None = Query(None, alias="to"), p=require("operator")) -> Any:
    ctx = get_ctx(request)
    items, total = ctx.store.evidence_list(instance_id=instance, categories=csv_list(category), severities=csv_list(severity, SEV), t_from=t_from and iso(parse_ts(t_from)),
                                           t_to=t_to and iso(parse_ts(t_to)), limit=limit, is_demo=ctx.demo)
    audit(request, "evidence.export", instance or "all", details={"format": format, "rows": len(items)})
    if format == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["id", "instance", "category", "severity", "status", "first_seen", "last_seen", "cleared_at", "title", "pid", "observations", "source", "confidence"])
        safe = lambda v: ("'" + v) if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r") else v      # neutralize spreadsheet formula injection
        for e in items:
            w.writerow([safe(e[k]) for k in ("id", "instance_id", "category", "severity", "status", "first_seen", "last_seen", "cleared_at", "title", "pid", "observation_count", "source", "confidence")])
        return PlainTextResponse(buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="evidence.csv"'})
    snaps, _n = ctx.store.snapshot_list(limit=min(limit, 100), is_demo=ctx.demo)
    incs, _m = ctx.store.incident_list(limit=min(limit, 200), is_demo=ctx.demo)
    return {"exported_at": iso(), "demo_mode": ctx.demo, "evidence": items, "evidence_total": total, "snapshots": snaps, "incidents": incs,
            "note": "Query text is redacted according to QUERY_TEXT_MODE. Treat this export as sensitive."}


@router.get("/api/evidence/{evidence_id}", tags=["evidence"])
def evidence_get(evidence_id: str, request: Request, p=require("viewer")) -> dict[str, Any]:
    ctx = get_ctx(request)
    ev = ctx.store.evidence_get(evidence_id)
    if not ev:
        raise NotFound("Evidence not found")
    incs = [r["incident_id"] for r in ctx.store.rows("SELECT incident_id FROM incident_evidence WHERE evidence_id=?", (evidence_id,))]
    return {"evidence": ev, "card": build_card(ev), "incident_ids": incs}


@router.get("/api/evidence/{evidence_id}/related", tags=["evidence"])
def evidence_related(evidence_id: str, request: Request, window_min: int = Query(10, ge=1, le=120), p=require("viewer")) -> dict[str, Any]:
    ctx = get_ctx(request)
    ev = ctx.store.evidence_get(evidence_id)
    if not ev:
        raise NotFound("Evidence not found")
    return related_evidence(ctx.store, ev, window_min)


@router.get("/api/timeline", tags=["evidence"])
def timeline(request: Request, iid: str = Depends(resolve_instance), t_from: str | None = Query(None, alias="from"), t_to: str | None = Query(None, alias="to"),
             category: str | None = Query(None, max_length=400), severity: str | None = Query(None, max_length=60), p=require("viewer")) -> dict[str, Any]:
    ctx = get_ctx(request)
    start, end = time_range(t_from, t_to, 6.0)
    out = build_timeline(ctx.store, ctx.settings, iid, start, end, ctx.demo, csv_list(category), csv_list(severity, SEV))
    out["demo_mode"] = ctx.demo
    return out


# --------------------------------------------------------------------------- incidents
class IncidentIn(BaseModel):
    title: str = Field(..., min_length=3, max_length=200)
    symptom: str = Field("", max_length=4000)
    severity: str = Field("medium", pattern="^(low|medium|high|critical)$")
    environment: str = Field("", max_length=200)
    ticket_ref: str = Field("", max_length=100)
    notes: str = Field("", max_length=8000)
    instance: str | None = Field(None, max_length=32)
    started_at: str | None = None
    evidence_ids: list[str] = Field(default_factory=list, max_length=100)


class IncidentPatch(BaseModel):
    title: str | None = Field(None, min_length=3, max_length=200)
    symptom: str | None = Field(None, max_length=4000)
    severity: str | None = Field(None, pattern="^(low|medium|high|critical)$")
    status: str | None = Field(None, pattern="^(candidate|open|investigating|mitigated|resolved|closed|dismissed)$")
    stage: str | None = Field(None, pattern="^(detect|collect|correlate|investigate|recommend|resolve|review)$")
    environment: str | None = Field(None, max_length=200)
    ticket_ref: str | None = Field(None, max_length=100)
    notes: str | None = Field(None, max_length=8000)
    suspected_root_cause: str | None = Field(None, max_length=4000)
    confirmed_root_cause: str | None = Field(None, max_length=4000)
    resolution: str | None = Field(None, max_length=4000)
    review_notes: str | None = Field(None, max_length=8000)
    started_at: str | None = None
    ended_at: str | None = None
    finding: dict[str, Any] | None = None
    action: dict[str, Any] | None = None


class NoteIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=4000)
    kind: str = Field("note", pattern="^(note|finding|action|hypothesis)$")


class LinkIn(BaseModel):
    evidence_id: str = Field(..., min_length=3, max_length=60)


def _author(request: Request) -> str:
    p = getattr(request.state, "principal", None)
    return p.name if p else ""


@router.post("/api/incidents", tags=["incidents"])
def incident_create(body: IncidentIn, request: Request, p=require("operator")) -> dict[str, Any]:
    ctx = get_ctx(request)
    instance = body.instance or (ctx.settings.instances[0].id if ctx.settings.instances else "default")
    if ctx.settings.instances and instance not in ctx.recorder.rt:
        raise NotFound(f"Unknown instance '{instance}'")
    if body.started_at and not parse_ts(body.started_at):
        raise AppError("started_at must be ISO-8601", code="VALIDATION_ERROR")
    for eid in body.evidence_ids:
        if not ctx.store.evidence_get(eid):
            raise NotFound(f"Evidence {eid} not found")
    inc = ctx.store.incident_create(body.model_dump(exclude={"instance", "evidence_ids", "started_at"}), source="manual", is_demo=ctx.demo, instance_id=instance, evidence_ids=body.evidence_ids,
                                    actor=_author(request), started_at=iso(parse_ts(body.started_at)) if body.started_at else None)
    audit(request, "incident.create", inc["id"], details={"severity": inc["severity"], "evidence": len(body.evidence_ids)})
    return inc


@router.get("/api/incidents", tags=["incidents"])
def incident_list(request: Request, status: str | None = Query(None, max_length=120), severity: str | None = Query(None, pattern="^(low|medium|high|critical)$"),
                  instance: str | None = Query(None, max_length=32), q: str | None = Query(None, max_length=100), source: str | None = Query(None, pattern="^(manual|auto)$"),
                  pg=Depends(paging), p=require("viewer")) -> dict[str, Any]:
    from ..store import STATUSES
    ctx = get_ctx(request)
    if status and any(s not in STATUSES for s in status.split(",")):
        raise AppError("Unknown status", code="VALIDATION_ERROR")
    page, size = pg
    items, total = ctx.store.incident_list(status=status, severity=severity, instance_id=instance, q=q, source=source, limit=size, offset=(page - 1) * size, is_demo=ctx.demo)
    return page_out(items, total, page, size, counts=ctx.store.incident_counts(ctx.demo), workflow=["detect", "collect", "correlate", "investigate", "recommend", "resolve", "review"])


@router.get("/api/incidents/{incident_id}", tags=["incidents"])
def incident_get(incident_id: str, request: Request, p=require("viewer")) -> dict[str, Any]:
    inc = get_ctx(request).store.incident_get(incident_id)
    if not inc:
        raise NotFound("Incident not found")
    return inc


@router.patch("/api/incidents/{incident_id}", tags=["incidents"])
def incident_patch(incident_id: str, body: IncidentPatch, request: Request, p=require("operator")) -> dict[str, Any]:
    ctx = get_ctx(request)
    patch = body.model_dump(exclude_none=True)
    if not patch:
        raise AppError("Nothing to update", code="VALIDATION_ERROR")
    before = ctx.store.incident_get(incident_id)
    inc = ctx.store.incident_update(incident_id, patch, actor=_author(request))
    audit(request, "incident.update", incident_id, details={"fields": sorted(k for k in patch if k not in ("finding", "action")), "status": patch.get("status"),
                                                          "from": before["status"] if before else None})
    return inc


@router.post("/api/incidents/{incident_id}/notes", tags=["incidents"])
def incident_note(incident_id: str, body: NoteIn, request: Request, p=require("operator")) -> dict[str, Any]:
    note = get_ctx(request).store.incident_add_note(incident_id, body.kind, body.text, _author(request))
    audit(request, "incident.note", incident_id, details={"kind": body.kind})
    return note


@router.get("/api/incidents/{incident_id}/evidence", tags=["incidents"])
def incident_evidence(incident_id: str, request: Request, p=require("viewer")) -> dict[str, Any]:
    ctx = get_ctx(request)
    if not ctx.store.incident_get(incident_id):
        raise NotFound("Incident not found")
    items = ctx.store.incident_evidence(incident_id)
    return {"items": items, "total": len(items), "cards": [build_card(e) for e in items]}


@router.post("/api/incidents/{incident_id}/evidence", tags=["incidents"])
def incident_link(incident_id: str, body: LinkIn, request: Request, p=require("operator")) -> dict[str, Any]:
    get_ctx(request).store.incident_link_evidence(incident_id, body.evidence_id, "manual", _author(request))
    audit(request, "incident.link_evidence", incident_id, details={"evidence_id": body.evidence_id})
    return {"incident_id": incident_id, "evidence_id": body.evidence_id, "linked": True}


@router.delete("/api/incidents/{incident_id}/evidence/{evidence_id}", tags=["incidents"])
def incident_unlink(incident_id: str, evidence_id: str, request: Request, p=require("operator")) -> dict[str, Any]:
    ok = get_ctx(request).store.incident_unlink_evidence(incident_id, evidence_id)
    if not ok:
        raise NotFound("Link not found")
    audit(request, "incident.unlink_evidence", incident_id, details={"evidence_id": evidence_id})
    return {"incident_id": incident_id, "evidence_id": evidence_id, "linked": False}


@router.post("/api/incidents/{incident_id}/capture", tags=["incidents"])
def incident_capture(incident_id: str, request: Request, p=require("operator")) -> dict[str, Any]:
    ctx = get_ctx(request)
    inc = ctx.store.incident_get(incident_id)
    if not inc:
        raise NotFound("Incident not found")
    iid = inc["instance_id"] if inc["instance_id"] in ctx.recorder.rt else next(iter(ctx.recorder.rt))
    ctx.recorder.run_tier_sync(iid, "fast", "capture", wait=True)
    rt = ctx.recorder.runtime(iid)
    snap = ctx.store.snapshot_add(iid, rt.cfg.dbname, f"{incident_id} evidence capture", "Manual capture from the incident page.", ctx.recorder.snapshot_payload(rt), incident_id, ctx.demo)
    ctx.store.incident_add_note(incident_id, "system", f"Evidence snapshot #{snap['id']} captured.", _author(request))
    audit(request, "incident.capture", incident_id, details={"snapshot_id": snap["id"]})
    return {"snapshot_id": snap["id"], "captured_at": snap["captured_at"], "demo_mode": ctx.demo, "incident_id": incident_id}


@router.get("/api/incidents/{incident_id}/report", tags=["incidents"])
def incident_report_view(incident_id: str, request: Request, format: str = Query("json", pattern="^(json|markdown)$"), p=require("viewer")) -> Any:
    ctx = get_ctx(request)
    rep = incident_report(ctx.store, incident_id)
    if not rep:
        raise NotFound("Incident not found")
    snaps, _n = ctx.store.snapshot_list(limit=50, is_demo=ctx.demo, incident_id=incident_id)
    rep["related_snapshots"] = snaps
    audit(request, "incident.report", incident_id, details={"format": format})
    if format == "markdown":
        return PlainTextResponse(report_markdown(rep), media_type="text/markdown")
    return rep


@router.get("/api/snapshots/{snapshot_id}", tags=["incidents"])
def snapshot_get(snapshot_id: int, request: Request, p=require("operator")) -> dict[str, Any]:
    snap = get_ctx(request).store.snapshot_get(snapshot_id)
    if not snap:
        raise NotFound("Snapshot not found")
    audit(request, "snapshot.read", str(snapshot_id))
    return snap
