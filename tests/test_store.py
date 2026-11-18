"""Persistence: evidence lifecycle, incidents, retention, UTC handling and migration from a v0.6 history database."""
import json
import sqlite3
from datetime import timedelta

import pytest

from app.errors import AppError, NotFound
from app.store import Store
from app.store.core import LEGACY_DDL
from app.timeutil import iso, utcnow

T0 = utcnow().replace(microsecond=0) - timedelta(hours=1)


def cand(fp="fp1", sev="warning", collector="activity", **kw):
    return {"category": kw.pop("category", "blocking"), "severity": sev, "title": kw.pop("title", "t"), "fingerprint": fp, "collector": collector, "source": "test", "details": kw.pop("details", {}), **kw}


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "t.sqlite3")


# ------------------------------------------------------------------ evidence lifecycle
def test_new_update_escalate_clear(store):
    c = store.evidence_apply("i", T0, [cand(peaks={"n": 3})], {"activity"}, set())
    assert len(c["new"]) == 1
    c = store.evidence_apply("i", T0 + timedelta(seconds=15), [cand(sev="critical", peaks={"n": 2})], {"activity"}, set())
    assert len(c["updated"]) == 1 and len(c["escalated"]) == 1
    ev = store.evidence_list(instance_id="i")[0][0]
    assert ev["severity"] == "critical" and ev["observation_count"] == 2 and ev["peaks"] == {"n": 3} and ev["first_seen"] == iso(T0)
    c = store.evidence_apply("i", T0 + timedelta(seconds=30), [], {"activity"}, set())
    assert len(c["cleared"]) == 1
    ev = store.evidence_get(ev["id"])
    assert ev["status"] == "cleared" and ev["cleared_at"] == iso(T0 + timedelta(seconds=30))


def test_evidence_is_not_cleared_when_its_collector_failed(store):
    store.evidence_apply("i", T0, [cand()], {"activity"}, set())
    c = store.evidence_apply("i", T0 + timedelta(seconds=15), [], set(), {"activity"})
    ev = store.evidence_list(instance_id="i")[0][0]
    assert not c["cleared"] and ev["status"] == "active" and ev["collection_status"] == "stale"
    store.evidence_apply("i", T0 + timedelta(seconds=30), [cand()], {"activity"}, set())
    assert store.evidence_list(instance_id="i")[0][0]["collection_status"] == "ok"


def test_other_collectors_evidence_untouched(store):
    store.evidence_apply("i", T0, [cand("a", collector="activity"), cand("b", collector="vacuum")], {"activity", "vacuum"}, set())
    store.evidence_apply("i", T0 + timedelta(seconds=15), [], {"activity"}, set())
    st = {e["fingerprint"]: e["status"] for e in store.evidence_list(instance_id="i")[0]}
    assert st == {"a": "cleared", "b": "active"}


def test_instant_events_use_their_own_time_and_are_deduplicated(store):
    when = T0 - timedelta(minutes=5)
    c = store.evidence_apply("i", T0, [cand("x", instant=True, ts=when)], {"errorlog"}, set())
    c2 = store.evidence_apply("i", T0 + timedelta(seconds=60), [cand("x", instant=True, ts=when)], {"errorlog"}, set())
    items, total = store.evidence_list(instance_id="i")
    assert len(c["new"]) == 1 and not c2["new"] and total == 1 and items[0]["first_seen"] == iso(when) and items[0]["status"] == "cleared"


def test_duplicate_candidates_in_one_run_collapse(store):
    c = store.evidence_apply("i", T0, [cand("same"), cand("same")], {"activity"}, set())
    assert len(c["new"]) == 1


def test_instances_and_demo_flag_never_mix(store):
    store.evidence_apply("a", T0, [cand("p")], {"activity"}, set(), is_demo=False)
    store.evidence_apply("a", T0, [cand("q")], {"activity"}, set(), is_demo=True)
    store.evidence_apply("b", T0, [cand("p")], {"activity"}, set(), is_demo=False)
    assert store.evidence_list(instance_id="a", is_demo=False)[1] == 1 and store.evidence_list(instance_id="a", is_demo=True)[1] == 1 and store.evidence_list(is_demo=False)[1] == 2
    store.evidence_apply("a", T0 + timedelta(seconds=15), [], {"activity"}, set(), is_demo=False)      # clearing live must not touch demo
    assert store.evidence_list(instance_id="a", is_demo=True)[0][0]["status"] == "active"


