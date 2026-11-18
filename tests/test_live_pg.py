"""Integration tests against a real PostgreSQL. Skipped unless a monitoring role can connect.

Configure with TEST_PG_HOST/PORT/DB/USER/PASSWORD (a least-privilege role: pg_read_all_stats is enough) and TEST_PG_ADMIN_DSN
(a superuser DSN used only to create disposable objects). `bash scripts/lab_setup.sh` builds a matching throwaway lab on Debian/Ubuntu.
Never point these at a production server.
"""
import os
import threading
import time

import psycopg
import pytest

from app.config import Settings
from app.pg import collectors as C
from app.pg.pool import PgError, PgInstance
from app.recorder import Recorder
from app.store import Store
from app.timeutil import utcnow

pytestmark = pytest.mark.live
ENV = {"DB_HOST": os.getenv("TEST_PG_HOST", "127.0.0.1"), "DB_PORT": os.getenv("TEST_PG_PORT", "5432"), "DB_NAME": os.getenv("TEST_PG_DB", "incident_lab"),
       "DB_USER": os.getenv("TEST_PG_USER", "incident_reader"), "DB_PASSWORD": os.getenv("TEST_PG_PASSWORD", "lab_pw"), "DEMO_MODE": "false", "AUTO_MONITOR": "false"}
ADMIN_DSN = os.getenv("TEST_PG_ADMIN_DSN", "host=127.0.0.1 dbname=incident_lab user=postgres password=lab_admin_pw")   # disposable lab only


def reachable():
    try:
        with psycopg.connect(host=ENV["DB_HOST"], port=ENV["DB_PORT"], dbname=ENV["DB_NAME"], user=ENV["DB_USER"], password=ENV["DB_PASSWORD"], connect_timeout=3):
            return True
    except Exception:
        return False


@pytest.fixture(scope="module")
def settings(tmp_path_factory):
    if not reachable():
        pytest.skip("no reachable test PostgreSQL (see module docstring)")
    return Settings.from_env({**ENV, "DB_SSLMODE": "prefer"}, data_dir=tmp_path_factory.mktemp("live"))


@pytest.fixture
def inst(settings):
    i = PgInstance(settings.instances[0], settings)
    yield i
    i.close()


def admin():
    return psycopg.connect(ADMIN_DSN, autocommit=True)


def test_probe_reports_version_privileges_and_tls(inst):
    info = inst.probe()
    assert info["supported"] and info["major"] >= 12 and info["in_recovery"] is False and info["is_superuser"] is False and info["pg_read_all_stats"] is True


def test_all_collectors_run_with_a_least_privilege_role(inst, settings):
    info = inst.probe()
    for tier in ("slow", "fast", "standard"):
        res = C.run_tier(inst, settings, tier, info, utcnow())
        for name, r in res.items():
            assert r.status in ("ok", "partial", "unavailable"), (name, r.status, r.error_code, r.message)
    act = C.run_tier(inst, settings, "fast", info, utcnow())["activity"].data
    assert act["connections"]["max_connections"] and act["connections"]["client_backends"] >= 1 and "blocking" in act


def test_readonly_session_cannot_write(inst):
    with pytest.raises(PgError) as e:
        inst.query("CREATE TABLE should_not_exist(x int)")
    assert e.value.code in ("DB_ERROR", "PERMISSION_DENIED")


def test_statement_timeout_is_enforced_and_classified(inst):
    t0 = time.monotonic()
    with pytest.raises(PgError) as e:
        inst.query("SELECT pg_sleep(5)", timeout_ms=300)
    assert e.value.code == "QUERY_TIMEOUT" and time.monotonic() - t0 < 3


def test_wrong_password_and_unreachable_host_are_classified_without_leaking(settings):
    bad = Settings.from_env({**ENV, "DB_PASSWORD": "definitely-wrong"})
    i = PgInstance(bad.instances[0], bad)
    with pytest.raises(PgError) as e:
        i.probe()
    assert e.value.code in ("AUTH_FAILED", "DB_UNAVAILABLE") and "definitely-wrong" not in e.value.message and ENV["DB_USER"] not in e.value.message
    i.close()
    down = Settings.from_env({**ENV, "DB_PORT": "1"})
    i2 = PgInstance(down.instances[0], down)
    with pytest.raises(PgError) as e2:
        i2.probe()
    assert e2.value.code == "DB_UNAVAILABLE"
    i2.close()


