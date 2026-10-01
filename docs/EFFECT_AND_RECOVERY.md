# Effect and Recovery Contract

## Rule zero

Never turn uncertainty into a retry decision by assumption.

For a mutating tool call, these are separate facts:

```text
REQUEST
AUTHORITY
ATTEMPT
EFFECT
VERIFIED_EFFECT
```

No item silently proves the next.

## Mutation admission

Before invoking a mutating handler, Pre-Active persists:

- `request_id`;
- tool name;
- canonical request JSON;
- request SHA-256;
- state `EXECUTING`.

That write happens before handler dispatch. If the ledger cannot admit the request, the mutation is not invoked.

## Idempotent replay

If the same `request_id` reappears:

- same tool + same canonical input + `COMMITTED` -> return stored result, do not invoke handler;
- different tool or input -> `IdempotencyConflict`;
- unresolved state -> `AmbiguousEffect`;
- `RECONCILED_NO_EFFECT` -> the exact persisted request may be retried.

A request ID is therefore an effect identity, not a casual correlation ID.

## Run-step decision binding

Mutation idempotency is not sufficient if a retried run step can ask the model to generate a different request ID. Pre-Active therefore persists the exact model decision for `(run_id, step)` before any tool dispatch. A reclaimed/retried step reuses that decision verbatim.

This closes the crash window:

```text
model emits mutation A / request_id=A1
-> mutation A commits externally
-> process dies before run generation advances
-> old event is redelivered
-> persisted decision A1 is reused
-> committed result is replayed, not re-executed
```

The run generation and successor `run.step` event are also advanced in one SQLite transaction. A failure to schedule the successor cannot leave a run durably advanced with no event capable of continuing it. The same rule applies when a `BLOCKED_EFFECT` run resumes after reconciliation.

Assistant decisions, tool results, and reconciled-effect results use stable transcript keys. If a crash occurs after a message is durable but before the step transition commits, replay verifies/reuses the same transcript entry instead of appending duplicate history.

## Ambiguous outcome

A handler exception is conservatively treated as an unknown outcome because the handler may have dispatched an external effect before the local failure became visible.

The ledger records `ATTEMPTED_UNKNOWN`. The engine moves the run to `BLOCKED_EFFECT` and removes automatic queue pressure for that run.

This prevents this unsafe sequence:

```text
mutation dispatched
-> response lost
-> event retried
-> model emits a fresh request ID
-> mutation happens twice
```

## Reconciliation

A host calls `ToolRegistry.reconcile` with a SHA-256 digest of external reconciliation evidence and one of two claims.

### Effect confirmed

`effect_occurred=True` requires a result object. Pre-Active stores that result and moves the request to `COMMITTED`. Resuming the run replays the committed result into the durable transcript without repeating the handler.

### No effect confirmed

`effect_occurred=False` forbids a result object. Pre-Active moves the request to `RECONCILED_NO_EFFECT`. Resuming the run invokes the **exact stored request ID, tool name, and arguments**. It does not ask the model to formulate a replacement call first.

## Evidence ceiling

The reconciliation evidence digest proves only that the host supplied a binding to some external evidence. Pre-Active V1 does not independently validate the truth of that evidence. A production host should bind reconciliation to a trusted readback/verifier appropriate to the target system.

## Host responsibilities

A tool adapter that mutates an external system should prefer:

- target-native idempotency keys when available;
- compare-and-swap or generation checks;
- transaction boundaries;
- exact target identifiers;
- bounded timeouts;
- readback after write;
- reconciliation probes that are independent from the original response path.

Do not use the Pre-Active request ledger as a substitute for target-native transactional guarantees when those exist.


## Queue ownership is separate from effect authority

Event leases coordinate which worker may advance durable queue/run state. Every claim gets a fresh fencing token, and acknowledgement, retry/dead-letter transition, and renewal require that exact token. A stale worker that loses its lease cannot later acknowledge or reschedule the reclaimed event.

