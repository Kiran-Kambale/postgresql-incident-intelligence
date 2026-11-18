"""Flight recorder: schedules bounded, non-overlapping collections per instance and persists metrics and evidence.

Guarantees
- One run per (instance, tier) at a time; overlapping triggers are skipped and counted, never queued.
- A global semaphore bounds concurrent collections so the recorder cannot overwhelm monitored databases.
- Collection failures are persisted (collection_runs + collector_failure evidence); they never raise into the web process.
- Evidence is cleared only when its collector succeeded; otherwise it is marked stale.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .config import InstanceConfig, Settings
from .pg import analysis as A
from .pg import collectors as C
from .pg.collectors import CResult
from .pg.pool import PgInstance
from .services import COUNTERS, create_candidates, query_regressions
from .store import Store
from .timeutil import iso, parse_ts, utcnow
from .util import jsonable

log = logging.getLogger("dir.recorder")
TIERS = ("fast", "standard", "slow")
START_DELAY = {"fast": 0.3, "standard": 2.0, "slow": 4.0}     # staggered first collections
START_JITTER = 1.0
PREV_NAMES = sorted(COUNTERS | {"db_deadlocks"})


@dataclass
class TierState:
    last_run_at: str | None = None
    last_ok_at: str | None = None
    last_status: str | None = None
    last_duration_ms: int | None = None
    consecutive_failures: int = 0
    runs: int = 0
    skipped: int = 0
    running: bool = False
    next_due: str | None = None


@dataclass
class Runtime:
    cfg: InstanceConfig
    pg: PgInstance | None
    info: dict[str, Any] = field(default_factory=dict)
    latest: dict[str, dict[str, Any]] = field(default_factory=dict)
    tiers: dict[str, TierState] = field(default_factory=lambda: {t: TierState() for t in TIERS})
    streaks: dict[str, int] = field(default_factory=dict)
    enabled: bool = True
    locks: dict[str, threading.Lock] = field(default_factory=lambda: {t: threading.Lock() for t in TIERS})


class Recorder:
    def __init__(self, settings: Settings, store: Store, demo_source=None):
        self.settings, self.store, self.demo = settings, store, demo_source
        self.is_demo = settings.demo_mode
        self.sem = threading.BoundedSemaphore(settings.max_concurrent)
        self.rt: dict[str, Runtime] = {}
        for cfg in settings.instances:
            self.rt[cfg.id] = Runtime(cfg, None if self.is_demo else PgInstance(cfg, settings), enabled=cfg.enabled)
            if self.is_demo:
                self.rt[cfg.id].info = demo_source.info()
        self.tasks: list[asyncio.Task] = []
        self._stop: asyncio.Event | None = None
        self.running = False
        self.started_at: str | None = None
        self.last_retention: dict[str, Any] | None = None
        self.seeding = False

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        if self.running:
            return
        self._stop = asyncio.Event()
        self.running = True
        self.started_at = iso()
        for iid in self.rt:
            for tier in TIERS:
                self.tasks.append(asyncio.create_task(self._loop(iid, tier), name=f"rec-{iid}-{tier}"))
        self.tasks.append(asyncio.create_task(self._maintenance(), name="rec-maintenance"))
        log.info("flight recorder started instances=%s", list(self.rt))

    async def stop(self) -> None:
        if not self.running:
            return
        self.running = False
        self._stop.set()
        for t in self.tasks:
            t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()
        for r in self.rt.values():
            if r.pg:
                await asyncio.to_thread(r.pg.close)
        log.info("flight recorder stopped")

    async def _loop(self, iid: str, tier: str) -> None:
        rt = self.rt[iid]
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=START_DELAY[tier] + random.random() * START_JITTER)
            return
        except asyncio.TimeoutError:
            pass
        while not self._stop.is_set():
            interval = float(self.settings.t(f"{tier}_interval_s"))
            if rt.enabled:
                try:
                    await asyncio.to_thread(self.run_tier_sync, iid, tier, "scheduled")
                except Exception:   # the loop must survive anything
                    log.exception("recorder loop error instance=%s tier=%s", iid, tier)
            wait = interval + random.uniform(0, min(2.0, interval * 0.1))
            rt.tiers[tier].next_due = iso(utcnow().replace(microsecond=0) + timedelta(seconds=wait))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=wait)
                return
            except asyncio.TimeoutError:
                continue

    async def _maintenance(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.to_thread(self.run_retention)
            except Exception:
                log.exception("retention failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=3600)
                return
            except asyncio.TimeoutError:
                continue

    def run_retention(self, dry_run: bool = False) -> dict[str, int]:
        days = {k: float(self.settings.t(k)) for k in ("retention_metrics_days", "retention_runs_days", "retention_query_stats_days", "retention_evidence_days",
                                                       "retention_snapshots_days", "retention_audit_days")}
        out = self.store.retention_run(days, dry_run=dry_run)
        if not dry_run:
            self.last_retention = {"at": iso(), "deleted": out}
        return out

    # ------------------------------------------------------------------ one collection
    def run_tier_sync(self, iid: str, tier: str, trigger: str = "scheduled", ts: datetime | None = None, wait: bool = False) -> dict[str, Any]:
        rt = self.rt[iid]
        lock = rt.locks[tier]
        if not lock.acquire(blocking=wait):
            rt.tiers[tier].skipped += 1
            return {"status": "skipped", "reason": "a previous run of this tier is still in progress"}
        ts_state = rt.tiers[tier]
        ts_state.running = True
        try:
            with self.sem:
                return self._pipeline(rt, tier, trigger, ts or utcnow())
        finally:
            ts_state.running = False
            lock.release()

    def _pipeline(self, rt: Runtime, tier: str, trigger: str, ts: datetime) -> dict[str, Any]:
        iid, T, store, demo = rt.cfg.id, self.settings, self.store, self.is_demo
        t0 = time.monotonic()
        try:
            results: dict[str, CResult] = self.demo.generate(tier, ts, rt) if demo else C.run_tier(rt.pg, T, tier, rt.info, ts, store)
            if "server" in results and results["server"].status == "ok" and not demo:
                rt.info.update(rt.pg.info)
            for name, r in results.items():
                rt.streaks[name] = rt.streaks.get(name, 0) + 1 if r.status == "error" else 0
            cands, metrics, table_samples, qrows, updates = self._analyze(rt, tier, results, ts)
            cands += A.analyze_failures(results, rt.streaks)
            ok = {n for n, r in results.items() if r.status in ("ok", "partial")}
            failed = {n for n, r in results.items() if r.status in ("error", "unavailable")}
            if qrows:       # store samples first so regression analysis sees them, then fold the hypotheses into the same evidence pass
                store.qstats_add(iid, iso(ts), qrows, demo)
                cands += query_regressions(store, iid, ts, demo)
            changes = store.evidence_apply(iid, ts, cands, ok, failed, demo)
            store.metrics_add(iid, iso(ts), metrics, demo)
            store.table_samples_add(iid, iso(ts), table_samples, demo)
            for key, value in updates:
                store.kv_set(key, json.dumps(value, default=str))
            for name, r in results.items():
                if r.status in ("ok", "partial"):
                    rt.latest[name] = {"ts": iso(ts), "status": r.status, "data": r.data, "message": r.message}
                else:
                    prev = rt.latest.get(name)
                    rt.latest[name] = {"ts": prev["ts"] if prev else None, "status": r.status, "data": prev["data"] if prev else {}, "message": r.message,
                                       "error_code": r.error_code, "hint": r.hint, "failed_at": iso(ts), "stale": True}
            create_candidates(store, T, iid, changes, demo, lambda: self.snapshot_payload(rt), rt.cfg.dbname)
            dur = int((time.monotonic() - t0) * 1000)
            status = "error" if results and all(r.status == "error" for r in results.values()) else ("ok" if all(r.status == "ok" for r in results.values()) else "partial")
            store.run_add(iid, tier, trigger, iso(ts), iso(), dur, status, {n: r.summary() for n, r in results.items()}, None, demo)
            st = rt.tiers[tier]
            st.runs += 1
            st.last_run_at, st.last_status, st.last_duration_ms = iso(ts), status, dur
            if status == "error":
                st.consecutive_failures += 1
            else:
                st.consecutive_failures, st.last_ok_at = 0, iso(ts)
            return {"status": status, "duration_ms": dur, "new_evidence": len(changes["new"]), "cleared": len(changes["cleared"]),
                    "collectors": {n: r.status for n, r in results.items()}}
        except Exception as exc:
            log.exception("pipeline crashed instance=%s tier=%s", iid, tier)
            try:
                store.run_add(iid, tier, trigger, iso(ts), iso(), int((time.monotonic() - t0) * 1000), "error", {}, f"{type(exc).__name__}", demo)
            except Exception:   # pragma: no cover
                pass
            rt.tiers[tier].consecutive_failures += 1
            rt.tiers[tier].last_status = "error"
            return {"status": "error", "error": type(exc).__name__}

    def _analyze(self, rt: Runtime, tier: str, results: dict[str, CResult], ts: datetime):
        T, store, iid = self.settings, self.store, rt.cfg.id
        cands: list[dict] = []
        metrics: dict[str, float | None] = {}
        tables: list[dict] = []
        qrows: list[dict] = []
        updates: list[tuple[str, Any]] = []
        if tier == "fast":
            r = results.get("activity")
            if r and r.status in ("ok", "partial"):
                c, m = A.analyze_activity(T, r.data, ts)
                cands += c
                metrics.update(m)
        elif tier == "standard":
            prev = {k: v["value"] for k, v in store.metrics_latest(iid, PREV_NAMES, self.is_demo).items()}
            a = A.analyze_standard(T, results, prev, ts)
            cands += a.candidates
            metrics.update(a.metrics)
            st = results.get("statements")
            if st and st.status in ("ok", "partial"):
                qrows = [{k: q.get(k) for k in ("dbid", "userid", "queryid", "fingerprint", "text", "calls", "total_exec_ms", "max_exec_ms", "rows", "shared_hit", "shared_read", "temp_written")}
                         | {"stats_reset": st.data.get("stats_reset")} for q in st.data["queries"]]
            lg = results.get("errorlog")
            if lg and lg.status in ("ok", "partial"):
                cands += lg.data["candidates"]
                su = lg.data["state_update"]
                updates.append((su["key"], su["value"]))
        elif tier == "slow":
            v = results.get("vacuum")
            if v and v.status in ("ok", "partial"):
                a = A.analyze_vacuum(T, v.data, {})
                cands += a.candidates
                metrics.update(a.metrics)
                tables = a.table_samples
            s = results.get("settings")
            if s and s.status in ("ok", "partial"):
                prev_map = json.loads(store.kv_get(f"settings_map:{iid}") or "null")
                a = A.analyze_settings(T, s.data, prev_map, rt.info)
                cands += a.candidates
                updates += [(f"settings_map:{iid}", a.state_updates[0][1])]
        return cands, metrics, tables, qrows, updates

    # ------------------------------------------------------------------ read side
    def runtime(self, iid: str) -> Runtime:
        return self.rt[iid]

    def latest(self, iid: str, name: str) -> dict[str, Any] | None:
        return self.rt[iid].latest.get(name)

    def ensure_fresh(self, iid: str, tier: str, collector: str, max_age_s: float = 5.0) -> dict[str, Any] | None:
        """Serve the recorder's latest data; collect on demand (throttled, coalesced) only if nothing recent exists and the recorder is not providing it."""
        cur = self.latest(iid, collector)
        if cur and cur.get("ts"):
            age = (utcnow() - parse_ts(cur["ts"])).total_seconds()
            if age <= (max_age_s if not self.running else max(max_age_s, 3 * float(self.settings.t(f"{tier}_interval_s")))):
                return cur
        if self.running and cur is None:
            return None            # the scheduler will deliver the first sample shortly
        if not self.is_demo or cur is None:
            self.run_tier_sync(iid, tier, "on_demand", wait=True)
        return self.latest(iid, collector)

    def snapshot_payload(self, rt: Runtime) -> dict[str, Any]:
        act = (rt.latest.get("activity") or {}).get("data") or {}
        vac = (rt.latest.get("vacuum") or {}).get("data") or {}
        rep = (rt.latest.get("replication") or {}).get("data") or {}
        return jsonable({"captured_at": iso(), "demo_mode": self.is_demo, "instance": rt.cfg.public(), "connections": act.get("connections"), "blocking": act.get("blocking"),
                         "activity": (act.get("rows") or [])[:200], "vacuum_tables": (vac.get("tables") or [])[:15], "databases_xid": vac.get("databases_xid"),
                         "replication": {"role": rep.get("role"), "standbys": rep.get("standbys"), "slots": rep.get("slots")}})

    def status(self) -> dict[str, Any]:
        return {"running": self.running, "started_at": self.started_at, "seeding": self.seeding, "max_concurrent": self.settings.max_concurrent, "last_retention": self.last_retention,
                "instances": {iid: {"enabled": r.enabled, "tiers": {t: vars(s) for t, s in r.tiers.items()}, "failure_streaks": {k: v for k, v in r.streaks.items() if v},
                                    "latest": {n: {"ts": v.get("ts"), "status": v.get("status"), "stale": v.get("stale", False)} for n, v in r.latest.items()}} for iid, r in self.rt.items()}}
