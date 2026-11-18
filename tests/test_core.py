"""Pure-logic tests: time handling, redaction, configuration validation, blocking graph, version-aware SQL, error classification."""
import datetime as dt

import psycopg
import pytest
from psycopg import errors as pgerr

from app.config import Settings, TUNABLES
from app.pg import versions as V
from app.pg.graph import build_blocking_graph
from app.pg.pool import PgError, classify
from app.security import redact_log_message, redact_secrets, redact_sql, safe_text, sql_fingerprint
from app.timeutil import iso, normalize_ts, parse_ts


# ------------------------------------------------------------------ timestamps
@pytest.mark.parametrize("raw,expected", [
    ("2026-10-14 10:03:00.123+00", "2026-10-14T10:03:00.123Z"), ("2026-10-14T10:03:00Z", "2026-10-14T10:03:00.000Z"),
    ("2026-10-14 15:33:00+05:30", "2026-10-14T10:03:00.000Z"), ("2026-10-14 10:03:00", "2026-10-14T10:03:00.000Z"),
    ("2026-10-14 10:03:00.123456-0200", "2026-10-14T12:03:00.123Z")])
def test_parse_and_normalize_timestamps_to_utc(raw, expected):
    assert normalize_ts(raw) == expected


def test_unparseable_timestamps_are_none():
    assert parse_ts("not a time") is None and parse_ts("") is None and normalize_ts(None) is None


def test_iso_is_utc_sortable_and_naive_assumed_utc():
    a = iso(dt.datetime(2026, 1, 1, 0, 0, 0)); b = iso(dt.datetime(2026, 1, 1, 5, 0, 0, tzinfo=dt.timezone(dt.timedelta(hours=5))))
    assert a == b == "2026-01-01T00:00:00.000Z"


# ------------------------------------------------------------------ redaction
def test_sql_literals_are_masked_in_redact_mode():
    out = redact_sql("UPDATE users SET email='a@b.com', age=42, note=$$secret$$ WHERE id = $1 AND balance > 12.5")
    assert "a@b.com" not in out and "secret" not in out and "42" not in out and "$1" in out and "12.5" not in out


def test_dollar_quoted_strings_with_tags_are_masked():
    assert "topsecret" not in redact_sql("SELECT $tag$topsecret$tag$, 1")


def test_password_clauses_masked_in_every_mode():
    for mode in ("redact", "full"):
        assert "s3cret" not in redact_sql("ALTER ROLE app PASSWORD 's3cret'", mode)
    assert "hunter2" not in redact_sql("CREATE USER x WITH ENCRYPTED PASSWORD 'hunter2'", "full")


def test_none_mode_hides_text_and_full_keeps_literals():
    assert "hidden" in redact_sql("SELECT 1", "none")
    assert "'abc'" in redact_sql("SELECT 'abc'", "full")


def test_secret_patterns_connection_urls_tokens_and_keys():
    t = redact_secrets("postgres://app:pw123@db/x token=abc123 Bearer abcdefghijkl AKIAABCDEFGHIJKLMNOP")
    for leak in ("pw123", "abc123", "abcdefghijkl", "AKIAABCDEFGHIJKLMNOP"):
        assert leak not in t


def test_log_messages_hide_constraint_values_and_emails():
    t = redact_log_message("duplicate key. Key (email)=(joe@x.io) already exists. contact bob@y.org")
    assert "joe@x.io" not in t and "bob@y.org" not in t and "Key (email)" in t


def test_fingerprint_ignores_literals_and_whitespace():
    assert sql_fingerprint("select * from t where id = 1") == sql_fingerprint("SELECT *   FROM t WHERE id = 99")
    assert sql_fingerprint("select 1") != sql_fingerprint("select 2 from t")


def test_safe_text_strips_dsn_passwords():
    assert "topsecret" not in safe_text("connection to server failed: host=x password=topsecret user=y")


def test_extra_redaction_patterns_apply():
    assert "ACME-9" not in redact_sql("select 'x' -- ACME-9", "redact", [r"ACME-\d"])