def test_filters_search_sort_pagination(store):
    for i in range(7):
        store.evidence_apply("i", T0 + timedelta(minutes=i), [cand(f"f{i}", sev="critical" if i % 2 else "info", category="blocking" if i < 4 else "vacuum_pressure", title=f"Table t{i} thing", pid=987650 + i)], {"activity"}, set())
    assert store.evidence_list(severities=["critical"])[1] == 3 and store.evidence_list(categories=["vacuum_pressure"])[1] == 3
    assert store.evidence_list(q="table t5 thing")[1] == 1 and store.evidence_list(q="987655")[1] == 1
    items, total = store.evidence_list(limit=3, offset=3, sort="first_seen", order="asc")
    assert total == 7 and [e["fingerprint"] for e in items] == ["f3", "f4", "f5"]
    assert store.evidence_list(q="%")[1] == 0                                  # wildcards are literal
    assert store.evidence_list(t_from=iso(T0 + timedelta(minutes=5)), status="cleared")[1] >= 0


def test_details_are_size_bounded(store):
    store.evidence_apply("i", T0, [cand("big", details={"blob": "x" * 200_000})], {"activity"}, set())
    d = store.evidence_list(instance_id="i")[0][0]["details"]
    assert d.get("_truncated") is True


# ------------------------------------------------------------------ incidents
def test_incident_lifecycle_and_stage(store):
    i = store.incident_create({"title": "Checkout slow", "symptom": "p95 up", "severity": "high"}, instance_id="i")
    assert i["status"] == "open" and i["stage"] == "collect" and i["started_at"]
    i = store.incident_update(i["id"], {"status": "investigating"}, "kiran")
    assert i["stage"] == "investigate"
    i = store.incident_update(i["id"], {"suspected_root_cause": "idle txn", "finding": {"summary": "waits on accounts"}}, "kiran")
    assert i["findings"][0]["classification"] == "hypothesis" and any(n["kind"] == "hypothesis" for n in i["timeline_notes"])
    i = store.incident_update(i["id"], {"status": "resolved", "resolution": "terminated"}, "kiran")
    assert i["ended_at"] and i["stage"] == "resolve"
    i = store.incident_update(i["id"], {"status": "investigating"}, "kiran")
    assert i["ended_at"] is None
    assert [n["kind"] for n in i["timeline_notes"]].count("status") == 3


def test_invalid_incident_inputs_rejected(store):
    i = store.incident_create({"title": "x y z"})
    for patch in ({"status": "weird"}, {"severity": "urgent"}, {"stage": "party"}, {"started_at": "yesterday-ish"}, {"finding": {"summary": "s", "classification": "certain"}}):
        with pytest.raises(AppError):
            store.incident_update(i["id"], patch)
    with pytest.raises(NotFound):
        store.incident_update("DIR-nope", {"status": "open"})


def test_candidates_dedupe_and_cooldown(store):
    assert not store.incident_candidate_blocked("i:blocking", 30)
    inc = store.incident_create({"title": "auto"}, source="auto", candidate_key="i:blocking", instance_id="i")
    assert inc["status"] == "candidate" and store.incident_candidate_blocked("i:blocking", 30)
    store.incident_update(inc["id"], {"status": "dismissed"})
    assert store.incident_candidate_blocked("i:blocking", 30)                      # created within the cooldown window
    assert not store.incident_candidate_blocked("other:key", 30)


