# Pre-Active Resilience Hardening V1

Status: implementation target for `feature/resilience-leases-dlq-v1-20261001`.

## Problem

Pre-Active already persists queue state, run generations, model decisions, tool effects, and explicit ambiguous-effect recovery. Three reliability gaps are load-bearing:

1. event ownership is represented only by `lease_owner` + `lease_until`; there is no per-claim fencing token and no lease renewal while a long model/tool operation is active;
2. retry scheduling is exponential but unbounded; a permanently failing event can cycle forever and a run can remain indefinitely nonterminal;
3. once bounded retry creates a dead-letter state, operators need an explicit inspection/redrive path or recovery degrades into manual SQLite editing.

The default CLI currently permits a 120-second model timeout with a 30-second event lease, so a healthy model request can outlive its queue ownership window.

## Research basis

Current durable-runtime practice converges on the same controls:

- Google Cloud Pub/Sub automatically extends acknowledgement deadlines for active long-running consumers and recommends lease management when processing duration varies.
- Amazon SQS recommends extending visibility while a consumer is still processing and routing repeatedly failing messages to a dead-letter queue.
- Temporal carries heartbeat progress across long-running Activity retries and treats retries/timeouts as first-class durable execution policy.
- Azure Durable Functions exposes bounded retry policies with attempt ceilings, backoff, maximum intervals, and retry timeouts.
- LangGraph distinguishes transient failures suitable for automatic retry from failures that require model, human, or developer intervention.

These systems are larger than Pre-Active; the goal is to import the reliability invariant, not their architecture.

## Design

### Fenced claims

Each successful event claim receives a fresh opaque `lease_token`. The token changes on every reclaim. Acknowledgement, retry/dead-letter transition, and lease renewal must match event ID, worker ID, and lease token. A stale worker therefore cannot complete or reschedule a claim after another worker has reclaimed it.

### Renewable bounded leases

A `LeaseHeartbeat` helper renews the current claim on a dedicated SQLite connection while the engine is inside potentially blocking model/tool work. Renewal succeeds only while the matching claim is still active and unexpired.

Heartbeat ownership has a maximum extension duration. Once that ceiling is reached, renewal stops and the engine treats the claim as lost when control returns. This prevents a wedged process from extending ownership forever.

No heartbeat itself grants effect authority. Existing tool-effect fencing remains authoritative for mutations.

### Fenced durable progress

A heartbeat assertion alone leaves a check-then-write race. Fresh model decisions and run-progress commits therefore revalidate the exact event claim while holding the SQLite write lock. The heartbeat's bounded-extension assertion is checked again before commit. Tool-result/run-generation/successor/ACK and final-completion/ACK transitions are grouped so stale workers cannot advance durable run state after losing ownership.

### Bounded retry and dead-letter state

Unexpected retryable execution failures keep the existing exponential backoff, but only up to a configurable `max_event_attempts`. When the current attempt reaches that ceiling:

- the event transitions to `DEAD`;
- lease fields are cleared;
- the last error and dead-letter timestamp are persisted;
- an `EVENT_DEAD_LETTERED` journal record is appended;
- an associated running run is moved to `FAILED`.

The default attempt ceiling is 16, deliberately above the previously observed ten-attempt GPU-contention recovery case.

### Explicit dead-letter inspection and redrive

Dead events remain durable and inspectable. Redrive is manual and one event at a time; it resets attempts while preserving event identity and dedup binding.

For a dead `run.step`, the associated run records the exact dead-letter event ID. Redrive may resume the run only when that ID and the run generation still match, preventing accidental resurrection of an unrelated or subsequently changed failed run.

### Compatibility and scope

Existing SQLite databases migrate in place by adding nullable columns. No external database, distributed consensus layer, UI, or provider framework is introduced.

This change does not claim exactly-once external effects. The existing invariant remains:

`REQUEST != AUTHORITY != ATTEMPT != EFFECT != VERIFIED_EFFECT`

## Hostile review

> **HOSTILE REVIEWER:** Lease heartbeats can hide dead workers and delay failover.

**PARTIALLY ACCEPTED.** Renewal is bounded by a maximum extension duration and is valid only for the exact active claim token. A dead worker stops heartbeating and becomes reclaimable; a wedged worker eventually reaches the extension ceiling.

> **HOSTILE REVIEWER:** Dead-lettering can convert a temporary outage into permanent failure.

**PARTIALLY ACCEPTED.** The ceiling is configurable and defaults to 16 attempts, above the repository's historical ten-attempt recovery observation. Dead-letter state preserves the error for operator action instead of deleting work.

> **HOSTILE REVIEWER:** A claim token does not make external effects exactly once.

**ACCEPTED.** It only fences queue ownership. External mutations remain governed by the existing effect ledger and reconciliation barrier.

> **HOSTILE REVIEWER:** A pre-write heartbeat check still permits a stale worker to commit if the lease expires during persistence.

**ACCEPTED.** Run progress is committed inside a claim-validating SQLite transaction with heartbeat revalidation before commit.

> **HOSTILE REVIEWER:** Dead-lettering without redrive just trades an infinite retry loop for manual database surgery.

**ACCEPTED.** Dead events are inspectable and may be explicitly redriven one at a time; run resurrection requires an exact failure binding.

## Claim ceiling

Source-level reliability hardening only. No live Lappy installation, cutover, runtime requalification, or distributed-production guarantee is created by this change.