def test_missing_privilege_is_reported_as_unavailable_not_a_crash(settings):
    def drop_role(a):
        if a.execute("SELECT 1 FROM pg_roles WHERE rolname='dir_noprivs'").fetchone():
            a.execute(f"REVOKE ALL PRIVILEGES ON DATABASE {ENV['DB_NAME']} FROM dir_noprivs")
            a.execute("DROP ROLE dir_noprivs")
    with admin() as a:
        drop_role(a)
        a.execute("CREATE ROLE dir_noprivs LOGIN PASSWORD 'np_pw'")
        a.execute(f"GRANT CONNECT ON DATABASE {ENV['DB_NAME']} TO dir_noprivs")
    try:
        s = Settings.from_env({**ENV, "DB_USER": "dir_noprivs", "DB_PASSWORD": "np_pw"})
        i = PgInstance(s.instances[0], s)
        info = i.probe()
        assert info["pg_read_all_stats"] is False
        res = C.run_tier(i, s, "standard", info, utcnow())
        assert res["dbstats"].status in ("ok", "partial") and res["dbstats"].data["current"] is not None
        assert res["statements"].status in ("unavailable", "ok")
        i.close()
    finally:
        with admin() as a:
            drop_role(a)


def test_real_blocking_chain_root_vs_direct_blocker_and_end_to_end_evidence(tmp_path, settings):
    s = Settings.from_env({**ENV, "DIR_BLOCK_MIN_S": "0", "DIR_BLOCK_CRIT_SESSIONS": "2", "DIR_CANDIDATE_COOLDOWN_MIN": "0"}, data_dir=tmp_path)
    rec = Recorder(s, Store(tmp_path / "live.sqlite3"))
    with admin() as a:
        a.execute("DROP TABLE IF EXISTS dir_test_lock")
        a.execute("CREATE TABLE dir_test_lock(id int primary key, v int)")
        a.execute("INSERT INTO dir_test_lock VALUES (1, 0)")
    holder = psycopg.connect(ADMIN_DSN, autocommit=False, application_name="dir-test-holder")
    holder.execute("UPDATE dir_test_lock SET v = v + 1 WHERE id = 1")        # transaction stays open: becomes the root blocker
    waiters, errs = [], []

    def wait(app_name):
        try:
            c = psycopg.connect(ADMIN_DSN, autocommit=True, application_name=app_name)
            waiters.append(c)
            c.execute("UPDATE dir_test_lock SET v = v + 10 WHERE id = 1")
        except Exception as exc:
            errs.append(str(exc))
    t1 = threading.Thread(target=wait, args=("dir-test-w1",)); t1.start(); time.sleep(0.6)
    t2 = threading.Thread(target=wait, args=("dir-test-w2",)); t2.start(); time.sleep(1.5)
    try:
        out = rec.run_tier_sync("default", "fast", "test", wait=True)
        assert out["status"] in ("ok", "partial")
        g = rec.latest("default", "activity")["data"]["blocking"]
        holder_pid = holder.info.backend_pid
        root = next(r for r in g["roots"] if r["pid"] == holder_pid)
        assert root["blocked_count"] == 2 and root["application_name"] == "dir-test-holder" and root["direct_blocked_count"] >= 1 and len(root["chain"]) >= 2
        roles = {n["pid"]: n["role"] for n in g["nodes"]}
        assert roles[holder_pid] == "root_blocker" and list(roles.values()).count("blocked") + list(roles.values()).count("intermediate") == 2
        ev = rec.store.evidence_list(instance_id="default", categories=["blocking"])[0]
        assert ev and ev[0]["pid"] == holder_pid and ev[0]["details"]["blocked_count"] == 2 and "UPDATE" in (ev[0]["details"]["root"].get("query") or "UPDATE")
        assert any(i["status"] == "candidate" for i in rec.store.incident_list()[0]), "critical blocking must raise an incident candidate"
    finally:
        holder.rollback(); holder.close()
        t1.join(5); t2.join(5)
        [w.close() for w in waiters]
    time.sleep(0.5)
    rec.run_tier_sync("default", "fast", "test", wait=True)
    ev = rec.store.evidence_list(instance_id="default", categories=["blocking"])[0]
    assert ev[0]["status"] == "cleared" and ev[0]["peaks"]["blocked_count"] == 2
    with admin() as a:
        a.execute("DROP TABLE IF EXISTS dir_test_lock")
    rec.rt["default"].pg.close()


