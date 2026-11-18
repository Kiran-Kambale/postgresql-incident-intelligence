"""Integration tests for replication against a real primary + physical standby. Skipped unless both are reachable.

Build the disposable lab with `bash scripts/lab_standby.sh` (primary on 5432, standby on 5433). Never point these at production.
"""
import os
import time

import psycopg
import pytest

from app.config import Settings
from app.pg import collectors as C
from app.pg.pool import PgInstance
from app.recorder import Recorder
from app.store import Store
from app.timeutil import utcnow

pytestmark = pytest.mark.live
BASE = {"DB_HOST": "127.0.0.1", "DB_NAME": os.getenv("TEST_PG_DB", "incident_lab"), "DB_USER": os.getenv("TEST_PG_USER", "incident_reader"), "DB_PASSWORD": os.getenv("TEST_PG_PASSWORD", "lab_pw"), "DEMO_MODE": "false", "AUTO_MONITOR": "false"}
PRIMARY = {**BASE, "DB_PORT": "5432"}
STANDBY = {**BASE, "DB_PORT": "5433"}
ADMIN_P = "host=127.0.0.1 port=5432 dbname=incident_lab user=postgres password=lab_admin_pw"
ADMIN_S = "host=127.0.0.1 port=5433 dbname=incident_lab user=postgres password=lab_admin_pw"


def up(env):
    try:
        with psycopg.connect(host=env["DB_HOST"], port=env["DB_PORT"], dbname=env["DB_NAME"], user=env["DB_USER"], password=env["DB_PASSWORD"], connect_timeout=3):
            return True
    except Exception:
        return False


@pytest.fixture(scope="module")
def lab():
    if not (up(PRIMARY) and up(STANDBY)):
        pytest.skip("no primary+standby lab (run scripts/lab_standby.sh)")
    with psycopg.connect(ADMIN_P, autocommit=True) as a:
        for _ in range(30):
            if a.execute("SELECT count(*) FROM pg_stat_replication WHERE state='streaming'").fetchone()[0] >= 1:
                break
            time.sleep(1)
        else:
            pytest.skip("standby is not streaming")
    return True


def collect(env, tier, tmp):
    s = Settings.from_env(env, data_dir=tmp)
    i = PgInstance(s.instances[0], s)
    info = i.probe()
    res = C.run_tier(i, s, tier, info, utcnow())
    i.close()
    return info, res


def test_primary_sees_the_streaming_standby_and_its_slot(lab, tmp_path):
    info, res = collect(PRIMARY, "standard", tmp_path)
    rep = res["replication"]
    assert rep.status in ("ok", "partial") and rep.data["role"] == "primary"
    sb = rep.data["standbys"]
    assert sb and sb[0]["state"] == "streaming" and sb[0]["sync_state"] in ("async", "sync", "potential", "quorum")
    assert all(k in sb[0] for k in ("write_lag_s", "flush_lag_s", "replay_lag_s", "total_lag_bytes", "replay_lag_bytes"))
    slot = next(s for s in rep.data["slots"] if s["slot_name"] == "standby_slot")
    assert slot["slot_type"] == "physical" and slot["active"] is True and slot["wal_status"] in ("reserved", "extended") and slot["retained_bytes"] is not None


def test_standby_side_collectors_report_receiver_and_recovery(lab, tmp_path):
    info, res = collect(STANDBY, "standard", tmp_path)
    assert info["in_recovery"] is True
    rep = res["replication"]
    assert rep.status in ("ok", "partial") and rep.data["role"] == "standby" and rep.data["receiver"]["status"] == "streaming"
    assert rep.data["recovery"]["replay_paused"] is False and rep.data["recovery"]["receive_replay_gap_bytes"] is not None
    for tier in ("fast", "slow"):
        for name, r in collect(STANDBY, tier, tmp_path)[1].items():
            assert r.status in ("ok", "partial", "unavailable"), (tier, name, r.status, r.message)


def test_paused_replay_is_observed_end_to_end_and_lag_grows(lab, tmp_path):
    s = Settings.from_env(STANDBY, data_dir=tmp_path)
    rec = Recorder(s, Store(tmp_path / "standby.sqlite3"))
    with psycopg.connect(ADMIN_S, autocommit=True) as sb:
        sb.execute("SELECT pg_wal_replay_pause()")
    try:
        with psycopg.connect(ADMIN_P, autocommit=True) as p:
            p.execute("CREATE TABLE IF NOT EXISTS dir_repl_test(id serial primary key, payload text)")
            p.execute("INSERT INTO dir_repl_test(payload) SELECT repeat('x', 200) FROM generate_series(1, 20000)")
            p.execute("SELECT pg_switch_wal()")
        time.sleep(2)
        out = rec.run_tier_sync("default", "standard", "test", wait=True)
        assert out["status"] in ("ok", "partial")
        r = rec.latest("default", "replication")["data"]
        assert r["role"] == "standby" and r["recovery"]["replay_paused"] is True and r["recovery"]["receive_replay_gap_bytes"] > 0
        ev = [e for e in rec.store.evidence_list(instance_id="default", categories=["replication_lag"])[0] if "paused" in e["title"]]
        assert ev and ev[0]["severity"] == "warning" and ev[0]["status"] == "active"
        _info, prim = collect(PRIMARY, "standard", tmp_path)
        assert prim["replication"].data["standbys"][0]["replay_lag_bytes"] > 0
    finally:
        with psycopg.connect(ADMIN_S, autocommit=True) as sb:
            sb.execute("SELECT pg_wal_replay_resume()")
        with psycopg.connect(ADMIN_P, autocommit=True) as p:
            p.execute("DROP TABLE IF EXISTS dir_repl_test")
        rec.rt["default"].pg.close()
    time.sleep(2)
    rec2 = Recorder(Settings.from_env(STANDBY, data_dir=tmp_path), Store(tmp_path / "standby2.sqlite3"))
    rec2.run_tier_sync("default", "standard", "test", wait=True)
    assert rec2.latest("default", "replication")["data"]["recovery"]["replay_paused"] is False
    rec2.rt["default"].pg.close()
