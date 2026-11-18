"""Analyzer rules: thresholds, context, hypotheses vs observations, and query-statistics deltas."""
from datetime import timedelta

import pytest

from app.config import Settings
from app.pg import analysis as A
from app.pg.collectors import CResult, summarize_connections
from app.pg.graph import build_blocking_graph
from app.services import query_regressions, query_top
from app.store import Store
from app.timeutil import iso, utcnow

T = Settings.from_env({"DEMO_MODE": "true"})
NOW = utcnow().replace(microsecond=0)


def r(pid, state="active", qage=1.0, xact=None, blocked=(), app="app", query="SELECT 1", btype="client backend", wait=(None, None), sage=None):
    return {"pid": pid, "datname": "d", "usename": "u", "application_name": app, "client_addr": "1.2.3.4", "backend_type": btype, "state": state, "wait_event_type": wait[0], "wait_event": wait[1],
            "backend_start": "2026-01-01T00:00:00Z", "xact_start": None, "query_start": "2026-01-01T00:00:01Z", "state_change": None, "xact_age_s": xact, "query_age_s": qage, "state_age_s": sage if sage is not None else qage,
            "xid_age": None, "xmin_age": None, "query_id": None, "query": query, "query_fingerprint": f"fp{pid}", "blocked_by": list(blocked)}


def activity(rows, maxc=100, reserved=3, waits=()):
    grouped = {}
    for x in rows:
        k = (x["datname"], x["usename"], x["application_name"], x["state"], x["backend_type"])
        grouped.setdefault(k, 0); grouped[k] += 1
    conn = summarize_connections([{"datname": k[0], "usename": k[1], "application_name": k[2], "state": k[3], "backend_type": k[4], "sessions": n} for k, n in grouped.items()], maxc, reserved, rows)
    return {"rows": rows, "connections": conn, "waits": list(waits), "blocking": build_blocking_graph(rows)}


def cats(cands):
    return sorted((c["category"], c["severity"]) for c in cands)


def with_settings(**kw):
    s = Settings.from_env({"DEMO_MODE": "true"})
    for k, v in kw.items():
        s.set_override(k, v)
    return s


# ------------------------------------------------------------------ blocking
def blocking_rows(wait, n=1, root_state="idle in transaction"):
    return [r(1, root_state, qage=5, xact=900, sage=900)] + [r(100 + i, qage=wait, blocked=[1], wait=("Lock", "transactionid")) for i in range(n)]


@pytest.mark.parametrize("wait,n,expected", [(2, 1, None), (5, 1, "info"), (20, 1, "warning"), (70, 1, "critical"), (5, 6, "critical")])
def test_blocking_severity_follows_thresholds(wait, n, expected):
    c, _m = A.analyze_activity(T, activity(blocking_rows(wait, n)), NOW)
    got = [x["severity"] for x in c if x["category"] == "blocking"]
    assert (got[0] if got else None) == expected


def test_blocking_evidence_names_root_and_marks_limits():
    c, m = A.analyze_activity(T, activity(blocking_rows(30, 3)), NOW)
    b = next(x for x in c if x["category"] == "blocking")
    assert b["pid"] == 1 and b["details"]["root_is_idle_in_transaction"] and b["details"]["blocked_count"] == 3 and b["peaks"]["max_wait_s"] == 30
    assert "snapshot" in b["limitations"] and b["fingerprint"].startswith("blocking:d:1:")
    assert m["blocked_sessions"] == 3 and m["root_blockers"] == 1


def test_blocked_sessions_are_not_reported_as_long_queries():
    c, _ = A.analyze_activity(T, activity(blocking_rows(500, 2)), NOW)
    assert not any(x["category"] == "long_query" for x in c)


def test_long_query_threshold_and_autovacuum_exclusion():
    rows = [r(1, qage=100), r(2, qage=5), r(3, qage=900, query="autovacuum: VACUUM public.t")]
    c, _ = A.analyze_activity(T, activity(rows), NOW)
    assert [x["pid"] for x in c if x["category"] == "long_query"] == [1]


