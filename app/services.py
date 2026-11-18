"""Cross-cutting investigation services built on persisted evidence: query statistics, timeline/gaps, correlation, incident candidates, reports."""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from .config import Settings
from .pg.analysis import mk
from .recommend import build_card
from .store import OPEN_STATUSES, Store
from .store.evidence import ev_out
from .timeutil import iso, parse_ts, utcnow

log = logging.getLogger("dir.services")


# --------------------------------------------------------------------------- query statistics
def _deltas(samples: list[dict[str, Any]]) -> dict[str, Any]:
    """Sum positive deltas between consecutive samples of one statement; counter resets are detected and skipped."""
    d = {"calls": 0.0, "total_ms": 0.0, "rows": 0.0, "shared_hit": 0.0, "shared_read": 0.0, "temp_written": 0.0, "resets": 0, "samples": len(samples)}
    for a, b in zip(samples, samples[1:]):
        if b["calls"] < a["calls"] or b["total_exec_ms"] < a["total_exec_ms"] or (a.get("stats_reset") and b.get("stats_reset") and a["stats_reset"] != b["stats_reset"]):
            d["resets"] += 1
            continue
        d["calls"] += b["calls"] - a["calls"]
        d["total_ms"] += b["total_exec_ms"] - a["total_exec_ms"]
        for k, col in (("rows", "rows"), ("shared_hit", "shared_hit"), ("shared_read", "shared_read"), ("temp_written", "temp_written")):
            if a.get(col) is not None and b.get(col) is not None and b[col] >= a[col]:
                d[k] += b[col] - a[col]
    return d


def query_window(store: Store, instance_id: str, t_from: datetime, t_to: datetime, is_demo: bool) -> dict[str, dict[str, Any]]:
    samples = store.qstats_samples(instance_id, iso(t_from), iso(t_to), is_demo)
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for s in samples:
        groups[(s["dbid"], s["userid"], s["queryid"])].append(s)
    out = {}
    for key, rows in groups.items():
        d = _deltas(rows)
        last = rows[-1]
        out["|".join(key)] = {**d, "queryid": key[2], "dbid": key[0], "userid": key[1], "fingerprint": last["fingerprint"], "text": last.get("text"),
                              "last_ts": last["ts"], "first_ts": rows[0]["ts"], "cum_calls": last["calls"], "cum_total_ms": last["total_exec_ms"], "max_exec_ms_since_reset": last.get("max_exec_ms")}
    return out


def query_top(store: Store, instance_id: str, window_s: int, now: datetime, is_demo: bool, limit: int = 25, sort: str = "total") -> dict[str, Any]:
    cur = query_window(store, instance_id, now - timedelta(seconds=window_s), now, is_demo)
    total_ms = sum(v["total_ms"] for v in cur.values()) or 0.0
    items = []
    for v in cur.values():
        if v["samples"] < 2 or v["calls"] <= 0:
            continue
        items.append({**v, "mean_ms": v["total_ms"] / v["calls"], "share_pct": round(100 * v["total_ms"] / total_ms, 1) if total_ms else None,
                      "cache_hit_pct": round(100 * v["shared_hit"] / (v["shared_hit"] + v["shared_read"]), 1) if (v["shared_hit"] + v["shared_read"]) else None,
                      "rows_per_call": v["rows"] / v["calls"]})
    key = {"total": "total_ms", "mean": "mean_ms", "calls": "calls", "rows": "rows", "temp": "temp_written"}.get(sort, "total_ms")
    items.sort(key=lambda r: -r[key])
    first = store.one("SELECT min(ts) AS m, max(ts) AS x, count(DISTINCT ts) AS n FROM query_stat_samples WHERE instance_id=? AND is_demo=?", (instance_id, int(is_demo)))
    return {"window_s": window_s, "items": items[:limit], "total_ms": total_ms, "queries_seen": len(items), "samples_available": first["n"] if first else 0,
            "oldest_sample": first["m"] if first else None, "newest_sample": first["x"] if first else None,
            "limitations": ["Deltas are computed between stored samples, not from individual executions. A mean is a window average, not the duration of one execution.",
                            "Only the top 200 statements by cumulative time are sampled; the share is relative to the sampled statements.",
                            "pg_stat_statements resets (or evictions) are detected and skipped, which can understate activity around a reset."]}


