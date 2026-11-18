"""HTTP API contracts, workflows, validation and error shape (demo mode: synthetic data in a separate store)."""
import json
from datetime import timedelta

from fastapi.testclient import TestClient

from app.main import create_app
from app.timeutil import iso, utcnow
from tests.conftest import make_settings, phase_offset_for


def ok(r, code=200):
    assert r.status_code == code, r.text
    return r.json()


# ------------------------------------------------------------------ basics, headers, errors
def test_index_and_static_assets(demo_app):
    r = demo_app.get("/")
    assert r.status_code == 200 and "DB Incident Replay" in r.text
    assert demo_app.get("/static/js/main.js").status_code == 200 and demo_app.get("/static/css/app.css").status_code == 200


def test_security_headers_present(demo_app):
    for path in ("/", "/api/health"):
        h = demo_app.get(path).headers
        assert h["x-content-type-options"] == "nosniff" and h["x-frame-options"] == "DENY" and "default-src 'self'" in h["content-security-policy"] and h["cache-control"] == "no-store"
        assert "x-request-id" in h


def test_error_shape_is_consistent_and_leaks_nothing(demo_app):
    r = demo_app.get("/api/incidents/DIR-nope")
    b = r.json()
    assert r.status_code == 404 and b["error"]["code"] == "NOT_FOUND" and b["detail"] == b["error"]["message"] and b["error"]["request_id"] == r.headers["x-request-id"]
    r = demo_app.get("/api/live/activity?page_size=9999&sort=evil")
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR" and isinstance(r.json()["detail"], str)
    assert "Traceback" not in r.text and demo_app.get("/api/nope").status_code == 404
    assert demo_app.post("/api/incidents", json={"title": "x"}).status_code == 422


def test_openapi_lists_the_main_resources(demo_app):
    paths = ok(demo_app.get("/openapi.json"))["paths"]
    for p in ("/api/incidents", "/api/evidence", "/api/timeline", "/api/live/blocking", "/api/queries/top", "/api/settings", "/api/health/ready"):
        assert p in paths


def test_health_ready_live_and_auth_status(demo_app):
    assert ok(demo_app.get("/api/health/live"))["status"] == "ok" and ok(demo_app.get("/api/health/ready"))["status"] == "ready"
    h = ok(demo_app.get("/api/health"))
    assert h["demo_mode"] is True and h["instances"][0]["id"] == "demo" and ok(demo_app.get("/api/auth/status"))["auth_mode"] == "none"


def test_demo_data_lives_in_its_own_store_and_is_labelled(demo_app):
    ctx = demo_app.app_ctx
    assert ctx.store.path.name == "demo_history.sqlite3" and not (ctx.store.path.parent / "incident_history.sqlite3").exists()
    ev = ok(demo_app.get("/api/evidence?page_size=100"))
    assert ev["demo_mode"] is True and ev["items"] and all(e["is_demo"] for e in ev["items"])
    assert ok(demo_app.get("/api/live/overview"))["meta"]["demo_mode"] is True


# ------------------------------------------------------------------ live views
def test_overview_contract_and_kpis(demo_app):
    o = ok(demo_app.get("/api/live/overview"))
    assert set(o) >= {"meta", "monitoring", "kpis", "availability", "failures", "evidence", "incidents"} and o["monitoring"]["status"] == "demo"
    assert o["kpis"]["blocked_sessions"] >= 1 and o["kpis"]["root_blockers"] == 1 and 0 <= o["monitoring"]["coverage_1h_pct"] <= 100