def test_evidence_links_unlink_and_report_inputs(store):
    store.evidence_apply("i", T0, [cand("a"), cand("b")], {"activity"}, set())
    ids = [e["id"] for e in store.evidence_list()[0]]
    inc = store.incident_create({"title": "Link test"}, evidence_ids=ids[:1], instance_id="i")
    store.incident_link_evidence(inc["id"], ids[1], "manual", "me")
    assert set(store.incident_get(inc["id"])["evidence_ids"]) == set(ids) and store.incident_get(inc["id"])["stage"] == "correlate"
    assert store.incident_unlink_evidence(inc["id"], ids[1]) and not store.incident_unlink_evidence(inc["id"], ids[1])
    with pytest.raises(NotFound):
        store.incident_link_evidence(inc["id"], "EV-missing")
    assert store.evidence_list(incident_id=inc["id"])[1] == 1


def test_automatic_links_do_not_advance_the_workflow(store):
    store.evidence_apply("i", T0, [cand("a")], {"activity"}, set())
    eid = store.evidence_list()[0][0]["id"]
    inc = store.incident_create({"title": "auto candidate"}, source="auto", candidate_key="k", instance_id="i")
    store.incident_link_evidence(inc["id"], eid, "auto_structural", "recorder")
    assert store.incident_get(inc["id"])["stage"] == "detect"


# ------------------------------------------------------------------ metrics, runs, retention
def test_metrics_series_bucketing_and_none_values_skipped(store):
    base = T0.replace(second=0)            # align to the bucket boundary so the test is deterministic
    store.metrics_add("i", iso(base), {"a": 1.0, "b": None})
    store.metrics_add("i", iso(base + timedelta(seconds=10)), {"a": 3.0})
    s = store.metrics_series("i", ["a", "b"], iso(base - timedelta(seconds=1)), iso(base + timedelta(minutes=2)), 60)
    assert s["b"] == [] and s["a"][0]["avg"] == 2.0 and s["a"][0]["max"] == 3.0 and s["a"][0]["n"] == 2


def test_retention_deletes_old_rows_but_keeps_linked_evidence_and_incidents(store):
    old = utcnow() - timedelta(days=200)
    store.metrics_add("i", iso(old), {"m": 1.0}); store.metrics_add("i", iso(utcnow()), {"m": 2.0})
    store.run_add("i", "fast", "scheduled", iso(old), iso(old), 5, "ok", {}, None)
    store.evidence_apply("i", old, [cand("keep"), cand("drop")], {"activity"}, set())
    store.evidence_apply("i", old + timedelta(seconds=15), [], {"activity"}, set())
    keep = next(e for e in store.evidence_list()[0] if e["fingerprint"] == "keep")
    store.incident_create({"title": "protects evidence"}, evidence_ids=[keep["id"]], instance_id="i")
    store.audit_add("a", "admin", "x", "t", "ok", "", {})
    days = {k: 30.0 for k in ("retention_metrics_days", "retention_runs_days", "retention_query_stats_days", "retention_evidence_days", "retention_snapshots_days", "retention_audit_days")}
    dry = store.retention_run(days, dry_run=True)
    assert dry["metrics"] == 1 and store.stats()["rows"]["metrics"] == 2
    out = store.retention_run(days)
    assert out["metrics"] == 1 and out["collection_runs"] == 1 and out["evidence"] == 1
    assert {e["fingerprint"] for e in store.evidence_list()[0]} == {"keep"} and store.stats()["rows"]["incidents"] == 1 and store.stats()["rows"]["audit_log"] == 1


def test_run_history_and_gap_detection_inputs(store):
    for k in range(3):
        store.run_add("i", "fast", "scheduled", iso(T0 + timedelta(seconds=15 * k)), iso(T0), 3, "ok", {"activity": {"status": "ok"}}, None)
    store.run_add("i", "fast", "scheduled", iso(T0 + timedelta(seconds=60)), iso(T0), 3, "error", {}, None)
    items, total = store.runs_list("i", "fast")
    assert total == 4 and items[0]["status"] == "error" and len(store.run_times("i", "fast", iso(T0 - timedelta(minutes=1)), iso(T0 + timedelta(minutes=5)))) == 3


def test_audit_log_filters(store):
    store.audit_add("kiran", "admin", "incident.create", "DIR-1", "ok", "127.0.0.1", {"x": 1}); store.audit_add("bob", "viewer", "authz.denied", "/api/x", "denied", "", {})
    assert store.audit_list(outcome="denied")[1] == 1 and store.audit_list(action="incident")[1] == 1 and store.audit_list(q="kiran")[1] == 1