def test_idle_in_transaction_severity():
    rows = [r(1, "idle in transaction", sage=30), r(2, "idle in transaction", sage=120), r(3, "idle in transaction", sage=4000)]
    c, _ = A.analyze_activity(T, activity(rows), NOW)
    sev = {x["pid"]: x["severity"] for x in c if x["category"] == "idle_in_transaction"}
    assert sev == {2: "warning", 3: "critical"}


def test_old_active_transaction_flagged_separately():
    c, _ = A.analyze_activity(T, activity([r(1, qage=1, xact=7200)]), NOW)
    assert ("long_transaction", "warning") in cats(c)


@pytest.mark.parametrize("used,expected", [(70, None), (85, "warning"), (95, "critical"), (97, "critical")])
def test_connection_saturation(used, expected):
    c, m = A.analyze_activity(T, activity([r(i, "idle") for i in range(used)]), NOW)
    got = [x["severity"] for x in c if x["category"] == "connection_saturation"]
    assert (got[0] if got else None) == expected and m["conn_utilization_pct"] == used


def test_saturation_flags_possible_symptom_of_blocking_without_claiming_cause():
    rows = blocking_rows(40, 60) + [r(500 + i, "idle") for i in range(30)]
    c, _ = A.analyze_activity(T, activity(rows), NOW)
    cs = next(x for x in c if x["category"] == "connection_saturation")
    assert cs["details"]["possible_symptom_of_blocking"] is True and "symptom" in cs["limitations"].lower()


def test_wait_pileup_needs_many_sessions():
    rows = [r(i, wait=("LWLock", "WALWrite")) for i in range(12)]
    w = [{"wait_event_type": "LWLock", "wait_event": "WALWrite", "state": "active", "sessions": 12}]
    assert ("wait_event_pileup", "warning") in cats(A.analyze_activity(T, activity(rows, waits=w), NOW)[0])
    w2 = [{"wait_event_type": "LWLock", "wait_event": "WALWrite", "state": "active", "sessions": 3}]
    assert not any(x["category"] == "wait_event_pileup" for x in A.analyze_activity(T, activity(rows[:3], waits=w2), NOW)[0])


def test_thresholds_are_configurable():
    s = with_settings(long_query_s=2000, block_min_s=0, block_warn_s=1)
    c, _ = A.analyze_activity(s, activity([r(1, qage=900)] + blocking_rows(1.5, 1)[1:] + [r(9, "idle in transaction", qage=5, xact=30, sage=30)]), NOW)
    assert not any(x["category"] == "long_query" for x in c)


# ------------------------------------------------------------------ standard tier
def std(standbys=(), slots=(), dl=3, prev=None, bg=None, arch=None, rec=None, role="primary"):
    dbs = {"databases": [], "current": {"datname": "d", "deadlocks": dl, "xact_commit": 1, "xact_rollback": 0, "blks_hit": 1, "blks_read": 0}, "bgwriter": bg or {}, "wal": {}, "wal_dir": None, "archiver": arch or {}}
    rep = {"role": role, "in_recovery": role == "standby", "standbys": list(standbys), "slots": list(slots), "receiver": None, "recovery": rec, "logical_subscriptions": [], "settings": {"max_slot_wal_keep_size": "-1"}}
    res = {"dbstats": CResult("dbstats", "ok", dbs), "replication": CResult("replication", "ok", rep)}
    return A.analyze_standard(T, res, prev or {}, NOW)


def sb(lag=0.5, state="streaming", total=1000, **kw):
    return {"pid": 7, "application_name": "r1", "client_addr": "10.0.0.2", "state": state, "sync_state": "async", "replay_lag_s": lag, "write_lag_s": 0.1, "flush_lag_s": 0.2, "total_lag_bytes": total,
            "sent_lag_bytes": 0, "write_lag_bytes": 0, "flush_lag_bytes": 0, "replay_lag_bytes": total, **kw}


