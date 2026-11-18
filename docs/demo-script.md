# 10-minute demo (synthetic data)

1. Start with `DEMO_MODE=true`. Point out the DEMO banner: everything is synthetic and stored separately.
2. **Overview**: blocked sessions and connection pressure; Active findings; the incident candidate.
3. **Locks and Blocking**: the root blocker (PID 4242, billing-job, idle in transaction) and the sessions it blocks. Click the root node for its statement and locks.
4. **Evidence Explorer**: open the blocking evidence and read the six answers; point out *Limitations* and the risk on the cancel/terminate suggestion (it is only copied, never run).
5. **Related** tab: structural links (shared PID) before temporal ones; note the causation warning.
6. **Incidents → Candidates**: promote the candidate, record a *hypothesis* and later mark a cause *confirmed*; show how the report separates them.
7. **Query Performance**: the regression hypothesis on the UPDATE statement and its limits.
8. **Vacuum / Replication**: inactive logical slot retaining WAL; XID age.
9. Switch time zone and theme; zoom a chart by dragging; show the hatched "no data" band at the start of the range.
10. State the boundaries (docs/VALIDATION.md).

Do not present synthetic results as measurements from a real PostgreSQL server or as proof of root cause.
