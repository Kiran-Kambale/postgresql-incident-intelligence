"""Blocking graph analysis. Pure functions over activity rows (each with `pid` and `blocked_by`)."""
from __future__ import annotations

from collections import defaultdict, deque
from typing import Any

MAX_NODES = 200


def _sccs(nodes: set[int], succ: dict[int, list[int]]) -> list[list[int]]:
    """Iterative Tarjan: returns strongly connected components with a cycle (size > 1 or self-loop)."""
    index, low, on, stack, out, counter = {}, {}, set(), [], [], [0]
    for start in nodes:
        if start in index:
            continue
        work = [(start, iter(succ.get(start, [])))]
        index[start] = low[start] = counter[0]; counter[0] += 1
        stack.append(start); on.add(start)
        while work:
            node, it = work[-1]
            advanced = False
            for nxt in it:
                if nxt not in index:
                    index[nxt] = low[nxt] = counter[0]; counter[0] += 1
                    stack.append(nxt); on.add(nxt)
                    work.append((nxt, iter(succ.get(nxt, []))))
                    advanced = True
                    break
                if nxt in on:
                    low[node] = min(low[node], index[nxt])
            if advanced:
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[node])
            if low[node] == index[node]:
                comp = []
                while True:
                    w = stack.pop(); on.discard(w); comp.append(w)
                    if w == node:
                        break
                if len(comp) > 1 or node in succ.get(node, []):
                    out.append(sorted(comp))
    return out


def build_blocking_graph(rows: list[dict[str, Any]], lock_rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Distinguish direct blockers from root blockers.

    A *direct blocker* holds a lock a session is waiting for. A *root blocker* blocks others but is not itself waiting
    (it is the head of a chain). A session can be both blocked and blocking (an intermediate link).
    """
    by_pid = {r["pid"]: r for r in rows}
    succ: dict[int, list[int]] = defaultdict(list)      # blocked -> direct blockers
    rev: dict[int, list[int]] = defaultdict(list)       # blocker -> directly blocked
    for r in rows:
        for b in r.get("blocked_by") or []:
            succ[r["pid"]].append(b)
            rev[b].append(r["pid"])
    nodes = set(succ) | set(rev)
    waiting = set(succ)
    roots = sorted(p for p in rev if p not in waiting)
    cycles = _sccs(nodes, succ) if nodes else []

    locks_by_pid: dict[int, list[dict]] = defaultdict(list)
    for lr in lock_rows or []:
        locks_by_pid[lr["pid"]].append(lr)

    def node_info(pid: int) -> dict[str, Any]:
        r = by_pid.get(pid)
        role = "root_blocker" if pid in roots else ("intermediate" if pid in rev else "blocked")
        base = {"pid": pid, "role": role, "known": r is not None, "direct_blockers": sorted(set(succ.get(pid, []))),
                "directly_blocks": sorted(set(rev.get(pid, [])))}
        if r:
            base.update({k: r.get(k) for k in ("datname", "usename", "application_name", "client_addr", "state", "wait_event_type", "wait_event",
                                               "xact_age_s", "query_age_s", "state_age_s", "query", "query_fingerprint", "backend_start", "xact_start")})
            base["wait_s"] = r.get("query_age_s") if pid in waiting else None
        locks = locks_by_pid.get(pid, [])
        if locks:
            waiting_on = [l for l in locks if not l["granted"]]
            held = [l for l in locks if l["granted"]]
            base["waiting_for"] = [{k: l.get(k) for k in ("locktype", "mode", "relation_name", "transactionid")} for l in waiting_on[:5]]
            base["holds"] = [{k: l.get(k) for k in ("locktype", "mode", "relation_name")} for l in held[:8]]
            base["holds_total"] = len(held)
        return base

    root_summaries = []
    for root in roots:
        seen, depth_of, q = {root}, {root: 0}, deque([root])
        while q:
            cur = q.popleft()
            for nxt in rev.get(cur, []):
                if nxt not in seen:
                    seen.add(nxt); depth_of[nxt] = depth_of[cur] + 1; q.append(nxt)
        blocked = seen - {root}
        waits = [by_pid[p]["query_age_s"] for p in blocked if p in by_pid and by_pid[p].get("query_age_s") is not None]
        deepest = max(depth_of, key=depth_of.get)
        chain, cur = [deepest], deepest          # one longest chain: leaf -> ... -> root
        while cur != root:
            cur = next((b for b in succ.get(cur, []) if depth_of.get(b, -1) == depth_of[cur] - 1), root)
            chain.append(cur)
        info = node_info(root)
        info.update({"blocked_count": len(blocked), "direct_blocked_count": len(set(rev.get(root, []))),
                     "max_wait_s": max(waits) if waits else None, "depth": depth_of[deepest], "chain": list(reversed(chain)),
                     "blocked_pids": sorted(blocked)[:50]})
        root_summaries.append(info)
    root_summaries.sort(key=lambda r: (-(r["blocked_count"]), r["pid"]))

    ordered = sorted(nodes, key=lambda p: (0 if p in roots else 1, p))
    truncated = len(ordered) > MAX_NODES
    node_list = [node_info(p) for p in ordered[:MAX_NODES]]
    edges = [{"blocked": b, "blocker": k} for b, ks in sorted(succ.items()) for k in sorted(set(ks))]
    return {"roots": root_summaries, "nodes": node_list, "edges": edges[:600], "cycles": cycles, "blocked_sessions": len(waiting),
            "root_blockers": len(roots), "truncated": truncated,
            "note": "pg_blocking_pids() and pg_locks describe the lock manager at the instant of collection. Historical blocking exists only if "
                    "it was captured while it happened. Wait time is approximated by statement age (PostgreSQL does not expose wait start)."}