def test_activity_filters_sorting_pagination(demo_app):
    a = ok(demo_app.get("/api/live/activity?page_size=5&sort=pid&order=asc"))
    assert a["page"] == 1 and len(a["items"]) == 5 and a["total"] > 5 and a["pages"] >= 2 and a["items"][0]["pid"] < a["items"][-1]["pid"]
    b = ok(demo_app.get("/api/live/activity?blocked_only=true&page_size=100"))
    assert b["total"] >= 1 and all(r["blocked_by"] for r in b["items"])
    assert ok(demo_app.get("/api/live/activity?state=idle in transaction"))["items"][0]["pid"] == 4242
    assert ok(demo_app.get("/api/live/activity?q=billing-job"))["total"] == 1 and ok(demo_app.get("/api/live/activity?pid=4242"))["total"] == 1
    assert ok(demo_app.get("/api/live/activity?min_age_s=500"))["total"] < a["total"]
    assert ok(demo_app.get("/api/live/activity?page=999"))["items"] == []


def test_blocking_endpoint_distinguishes_root_from_direct_and_explains_snapshot_nature(demo_app):
    b = ok(demo_app.get("/api/live/blocking"))
    g = b["graph"]
    assert g["roots"][0]["pid"] == 4242 and g["roots"][0]["blocked_count"] >= 10 and g["roots"][0]["role"] == "root_blocker" and g["cycles"] == []
    assert any(n["role"] == "blocked" and n["direct_blockers"] == [4242] for n in g["nodes"])
    assert "instant of collection" in b["definitions"]["snapshot"] and b["recent_evidence"] and "root_blocker" in b["definitions"]


def test_other_live_views_return_meta_and_data(demo_app):
    for path, key in (("/api/live/connections", "connections"), ("/api/live/vacuum", "tables"), ("/api/live/replication", "standbys"), ("/api/live/database", "databases"), ("/api/live/config", "items")):
        d = ok(demo_app.get(path))
        assert d["meta"]["status"] == "ok" and key in d and d["meta"]["age_s"] is not None, path
    r = ok(demo_app.get("/api/live/replication"))
    assert r["integrations"]["patroni"]["status"] == "not_configured" and "SQL" in r["integrations"]["patroni"]["note"]
    assert any(s["slot_type"] == "logical" for s in r["slots"]) and any(s["slot_type"] == "physical" for s in r["slots"])


def test_manual_refresh_runs_collection(demo_app):
    before = demo_app.app_ctx.store.stats()["rows"]["collection_runs"]
    r = ok(demo_app.post("/api/live/refresh", json={"tiers": ["fast"]}))
    assert r["results"]["fast"]["status"] in ("ok", "partial") and demo_app.app_ctx.store.stats()["rows"]["collection_runs"] == before + 1
    assert demo_app.post("/api/live/refresh", json={"tiers": ["nope"]}).status_code == 422


# ------------------------------------------------------------------ metrics + timeline
def test_metric_series_gauges_rates_and_validation(demo_app):
    now = utcnow()
    q = f"from={iso(now - timedelta(minutes=40))}&to={iso(now)}"
    d = ok(demo_app.get(f"/api/metrics/series?names=sessions_total,blocked_sessions,db_xact_commit&{q}"))
    assert d["series"]["sessions_total"]["kind"] == "gauge" and d["series"]["db_xact_commit"]["kind"] == "rate_per_s" and d["series"]["blocked_sessions"]["points"]
    assert max(p["value"] for p in d["series"]["blocked_sessions"]["points"]) >= 10 and 0 <= d["coverage_pct"] <= 100 and "never interpolated" in d["note"]
    assert demo_app.get("/api/metrics/series?names=bogus").status_code == 422
    assert demo_app.get(f"/api/metrics/series?names=sessions_total&from={iso(now)}&to={iso(now - timedelta(hours=1))}").status_code == 422


def test_timeline_marks_gaps_before_monitoring_and_never_invents_data(demo_app):
    now = utcnow()
    t = ok(demo_app.get(f"/api/timeline?from={iso(now - timedelta(hours=3))}&to={iso(now)}"))
    kinds = {g["kind"] for g in t["gaps"]}
    assert "before_monitoring" in kinds and t["coverage_pct"] < 100 and t["events"] and all(set(e) >= {"id", "type", "start", "severity"} for e in t["events"])
    inside = ok(demo_app.get(f"/api/timeline?from={iso(now - timedelta(minutes=20))}&to={iso(now)}&category=blocking"))
    assert inside["events"] and all(e["category"] == "blocking" for e in inside["events"] if e["type"] == "evidence")
    empty = ok(demo_app.get(f"/api/timeline?from={iso(now - timedelta(days=30))}&to={iso(now - timedelta(days=29))}"))
    assert empty["events"] == [] and empty["coverage_pct"] == 0


