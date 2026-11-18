"""Server-log parsing/tailing and the flight recorder's lifecycle, failure handling and overlap protection."""
import asyncio
import json
import threading
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.demo import DemoSource
from app.pg import collectors as C
from app.pg.collectors import CResult
from app.pg.logs import classify_all, collect_errorlog, parse_log_text, read_window, recent_errors
from app.pg.pool import PgError
from app.recorder import Recorder
from app.store import Store
from app.timeutil import iso

DEBIAN = """2026-10-27 10:00:01.000 UTC [101] app@shop ERROR:  deadlock detected
2026-10-27 10:00:01.000 UTC [101] app@shop DETAIL:  Process 101 waits for ShareLock on transaction 55; blocked by process 102.
\tProcess 102 waits for ShareLock on transaction 54; blocked by process 101.
\tProcess 101: UPDATE t SET x = 'secret-one' WHERE id = 5
\tProcess 102: UPDATE t SET x = 'secret-two' WHERE id = 4
2026-10-27 10:00:01.000 UTC [101] app@shop STATEMENT:  UPDATE t SET x = 'secret-one' WHERE id = 5
2026-10-27 10:00:05.000 UTC [200] user=bob db=shop app=web client=10.0.0.5 LOG:  process 200 still waiting for ShareLock on transaction 7 after 1000.123 ms
2026-10-27 10:00:05.000 UTC [200] user=bob db=shop app=web client=10.0.0.5 DETAIL:  Process holding the lock: 150. Wait queue: 200.
2026-10-27 10:00:09.000 UTC [300] ERROR:  duplicate key value violates unique constraint "u". Key (email)=(a@b.c) already exists
2026-10-27 10:00:10.000 UTC [1] FATAL:  remaining connection slots are reserved for non-replication superuser connections
2026-10-27 10:00:11.000 UTC [2] LOG:  server process (PID 77) was terminated by signal 9: Killed
2026-10-27 10:00:12.000 UTC [3] FATAL:  password authentication failed for user "svc"
"""


def test_parser_handles_both_prefix_styles_and_continuations():
    es = parse_log_text(DEBIAN)
    assert [e["level"] for e in es][:3] == ["ERROR", "LOG", "ERROR"] and es[0]["user"] == "app" and es[0]["db"] == "shop" and es[1]["user"] == "bob" and es[1]["client"] == "10.0.0.5"
    assert "Process 102: UPDATE" in es[0]["extra"]["DETAIL"] and es[0]["extra"]["STATEMENT"].startswith("UPDATE t SET")


def test_timezone_offset_applied():
    e = parse_log_text("2026-10-27 15:30:00.000 [5] LOG:  hello", offset_min=330)[0]
    assert iso(e["ts"]) == "2026-10-27T10:00:00.000Z"
    assert iso(parse_log_text("2026-10-27 15:30:00 +0530 [5] LOG:  x")[0]["ts"]) == "2026-10-27T10:00:00.000Z"


def test_classification_and_redaction():
    c = {x["category"]: x for x in classify_all(parse_log_text(DEBIAN), "redact", [])}
    assert set(c) >= {"deadlock", "lock_wait_log", "error_log", "connection_refused", "server_restart", "auth_failure"}
    dl = c["deadlock"]
    assert dl["severity"] == "critical" and dl["instant"] and [p["pid"] for p in dl["details"]["processes"]] == [101, 102]
    assert "secret-one" not in json.dumps(dl, default=str) and "secret-two" not in json.dumps(dl, default=str)
    assert c["lock_wait_log"]["details"]["holder_pid"] == 150 and c["lock_wait_log"]["details"]["waited_ms"] == 1000.123
    assert "a@b.c" not in json.dumps(c["error_log"], default=str) and c["server_restart"]["details"]["possible_crash"] is True


def test_read_window_consumes_only_complete_lines_and_detects_rotation(tmp_path):
    p = tmp_path / "pg.log"
    p.write_text("line one\nline two\npartial")
    text, info = read_window(str(p), 0, 1_000_000)
    assert text == "line one\nline two\n" and info["new_offset"] == len("line one\nline two\n")
    with open(p, "a") as f:
        f.write(" now complete\n")
    text2, info2 = read_window(str(p), info["new_offset"], 1_000_000)
    assert text2 == "partial now complete\n"
    p.write_text("fresh\n")                                           # truncated/rotated: offset beyond size
    text3, info3 = read_window(str(p), info2["new_offset"], 1_000_000)
    assert text3 == "fresh\n" and info3["rotated"] is True


def test_read_window_bounds_work_on_huge_logs(tmp_path):
    p = tmp_path / "big.log"
    p.write_text("x" * 50 + "\n" + ("y" * 99 + "\n") * 1000)
    text, info = read_window(str(p), 0, 4096)
    assert info["skipped_bytes"] > 90_000 and len(text) <= 4096