def sl(name="s1", active=True, retained=1e6, status="reserved", typ="physical"):
    return {"slot_name": name, "slot_type": typ, "active": active, "retained_bytes": retained, "wal_status": status, "xmin_age": None, "catalog_xmin_age": None, "database": None, "plugin": None, "restart_lsn": "0/1", "safe_wal_size": None}


def test_deadlock_counter_increase_is_an_instant_event_and_first_sample_is_silent():
    assert not std(dl=5).candidates
    a = std(dl=5, prev={"db_deadlocks": 3})
    d = next(c for c in a.candidates if c["category"] == "deadlock")
    assert d["instant"] and d["details"]["increase"] == 2 and "Only the counter increase" in d["limitations"]


@pytest.mark.parametrize("lag,expected", [(5, None), (45, "warning"), (400, "critical")])
def test_replica_lag_thresholds_and_stage_decomposition(lag, expected):
    a = std([sb(lag=lag, flush_lag_s=lag * 0.9)])
    got = [c for c in a.candidates if c["category"] == "replication_lag"]
    assert (got[0]["severity"] if got else None) == expected
    if got:
        assert got[0]["details"]["delay_appears_mostly_in"] and "not a diagnosed cause" in got[0]["limitations"]


def test_standby_not_streaming_is_flagged_with_caveat():
    c = next(x for x in std([sb(state="catchup")]).candidates if x["category"] == "replication_lag")
    assert "persists" in c["limitations"]


def test_slot_wal_status_and_retention_rules():
    assert ("replication_slot", "critical") in cats(std(slots=[sl(status="lost")]).candidates)
    assert ("replication_slot", "warning") in cats(std(slots=[sl(status="unreserved")]).candidates)
    assert ("replication_slot", "warning") in cats(std(slots=[sl(active=True, retained=2 * 1024 ** 3)]).candidates)
    assert ("replication_slot", "critical") in cats(std(slots=[sl(active=False, retained=6 * 1024 ** 3)]).candidates)
    assert not std(slots=[sl(retained=5e6)]).candidates


def test_checkpoint_pressure_is_a_hypothesis_not_a_recommendation():
    a = std(prev={"bg_checkpoints_req": 10, "bg_checkpoints_timed": 100}, bg={"checkpoints_req": 20, "checkpoints_timed": 101})
    c = next(x for x in a.candidates if x["category"] == "checkpoint_pressure")
    assert c["confidence"] == "hypothesis" and "before changing max_wal_size" in c["limitations"]


def test_archiver_failures_detected_by_counter_delta():
    assert ("archiver_failure", "warning") in cats(std(arch={"failed_count": 4}, prev={"archive_failed_count": 1}).candidates)
    assert not std(arch={"failed_count": 4}, prev={"archive_failed_count": 4}).candidates


def test_standby_replay_delay_marked_as_hypothesis():
    a = std(role="standby", rec={"replay_paused": False, "replay_delay_s": 120, "receive_replay_gap_bytes": 0})
    assert any(c["confidence"] == "hypothesis" and c["category"] == "replication_lag" for c in a.candidates)


# ------------------------------------------------------------------ vacuum / XID / settings
def vac(tables=(), xid=100, prog=(), holders=None, prepared=()):
    return {"databases_xid": [{"datname": "d", "xid_age": xid, "mxid_age": 1}], "tables": list(tables), "table_sizes": [{"relation": "public.t", "size_bytes": 10, "n_live": 1, "n_dead": 1}], "vacuum_progress": list(prog),
            "maintenance": {"create_index": [], "cluster": []}, "prepared_xacts": list(prepared), "xmin_holders": holders or {"backends": [], "slots": [], "prepared": []}, "settings": {"autovacuum_freeze_max_age": "200000000"}}


