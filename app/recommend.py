"""Evidence-specific diagnostic cards: observed facts, why it matters, likely impact, what to investigate next, and graded suggestions.

Suggestions are classified informational / investigation_required / potentially_disruptive. Nothing here executes anything; SQL snippets are
shown for the engineer to run deliberately.
"""
from __future__ import annotations

from typing import Any, Callable

from .util import fmt_bytes, fmt_dur

EPISTEMIC = {"observed": "observed_fact", "likely": "likely_explanation", "hypothesis": "hypothesis", "confirmed": "confirmed_finding"}
CLASSES = ("informational", "investigation_required", "potentially_disruptive")


def rec(cls: str, text: str, rationale: str, risk: str | None = None, sql: str | None = None) -> dict[str, Any]:
    assert cls in CLASSES
    return {"class": cls, "text": text, "rationale": rationale, "risk": risk, "sql": sql}


def go(text: str, page: str, **params: Any) -> dict[str, Any]:
    return {"text": text, "drilldown": {"page": page, **params}}


def _blocking(ev: dict) -> tuple[dict, list[str]]:
    d, r = ev["details"], ev["details"].get("root", {})
    idle = d.get("root_is_idle_in_transaction")
    pid = r.get("pid")
    obs = [f"PID {pid} ({r.get('application_name') or 'no application_name'} / {r.get('usename')}) is blocking {d['blocked_count']} session(s); longest wait ≥ {fmt_dur(d['max_wait_s'])}.",
           f"The root blocker is {'idle in transaction' if idle else r.get('state')}; its transaction has been open {fmt_dur(r.get('xact_age_s'))}."]
    if d.get("relations_waited_on"):
        obs.append("Blocked sessions wait on: " + ", ".join(d["relations_waited_on"]) + ".")
    if r.get("query"):
        obs.append(f"Root's last statement: {r['query'][:200]}")
    return {
        "why_it_matters": "Blocked sessions keep their connections and application threads while they wait. "
                          + ("An idle-in-transaction root blocker does not finish on its own: it holds locks until the client commits, rolls back or disconnects." if idle
                             else "A long-running root blocker releases locks only when its statement and transaction end."),
        "impact": f"{d['blocked_count']} session(s) are stalled; requests touching the same rows or tables will queue or time out, and connection pools may fill up.",
        "investigate": [go("Open the blocking graph and inspect the chain", "locks", pid=pid),
                        go("Identify the application behind the root (application_name, client address, user)", "activity", pid=pid),
                        go("See related evidence around this time (connections, long transactions, config changes)", "evidence", related=ev["id"])],
        "recommendations": [
            rec("informational", "Review the root blocker's identity and its last statement before acting.", "Intervening without knowing what the transaction is doing can lose work or hide the cause."),
            rec("investigation_required", "Find out why the transaction stays open (application code path, connection pooler, batch job, manual session)." if idle
                else "Find out why the root statement is slow or holds locks so long (plan, missing index, lock mode).",
                "The same blocking will recur unless the underlying behaviour changes."),
            rec("potentially_disruptive", "If the owner confirms the work can be abandoned and the impact is severe, cancel the root's statement first; terminate the session only if cancel is not enough.",
                "Cancelling is the least intrusive way to release locks.",
                "Both roll back the transaction's uncommitted work and can surface errors in the application. Terminating a session also drops its connection. Confirm ownership first.",
                f"-- review before running\nSELECT pg_cancel_backend({pid});\n-- only if the cancel is not enough:\n-- SELECT pg_terminate_backend({pid});")]}, obs


def _long_query(ev):
    d = ev["details"]
    return {"why_it_matters": "Long statements hold snapshots (delaying vacuum cleanup), consume I/O and CPU, and may hold locks.",
            "impact": "Depends on what the statement does; a reporting query may be harmless while the same duration for an OLTP statement signals a plan problem or lock wait.",
            "investigate": [go("Check whether it is waiting on a lock or running", "activity", pid=ev["pid"]), go("Look for the statement in query performance trends", "queries", fingerprint=ev.get("query_fingerprint"))],
            "recommendations": [rec("investigation_required", "Decide whether the duration is expected for this workload; if not, capture its plan (EXPLAIN on a representative run).", "Duration alone does not distinguish a heavy report from a regression."),
                                rec("potentially_disruptive", "If it is harmful and its owner agrees, cancel it.", "Cancelling frees resources quickly.", "The statement's work is lost; applications may retry it.", f"SELECT pg_cancel_backend({ev['pid']});")]}, \
        [f"PID {d.get('pid')} has been running one statement for {fmt_dur(d.get('query_age_s'))}" + (" and is waiting on a lock." if d.get("waiting_on_lock") else "."), f"Application: {d.get('application_name') or 'unset'}; user {d.get('usename')}."]