def test_timeline_detects_collection_gap_between_runs(tmp_path, monkeypatch):
    monkeypatch.setenv("DEMO_PHASE_OFFSET_S", str(phase_offset_for(100)))
    app = create_app(make_settings(tmp_path), start_recorder=False)
    with TestClient(app) as c:
        rec = app.state.ctx.recorder
        t0 = (utcnow() - timedelta(minutes=30)).replace(microsecond=0)
        for k in range(10):
            rec.run_tier_sync("demo", "fast", "t", ts=t0 + timedelta(seconds=15 * k), wait=True)
        for k in range(10):                                       # 10 minute outage, then collection resumes
            rec.run_tier_sync("demo", "fast", "t", ts=t0 + timedelta(minutes=12, seconds=15 * k), wait=True)
        d = ok(c.get(f"/api/timeline?from={iso(t0)}&to={iso(t0 + timedelta(minutes=16))}"))
        outage = [g for g in d["gaps"] if g["kind"] == "no_collection"]
        assert outage and 400 <= outage[0]["seconds"] <= 700 and d["coverage_pct"] < 100


# ------------------------------------------------------------------ evidence
def test_evidence_list_filters_and_facets(demo_app):
    all_ = ok(demo_app.get("/api/evidence?page_size=200"))
    cats = {e["category"] for e in all_["items"]}
    assert {"blocking", "idle_in_transaction", "connection_saturation"} <= cats and "blocking" in all_["facets"]["categories"]
    assert all(e["category"] == "blocking" for e in ok(demo_app.get("/api/evidence?category=blocking"))["items"])
    assert all(e["severity"] == "critical" for e in ok(demo_app.get("/api/evidence?severity=critical"))["items"])
    assert all(e["status"] == "active" for e in ok(demo_app.get("/api/evidence?status=active"))["items"])
    assert demo_app.get("/api/evidence?severity=catastrophic").status_code == 422 and demo_app.get("/api/evidence?sort=drop").status_code == 422 and demo_app.get("/api/evidence?from=garbage").status_code == 422
    asc = ok(demo_app.get("/api/evidence?sort=first_seen&order=asc&page_size=50"))["items"]
    assert [e["first_seen"] for e in asc] == sorted(e["first_seen"] for e in asc)
    assert ok(demo_app.get("/api/evidence?q=4242"))["total"] >= 1


def test_evidence_detail_card_answers_the_six_questions(demo_app):
    b = ok(demo_app.get("/api/evidence?category=blocking&page_size=1"))["items"][0]
    d = ok(demo_app.get(f"/api/evidence/{b['id']}"))
    c = d["card"]
    assert c["observed"] and c["why_it_matters"] and c["impact"] and c["evidence"]["source"] and c["investigate"] and c["recommendations"] and "snapshot" in c["limitations"]
    assert c["epistemic"] == "observed_fact" and all(r["class"] in ("informational", "investigation_required", "potentially_disruptive") and r["rationale"] for r in c["recommendations"])
    disruptive = [r for r in c["recommendations"] if r["class"] == "potentially_disruptive"]
    assert disruptive and disruptive[0]["risk"] and "pg_cancel_backend" in disruptive[0]["sql"]
    assert all(i["drilldown"]["page"] for i in c["investigate"]) and d["evidence"]["details"]["root"]["pid"] == 4242
    assert demo_app.get("/api/evidence/EV-0-NOPE").status_code == 404