def query_trend(store: Store, instance_id: str, queryid: str | None, fingerprint: str | None, t_from: datetime, t_to: datetime, is_demo: bool, buckets: int = 48) -> dict[str, Any]:
    rows = store.qstats_samples(instance_id, iso(t_from), iso(t_to), is_demo, queryid)
    if fingerprint and not queryid:
        rows = [r for r in rows if r["fingerprint"] == fingerprint]
    by: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        by[(r["dbid"], r["userid"], r["queryid"])].append(r)
    points: dict[str, dict[str, float]] = defaultdict(lambda: {"calls": 0.0, "total_ms": 0.0})
    span = max(1.0, (t_to - t_from).total_seconds())
    step = span / buckets
    for series in by.values():
        for a, b in zip(series, series[1:]):
            if b["calls"] < a["calls"] or b["total_exec_ms"] < a["total_exec_ms"]:
                continue
            tb = parse_ts(b["ts"])
            idx = min(buckets - 1, int((tb - t_from).total_seconds() / step))
            k = iso(t_from + timedelta(seconds=idx * step))
            points[k]["calls"] += b["calls"] - a["calls"]
            points[k]["total_ms"] += b["total_exec_ms"] - a["total_exec_ms"]
    out = [{"ts": k, "calls": v["calls"], "total_ms": v["total_ms"], "mean_ms": (v["total_ms"] / v["calls"]) if v["calls"] else None} for k, v in sorted(points.items())]
    return {"points": out, "text": rows[-1].get("text") if rows else None, "samples": len(rows)}


def query_regressions(store: Store, instance_id: str, now: datetime, is_demo: bool, window_min: int = 15) -> list[dict[str, Any]]:
    """Hypotheses only: statements whose window mean rose sharply versus the previous window, or that dominate database time."""
    w = timedelta(minutes=window_min)
    cur = query_window(store, instance_id, now - w, now, is_demo)
    prev = query_window(store, instance_id, now - 2 * w, now - w, is_demo)
    total_cur = sum(v["total_ms"] for v in cur.values())
    out: list[dict[str, Any]] = []
    for k, c in cur.items():
        p = prev.get(k)
        if c["calls"] >= 20 and p and p["calls"] >= 20 and c["total_ms"] >= 1000:
            mc, mp = c["total_ms"] / c["calls"], p["total_ms"] / p["calls"]
            if mp > 0 and mc / mp >= 3:
                out.append(mk("query_regression", "warning", f"Statement mean time rose {mc / mp:.1f}× ({mp:.1f} ms → {mc:.1f} ms) between windows",
                              f"qreg:{c['fingerprint']}", "statements", "pg_stat_statements samples",
                              {"queryid": c["queryid"], "fingerprint": c["fingerprint"], "text": c["text"], "window_minutes": window_min, "mean_ms_now": mc, "mean_ms_before": mp,
                               "calls_now": c["calls"], "calls_before": p["calls"], "total_ms_now": c["total_ms"], "ratio": mc / mp},
                              confidence="hypothesis", query_fingerprint=c["fingerprint"], peaks={"ratio": mc / mp},
                              limitations="Compares window averages from cumulative counters. A different mix of parameters, a data-size change, contention or a plan change can each produce this; the cause is not established."))
        if total_cur >= 5000 and c["total_ms"] / total_cur >= 0.5 and c["calls"] >= 5:
            out.append(mk("query_hotspot", "info", f"One statement accounts for {100 * c['total_ms'] / total_cur:.0f}% of sampled execution time in the last {window_min} min",
                          f"qhot:{c['fingerprint']}", "statements", "pg_stat_statements samples",
                          {"queryid": c["queryid"], "fingerprint": c["fingerprint"], "text": c["text"], "window_minutes": window_min, "share_pct": 100 * c["total_ms"] / total_cur,
                           "calls_now": c["calls"], "calls_before": p["calls"] if p else None, "total_ms_now": c["total_ms"]},
                          confidence="observed", query_fingerprint=c["fingerprint"], limitations="Share is relative to the sampled top statements. Dominating time is expected for the main workload query."))
    return out


