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