def _idle(ev):
    d = ev["details"]
    obs = [f"PID {d['pid']} has been idle in transaction for {fmt_dur(d['idle_for_s'])} (transaction open {fmt_dur(d.get('xact_age_s'))}).",
           "It is currently blocking other sessions." if d.get("blocking_others") else "It is not blocking any session at the moment."]
    return {"why_it_matters": "An idle-in-transaction session keeps its locks and its snapshot. Locks can block writers or DDL, and the snapshot prevents vacuum from removing newer dead rows.",
            "impact": "Immediate if it blocks others; otherwise it silently accumulates bloat risk the longer it lives.",
            "investigate": [go("Identify the client (application_name, address) and whether it is a pooler artefact", "activity", pid=d["pid"]), go("Check for dependent blocked sessions", "locks", pid=d["pid"])],
            "recommendations": [rec("investigation_required", "Find why the client opened a transaction and stopped (missing COMMIT/ROLLBACK, waiting on another system inside a transaction, crashed worker).", "The fix belongs in the client."),
                                rec("informational", "Server-side idle_in_transaction_session_timeout and pooler settings can bound the damage; whether to use them is a policy decision for the application owner.", "They limit the worst case but terminate sessions that are legitimately slow."),
                                rec("potentially_disruptive", "Terminating the session releases its locks immediately.", "It is the only server-side way to end an idle transaction on demand.", "Uncommitted work is lost and the client will see a connection error.", f"SELECT pg_terminate_backend({d['pid']});")]}, obs


def _long_txn(ev):
    d = ev["details"]
    return {"why_it_matters": "An old transaction pins the xmin horizon, so VACUUM cannot remove rows deleted after it began.", "impact": "Bloat grows on write-heavy tables while it stays open.",
            "investigate": [go("Inspect the session", "activity", pid=ev["pid"]), go("Check vacuum pressure", "vacuum")],
            "recommendations": [rec("investigation_required", "Confirm whether this is an intentional long job (ETL, dump, migration).", "Planned long transactions are normal; unplanned ones usually are not.")]}, \
        [f"PID {d['pid']} has kept one transaction open for {fmt_dur(d.get('xact_age_s'))}.", f"xmin age: {d.get('xmin_age')}."]


def _connsat(ev):
    d = ev["details"]
    obs = [f"{d['slots_used']} of {d['max_connections']} connection slots are in use ({d['utilization_percent']}%).", f"{d['available_for_regular_users']} slot(s) remain for regular users after reserved slots."]
    top = d["by_application"][:3]
    if top:
        obs.append("Top applications: " + ", ".join(f"{a['name']} ({a['sessions']})" for a in top) + ".")
    sym = d.get("possible_symptom_of_blocking")
    return {"why_it_matters": "When slots run out, new connections are refused, which looks like an outage to applications.",
            "impact": "New clients fail to connect; existing sessions keep working.",
            "investigate": [go("Break connections down by application, user and state", "activity", view="connections"),
                            go("Check whether sessions are stuck behind locks (saturation may be a symptom)", "locks"), go("Compare with the connection trend", "overview")],
            "recommendations": [rec("informational", "Check whether many sessions are idle (pool over-sizing) or waiting (locks / slow queries).", "The remedy differs: pool sizing versus removing the blocker."),
                                rec("investigation_required", "A large number of sessions waiting on locks suggests saturation is a symptom; see the blocking evidence first." if sym else
                                    "Review pool sizes and application connection lifecycles; consider whether a pooler such as PgBouncer is appropriate.", "Raising max_connections only treats a symptom and increases memory use and contention."),
                                ]}, obs


