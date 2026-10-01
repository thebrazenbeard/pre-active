# Pre-Active V1 Architecture

Status: executable V1 candidate.

## Purpose

Pre-Active is a host-side continuity and execution kernel for stateless language-model inference. It owns durable orchestration state; a model remains a replaceable reasoning component. A host may run Pre-Active continuously, wake it from schedules or external events, and bind tool adapters to real systems without treating model output as effect authority.

The architecture is intentionally self-contained. Portfolio repositories influenced its invariants, but Pre-Active has no sibling-repository runtime dependency.

## Components

### Durable store

`pre_active.store.Store` uses SQLite in WAL mode. It persists:

- event queue entries and worker leases;
- task runs, source-event bindings, dead-letter failure bindings, idempotently keyed durable transcripts, and per-step model decisions;
- memory records with salience;
- interval schedules;
- append-only journal entries.

Queue work is claimed under `BEGIN IMMEDIATE`. Every successful claim receives a fresh opaque fencing token. Acknowledgement, retry/dead-letter transition, and renewal require the exact event ID + worker ID + fencing token, so a stale worker cannot complete a claim after another worker reclaims it. An expired lease can be reclaimed by another worker with a new token. A lease is not ownership forever; it is a bounded execution claim. Event deduplication keys are durably bound to canonical kind + payload + priority; reusing a key for different intent fails closed.

`pre_active.lease.LeaseHeartbeat` renews the exact active claim on a dedicated SQLite connection while model/tool work can block longer than the original lease. Renewal is bounded by `max_lease_extension_seconds`; reaching the ceiling or losing the exact claim makes the heartbeat fail closed. Lease ownership is queue coordination only and does not confer capability or effect authority.

### Scheduler

`pre_active.scheduler.Scheduler` emits due schedule occurrences into the same durable queue used by external events. Each occurrence has a stable deduplication key derived from schedule ID and due timestamp, so rerunning a scheduler tick does not create duplicate occurrences.

### Engine

`pre_active.engine.Engine` implements the persistent ReAct-style loop:

1. claim one durable event;
2. atomically create an idempotently source-bound durable run plus its initial `run.step`, or load an existing `run.step`;
3. assemble bounded context from system prompt, current task, durable transcript, and relevant memory;
4. load the already-admitted model decision for this run generation, or ask the model for exactly one of: final text or one structured tool call; durable admission of a fresh decision occurs only inside a transaction that proves the exact event claim is still active;
5. pass tool calls through bounded JSON-Schema-compatible validation, capability admission, and the effect ledger;
6. persist tool/final results under stable per-step transcript keys;
7. under the same fenced claim transaction, atomically commit the run-generation transition, successor event (when needed), and current-event acknowledgement. Effect-recovery resumes use the same atomic advance + successor rule.

A run has these operational states:

```text
RUNNING -> COMPLETED
   |
   +----> FAILED
   |
   +----> PAUSED -> RUNNING
   |         |
   |         +----> CANCELLED
   |
   +----> CANCELLED
   |
   +----> BLOCKED_EFFECT -> RUNNING / PAUSED / CANCELLED
                              (only after reconciliation)
```

Pause and cancel are durable cooperative control. The operator writes control intent directly to run state so it cannot queue behind the work it is intended to stop. The engine checks that intent before inference, after inference before fresh decision admission, before tool dispatch, after tool return, and before final completion. A pre-dispatch pause retains the exact current event/generation; resume requeues that same event. A pause after a completed tool result advances the generation exactly once without scheduling the successor until resume.

A cancel request does not terminate a worker process or prove an already-dispatched external operation stopped. If control arrives while a tool is executing, its result is classified first. An ambiguous mutation remains `BLOCKED_EFFECT`; reconciliation outranks pause/cancel. `RECONCILED_NO_EFFECT` plus pending control does not re-dispatch the mutation merely to complete recovery.

If a PAUSE request is still pending on a `RUNNING` run and has not reached a safe boundary, `resume` may atomically withdraw that pending PAUSE without disturbing the current event. A pending CANCEL cannot be withdrawn through resume.

`max_steps` bounds runaway tool/reasoning cycles. `max_event_attempts` bounds transient retry loops; the default is 16 attempts, deliberately above the historical ten-attempt backend-recovery observation recorded for the Windows runtime.

### Context assembler

`pre_active.context.ContextAssembler` applies a strict character budget. It preserves the system instruction and current task before optional durable memory. Memory is selected by simple lexical relevance plus stored salience. V1 deliberately keeps this selection deterministic and inspectable rather than hiding retrieval behind an opaque agent framework.

### Model boundary

The core depends only on the `ModelAdapter` protocol. `OpenAICompatibleAdapter` is a minimal implementation for chat-completions-compatible endpoints.

Pre-Active constrains provider output to one tool call per turn. This is not a claim that parallel work is always wrong; it is a deliberate effect-ordering boundary. A higher layer can decompose independent work into multiple durable events/runs instead of issuing concurrent unjournaled mutations from one inference response.

### Tool registry and effect ledger

A `ToolSpec` declares:

- tool name;
- description;
- input schema;
- required capability;
- whether the operation is a mutation.

Tool arguments are validated against a fail-closed JSON-Schema-compatible subset before any handler or effect-ledger admission. V1 supports ordinary type, enum/const, object/property/required/additional-property, array/item/uniqueness, string length/pattern, numeric bound/multiple, and allOf/anyOf/oneOf/not constraints. Unsupported keywords are rejected when a tool is registered rather than silently treated as enforced. Read-only calls then execute directly after capability admission. Mutations first write a durable request record containing canonical request identity. The ledger then records execution state and result identity.

The effect state machine is:

```text
                  success
  EXECUTING --------------------> COMMITTED
      |
      | exception / lost outcome
      v
  ATTEMPTED_UNKNOWN
      |              |
      | no effect    | effect confirmed + result
      v              v
  RECONCILED_     COMMITTED
  NO_EFFECT
      |
      | exact stored request retry
      v
  EXECUTING
```

A committed request with the same request ID and digest returns the stored result without invoking the handler again. Reusing the same request ID with different input fails as an idempotency conflict.

### Effect recovery barrier

If a mutation handler fails after durable admission, Pre-Active cannot infer whether the outside world changed. The engine therefore moves the run to `BLOCKED_EFFECT`, acknowledges the queue event, and stops automatic progress for that run.

Recovery requires external evidence passed to `ToolRegistry.reconcile`. If no effect occurred, `Engine.resume_blocked_effect` retries the exact persisted request. If the effect did occur, the reconciled committed result is replayed. In either case the model is not invited to invent a substitute mutation before the ambiguity is resolved.

See `docs/EFFECT_AND_RECOVERY.md`.

## Event state machine

```text
PENDING --claim(token N)--> CLAIMED --ack(token N)--> DONE
   ^                          |  |
   |                          |  +--attempt ceiling--> DEAD
   |                          |                         |
   |                          |                         +--explicit redrive--> PENDING
   |                          |
   |                          +--failure below ceiling--> PENDING at retry_at
   |                          |
   +----lease expiry / reclaim with fresh token----------+
```

A heartbeat may extend an unexpired matching claim, but only up to the configured maximum extension duration. Durable model decisions and run-progress commits revalidate the exact claim while holding the SQLite write lock, so a stale worker cannot advance a run after ownership is lost. `DEAD` events retain `last_error`, `dead_lettered_at`, attempt count, and an `EVENT_DEAD_LETTERED` journal record. Redrive is explicit and single-event; it resets attempts while preserving event identity and dedup binding. A dead `run.step` resumes only when its event ID is bound as the exact cause of the exact failed run generation. Claim priority remains deterministic: higher `priority`, then older creation time.

## Crash model

Pre-Active is designed around process death at arbitrary points:

- death before queue claim: event remains pending;
- death after claim but before completion: lease expires and event becomes reclaimable;
- death during initial submission: run creation and initial-step scheduling roll back together;
- death after `task.requested` creates a run but before source-event ACK: redelivery reuses the same source-bound run;
- death after model inference: a decision is admitted only while the exact event claim is valid; once admitted, the persisted run-step decision is reused instead of asking the model to mint a replacement tool request;
- death after a read-only call: the same admitted model decision can be retried;
- death after mutation admission but before committed result: effect remains unresolved and blocks blind replay;
- death after a committed mutation but before step advancement: the same persisted request ID replays the stored result;
- failure while scheduling a normal or effect-recovery successor step: run-generation advancement rolls back in the same SQLite transaction;
- retry after transcript persistence: stable per-step message keys reuse the same assistant/tool entry instead of duplicating history;
- death between model turns: run transcript, model decisions, and next-step events are durable.

V1 uses one SQLite database and therefore assumes a filesystem/storage setup where SQLite/WAL semantics are valid. Distributed multi-database consensus is outside scope.

## Authority model

Capabilities are explicit strings attached to a run. A tool appears to the model only when its capability is granted, and execution checks the same capability again. This is defense in depth, not a universal policy engine.

A host should keep these layers separate:

```text
model request
    != host capability grant
    != dispatch attempt
    != external effect
    != independently verified effect
```

Tool adapters are responsible for target-specific authorization, sandboxing, authentication, and readback beyond the generic Pre-Active ledger.

## Determinism and canonical identity

Mutation request identity uses canonical JSON (`sort_keys=True`, compact separators, UTF-8) and SHA-256. The semantic request bound to a mutation idempotency key is `{tool, arguments}`. Queue dedup keys bind canonical event kind + payload + priority; a conflicting reuse is rejected. Scheduling dedupe derives those keys from schedule ID + due occurrence.

V1 does not claim canonical JSON interoperability with every language/runtime. Cross-language protocols should define a dedicated canonicalization profile before treating digests as portable cryptographic identities.

## Failure classes

Pre-Active distinguishes:

- provider/model failure: retryable through the queue with bounded exponential delay and a configurable attempt ceiling; exhaustion dead-letters the event and fails an associated running run;
- daemon cycle exception: reported to stderr and polling continues after the engine has durably classified/requeued the work; `KeyboardInterrupt`/`SystemExit` still terminate normally;
- malformed `task.requested` envelope: terminally journaled as `EVENT_REJECTED` and acknowledged rather than retried forever;
- deterministic tool admission failure: reported to the run; host should repair configuration/input rather than blindly expand authority;
- ambiguous mutation: fail closed into `BLOCKED_EFFECT`;
- max-step exhaustion: deterministic run failure;
- expired lease: recoverable queue ownership loss; a reclaim rotates the fencing token, and fenced run-progress transactions prevent the stale worker from committing durable progress;
- dead-letter exhaustion: terminal automatic retry state; an operator may inspect and explicitly redrive one exact event after correcting the underlying condition;
- operator pause: cooperative safe-boundary transition to `PAUSED`, preserving the exact current generation;
- operator cancel: cooperative terminal transition to `CANCELLED`; it is not evidence that an already-dispatched external operation was cancelled;
- control during ambiguous mutation: `BLOCKED_EFFECT` remains authoritative until reconciliation resolves the effect outcome.

## Extension seams

Hosts can extend Pre-Active through:

- custom `ModelAdapter` implementations;
- custom `ToolRegistry` registrations;
- external producers that insert normalized events;
- richer memory retrieval behind the same context boundary;
- alternate durable stores, provided they preserve lease, idempotency, and recovery semantics;
- distributed schedulers that preserve schedule occurrence identity.
