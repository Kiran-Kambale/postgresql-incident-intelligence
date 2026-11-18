# Evidence snapshots and history

In v1.0 continuous history is kept by the flight recorder (see flight-recorder.md): evidence with first/last seen times, metrics, collection runs and query samples. **Snapshots** remain for a different purpose: a frozen, redacted copy of what the recorder had at one moment, optionally attached to an incident.

- `POST /api/incidents/{id}/capture` (or the "Capture evidence snapshot" button) stores connections, the blocking graph, up to 200 sessions, top vacuum tables, XID age and replication state, linked to the incident.
- `POST /api/history/capture` and `GET /api/history[/{id}]` are the v0.6-compatible equivalents for unattached snapshots.
- Snapshots are never deleted by retention while linked to an incident; unlinked snapshots follow `retention_snapshots_days` (90).

Limitations: a snapshot cannot recreate events the recorder did not observe; PIDs are reused over time and must not be used alone as an identity (evidence fingerprints include the backend start time for this reason); snapshots can contain statement text (redacted according to `QUERY_TEXT_MODE`), so review before sharing.