def _waitpile(ev):
    d = ev["details"]
    return {"why_it_matters": "Many sessions waiting on the same event at one instant points to contention on a shared resource.", "impact": "Throughput drops while sessions queue.",
            "investigate": [go("See which sessions are waiting", "activity", wait_event=d["wait_event"]), go("Check for blocking", "locks")],
            "recommendations": [rec("investigation_required", f"Look up the wait event {d['wait_event_type']}:{d['wait_event']} in the PostgreSQL documentation and compare with workload changes.", "Wait events describe where time goes, not why.")]}, \
        [f"{d['sessions']} sessions were waiting on {d['wait_event_type']}:{d['wait_event']} out of {d['active_sessions']} active sessions."]


def _deadlock(ev):
    d = ev["details"]
    procs = d.get("processes") or []
    obs = ["PostgreSQL resolved a deadlock" + (f" involving PIDs {', '.join(str(p['pid']) for p in procs)}." if procs else f" (counter increased by {int(d.get('increase') or 1)}).")]
    return {"why_it_matters": "One transaction was aborted to break the cycle; its work was rolled back and the application must retry.", "impact": "Failed requests unless the application retries deadlock errors.",
            "investigate": [go("Open the evidence for statements and lock order", "evidence", id=ev["id"]), go("Check recent lock waits", "evidence", category="lock_wait_log")],
            "recommendations": [rec("investigation_required", "Compare the statements in the deadlock report: the usual fix is to take locks in a consistent order or to shorten transactions.", "Deadlocks are an application-level ordering problem."),
                                rec("informational", "Application retry logic for SQLSTATE 40P01 turns a deadlock into a delay instead of an error.", "It does not remove the cause.")]}, obs


def _repl_lag(ev):
    d = ev["details"]
    lags = d.get("lags") or {}
    obs = [ev["title"] + "."]
    if lags:
        obs.append("Measured lag: write " + (fmt_dur(lags.get("write_lag_s")) if lags.get("write_lag_s") is not None else "n/a") + ", flush " + (fmt_dur(lags.get("flush_lag_s")) if lags.get("flush_lag_s") is not None else "n/a")
                   + ", replay " + fmt_dur(lags.get("replay_lag_s")) + f"; the delay appears mostly at the {d.get('delay_appears_mostly_in')} stage.")
    return {"why_it_matters": "A lagging standby widens the data-loss window on failover and can serve stale reads; with synchronous replication it can also stall commits.", "impact": "Depends on how the standby is used (failover target, read replica).",
            "investigate": [go("Compare write, flush and replay lag per standby", "replication"), go("Check slots and WAL retention", "replication", view="slots"), go("Look for concurrent checkpoint or I/O pressure", "evidence", category="checkpoint_pressure")],
            "recommendations": [rec("investigation_required", "Determine whether the delay is in transport (write lag), durability (flush lag) or replay on the standby, then examine that side (network, standby I/O, long queries causing replay conflicts).", "Each stage has a different owner and fix. This tool shows where the delay is visible, not why."),
                                rec("informational", "Avoid assigning a cause from correlation alone: heavy writes, a slow standby disk and network loss can all look alike on the primary.", "Replication lag is a symptom with several independent causes.")]}, obs


def _slot(ev):
    d = ev["details"]
    s = d["slot"]
    obs = [f"Slot '{s['slot_name']}' ({d['kind']}, {'active' if s['active'] else 'inactive'}) retains {fmt_bytes(s['retained_bytes'])} of WAL."] + d.get("reasons", [])
    return {"why_it_matters": "A slot makes PostgreSQL keep WAL until its consumer confirms it. If the consumer is gone or slow, WAL accumulates and can fill the WAL volume.", "impact": "Disk exhaustion on the primary is the worst case; that stops all writes.",
            "investigate": [go("Check whether the consumer (standby, subscriber, CDC tool) is still alive", "replication", view="slots"), go("Review max_slot_wal_keep_size and free space on the WAL volume", "settings")],
            "recommendations": [rec("investigation_required", "Confirm whether the slot's consumer still exists and is expected to return.", "Dropping a slot that is still needed forces a re-sync of its consumer."),
                                rec("potentially_disruptive", "Drop the slot only after the owner confirms it is obsolete.", "It releases retained WAL.", "Irreversible for the consumer, which will need a new base copy or initial sync.", f"-- review ownership first\nSELECT pg_drop_replication_slot('{s['slot_name']}');")]}, obs