# --------------------------------------------------------------------------- metrics
COUNTERS = {"db_xact_commit", "db_xact_rollback", "db_blks_hit", "db_blks_read", "db_deadlocks", "db_temp_bytes", "db_temp_files", "db_tup_returned", "db_tup_fetched",
            "db_tup_inserted", "db_tup_updated", "db_tup_deleted", "bg_checkpoints_timed", "bg_checkpoints_req", "bg_buffers_checkpoint", "bg_buffers_clean", "bg_buffers_backend",
            "wal_bytes", "wal_lsn_bytes", "archive_failed_count", "archive_count", "wal_fpi", "bg_checkpoint_write_ms", "bg_checkpoint_sync_ms"}


def series(store: Store, instance_id: str, names: list[str], t_from: datetime, t_to: datetime, max_points: int, is_demo: bool, fast_interval_s: float) -> dict[str, Any]:
    span = max(1.0, (t_to - t_from).total_seconds())
    step = max(int(fast_interval_s) if span / fast_interval_s <= max_points else 1, int(span / max_points))
    raw = store.metrics_series(instance_id, names, iso(t_from), iso(t_to), step, is_demo)
    out: dict[str, Any] = {}
    for n in names:
        pts = raw.get(n, [])
        if n in COUNTERS:        # cumulative counters -> per-second rates, skipping resets
            rate = []
            for a, b in zip(pts, pts[1:]):
                dt = (parse_ts(b["ts"]) - parse_ts(a["ts"])).total_seconds()
                if dt <= 0 or b["max"] < a["max"]:
                    rate.append({"ts": b["ts"], "value": None, "reset": b["max"] < a["max"]})
                else:
                    rate.append({"ts": b["ts"], "value": (b["max"] - a["max"]) / dt})
            out[n] = {"kind": "rate_per_s", "points": rate}
        else:
            out[n] = {"kind": "gauge", "points": [{"ts": p["ts"], "value": p["avg"], "max": p["max"]} for p in pts]}
    return {"step_s": step, "from": iso(t_from), "to": iso(t_to), "series": out}


# --------------------------------------------------------------------------- timeline, gaps, correlation
def collection_gaps(store: Store, instance_id: str, t_from: datetime, t_to: datetime, interval_s: float, is_demo: bool, tier: str = "fast") -> dict[str, Any]:
    limit = timedelta(seconds=interval_s * 2.5)
    first = store.one("SELECT min(started_at) AS m FROM collection_runs WHERE instance_id=? AND tier=? AND is_demo=?", (instance_id, tier, int(is_demo)))
    start_obs = parse_ts(first["m"]) if first and first["m"] else None
    pts = [parse_ts(r["started_at"]) for r in store.run_times(instance_id, tier, iso(t_from - 3 * limit), iso(t_to + limit), is_demo)]
    gaps: list[dict[str, Any]] = []
    now = utcnow()
    end = min(t_to, now)
    prev = t_from
    if start_obs is None or start_obs > t_from:
        stop = min(start_obs or end, end)
        if stop > t_from:
            gaps.append({"from": iso(t_from), "to": iso(stop), "kind": "before_monitoring", "seconds": (stop - t_from).total_seconds()})
        prev = max(t_from, stop)
    for p in (p for p in pts if prev <= p <= end):
        if p - prev > limit:
            gaps.append({"from": iso(prev), "to": iso(p), "kind": "no_collection", "seconds": (p - prev).total_seconds()})
        prev = p
    if end - prev > limit and (start_obs is not None):
        gaps.append({"from": iso(prev), "to": iso(end), "kind": "no_collection", "seconds": (end - prev).total_seconds()})
    total = max(1.0, (end - t_from).total_seconds())
    missing = min(total, sum(g["seconds"] for g in gaps))
    errors = [r for r in store.rows("SELECT started_at, tier, status, collectors_json FROM collection_runs WHERE instance_id=? AND is_demo=? AND status IN ('error') AND started_at>=? AND started_at<=? ORDER BY started_at LIMIT 200",
                                    (instance_id, int(is_demo), iso(t_from), iso(t_to)))]
    return {"gaps": gaps, "coverage_pct": round(100 * (1 - missing / total), 1), "expected_interval_s": interval_s,
            "collection_errors": [{"ts": e["started_at"], "tier": e["tier"]} for e in errors]}