# ------------------------------------------------------------------ configuration
ENV = {"DB_HOST": "h", "DB_PORT": "5432", "DB_NAME": "d", "DB_USER": "u", "DB_PASSWORD": "pw-secret"}


def test_settings_never_expose_password():
    s = Settings.from_env(ENV)
    assert "pw-secret" not in repr(s) and "pw-secret" not in str(s.public()) and "pw-secret" not in str(s.instances[0].public())


def test_tls_not_verified_is_reported_never_silent():
    s = Settings.from_env(ENV)
    assert any("not verified" in w for w in s.config_warnings)
    s2 = Settings.from_env({**ENV, "DB_SSLMODE": "verify-full", "DB_SSLROOTCERT": "/x.pem"})
    assert s2.instances[0].tls_verified and not s2.config_warnings


@pytest.mark.parametrize("bad", [{"AUTH_MODE": "magic"}, {"AUTH_MODE": "token"}, {"API_TOKENS": "admin:short"}, {"QUERY_TEXT_MODE": "raw"}, {"DB_SSLMODE": "bogus"}, {"DB_INSTANCE_ID": "Bad Id!"}])
def test_invalid_configuration_is_rejected(bad):
    with pytest.raises(ValueError):
        Settings.from_env({**ENV, **bad})


def test_tunable_overrides_are_bounded():
    s = Settings.from_env({"DEMO_MODE": "true"})
    assert s.set_override("long_query_s", 45) == 45 and s.t("long_query_s") == 45
    with pytest.raises(ValueError):
        s.set_override("fast_interval_s", 1)
    with pytest.raises(ValueError):
        s.set_override("conn_warn_pct", 500)
    with pytest.raises(KeyError):
        s.set_override("nope", 1)


def test_env_aliases_and_defaults_follow_v06_names():
    s = Settings.from_env({"DEMO_MODE": "true", "MONITOR_INTERVAL_SECONDS": "20", "LONG_QUERY_SECONDS": "12", "IDLE_TXN_SECONDS": "7"})
    assert (s.t("fast_interval_s"), s.t("long_query_s"), s.t("idle_txn_s")) == (20, 12, 7)
    assert all(name in TUNABLES for name in [r["name"] for r in s.tunable_rows()])


# ------------------------------------------------------------------ blocking graph
def row(pid, blocked_by=(), **kw):
    return {"pid": pid, "blocked_by": list(blocked_by), "query_age_s": kw.pop("age", 30.0), "state": kw.pop("state", "active"), "application_name": "app", "usename": "u", "datname": "d", **kw}


def test_root_vs_direct_vs_intermediate():
    rows = [row(1, state="idle in transaction", xact_age_s=600), row(2, [1]), row(3, [2], age=20), row(4, [1]), row(9)]
    g = build_blocking_graph(rows)
    assert [r["pid"] for r in g["roots"]] == [1]
    root = g["roots"][0]
    assert root["blocked_count"] == 3 and root["direct_blocked_count"] == 2 and root["chain"][0] == 1 and root["chain"][-1] == 3
    roles = {n["pid"]: n["role"] for n in g["nodes"]}
    assert roles == {1: "root_blocker", 2: "intermediate", 3: "blocked", 4: "blocked"}
    assert 9 not in roles and g["blocked_sessions"] == 3 and g["root_blockers"] == 1


def test_cycle_is_detected_and_has_no_root():
    g = build_blocking_graph([row(1, [2]), row(2, [1])])
    assert g["cycles"] == [[1, 2]] and g["roots"] == []


def test_unknown_root_blocker_still_reported():
    g = build_blocking_graph([row(5, [77])])
    assert [r["pid"] for r in g["roots"]] == [77] and g["roots"][0]["known"] is False


def test_lock_details_attach_to_nodes():
    locks = [{"pid": 1, "locktype": "relation", "mode": "RowExclusiveLock", "granted": True, "relation_name": "public.t"},
             {"pid": 2, "locktype": "transactionid", "mode": "ShareLock", "granted": False, "relation_name": None, "transactionid": "55"}]
    g = build_blocking_graph([row(1), row(2, [1])], locks)
    n = {x["pid"]: x for x in g["nodes"]}
    assert n[1]["holds"][0]["relation_name"] == "public.t" and n[2]["waiting_for"][0]["mode"] == "ShareLock"