def _vacuum(ev):
    d = ev["details"]
    obs = [f"{d['relation']} has {int(d['n_dead_tup']):,} dead tuples ({d['dead_ratio_pct']}% of rows)."] + d.get("context", [])
    return {"why_it_matters": "Dead tuples occupy space and slow scans until vacuum reclaims them for reuse.", "impact": "Bloat, slower queries and larger indexes; severe cases risk running out of disk.",
            "investigate": [go("See vacuum history, running vacuums and xmin holders", "vacuum", relation=d["relation"]), go("Check for old transactions or slots that block cleanup", "vacuum", view="horizon")],
            "recommendations": [rec("investigation_required", "Check whether autovacuum has been keeping up: when it last ran on this table, whether it was cancelled, and whether an old xmin holder prevents cleanup.", "Vacuum that runs but cannot remove rows looks identical in dead-tuple counts."),
                                rec("informational", "Table-level autovacuum settings (scale factor, cost limits) are workload-specific; examine this table's churn before changing them.", "Global thresholds suit average tables, not hot ones.")]}, obs


def _xid(ev):
    d = ev["details"]
    return {"why_it_matters": "If transaction ID age reaches the wraparound limit PostgreSQL stops accepting writes to protect data.", "impact": "Time-to-limit depends on how fast XIDs are consumed; compare with the trend.",
            "investigate": [go("See the XID age trend and what holds back freezing", "vacuum", view="xid"), go("Check xmin holders", "vacuum", view="horizon")],
            "recommendations": [rec("investigation_required", "Find what prevents anti-wraparound vacuum from completing (old xmin holder, prepared transaction, vacuum failing or blocked, autovacuum disabled).", "Freezing needs vacuum to finish on the oldest tables."),
                                rec("potentially_disruptive", "A manual VACUUM (FREEZE) on the oldest tables may be needed if autovacuum cannot keep up.", "It directly advances relfrozenxid.", "Adds I/O load and can run long on large tables; schedule with the application owner.")]}, \
        [f"Database {d['database']} has XID age {d['xid_age']:,}; the hard limit is about 2.1 billion.", f"autovacuum_freeze_max_age is {int(d['autovacuum_freeze_max_age']):,}."]


def _xmin(ev):
    d = ev["details"]
    return {"why_it_matters": "The oldest xmin limits what VACUUM may remove everywhere in the cluster.", "impact": "Bloat accumulates in every busy table while the holder remains.",
            "investigate": [go("Inspect xmin holders", "vacuum", view="horizon")],
            "recommendations": [rec("investigation_required", "Identify the holder (session, replication slot, prepared transaction, standby feedback) and why it is old.", "Each holder type is resolved differently.")]}, \
        [f"Oldest xmin holder: {h['holder']} (age {h['age']:,})." for h in d["holders"][:3]]


def _prepared(ev):
    d = ev["details"]
    return {"why_it_matters": "Prepared transactions survive restarts, keep locks and hold back vacuum until resolved.", "impact": "Blocks other sessions on its locks and pins the xmin horizon.",
            "investigate": [go("See the prepared transaction", "vacuum", view="horizon")],
            "recommendations": [rec("investigation_required", "Contact the owner (often a transaction manager or XA coordinator) to commit or roll back.", "Only the coordinator knows the intended outcome."),
                                rec("potentially_disruptive", "Resolve it manually only if the coordinator confirms the outcome.", "Frees locks and the xmin horizon.", "A wrong COMMIT/ROLLBACK breaks atomicity across the systems it coordinates.", f"-- decide with the coordinator\nCOMMIT PREPARED '{d['gid']}';  -- or ROLLBACK PREPARED")]}, \
        [f"Prepared transaction '{d['gid']}' (owner {d['owner']}) pending for {fmt_dur(d['age_s'])}."]


def _config_change(ev):
    ch = ev["details"]["changed"]
    return {"why_it_matters": "A parameter change can explain a change in behaviour that started around the same time.", "impact": "Depends on the parameter.",
            "investigate": [go("Compare the change time with other evidence", "evidence", related=ev["id"]), go("View current settings", "settings")],
            "recommendations": [rec("informational", "Correlation in time is not causation: confirm from the parameter's meaning that it could produce the observed symptoms.", "Many changes are unrelated to nearby incidents.")]}, \
        [f"{c['name']}: {c['old']} → {c['new']}{(' ' + c['unit']) if c.get('unit') else ''} (source: {c['source']})." for c in ch[:8]]


