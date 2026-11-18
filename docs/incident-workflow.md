# Incident workflow

```
Detect → Collect → Correlate → Investigate → Recommend → Resolve → Review
```

| Stage | Meaning | Typical status |
|---|---|---|
| Detect | Evidence suggests an incident; no human has looked yet | `candidate` |
| Collect | Someone owns it; symptom and timeline are being established | `open` |
| Correlate | Evidence is linked and related events examined | `open` / `investigating` |
| Investigate | Hypotheses are tested | `investigating` |
| Recommend | A mitigation or fix is chosen (recorded, not executed) | `mitigated` |
| Resolve | Impact ended; resolution recorded | `resolved` |
| Review | Follow-ups and prevention recorded | `closed` |

Changing the status moves the stage; the stage can also be set directly. Automatic evidence links never advance the stage; a person's actions do.

## Candidates

Critical evidence creates a candidate. A candidate shows the facts observed and states that the cause is not established. **Promote** moves it to `open`; **Dismiss** requires confirmation and is remembered for the cool-down. Dismissed incidents can be reopened.

## Evidence on an incident

Link evidence from the Evidence Explorer ("Add to incident…") or the API. Links are labelled `manual`, `auto_structural` (shared PID/statement/relation) or `auto_temporal` (time overlap only). Temporal links are context, not explanation.

## Confidence labels

| Label | Meaning |
|---|---|
| observed | Directly recorded from PostgreSQL (for example, PID 4242 was idle in transaction for 11 minutes) |
| likely | Consistent with the evidence but not verified |
| hypothesis | A possible explanation not yet tested |
| confirmed | Verified by a person (reproduced, confirmed with the owner, or proven by the logs) |

The *suspected* root cause field is always a hypothesis. A cause is shown as confirmed only if someone marks it so. Reports list observed facts, likely explanations, hypotheses and confirmed findings in separate sections.

## Reports

`GET /api/incidents/{id}/report` (JSON) or `?format=markdown`. Contains the incident, linked evidence and diagnostic cards, a merged timeline of notes and evidence, recommendations ordered informational → investigation required → potentially disruptive (de-duplicated, with the evidence that raised each), and related snapshots. Actions recorded on an incident are records of what people did (`execution: recorded_only`).

## Evidence snapshots

"Capture evidence snapshot" stores the recorder's latest connections, blocking graph, up to 200 sessions, top vacuum tables, XID age and replication state, linked to the incident. A snapshot is a copy of what was recorded at capture time, redacted per `QUERY_TEXT_MODE`; treat it as sensitive.