def _pids(ev: dict[str, Any]) -> set[int]:
    d = ev.get("details") or {}
    out: set[int] = set()
    if ev.get("pid"):
        out.add(int(ev["pid"]))
    for key in ("chain", "cycle"):
        out |= {int(x) for x in d.get(key, []) if isinstance(x, int)}
    for key in ("holder_pid", "waiter_pid"):
        if isinstance(d.get(key), int):
            out.add(d[key])
    root = d.get("root") or {}
    if isinstance(root.get("pid"), int):
        out.add(root["pid"])
    for b in d.get("blocked_sessions", []) or []:
        if isinstance(b.get("pid"), int):
            out.add(b["pid"])
    for p in d.get("processes", []) or []:
        out |= {int(p[k]) for k in ("pid", "blocked_by") if isinstance(p.get(k), int)}
    return out


def _relations(ev: dict[str, Any]) -> set[str]:
    d = ev.get("details") or {}
    out = set(d.get("relations_waited_on") or [])
    if d.get("relation"):
        out.add(d["relation"])
    return out


def related_evidence(store: Store, ev: dict[str, Any], window_min: int = 10, limit: int = 40) -> dict[str, Any]:
    start = parse_ts(ev["first_seen"]) - timedelta(minutes=window_min)
    end = parse_ts(ev.get("cleared_at") or ev["last_seen"]) + timedelta(minutes=window_min)
    cands = store.evidence_window(ev["instance_id"], iso(start), iso(end), bool(ev.get("is_demo")), exclude_id=ev["id"], limit=300)
    mine_p, mine_r = _pids(ev), _relations(ev)
    out = []
    for c in cands:
        links = []
        shared = mine_p & _pids(c)
        if shared:
            links.append({"type": "shared_pid", "strength": "structural", "why": f"Both involve PID {', '.join(map(str, sorted(shared)[:3]))} (PIDs can be reused over long periods)."})
        if ev.get("query_fingerprint") and ev.get("query_fingerprint") == c.get("query_fingerprint"):
            links.append({"type": "same_statement", "strength": "structural", "why": "Same normalized statement fingerprint."})
        rel = mine_r & _relations(c)
        if rel:
            links.append({"type": "same_relation", "strength": "structural", "why": f"Both involve {', '.join(sorted(rel)[:2])}."})
        if not links:
            links.append({"type": "time_overlap", "strength": "temporal", "why": f"Observed within {window_min} minutes. Temporal proximity alone does not show that one caused the other."})
        c["links"] = links
        c["strength"] = "structural" if any(l["strength"] == "structural" for l in links) else "temporal"
        out.append(c)
    gap = lambda c: abs((parse_ts(c["first_seen"]) - parse_ts(ev["first_seen"])).total_seconds())
    out.sort(key=lambda c: (c["strength"] != "structural", gap(c)))
    return {"evidence_id": ev["id"], "window": {"from": iso(start), "to": iso(end)}, "items": out[:limit], "total": len(out),
            "note": "Structural links (shared PID, statement or relation) are stronger than temporal overlap. Neither proves causation."}


def build_timeline(store: Store, settings: Settings, instance_id: str, t_from: datetime, t_to: datetime, is_demo: bool,
                   categories: list[str] | None = None, severities: list[str] | None = None, limit: int = 1500) -> dict[str, Any]:
    evs, total = store.evidence_list(instance_id=instance_id, categories=categories, severities=severities, t_from=iso(t_from), t_to=iso(t_to), limit=limit, order="asc", sort="first_seen", is_demo=is_demo)
    events = []
    for e in evs:
        instant = e["first_seen"] == e.get("cleared_at")
        events.append({"id": e["id"], "type": "evidence", "category": e["category"], "severity": e["severity"], "title": e["title"], "start": e["first_seen"],
                       "end": None if e["status"] == "active" else e.get("cleared_at"), "ongoing": e["status"] == "active", "instant": instant,
                       "stale": e.get("collection_status") == "stale", "pid": e.get("pid")})
    incs, _n = store.incident_list(instance_id=instance_id, is_demo=is_demo, limit=200)
    for i in incs:
        st = parse_ts(i.get("started_at") or i["created_at"])
        if st and t_from <= st <= t_to:
            events.append({"id": i["id"], "type": "incident", "category": "incident", "severity": {"critical": "critical", "high": "critical", "medium": "warning", "low": "info"}[i["severity"]],
                           "title": f"Incident: {i['title']}", "start": iso(st), "end": i.get("ended_at"), "ongoing": not i.get("ended_at"), "instant": False, "status": i["status"]})
    events.sort(key=lambda x: x["start"])
    cov = collection_gaps(store, instance_id, t_from, t_to, settings.t("fast_interval_s"), is_demo)
    return {"from": iso(t_from), "to": iso(t_to), "events": events, "truncated": total > limit, **cov}