# ------------------------------------------------------------------ migration from v0.6
def make_v06_db(path):
    con = sqlite3.connect(path)
    for ddl in LEGACY_DDL:
        con.execute(ddl)
    con.execute("INSERT INTO incidents VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", ("DIR-20260101-ABCDEF12", "2026-01-01T10:00:00+00:00", "2026-01-01T11:30:00+00:00", "Legacy incident", "slow checkout", "high", "open", "prod", "TCK-1", "old notes", '[{"summary":"f"}]', '[{"summary":"a"}]'))
    con.execute("INSERT INTO incidents VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", ("DIR-20260102-DEMO0001", "2026-01-02T10:00:00+00:00", "2026-01-02T10:00:00+00:00", "Demo one", "s", "low", "open", "", "", "", '[{"demo_mode": true}]', "[]"))
    ev = "INSERT INTO incident_events(event_key,started_at,ended_at,severity,title,details_json,active) VALUES (?,?,?,?,?,?,?)"
    con.execute(ev, ("blocking", "2026-01-01 10:03:00.123456+00:00", "2026-01-01 10:20:00+00:00", "critical", "Lock chain", json.dumps({"details": [{"query": "UPDATE t SET x=1 WHERE secret='abc'"}]}), 0))
    con.execute(ev, ("connections_high", "2026-01-01T10:05:00+00:00", None, "warning", "Connections high", "{}", 1))
    sn = "INSERT INTO snapshots(captured_at,database_name,title,notes,payload_json) VALUES (?,?,?,?,?)"
    con.execute(sn, ("2026-01-01T10:10:00+00:00", "shop", "DIR-20260101-ABCDEF12 evidence capture", "", '{"demo_mode": false}'))
    con.execute(sn, ("2026-01-02T10:10:00+00:00", "demo", "Manual", "", '{"demo_mode": true}'))
    con.execute("INSERT INTO query_performance_samples(captured_at,database_name,queryid,query_text,calls,total_exec_time,mean_exec_time,rows_returned,source) VALUES (?,?,?,?,?,?,?,?,?)",
                ("2026-01-01 10:00:00+00:00", "shop", "111", "SELECT * FROM users WHERE email = 'joe@x.io'", 10, 100, 10, 10, "pg_stat_statements"))
    con.commit(); con.close()


def test_v06_database_is_upgraded_in_place_without_data_loss(tmp_path):
    p = tmp_path / "incident_history.sqlite3"
    make_v06_db(p)
    s = Store(p)
    assert s.migrated_from == 0 and s.stats()["schema_version"] == 2
    inc = s.incident_get("DIR-20260101-ABCDEF12")
    assert inc["title"] == "Legacy incident" and inc["findings"] == [{"summary": "f"}] and inc["actions"] == [{"summary": "a"}] and inc["created_at"] == "2026-01-01T10:00:00.000Z" and inc["instance_id"] == "default" and not inc["is_demo"]
    assert inc["snapshot_ids"] == [1]
    assert s.incident_get("DIR-20260102-DEMO0001")["is_demo"] is True
    live, n = s.evidence_list(is_demo=False)
    assert n == 2 and {e["category"] for e in live} == {"blocking", "connection_saturation"} and next(e for e in live if e["category"] == "blocking")["first_seen"] == "2026-01-01T10:03:00.123Z"
    assert "UPDATE t SET" not in json.dumps(live), "raw v0.6 statement payloads must not be carried over"
    q = s.qstats_samples("default", "2025-01-01T00:00:00Z", "2027-01-01T00:00:00Z")
    assert len(q) == 1 and "joe@x.io" not in (q[0]["text"] or "") and "?" in q[0]["text"]
    s2 = Store(p)                                   # opening twice is idempotent
    assert s2.migrated_from == 2 and s2.evidence_list(is_demo=False)[1] == 2


def test_fresh_database_has_current_schema(tmp_path):
    s = Store(tmp_path / "fresh.sqlite3")
    assert s.migrated_from == 0 and s.stats()["schema_version"] == 2 and s.ping()
