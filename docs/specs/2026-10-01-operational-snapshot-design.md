# Pre-Active Operational Snapshot V1

Status: stacked implementation target on top of provider-retry PR #15.

## Goal

Make the durable runtime operationally inspectable without requiring direct SQLite queries and without importing a telemetry backend into core.

## Research basis

Mature queue/workflow systems expose more than raw backlog size:

- Amazon SQS exposes oldest-message age plus visible, in-flight, and delayed message counts because backlog size alone cannot distinguish healthy throughput from stuck consumers.
- Temporal worker guidance emphasizes Task Queue backlog together with worker/poller availability when diagnosing delayed work.
- OpenTelemetry messaging conventions standardize messaging operation/process metrics but remain under active semantic-convention evolution and warn against uncontrolled-cardinality attributes.

Pre-Active should first expose a stable provider-neutral snapshot that exporters can consume later.

## Snapshot

`Store.operational_snapshot(now=...)` returns deterministic JSON-safe measurements:

- compatibility totals: pending/in-flight and dead counts;
- event counts by durable status;
- ready pending events;
- delayed pending events;
- retry-pending events;
- active claims;
- expired claims;
- age of the oldest ready event;
- age past the oldest expired lease;
- age of the oldest dead-letter item;
- run counts by status;
- enabled schedules;
- schedules currently due.

A missing age is `None`, not zero, so “nothing exists” is distinct from “exists and is brand new.”

## Status CLI

`pre-active status` emits that snapshot as JSON. Existing top-level `pending_events`, `dead_events`, and `runs` fields remain for compatibility.

## Non-goals

V1 does not:

- assign a universal `healthy` boolean;
- define alert thresholds;
- scrape CPU/memory;
- claim daemon/process liveness from database inactivity;
- add Prometheus/OpenTelemetry dependencies;
- attach event IDs/run IDs as metric labels.

Those require separate host/exporter policy.

## Hostile review

> **HOSTILE REVIEWER:** Just add OpenTelemetry now.

**REJECTED FOR THIS LANE.** The messaging semantic conventions are still evolving, and exporter choice is host policy. A stable internal snapshot is the prerequisite and keeps core self-contained.

> **HOSTILE REVIEWER:** A single `healthy` boolean is easier for automation.

**REJECTED.** “Too old,” “too many,” and “too blocked” are workload-specific thresholds. Pre-Active should report evidence, not invent an SLA.

> **HOSTILE REVIEWER:** Counts are enough; age metrics are decorative.

**REJECTED WITH EVIDENCE.** Queue systems such as SQS explicitly surface oldest-message age because a small but stuck backlog can be more serious than a large fast-moving one.

> **HOSTILE REVIEWER:** Last database activity proves the daemon is alive.

**REJECTED.** Activity and liveness are different propositions. Worker heartbeat/liveness evidence belongs in a separate mechanism.

## Claim ceiling

Source-level operational visibility only. Snapshot values describe one SQLite state view at the supplied timestamp; they do not establish host/process liveness or external-system health.