# --------------------------------------------------------------------------- incident candidates
CANDIDATE_CATEGORIES = ("blocking", "connection_saturation", "xid_age", "replication_slot", "replication_lag", "collector_failure", "vacuum_pressure", "idle_in_transaction", "deadlock")


def create_candidates(store: Store, settings: Settings, instance_id: str, changes: dict[str, list[dict[str, Any]]], is_demo: bool,
                      snapshot_fn=None, db_name: str = "") -> list[dict[str, Any]]:
    """Generate incident candidates from new/escalated critical evidence. Humans triage them (promote or dismiss)."""
    created: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in [*changes.get("new", []), *changes.get("escalated", [])]:
        ev = ev_out(raw)
        if ev["severity"] != "critical" or ev["category"] not in CANDIDATE_CATEGORIES or ev["id"] in seen:
            continue
        seen.add(ev["id"])
        key = f"{instance_id}:{ev['category']}"
        existing = store.one(f"SELECT id FROM incidents WHERE candidate_key=? AND is_demo=? AND status IN ({','.join('?' for _ in OPEN_STATUSES)}) ORDER BY created_at DESC LIMIT 1",
                             [key, int(is_demo), *OPEN_STATUSES])
        if existing:
            store.incident_link_evidence(existing["id"], ev["id"], "auto", "recorder")
            continue
        mine = _pids(ev)
        sibling = None
        if mine:     # the same session already explains an open incident: add this evidence instead of opening a duplicate
            for row in store.rows(f"SELECT id FROM incidents WHERE instance_id=? AND is_demo=? AND source='auto' AND status IN ({','.join('?' for _ in OPEN_STATUSES)}) "
                                  "AND created_at >= ? ORDER BY created_at DESC LIMIT 20", [instance_id, int(is_demo), *OPEN_STATUSES, iso(utcnow() - timedelta(hours=6))]):
                if any(mine & _pids(x) for x in store.incident_evidence(row["id"])):
                    sibling = row["id"]
                    break
        if sibling:
            store.incident_link_evidence(sibling, ev["id"], "auto_structural", "recorder")
            continue
        if store.incident_candidate_blocked(key, settings.t("candidate_cooldown_min"), is_demo):
            continue
        card = build_card(ev)
        facts = " ".join(card["observed"][:3])
        inc = store.incident_create(
            {"title": f"Auto-detected: {ev['title']}"[:200], "symptom": f"Detected automatically from diagnostic evidence {ev['id']} at {ev['first_seen']}. Facts observed: {facts} "
                                                                        "This is a candidate: the cause is not established.", "severity": "high",
             "environment": instance_id, "notes": "Review the linked evidence, then promote this candidate to an open incident or dismiss it."},
            source="auto", is_demo=is_demo, instance_id=instance_id, candidate_key=key, evidence_ids=[ev["id"]], actor="recorder", started_at=ev["first_seen"])
        rel = related_evidence(store, ev, window_min=5, limit=15)
        for r in rel["items"]:
            store.incident_link_evidence(inc["id"], r["id"], "auto_structural" if r["strength"] == "structural" else "auto_temporal", "recorder")
        if snapshot_fn:
            try:
                store.snapshot_add(instance_id, db_name, f"{inc['id']} evidence capture", "Automatic capture when the incident candidate was created.", snapshot_fn(), inc["id"], is_demo)
            except Exception:   # pragma: no cover - snapshot is best effort
                log.exception("auto snapshot failed")
        created.append(store.incident_get(inc["id"]))
    return created