def tbl(live=100_000, dead=50_000, last="2026-01-01T00:00:00Z", **kw):
    return {"schemaname": "public", "relname": "t", "n_live_tup": live, "n_dead_tup": dead, "n_mod_since_analyze": 1, "n_ins_since_vacuum": 0, "last_vacuum": None, "last_autovacuum": last, "last_analyze": None,
            "last_autoanalyze": None, "vacuum_count": 0, "autovacuum_count": 3, "total_bytes": 10 ** 8, "table_bytes": 10 ** 8, "reloptions": kw.get("reloptions"), "relfrozenxid_age": 5,
            "dead_ratio_pct": round(100 * dead / (live + dead), 1)}


@pytest.mark.parametrize("age,expected", [(300_000_000, "info"), (1_100_000_000, "warning"), (1_600_000_000, "critical"), (50_000_000, None)])
def test_xid_age_thresholds(age, expected):
    got = [c["severity"] for c in A.analyze_vacuum(T, vac(xid=age), {}).candidates if c["category"] == "xid_age"]
    assert (got[0] if got else None) == expected


def test_vacuum_pressure_requires_absolute_and_relative_thresholds():
    assert not A.analyze_vacuum(T, vac([tbl(live=50_000_000, dead=20_000)]), {}).candidates       # large table, tiny fraction: not flagged
    assert not A.analyze_vacuum(T, vac([tbl(live=100, dead=60)]), {}).candidates                  # high fraction, tiny absolute count
    c = [x for x in A.analyze_vacuum(T, vac([tbl()]), {}).candidates if x["category"] == "vacuum_pressure"]
    assert len(c) == 1 and c[0]["severity"] == "warning" and c[0]["details"]["context"]


def test_vacuum_pressure_skipped_while_vacuum_is_running_on_the_table():
    d = vac([tbl()], prog=[{"pid": 9, "relation": "public.t", "running_s": 5}])
    assert not [x for x in A.analyze_vacuum(T, d, {}).candidates if x["category"] == "vacuum_pressure"]


def test_vacuum_context_mentions_old_xmin_holder_and_table_options():
    d = vac([tbl(reloptions="{autovacuum_vacuum_scale_factor=0.01}")], holders={"backends": [{"pid": 4, "xmin_age": 500_000_000, "state": "idle in transaction", "application_name": "job", "usename": "u"}], "slots": [], "prepared": []})
    a = A.analyze_vacuum(T, d, {})
    ctx = next(x for x in a.candidates if x["category"] == "vacuum_pressure")["details"]["context"]
    assert any("xmin holder" in c for c in ctx) and any("storage options" in c for c in ctx)
    assert ("old_xmin_holder", "warning") in cats(a.candidates)


def test_orphaned_prepared_transaction():
    old = iso(utcnow() - timedelta(hours=3))
    a = A.analyze_vacuum(T, vac(prepared=[{"gid": "tx1", "prepared": old, "owner": "u", "database": "d", "xid_age": 10}]), {})
    assert ("orphaned_prepared_xact", "warning") in cats(a.candidates)


def settings_data(**kv):
    base = {"autovacuum": "on", "fsync": "on", "full_page_writes": "on", "track_counts": "on", "track_activities": "on", "log_lock_waits": "on", "idle_in_transaction_session_timeout": "900000", "max_connections": "100"}
    base.update(kv)
    return {"settings": {k: {"setting": v, "unit": None, "source": "configuration file", "pending_restart": False, "context": "sighup"} for k, v in base.items()}, "hash": "h" + str(sorted(base.items()))[:20]}


def test_settings_change_detection_and_severity():
    prev = {"max_connections": "100", "autovacuum": "on", "fsync": "on", "full_page_writes": "on", "track_counts": "on", "track_activities": "on", "log_lock_waits": "on", "idle_in_transaction_session_timeout": "900000"}
    d = settings_data(max_connections="200")
    a = A.analyze_settings(T, d, prev, {"pg_stat_statements": {"extname": "pg_stat_statements"}})
    ch = next(c for c in a.candidates if c["category"] == "config_change")
    assert ch["severity"] == "warning" and ch["instant"] and ch["details"]["changed"] == [{"name": "max_connections", "old": "100", "new": "200", "unit": None, "source": "configuration file"}]
    assert not [c for c in A.analyze_settings(T, d, None, {"pg_stat_statements": {"extname": "pg_stat_statements"}}).candidates if c["category"] == "config_change"]