For blocking model/tool work, a bounded heartbeat may extend the queue lease while the exact claim remains active. The heartbeat stops extending after its configured maximum duration. This is a liveness mechanism only:

```text
QUEUE_LEASE != TOOL_CAPABILITY
QUEUE_LEASE != EFFECT_AUTHORITY
QUEUE_LEASE != VERIFIED_EFFECT
```

Mutation safety continues to come from the effect ledger, stable model-decision binding, and reconciliation barrier described above.

### Fenced durable progress

Heartbeat checks alone are not treated as sufficient because a lease can expire between a check and a durable write. Fresh model-decision admission and run-progress commits therefore occur inside a SQLite write transaction that first validates the exact event ID, worker ID, fencing token, and unexpired lease. The heartbeat ceiling is rechecked before commit.

For a successful tool step, the tool-result transcript, run-generation advance, successor event, and acknowledgement of the current event commit together under that fence. Final-text completion and acknowledgement likewise commit together. This is queue/run fencing; it does not convert a tool result into verified external effect evidence.

## Retry exhaustion and dead letters

Transient engine failures retry with bounded exponential delay until the configured event-attempt ceiling. At the ceiling, the exact claimed event moves atomically to `DEAD`, clears its lease, records the last error and dead-letter timestamp, and appends `EVENT_DEAD_LETTERED` evidence. An associated `RUNNING` run moves to `FAILED`.

Dead-lettering does not assert that an external mutation failed or did not occur. If a mutation outcome is ambiguous, the existing `BLOCKED_EFFECT` path takes precedence; that state must still be reconciled from external evidence rather than converted into a retry/dead-letter assumption.


## Dead-letter inspection and redrive

A `DEAD` event is durable operator evidence, not deletion. `pre-active dead` lists dead events with their payload, attempt count, last error, and dead-letter timestamp.

Redrive is explicit and one event at a time:

```text
DEAD --operator redrive--> PENDING
```

Redrive resets the event attempt count and retry evidence fields while preserving the event ID, payload, priority, and dedup binding. It appends `EVENT_REDRIVEN` with the prior attempts/error/dead-letter timestamp.

For `run.step` events, Pre-Active also records the exact dead-letter event ID on the failed run. A redrive may move that run back to `RUNNING` only when all of these still match: the run is `FAILED`, its `failed_event_id` is the redriven event, and the event's step equals the run's current generation. This prevents a generic dead event from resurrecting an unrelated or subsequently changed run.

Redrive does not bypass `BLOCKED_EFFECT`. Ambiguous mutations still require reconciliation evidence before any retry.


## Cooperative run control

Pause and cancel are durable orchestration controls, not external-effect controls:

```text
CANCEL_REQUESTED != EXTERNAL_EFFECT_CANCELLED
PAUSE_REQUESTED != WORKER_PROCESS_STOPPED
CONTROL_ACCEPTED != CONTROL_APPLIED
```

For a `RUNNING` run, the engine applies pending PAUSE/CANCEL at fenced safe boundaries. Before tool dispatch, control prevents a new tool call from starting. If a tool handler is already executing, Pre-Active does not kill the process; the operation is allowed to return or enter the existing ambiguity path, and control applies after its result is durably classified.

`BLOCKED_EFFECT` has precedence over control. A PAUSE/CANCEL request may be recorded while the run is blocked, but it cannot erase `ATTEMPTED_UNKNOWN` or make the run terminal before reconciliation.

After reconciliation:

- confirmed effect: the reconciled result is durably recorded, the run generation advances once, and pending PAUSE/CANCEL is then applied;
- confirmed no effect + CANCEL: the exact mutation is not retried; the run becomes `CANCELLED`;
- confirmed no effect + PAUSE: the exact mutation is not retried; the run becomes `PAUSED`; a later resume reuses the already-admitted model decision and exact persisted request identity.

This preserves the effect contract while giving operators durable control over future orchestration progress.