def test_related_evidence_separates_structural_from_temporal_and_warns_about_causation(demo_app):
    b = ok(demo_app.get("/api/evidence?category=blocking&page_size=1&sort=first_seen&order=desc"))["items"][0]
    rel = ok(demo_app.get(f"/api/evidence/{b['id']}/related?window_min=30"))
    assert "causation" in rel["note"] and rel["items"]
    structural = [x for x in rel["items"] if x["strength"] == "structural"]
    assert structural and any(l["type"] == "shared_pid" for x in structural for l in x["links"])
    assert all(x["strength"] in ("structural", "temporal") and x["links"] for x in rel["items"])
    assert [x["strength"] for x in rel["items"]].index("structural") == 0


def test_export_json_and_csv_with_formula_injection_protection(demo_app):
    ctx = demo_app.app_ctx
    ctx.store.evidence_apply("demo", utcnow(), [{"category": "error_log", "severity": "info", "title": "=HYPERLINK(\"http://evil\")", "fingerprint": "x", "collector": "errorlog", "source": "t", "details": {}, "instant": True}], set(), set(), True)
    j = ok(demo_app.get("/api/evidence/export?format=json&limit=50"))
    assert j["evidence"] and "snapshots" in j and "incidents" in j and j["demo_mode"] is True
    r = demo_app.get("/api/evidence/export?format=csv&limit=500")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv") and "'=HYPERLINK" in r.text and "\n=HYPERLINK" not in r.text and ",=HYPERLINK" not in r.text
    assert any(a["action"] == "evidence.export" for a in ok(demo_app.get("/api/audit"))["items"])


# ------------------------------------------------------------------ incidents
def test_critical_evidence_creates_a_candidate_that_a_human_triages(demo_app):
    cands = ok(demo_app.get("/api/incidents?status=candidate"))
    assert cands["total"] >= 1
    c = cands["items"][0]
    assert c["source"] == "auto" and c["status"] == "candidate" and c["stage"] == "detect" and c["is_demo"] is True
    full = ok(demo_app.get(f"/api/incidents/{c['id']}"))
    assert full["evidence_ids"] and "not established" in full["symptom"] and any("candidate" in n["text"].lower() for n in full["timeline_notes"])
    p = ok(demo_app.patch(f"/api/incidents/{c['id']}", json={"status": "open"}))
    assert p["status"] == "open" and p["stage"] == "collect"


def test_manual_incident_full_workflow_with_report(demo_app):
    ev = ok(demo_app.get("/api/evidence?category=blocking&page_size=1"))["items"][0]
    inc = ok(demo_app.post("/api/incidents", json={"title": "Checkout stalls at 10:03", "symptom": "Orders time out", "severity": "high", "ticket_ref": "TCK-9", "evidence_ids": [ev["id"]]}))
    assert inc["status"] == "open" and inc["instance_id"] == "demo" and inc["evidence_ids"] == [ev["id"]]
    iid = inc["id"]
    ok(demo_app.patch(f"/api/incidents/{iid}", json={"status": "investigating", "suspected_root_cause": "Billing job left a transaction open"}))
    ok(demo_app.patch(f"/api/incidents/{iid}", json={"finding": {"summary": "PID 4242 idle in transaction", "classification": "observed"}}))
    ok(demo_app.patch(f"/api/incidents/{iid}", json={"action": {"summary": "Asked owner to commit"}}))
    ok(demo_app.post(f"/api/incidents/{iid}/notes", json={"text": "Owner confirmed batch bug", "kind": "note"}))
    done = ok(demo_app.patch(f"/api/incidents/{iid}", json={"confirmed_root_cause": "Missing COMMIT in batch job (reproduced)", "status": "resolved", "resolution": "Session terminated by owner; job fixed"}))
    assert done["status"] == "resolved" and done["ended_at"] and done["actions"][0]["execution"] == "recorded_only" and done["findings"][0]["classification"] == "observed"
    rep = ok(demo_app.get(f"/api/incidents/{iid}/report"))
    s = rep["sections"]
    assert s["observed_facts"] and s["confirmed_findings"][0]["text"].startswith("Missing COMMIT") and rep["cards"] and rep["recommendations"] and rep["timeline"] and "related_snapshots" in rep
    assert [r["class"] for r in rep["recommendations"]] == sorted((r["class"] for r in rep["recommendations"]), key=["informational", "investigation_required", "potentially_disruptive"].index)
    md = demo_app.get(f"/api/incidents/{iid}/report?format=markdown")
    assert md.status_code == 200 and "## Hypotheses (not confirmed)" in md.text and "Confirmed: Missing COMMIT" in md.text and "Suspected: Billing job" in md.text
    cap = ok(demo_app.post(f"/api/incidents/{iid}/capture"))
    assert cap["demo_mode"] is True and ok(demo_app.get(f"/api/incidents/{iid}"))["snapshot_ids"] == [cap["snapshot_id"]]
    assert ok(demo_app.get(f"/api/snapshots/{cap['snapshot_id']}"))["payload"]["demo_mode"] is True


