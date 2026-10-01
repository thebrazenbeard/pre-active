# Pre-Active V1 Architecture

Status: executable V1 candidate.

## Purpose

Pre-Active is a host-side continuity and execution kernel for stateless language-model inference. It owns durable orchestration state; a model remains a replaceable reasoning component. A host may run Pre-Active continuously, wake it from schedules or external events, and bind tool adapters to real systems without treating model output as effect authority.

The architecture is intentionally self-contained. Portfolio repositories influenced its invariants, but Pre-Active has no sibling-repository runtime dependency.

## Components

### Durable store

`pre_active.store.Store` uses SQLite in WAL mode. It persists:

- event queue entries and worker leases;
- task runs, source-event bindings, idempotently keyed durable transcripts, and per-step model decisions;
- memory records with salience;
- interval schedules;
- append-only journal entries.

Queue work is claimed under `BEGIN IMMEDIATE`. An expired lease can be reclaimed by another worker. A lease is not ownership forever; it is a bounded execution claim. Event deduplication keys are durably bound to canonical kind + payload + priority; reusing a key for different intent fails closed.

### Scheduler

`pre_active.scheduler.Scheduler` emits due schedule occurrences into the same durable queue used by external events. Each occurrence has a stable deduplication key derived from schedule ID and due timestamp, so rerunning a scheduler tick does not create duplicate occurrences.

### Engine

`pre_active.engine.Engine` implements the persistent ReAct-style loop:

1. claim one durable event;
2. atomically create an idempotently source-bound durable run plus its initial `run.step`, or load an existing `run.step`;
3. assemble bounded context from system prompt, current task, durable transcript, and relevant memory;
4. load the already-admitted model decision for this run generation, or ask the model for exactly one of: final text or one structured tool call and persist that decision before dispatch;
5. pass tool calls through bounded JSON-Schema-compatible validation, capability admission, and the effect ledger;
6. persist the result under a stable per-step transcript key;
7. atomically advance the run generation and schedule its successor event, or close the run. Effect-recovery resumes use the same atomic advance + successor rule.

A run has these operational states:

```text
RUNNING -> COMPLETED
   |
   +----> FAILED
   |
   +----> BLOCKED_EFFECT -> RUNNING  (only after reconciliation)
```

`max_steps` bounds runaway tool/reasoning cycles.

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
PENDING --claim--> CLAIMED --ack--> DONE
   ^                  |
   |                  +--failure--> PENDING at retry_at
   |                  |
   +---lease expiry---+
```

Claim priority is deterministic: higher `priority`, then older creation time.

## Crash model

Pre-Active is designed around process death at arbitrary points:

- death before queue claim: event remains pending;
- death after claim but before completion: lease expires and event becomes reclaimable;
- death during initial submission: run creation and initial-step scheduling roll back together;
- death after `task.requested` creates a run but before source-event ACK: redelivery reuses the same source-bound run;
- death after model inference: the persisted run-step decision is reused instead of asking the model to mint a replacement tool request;
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

- provider/model failure: retryable through the queue with bounded exponential delay;
- daemon cycle exception: reported to stderr and polling continues after the engine has durably classified/requeued the work; `KeyboardInterrupt`/`SystemExit` still terminate normally;
- malformed `task.requested` envelope: terminally journaled as `EVENT_REJECTED` and acknowledged rather than retried forever;
- deterministic tool admission failure: reported to the run; host should repair configuration/input rather than blindly expand authority;
- ambiguous mutation: fail closed into `BLOCKED_EFFECT`;
- max-step exhaustion: deterministic run failure;
- expired lease: recoverable queue ownership loss.

## Extension seams

Hosts can extend Pre-Active through:

- custom `ModelAdapter` implementations;
- custom `ToolRegistry` registrations;
- external producers that insert normalized events;
- richer memory retrieval behind the same context boundary;
- alternate durable stores, provided they preserve lease, idempotency, and recovery semantics;
- distributed schedulers that preserve schedule occurrence identity.
