# Volition ↔ Pre-Active Bridge V1 Design

## Goal

Connect canonical Volition motive state to Pre-Active's resident event loop so typed Volition signals can durably produce bounded ENDOGENOUS cognition turns without creating effect authority.

## Architecture

Pre-Active remains the durable resident runtime and queue owner. Volition remains the motive/choice/goal engine. V1 introduces a `volition.signal` event that carries an explicit typed Volition Signal; Pre-Active never infers motive semantics from arbitrary event text.

When a claimed signal is processed, the bridge restores the persisted `VOLITION_STATE_V2` snapshot (or creates a fresh engine), applies exactly one signal through `VolitionEngine.tick`, asks Volition for at most one `CognitionRequest`, persists the new snapshot, and—only when a request exists—enqueues one `autonomous.turn` with `source=ENDOGENOUS` and an empty capability set. Snapshot update, cognition enqueue, receipt, journal entry, and source-event acknowledgement occur inside the same fenced event transaction.

## Durable data

`volition_state` stores the current serialized Volition snapshot, revision, and update time.

`volition_signal_receipts` binds one claimed Pre-Active signal event to its resulting state revision and optional cognition event. This makes crash replay and duplicate delivery observable and non-amplifying.

The cognition event receives a deterministic dedup key derived from the source signal event. The receipt also preserves signal provenance/source and active Choice/Goal identifiers.

## Authority boundary

The bridge rejects any input claiming `effect_authority=true`. A Volition `CognitionRequest` must have `source=ENDOGENOUS` and `effect_authority=False`. Generated Pre-Active turns always carry `capabilities=[]`.

Invariant:

`VOLITION_SIGNAL != WANT != CHOICE != GOAL != COGNITION_REQUEST != CAPABILITY != EFFECT_AUTHORITY`

Urgency affects recorded evidence only; it does not alter priority or permissions in V1.

## Budget boundary

Volition's persisted `endogenous_turn_budget` remains authoritative for whether `request_cognition()` returns a request. Because the complete snapshot is persisted after each signal, budget consumption survives process restart. Pre-Active independently retains its existing event/run limits.

## Failure behavior

Malformed signals are rejected and acknowledged as non-retryable input errors. Missing/unimportable Volition runtime is treated as an execution failure so the event follows the existing retry/dead-letter path rather than being silently discarded.

Any crash before the active-claim transaction commits rolls back the snapshot, receipt, generated cognition event, journal, and source acknowledgement together.

## Hostile review

> **HOSTILE REVIEWER:** This can become a second scheduler that amplifies weak events into model calls and bypasses ordinary authority controls.

**Accepted and bounded.** V1 does not infer motive signals, accepts only explicit typed `volition.signal` input, emits at most one cognition request per processed signal, persists Volition's own cognition budget, fixes generated capabilities to the empty set, and leaves Pre-Active as the sole queue/run admission layer.

> **HOSTILE REVIEWER:** Co-installing packages is not integration, and a stored snapshot is not proof the live daemon consumes Volition.

**Accepted.** Completion requires an end-to-end test in which a real Volition signal is consumed by Pre-Active, produces an ENDOGENOUS autonomous event, survives durable readback, and results in a model run while preserving zero capabilities.