# --------------------------------------------------------------------------- incident report
def incident_report(store: Store, incident_id: str) -> dict[str, Any] | None:
    inc = store.incident_get(incident_id)
    if not inc:
        return None
    evs = store.incident_evidence(incident_id)
    link_type = {e["id"]: e.get("link_type") for e in evs}
    cards = [build_card(e) for e in evs]
    facts = [{"evidence_id": e["id"], "title": e["title"], "severity": e["severity"], "first_seen": e["first_seen"], "link": link_type[e["id"]]} for e in evs if e["confidence"] == "observed"]
    likely = [{"evidence_id": e["id"], "title": e["title"]} for e in evs if e["confidence"] == "likely"]
    hypotheses = [{"evidence_id": e["id"], "title": e["title"], "limitations": e["limitations"]} for e in evs if e["confidence"] == "hypothesis"]
    hypotheses += [{"source": "engineer", "text": f.get("summary") or f.get("text") or str(f), "recorded_at": f.get("recorded_at")} for f in inc["findings"] if f.get("classification") == "hypothesis"]
    confirmed = [{"source": "engineer", "text": f.get("summary") or f.get("text") or str(f), "recorded_at": f.get("recorded_at")} for f in inc["findings"] if f.get("classification") == "confirmed"]
    if inc["confirmed_root_cause"]:
        confirmed.insert(0, {"source": "engineer", "text": inc["confirmed_root_cause"]})
    recs: dict[str, dict[str, Any]] = {}
    for c in cards:
        for r in c["recommendations"]:
            k = r["text"]
            if k not in recs:
                recs[k] = {**r, "evidence_refs": []}
            recs[k]["evidence_refs"].append(c["evidence_id"])
    order = {"informational": 0, "investigation_required": 1, "potentially_disruptive": 2}
    timeline = [{"ts": n["ts"], "kind": n["kind"], "text": n["text"], "author": n["author"]} for n in inc["timeline_notes"]]
    for e in evs:
        timeline.append({"ts": e["first_seen"], "kind": "evidence_started", "text": e["title"], "evidence_id": e["id"], "severity": e["severity"]})
        if e.get("cleared_at") and e["cleared_at"] != e["first_seen"]:
            timeline.append({"ts": e["cleared_at"], "kind": "evidence_cleared", "text": "Cleared: " + e["title"], "evidence_id": e["id"]})
    timeline.sort(key=lambda x: x["ts"])
    return {"incident": inc, "evidence": evs, "cards": cards, "timeline": timeline, "sections": {"observed_facts": facts, "likely_explanations": likely, "hypotheses": hypotheses,
                                                                                               "confirmed_findings": confirmed},
            "recommendations": sorted(recs.values(), key=lambda r: order[r["class"]]),
            "note": "Observed facts come from recorded evidence. Hypotheses are not confirmed. Only items the engineer marked as confirmed are presented as confirmed."}


def report_markdown(rep: dict[str, Any]) -> str:
    i = rep["incident"]
    L = [f"# {i['title']}", "", f"- **ID:** {i['id']}  |  **Status:** {i['status']}  |  **Severity:** {i['severity']}  |  **Stage:** {i['stage']}",
         f"- **Instance:** {i['instance_id']}  |  **Started:** {i.get('started_at')}  |  **Ended:** {i.get('ended_at') or 'ongoing'}", "", "## Symptom", i["symptom"], ""]
    s = rep["sections"]
    for title, items in (("Observed facts", s["observed_facts"]), ("Likely explanations", s["likely_explanations"]), ("Hypotheses (not confirmed)", s["hypotheses"]), ("Confirmed findings", s["confirmed_findings"])):
        L.append(f"## {title}")
        L += [f"- {x.get('title') or x.get('text')}" + (f" ({x['evidence_id']})" if x.get("evidence_id") else "") for x in items] or ["- none recorded"]
        L.append("")
    L += ["## Root cause", f"- Suspected: {i['suspected_root_cause'] or 'not recorded'}", f"- Confirmed: {i['confirmed_root_cause'] or 'not confirmed'}", "", "## Timeline"]
    L += [f"- {t['ts']}  {t['kind']}: {t['text']}" for t in rep["timeline"]]
    L += ["", "## Recommended next steps"]
    L += [f"- [{r['class']}] {r['text']}" + (f" Risk: {r['risk']}" if r.get("risk") else "") for r in rep["recommendations"]]
    L += ["", "## Resolution", i["resolution"] or "not recorded", "", "## Review notes", i["review_notes"] or "not recorded", "", f"_{rep['note']}_"]
    return "\n".join(L)