def test_config_observations_are_context_not_recommendations():
    a = A.analyze_settings(T, settings_data(autovacuum="off", fsync="off", log_lock_waits="off", idle_in_transaction_session_timeout="0"), None, {"pg_stat_statements": None})
    obs = {c["details"]["observation"]: c for c in a.candidates if c["category"] == "config_observation"}
    assert {"autovacuum_off", "fsync_off", "log_lock_waits_off", "idle_txn_timeout_unset", "no_pg_stat_statements"} <= set(obs)
    assert obs["autovacuum_off"]["severity"] == "warning" and obs["log_lock_waits_off"]["severity"] == "info" and all("not a recommendation" in c["limitations"] for c in obs.values())
    assert not [c for c in A.analyze_settings(T, settings_data(), None, {"pg_stat_statements": {"extname": "pg_stat_statements"}}).candidates if c["category"] == "config_observation"]


# ------------------------------------------------------------------ failures
def test_failure_streak_escalates_and_capability_gaps_stay_informational():
    res = {"activity": CResult("activity", "error", {}, "DB_UNAVAILABLE", "The database could not be reached."), "statements": CResult("statements", "unavailable", {}, "NOT_INSTALLED", "not installed"),
           "dbstats": CResult("dbstats", "partial", {}, None, "x", None, 1, {"wal_dir": "PERMISSION_DENIED"})}
    c1 = A.analyze_failures(res, {"activity": 1})
    assert ("collector_failure", "warning") in cats(c1) and ("capability_gap", "info") in cats(c1) and len([c for c in c1 if c["category"] == "capability_gap"]) == 2
    assert ("collector_failure", "critical") in cats(A.analyze_failures(res, {"activity": 3}))


# ------------------------------------------------------------------ query statistics (store-backed)
def qrow(qid, calls, total, fp="fpq", reset="r0"):
    return {"queryid": qid, "dbid": "1", "userid": "1", "fingerprint": fp, "text": "SELECT ?", "calls": calls, "total_exec_ms": total, "rows": calls, "stats_reset": reset}


def test_regression_detected_between_windows_and_counter_resets_ignored(tmp_path):
    s = Store(tmp_path / "q.sqlite3")
    t = NOW
    cum_c = cum_t = 0.0
    for k in range(31):                              # 30 minutes: first 15 min at 1 ms/call, next 15 at 6 ms/call
        cum_c += 100
        cum_t += 100 * (1.0 if k <= 15 else 6.0)
        s.qstats_add("i", iso(t - timedelta(minutes=30 - k)), [qrow("1", cum_c, cum_t)])
    out = query_regressions(s, "i", t, False, window_min=15)
    reg = next(x for x in out if x["category"] == "query_regression")
    assert reg["confidence"] == "hypothesis" and reg["details"]["ratio"] >= 4 and "not established" in reg["limitations"]
    s.qstats_add("i", iso(t + timedelta(seconds=30)), [qrow("1", 5, 9, reset="r1")])      # a reset produces negative deltas that must be skipped, not counted
    top = query_top(s, "i", 3600, t + timedelta(minutes=1), False)
    assert top["items"][0]["resets"] >= 1 and top["items"][0]["total_ms"] > 0 and top["items"][0]["mean_ms"] > 0


def test_top_queries_share_and_limitations(tmp_path):
    s = Store(tmp_path / "q2.sqlite3")
    for k in range(3):
        s.qstats_add("i", iso(NOW - timedelta(minutes=10 - 5 * k)), [qrow("a", 100 * k, 1000 * k, "fa"), qrow("b", 10 * k, 9000 * k, "fb")])
    top = query_top(s, "i", 1800, NOW, False)
    assert [i["queryid"] for i in top["items"]] == ["b", "a"] and top["items"][0]["share_pct"] == 90.0 and any("not the duration of one execution" in l for l in top["limitations"])