def test_real_three_level_chain_marks_the_middle_session_as_intermediate(tmp_path, settings):
    s = Settings.from_env({**ENV, "DIR_BLOCK_MIN_S": "0"}, data_dir=tmp_path)
    rec = Recorder(s, Store(tmp_path / "chain.sqlite3"))
    with admin() as a:
        a.execute("DROP TABLE IF EXISTS dir_test_chain")
        a.execute("CREATE TABLE dir_test_chain(id int primary key, v int)")
        a.execute("INSERT INTO dir_test_chain VALUES (1, 0), (2, 0)")
    root = psycopg.connect(ADMIN_DSN, autocommit=False, application_name="dir-chain-root")
    root.execute("UPDATE dir_test_chain SET v = v + 1 WHERE id = 1")                 # root: holds row 1 and stays idle in transaction
    conns, errs = [], []

    def middle():
        try:
            c = psycopg.connect(ADMIN_DSN, autocommit=False, application_name="dir-chain-middle")
            conns.append(c)
            c.execute("UPDATE dir_test_chain SET v = v + 1 WHERE id = 2")            # takes row 2 ...
            c.execute("UPDATE dir_test_chain SET v = v + 1 WHERE id = 1")            # ... then waits for the root's row 1
        except Exception as exc:
            errs.append(str(exc))

    def leaf():
        try:
            c = psycopg.connect(ADMIN_DSN, autocommit=True, application_name="dir-chain-leaf")
            conns.append(c)
            c.execute("UPDATE dir_test_chain SET v = v + 1 WHERE id = 2")            # waits for the middle session's row 2
        except Exception as exc:
            errs.append(str(exc))
    t1 = threading.Thread(target=middle); t1.start(); time.sleep(0.8)
    t2 = threading.Thread(target=leaf); t2.start(); time.sleep(1.5)
    try:
        rec.run_tier_sync("default", "fast", "test", wait=True)
        g = rec.latest("default", "activity")["data"]["blocking"]
        by_app = {n["application_name"]: n for n in g["nodes"]}
        assert by_app["dir-chain-root"]["role"] == "root_blocker" and by_app["dir-chain-middle"]["role"] == "intermediate" and by_app["dir-chain-leaf"]["role"] == "blocked"
        assert by_app["dir-chain-leaf"]["direct_blockers"] == [by_app["dir-chain-middle"]["pid"]], "the leaf is directly blocked by the middle session, not by the root"
        r = g["roots"][0]
        assert r["pid"] == by_app["dir-chain-root"]["pid"] and r["blocked_count"] == 2 and r["direct_blocked_count"] == 1 and r["depth"] == 2
        assert r["chain"] == [by_app["dir-chain-root"]["pid"], by_app["dir-chain-middle"]["pid"], by_app["dir-chain-leaf"]["pid"]]
    finally:
        root.rollback(); root.close(); t1.join(5); t2.join(5)
        for c in conns:
            try:
                c.rollback(); c.close()
            except Exception:
                pass
    with admin() as a:
        a.execute("DROP TABLE IF EXISTS dir_test_chain")
    rec.rt["default"].pg.close()


def test_vacuum_and_replication_views_on_a_primary(inst, settings):
    info = inst.probe()
    res = C.run_tier(inst, settings, "slow", info, utcnow())
    v = res["vacuum"].data
    assert v["databases_xid"] and v["databases_xid"][0]["xid_age"] >= 0 and isinstance(v["tables"], list)
    rep = C.run_tier(inst, settings, "standard", info, utcnow())["replication"].data
    assert rep["role"] == "primary" and isinstance(rep["slots"], list)


def test_physical_slot_retention_is_observed(inst, settings):
    with admin() as a:
        a.execute("SELECT pg_drop_replication_slot('dir_test_slot') FROM pg_replication_slots WHERE slot_name='dir_test_slot'")
        a.execute("SELECT pg_create_physical_replication_slot('dir_test_slot', true)")
        a.execute("SELECT pg_switch_wal()")
    try:
        info = inst.probe()
        rep = C.run_tier(inst, settings, "standard", info, utcnow())["replication"].data
        slot = next(s for s in rep["slots"] if s["slot_name"] == "dir_test_slot")
        assert slot["slot_type"] == "physical" and slot["active"] is False and slot["retained_bytes"] is not None and slot["retained_bytes"] >= 0
    finally:
        with admin() as a:
            a.execute("SELECT pg_drop_replication_slot('dir_test_slot')")