def test_incident_validation_links_and_listing(demo_app):
    assert demo_app.post("/api/incidents", json={"title": "ab"}).status_code == 422
    assert demo_app.post("/api/incidents", json={"title": "valid title", "evidence_ids": ["EV-missing"]}).status_code == 404
    assert demo_app.post("/api/incidents", json={"title": "valid title", "instance": "ghost"}).status_code == 404
    inc = ok(demo_app.post("/api/incidents", json={"title": "Link me please"}))
    ev = ok(demo_app.get("/api/evidence?page_size=2"))["items"]
    assert ok(demo_app.post(f"/api/incidents/{inc['id']}/evidence", json={"evidence_id": ev[0]["id"]}))["linked"]
    assert ok(demo_app.get(f"/api/incidents/{inc['id']}/evidence"))["total"] == 1 and ok(demo_app.get(f"/api/evidence?incident_id={inc['id']}"))["total"] == 1
    assert ok(demo_app.delete(f"/api/incidents/{inc['id']}/evidence/{ev[0]['id']}"))["linked"] is False and demo_app.delete(f"/api/incidents/{inc['id']}/evidence/{ev[0]['id']}").status_code == 404
    assert demo_app.patch(f"/api/incidents/{inc['id']}", json={"status": "bogus"}).status_code == 422 and demo_app.patch(f"/api/incidents/{inc['id']}", json={}).status_code == 422
    assert demo_app.patch(f"/api/incidents/{inc['id']}", json={"finding": {"summary": "x", "classification": "sure"}}).status_code == 422
    lst = ok(demo_app.get("/api/incidents?status=open,candidate&q=link me"))
    assert [i["id"] for i in lst["items"]] == [inc["id"]] and lst["counts"]["open"] >= 1


def test_dismissed_candidate_is_not_recreated_during_cooldown(demo_app):
    ctx = demo_app.app_ctx
    c = ok(demo_app.get("/api/incidents?status=candidate"))["items"][0]
    ok(demo_app.patch(f"/api/incidents/{c['id']}", json={"status": "dismissed"}))
    n = ok(demo_app.get("/api/incidents"))["total"]
    ctx.recorder.run_tier_sync("demo", "fast", "t", wait=True)      # same ongoing condition
    assert ok(demo_app.get("/api/incidents"))["total"] == n


# ------------------------------------------------------------------ query performance
def test_query_top_trend_and_regressions(demo_app):
    t = ok(demo_app.get("/api/queries/top?window_min=60&sort=mean"))
    assert t["items"] and t["limitations"] and "UPDATE accounts" in " ".join(i["text"] or "" for i in t["items"])
    assert all(t["items"][i]["mean_ms"] >= t["items"][i + 1]["mean_ms"] for i in range(len(t["items"]) - 1))
    q = t["items"][0]
    tr = ok(demo_app.get(f"/api/queries/trend?queryid={q['queryid']}"))
    assert tr["points"] and "derived from cumulative" in tr["note"]
    assert demo_app.get("/api/queries/trend").status_code == 422 and demo_app.get("/api/queries/top?sort=hack").status_code == 422
    assert any(e["category"] == "query_regression" for e in t["regressions"])