def _config_obs(ev):
    d = ev["details"]
    return {"why_it_matters": d["why_it_matters"], "impact": "Context-dependent.", "investigate": [go("Open monitoring settings", "settings")],
            "recommendations": [rec("informational", "Read the documentation for this parameter and decide in the context of this workload.", "A value that differs from a generic recommendation is not by itself a problem.")]}, [ev["title"] + "."]


def _collector(ev):
    d = ev["details"]
    return {"why_it_matters": "While collection fails, evidence for this area is not being recorded and the timeline has a gap.", "impact": "Incidents during the gap cannot be reconstructed.",
            "investigate": [go("See collection run history", "audit")], "recommendations": [rec("investigation_required", d.get("hint") or "Check connectivity and privileges.", "The error code identifies the failure class.")]}, \
        [f"Collector '{d['collector']}' failed with {d['error_code']}: {d.get('message')}", f"Consecutive failures: {d.get('consecutive_failures')}."]


def _gap(ev):
    d = ev["details"]
    return {"why_it_matters": "This diagnostic area shows 'unavailable' instead of data.", "impact": "Reduced visibility only.", "investigate": [go("Review the permission matrix", "audit", view="capabilities")],
            "recommendations": [rec("informational", d.get("hint") or "See docs/permissions.md for the privileges each diagnostic needs.", "Least-privilege roles intentionally lack some capabilities.")]}, [ev["title"]]


def _query(ev):
    d = ev["details"]
    return {"why_it_matters": "Cumulative statistics can reveal a statement that consumes a disproportionate share of database time or became slower between windows.", "impact": "Depends on the statement's role in the workload.",
            "investigate": [go("Open the statement's trend", "queries", fingerprint=ev.get("query_fingerprint"))],
            "recommendations": [rec("investigation_required", "Compare plans before and after (auto_explain or EXPLAIN on a representative run) and check data volume, statistics freshness and recent deployments.", "A higher mean time can come from a plan change, data growth, contention or a different parameter mix.")]}, \
        [ev["title"] + ".", f"Window: {d.get('window_minutes')} min; calls {d.get('calls_now')} (previous {d.get('calls_before')})."]


def _generic(ev):
    return {"why_it_matters": "Recorded from the PostgreSQL server log or a diagnostic view.", "impact": "See the evidence details.", "investigate": [go("Open surrounding evidence", "evidence", related=ev["id"])],
            "recommendations": [rec("informational", "Review the details and surrounding events.", "Context decides significance.")]}, [ev["title"] + "."]


BUILDERS: dict[str, Callable[[dict], tuple[dict, list[str]]]] = {
    "blocking": _blocking, "long_query": _long_query, "idle_in_transaction": _idle, "long_transaction": _long_txn, "connection_saturation": _connsat,
    "wait_event_pileup": _waitpile, "deadlock": _deadlock, "replication_lag": _repl_lag, "replication_slot": _slot, "vacuum_pressure": _vacuum,
    "xid_age": _xid, "old_xmin_holder": _xmin, "orphaned_prepared_xact": _prepared, "config_change": _config_change, "config_observation": _config_obs,
    "collector_failure": _collector, "capability_gap": _gap, "query_regression": _query, "query_hotspot": _query,
}


def build_card(ev: dict[str, Any]) -> dict[str, Any]:
    """Return the diagnostic card for an evidence record (see docs for the six questions it answers)."""
    builder = BUILDERS.get(ev["category"], _generic)
    body, observed = builder(ev)
    return {"evidence_id": ev["id"], "category": ev["category"], "severity": ev["severity"], "title": ev["title"],
            "epistemic": EPISTEMIC.get(ev.get("confidence", "observed"), "observed_fact"), "observed": observed,
            "why_it_matters": body["why_it_matters"], "impact": body["impact"],
            "evidence": {"source": ev["source"], "first_seen": ev["first_seen"], "last_seen": ev["last_seen"], "cleared_at": ev.get("cleared_at"),
                         "observations": ev.get("observation_count"), "collection_status": ev.get("collection_status")},
            "investigate": body["investigate"], "recommendations": body["recommendations"], "limitations": ev.get("limitations", "")}