def test_graph_node_cap():
    rows = [row(1)] + [row(100 + i, [1]) for i in range(260)]
    g = build_blocking_graph(rows)
    assert g["truncated"] and len(g["nodes"]) == 200 and g["roots"][0]["blocked_count"] == 260


# ------------------------------------------------------------------ version-aware SQL
def test_activity_sql_query_id_only_on_14_plus():
    assert "a.query_id" in V.activity_sql(140000) and "a.query_id" not in V.activity_sql(130000)
    assert "pg_blocking_pids" in V.activity_sql(120000) and "wait_event_type = 'Lock'" in V.activity_sql(120000)


def test_statements_sql_column_names_by_version():
    assert "total_exec_time" in V.statements_sql(130000, "public") and "total_time" in V.statements_sql(120000, "public") and "total_exec_time" not in V.statements_sql(120000, "public")
    assert '"my schema"' in V.statements_sql(160000, "my schema") and V.statements_info_sql(130000, "public") is None and V.statements_info_sql(140000, "public")


def test_checkpointer_split_in_17():
    assert "pg_stat_checkpointer" in V.bgwriter_sql(170000) and "pg_stat_checkpointer" not in V.bgwriter_sql(160000) and "checkpoints_timed" in V.bgwriter_sql(160000)


def test_slots_and_receiver_gating():
    s12, s13, s16, s17 = (V.slots_sql(v, False) for v in (120000, 130000, 160000, 170000))
    assert "wal_status" not in s12 and "wal_status" in s13 and "conflicting" in s16 and "conflicting" not in s13 and "invalidation_reason" in s17
    assert "pg_last_wal_replay_lsn" in V.slots_sql(160000, True) and "flushed_lsn" in V.receiver_sql(130000) and "flushed_lsn" not in V.receiver_sql(120000)
    assert "pg_last_wal_receive_lsn" in V.replication_sql(160000, True)


def test_wal_and_vacuum_gating_and_identifier_quoting():
    assert "pg_stat_wal" in V.wal_sql(140000) and "pg_stat_wal" not in V.wal_sql(130000) and "n_ins_since_vacuum" in V.vacuum_tables_sql(130000)
    assert V.quote_ident('a"b') == '"a""b"' and V.major(160015) == 16


def test_every_sql_uses_parameters_not_string_values():
    for sql in (V.activity_sql(160000), V.LOCKS_SQL, V.SETTINGS_SQL, V.vacuum_tables_sql(160000), V.statements_sql(160000, "public")):
        assert "%(" in sql


# ------------------------------------------------------------------ error classification
@pytest.mark.parametrize("exc,code,status", [
    (pgerr.InsufficientPrivilege("x"), "PERMISSION_DENIED", "unavailable"), (pgerr.UndefinedTable("x"), "NOT_INSTALLED", "unavailable"), (pgerr.UndefinedColumn("x"), "UNSUPPORTED_VERSION", "unavailable"),
    (pgerr.QueryCanceled("x"), "QUERY_TIMEOUT", "error"), (pgerr.LockNotAvailable("x"), "LOCK_TIMEOUT", "error"), (psycopg.OperationalError("connection refused"), "DB_UNAVAILABLE", "error"),
    (psycopg.OperationalError('FATAL: password authentication failed for user "u"'), "AUTH_FAILED", "error"), (psycopg.OperationalError("SSL error: certificate verify failed"), "TLS_FAILED", "error"),
    (psycopg.errors.DataError("x"), "DB_ERROR", "error"), (RuntimeError("boom"), "COLLECTOR_ERROR", "error")])
def test_error_classification(exc, code, status):
    e = classify(exc)
    assert isinstance(e, PgError) and e.code == code and e.status == status


def test_classified_messages_never_leak_connection_details():
    e = classify(psycopg.OperationalError('connection to server at "10.1.2.3", port 5432 failed: FATAL: password authentication failed for user "svc" password=hunter2'))
    assert "10.1.2.3" not in e.message and "hunter2" not in e.message and "svc" not in e.message