def test_legacy_query_performance_endpoints_still_work(demo_app):
    cap = ok(demo_app.post("/api/query-performance/capture"))
    assert cap["captured"] is True and cap["demo_mode"] is True
    h = ok(demo_app.get("/api/query-performance/history"))
    assert h["count"] >= 1 and all("interval_mean_ratio" in i for i in h["items"]) and h["limitations"]
    upd = next(i for i in h["items"] if "UPDATE accounts" in (i["query"] or ""))
    assert max(upd["interval_means_ms"]) / max(1e-9, min(upd["interval_means_ms"])) > 10        # the slow intervals during the lock pile-up are visible


# ------------------------------------------------------------------ v0.6 compatible endpoints
def test_v06_diagnostics_and_monitor_endpoints(demo_app):
    s = ok(demo_app.get("/api/diagnostics/summary"))
    assert s["locks"] >= 1 and s["demo_mode"] is True and s["findings"]
    assert ok(demo_app.get("/api/diagnostics/locks"))["root_blockers"][0]["pid"] == 4242 and ok(demo_app.get("/api/diagnostics/idle-transactions"))["count"] >= 1
    assert ok(demo_app.get("/api/diagnostics/connections"))["client_backends"] > 0 and ok(demo_app.get("/api/diagnostics/activity?limit=3"))["count"] == 3
    latest = ok(demo_app.get("/api/monitor/latest"))
    assert latest["payload"]["demo_mode"] is True and latest["payload"]["locks"]["count"] >= 1
    assert ok(demo_app.get("/api/monitor/status"))["demo_mode"] is True and ok(demo_app.get("/api/monitor/history?hours=1"))["series"]["sessions_total"] and ok(demo_app.get("/api/events"))["items"]


def test_v06_snapshot_history_endpoints(demo_app):
    cap = ok(demo_app.post("/api/history/capture", json={"title": "before restart", "notes": "n"}))
    assert cap["demo_mode"] is True and cap["id"]
    assert ok(demo_app.get("/api/history"))["items"][0]["title"] == "before restart" and ok(demo_app.get(f"/api/history/{cap['id']}"))["payload"]["blocking"]["roots"]
    assert demo_app.get("/api/history/999999").status_code == 404


def test_runbooks_are_preserved(demo_app):
    rb = ok(demo_app.get("/api/runbooks"))
    assert rb["total"] >= 5 and all("id" in r for r in rb["items"])
    d = ok(demo_app.get(f"/api/runbooks/{rb['items'][0]['id']}"))
    assert d["checks"] and demo_app.get("/api/runbooks/none").status_code == 404


def test_log_ingest_parses_redacts_and_stores_nothing(demo_app):
    text = "2026-10-27 10:00:09.000 UTC [300] app@shop ERROR:  duplicate key value violates unique constraint \"u\". Key (email)=(a@b.c) already exists\n2026-10-27 10:00:09.000 UTC [300] app@shop STATEMENT:  INSERT INTO u(email) VALUES ('a@b.c')\n"
    before = demo_app.app_ctx.store.stats()["rows"]["evidence"]
    r = ok(demo_app.post("/api/diagnostics/failed-queries/ingest", json={"source_name": "paste", "log_text": text}))
    assert r["count"] == 1 and "a@b.c" not in json.dumps(r["items"]) and demo_app.app_ctx.store.stats()["rows"]["evidence"] == before
    assert demo_app.post("/api/diagnostics/failed-queries/ingest", json={"log_text": ""}).status_code == 422
    assert ok(demo_app.get("/api/diagnostics/failed-queries"))["available"] is False