def test_unreadable_or_missing_log_is_a_classified_unavailable_result(tmp_path):
    with pytest.raises(PgError) as e:
        read_window(str(tmp_path / "missing.log"), 0, 1000)
    assert e.value.code == "LOG_NOT_READABLE" and e.value.status == "unavailable"


def test_collect_errorlog_persists_offset_and_only_emits_new_events(tmp_path):
    log = tmp_path / "pg.log"
    log.write_text(DEBIAN)
    store = Store(tmp_path / "s.sqlite3")
    s = Settings.from_env({"DEMO_MODE": "false", "DB_HOST": "h", "DB_PORT": "5", "DB_NAME": "d", "DB_USER": "u", "DB_PASSWORD": "p", "PG_LOG_FILE": str(log)})
    ctx = SimpleNamespace(inst=SimpleNamespace(cfg=s.instances[0]), settings=s, store=store)
    d1 = collect_errorlog(ctx)
    store.kv_set(d1["state_update"]["key"], json.dumps(d1["state_update"]["value"]))
    assert len(d1["candidates"]) >= 5
    d2 = collect_errorlog(ctx)
    assert d2["bytes_read"] == 0 and d2["candidates"] == []
    with open(log, "a") as f:
        f.write("2026-10-27 10:05:00.000 UTC [9] ERROR:  relation \"x\" does not exist\n")
    assert len(collect_errorlog(ctx)["candidates"]) == 1


def test_recent_errors_redacts_statements_in_detail_lines(tmp_path):
    log = tmp_path / "pg.log"; log.write_text(DEBIAN)
    out = recent_errors(str(log), 1_000_000, 10, 0, "redact", [])
    blob = json.dumps(out)
    assert out and "secret-one" not in blob and "secret-two" not in blob and "a@b.c" not in blob


# ------------------------------------------------------------------ recorder
LIVE_ENV = {"DEMO_MODE": "false", "AUTO_MONITOR": "false", "DB_HOST": "127.0.0.1", "DB_PORT": "1", "DB_NAME": "d", "DB_USER": "u", "DB_PASSWORD": "p"}


def live_recorder(tmp_path):
    s = Settings.from_env(LIVE_ENV, data_dir=tmp_path)
    return Recorder(s, Store(tmp_path / "live.sqlite3")), s


def ok_activity(n_blocked=0):
    from tests.test_analysis import activity, blocking_rows, r
    rows = blocking_rows(70, n_blocked) if n_blocked else [r(1, "idle")]
    return CResult("activity", "ok", activity(rows), duration_ms=3)


def test_successful_run_persists_everything(tmp_path, monkeypatch):
    rec, s = live_recorder(tmp_path)
    monkeypatch.setattr(C, "run_tier", lambda *a, **k: {"activity": ok_activity(2)})
    out = rec.run_tier_sync("default", "fast", "test")
    assert out["status"] == "ok" and out["new_evidence"] >= 1
    st = rec.store
    assert st.stats()["rows"]["collection_runs"] == 1 and st.metrics_latest("default", ["blocked_sessions"])["blocked_sessions"]["value"] == 2
    assert rec.latest("default", "activity")["status"] == "ok" and st.evidence_counts("default")["active_by_category"]["blocking"] == 1
    assert st.incident_list()[1] == 1 and st.incident_list()[0][0]["status"] == "candidate"          # critical blocking -> candidate incident


def test_collector_failure_is_recorded_marks_evidence_stale_and_recovers(tmp_path, monkeypatch):
    rec, s = live_recorder(tmp_path)
    fail = lambda: {"activity": CResult("activity", "error", {}, "DB_UNAVAILABLE", "The database could not be reached.")}
    seq = iter([{"activity": ok_activity(2)}, fail(), fail(), fail(), {"activity": ok_activity(0)}])
    monkeypatch.setattr(C, "run_tier", lambda *a, **k: next(seq))
    rec.run_tier_sync("default", "fast")
    rec.run_tier_sync("default", "fast")
    ev = {e["category"]: e for e in rec.store.evidence_list(instance_id="default")[0]}
    assert ev["blocking"]["collection_status"] == "stale" and ev["blocking"]["status"] == "active", "evidence must not be cleared by a failed collection"
    assert ev["collector_failure"]["severity"] == "warning"
    rec.run_tier_sync("default", "fast"); rec.run_tier_sync("default", "fast")
    assert {e["category"]: e for e in rec.store.evidence_list(instance_id="default")[0]}["collector_failure"]["severity"] == "critical"
    st = rec.runtime("default").tiers["fast"]
    assert st.consecutive_failures == 3 and rec.runtime("default").latest["activity"]["stale"] is True and rec.runtime("default").latest["activity"]["error_code"] == "DB_UNAVAILABLE"
    rec.run_tier_sync("default", "fast")           # database is back
    after = {e["category"]: e for e in rec.store.evidence_list(instance_id="default")[0]}
    assert after["collector_failure"]["status"] == "cleared" and after["blocking"]["status"] == "cleared" and rec.runtime("default").tiers["fast"].consecutive_failures == 0
    runs, total = rec.store.runs_list("default", "fast")
    assert total == 5 and [x["status"] for x in runs].count("error") == 3