# ------------------------------------------------------------------ settings, retention, instances, audit
def test_settings_validation_persistence_and_audit(tmp_path, monkeypatch):
    monkeypatch.setenv("DEMO_PHASE_OFFSET_S", str(phase_offset_for(1500)))
    app = create_app(make_settings(tmp_path), start_recorder=False)
    with TestClient(app) as c:
        items = ok(c.get("/api/settings"))["items"]
        assert {i["name"] for i in items} >= {"fast_interval_s", "block_warn_s", "retention_evidence_days"}
        r = c.put("/api/settings", json={"values": {"long_query_s": 90, "conn_warn_pct": 70}})
        assert r.status_code == 200 and ok(c.get("/api/settings"))["items"][[i["name"] for i in items].index("long_query_s")]["value"] == 90
        bad = c.put("/api/settings", json={"values": {"long_query_s": 5, "fast_interval_s": 1}})
        assert bad.status_code == 422 and bad.json()["error"]["details"]["fast_interval_s"] and app.state.ctx.settings.t("long_query_s") == 90
        assert c.put("/api/settings", json={"values": {"nope": 1}}).status_code == 422 and c.put("/api/settings", json={"values": {}}).status_code == 422
        assert any(a["action"] == "settings.update" for a in ok(c.get("/api/audit"))["items"])
    app2 = create_app(make_settings(tmp_path), start_recorder=False)        # restart: overrides come back from the store
    assert app2.state.ctx.settings.t("long_query_s") == 90 and app2.state.ctx.settings.t("conn_warn_pct") == 70
    with TestClient(app2) as c2:
        assert ok(c2.delete("/api/settings/long_query_s"))["overridden"] is False and c2.delete("/api/settings/nope").status_code == 404


def test_retention_endpoint_defaults_to_dry_run(demo_app):
    r = ok(demo_app.post("/api/maintenance/retention"))
    assert r["dry_run"] is True and "metrics" in r["rows_affected"] and "never removed" in r["note"]
    assert ok(demo_app.post("/api/maintenance/retention?dry_run=false"))["dry_run"] is False


def test_instances_and_capabilities(demo_app):
    i = ok(demo_app.get("/api/instances"))
    assert i["total"] == 1 and i["items"][0]["demo"] is True and "password" not in json.dumps(i)
    assert ok(demo_app.post("/api/instances/demo/test"))["demo"] is True and demo_app.post("/api/instances/ghost/test").status_code == 404
    assert ok(demo_app.post("/api/instances/demo/monitoring", json={"enabled": False}))["enabled"] is False
    caps = ok(demo_app.get("/api/system/capabilities"))
    assert {c["capability"] for c in caps["capabilities"]} >= {"activity", "blocking", "statements", "replication", "vacuum", "errorlog"} and caps["integrations"]["patroni"]["status"] == "not_configured"
    assert next(c for c in caps["capabilities"] if c["capability"] == "errorlog")["status"] == "not_configured"


def test_system_status_runs_and_audit(demo_app):
    s = ok(demo_app.get("/api/system/status"))
    assert s["demo_mode"] is True and s["store"]["schema_version"] == 2 and "password" not in json.dumps(s).lower().replace("password_env", "")
    runs = ok(demo_app.get("/api/runs?tier=fast&page_size=3"))
    assert runs["total"] > 3 and len(runs["items"]) == 3 and "activity" in runs["items"][0]["collectors"]
    ok(demo_app.post("/api/incidents", json={"title": "audit me please"}))
    a = ok(demo_app.get("/api/audit?action=incident.create"))
    assert a["total"] >= 1 and a["items"][0]["actor"] == "local" and a["items"][0]["details"]["severity"] == "medium"


def test_empty_store_returns_pending_not_errors(empty_demo_app):
    c = empty_demo_app
    assert ok(c.get("/api/evidence"))["total"] == 0 and ok(c.get("/api/incidents"))["total"] == 0 and ok(c.get("/api/timeline"))["events"] == []
    q = ok(c.get("/api/queries/top"))
    assert q["items"] == [] and ok(c.get("/api/metrics/series?names=sessions_total"))["series"]["sessions_total"]["points"] == []