def test_overlapping_runs_are_skipped_not_queued(tmp_path, monkeypatch):
    rec, _ = live_recorder(tmp_path)
    gate, started = threading.Event(), threading.Event()

    def slow(*a, **k):
        started.set(); gate.wait(5); return {"activity": ok_activity(0)}
    monkeypatch.setattr(C, "run_tier", slow)
    t = threading.Thread(target=rec.run_tier_sync, args=("default", "fast")); t.start(); started.wait(5)
    res = rec.run_tier_sync("default", "fast")                     # a second trigger while the first is running
    assert res["status"] == "skipped" and rec.runtime("default").tiers["fast"].skipped == 1
    gate.set(); t.join(5)
    assert rec.store.stats()["rows"]["collection_runs"] == 1


def test_global_concurrency_is_bounded(tmp_path, monkeypatch):
    s = Settings.from_env({**LIVE_ENV, "MAX_CONCURRENT_COLLECTIONS": "2", "DB_HOST": "h"}, data_dir=tmp_path)
    rec = Recorder(s, Store(tmp_path / "c.sqlite3"))
    active = {"n": 0, "max": 0}; lock = threading.Lock()

    def work(*a, **k):
        with lock:
            active["n"] += 1; active["max"] = max(active["max"], active["n"])
        import time; time.sleep(0.05)
        with lock:
            active["n"] -= 1
        return {"activity": ok_activity(0)}
    monkeypatch.setattr(C, "run_tier", work)
    for k in ("b", "c", "d"):
        rec.rt[k] = rec.rt["default"].__class__(rec.rt["default"].cfg, None)
    ts = [threading.Thread(target=rec.run_tier_sync, args=(k, "fast", "t")) for k in ("default", "b", "c", "d")]
    [t.start() for t in ts]; [t.join(5) for t in ts]
    assert active["max"] <= 2


def test_pipeline_crash_is_contained_and_recorded(tmp_path, monkeypatch):
    rec, _ = live_recorder(tmp_path)
    monkeypatch.setattr(C, "run_tier", lambda *a, **k: {"activity": ok_activity(0)})
    monkeypatch.setattr(rec, "_analyze", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("bug in analyzer")))
    out = rec.run_tier_sync("default", "fast")
    assert out["status"] == "error" and rec.store.runs_list("default", "fast")[0][0]["status"] == "error" and rec.runtime("default").tiers["fast"].running is False


def test_unreachable_database_end_to_end_without_mocks(tmp_path):
    rec, _ = live_recorder(tmp_path)               # port 1 refuses connections
    out = rec.run_tier_sync("default", "fast")
    assert out["status"] == "error"
    ev = rec.store.evidence_list(instance_id="default")[0]
    assert ev and ev[0]["category"] == "collector_failure" and "DB_UNAVAILABLE" in json.dumps(ev[0], default=str) and "127.0.0.1" not in json.dumps(ev[0], default=str)
    rec.rt["default"].pg.close()


def test_lifecycle_start_stop_is_clean_and_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr("app.recorder.START_DELAY", {"fast": 0.05, "standard": 0.1, "slow": 0.15})
    monkeypatch.setattr("app.recorder.START_JITTER", 0.0)

    async def go():
        s = Settings.from_env({"DEMO_MODE": "true", "MONITOR_INTERVAL_SECONDS": "5"}, data_dir=tmp_path)
        rec = Recorder(s, Store(tmp_path / "d.sqlite3"), DemoSource())
        await rec.start(); await rec.start()
        assert rec.running and len(rec.tasks) == 4          # one task per tier for the single instance, plus maintenance
        await asyncio.sleep(1.5)
        await rec.stop(); await rec.stop()
        assert not rec.running and not rec.tasks
        return rec
    rec = asyncio.run(go())
    assert rec.store.stats()["rows"]["collection_runs"] >= 3


def test_on_demand_collection_when_recorder_not_running(tmp_path):
    s = Settings.from_env({"DEMO_MODE": "true"}, data_dir=tmp_path)
    rec = Recorder(s, Store(tmp_path / "o.sqlite3"), DemoSource())
    e = rec.ensure_fresh("demo", "fast", "activity")
    assert e and e["status"] == "ok" and rec.store.stats()["rows"]["collection_runs"] == 1
    assert rec.ensure_fresh("demo", "fast", "activity")["ts"] == e["ts"] and rec.store.stats()["rows"]["collection_runs"] == 1   # fresh data is reused, not re-collected
