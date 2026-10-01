# Pre-Active Durable Run Control V1

Status: stacked implementation target on top of resilience PR #13.

## Goal

Give operators durable pause, resume, and cancel control over a running Pre-Active run without treating process termination as equivalent to stopping an external effect.

## Research basis

Current durable runtimes distinguish orchestration control from already-running activity/effect control:

- Azure Durable Task exposes suspend/resume and terminate at orchestration-instance level, and documents that termination does not necessarily propagate to already-running activities.
- Celery revoke prevents future execution but warns that force-terminating the worker process is a last-resort administrative action rather than a safe task-cancellation primitive.
- AWS Step Functions makes best-effort attempts to cancel integrated work when an execution is stopped but documents that cancellation can fail.
- LangGraph models human intervention as a durable pause/checkpoint that can later resume.

Pre-Active should adopt the invariant, not those platforms.

## Control model

A run may carry a durable control request:

```text
NONE | PAUSE | CANCEL
```

A request records action, reason, and requested timestamp. Writing the request is not proof that execution has stopped.

### Safe-boundary rule

The engine checks control intent:

1. before model inference;
2. after model inference but before admitting a fresh model decision;
3. after loading/admitting a tool-call decision but before tool dispatch;
4. after tool execution returns and before successor scheduling;
5. before final completion.

If pause/cancel is requested before any external tool dispatch for the current generation, no new tool effect is started.

If pause/cancel arrives while a tool call is already executing, Pre-Active does not kill the worker. The tool outcome is first classified and durably recorded. The control request then applies at the next safe boundary.

### Ambiguous-effect precedence

`BLOCKED_EFFECT` outranks pause/cancel completion. If a mutation becomes `ATTEMPTED_UNKNOWN`, the run remains blocked until reconciliation resolves whether the effect occurred.

A pending PAUSE or CANCEL request remains durable while blocked:

- reconciliation confirms effect occurred: persist/replay the reconciled result, then apply pause/cancel instead of scheduling ordinary continuation;
- reconciliation confirms no effect: do not retry the mutation when CANCEL is pending; for PAUSE, enter PAUSED before any retry. Resume may later retry the exact stored request through the existing recovery contract.

No control request may erase unresolved effect evidence.

## States

```text
RUNNING --pause request--> RUNNING(control=PAUSE) --safe boundary--> PAUSED
RUNNING --cancel request-> RUNNING(control=CANCEL) --safe boundary--> CANCELLED
PAUSED  --resume---------> RUNNING
PAUSED  --cancel---------> CANCELLED

BLOCKED_EFFECT --pause/cancel request--> BLOCKED_EFFECT(control=...)
BLOCKED_EFFECT --reconcile--> PAUSED/CANCELLED or ordinary recovery
```

Terminal states `COMPLETED`, `FAILED`, and `CANCELLED` reject new pause/resume/cancel requests except idempotent reads.

## Event handling

For pause before current-generation progress is committed, the exact claimed `run.step` event becomes `PAUSED`. Resume requeues that same event identity.

For pause after a tool result is successfully committed, the current event is consumed and the run generation advances without creating a successor event. Resume creates the successor event for the current generation.

Cancel consumes the current work event into `CANCELLED`. No successor is scheduled.

## Authority boundary

```text
CANCEL_REQUESTED != EXTERNAL_EFFECT_CANCELLED
PAUSE_REQUESTED != WORKER_PROCESS_STOPPED
CONTROL_ACCEPTED != CONTROL_APPLIED
```

Pre-Active provides cooperative durable control, not arbitrary process killing.

## Hostile review

> **HOSTILE REVIEWER:** Directly killing the worker would stop faster.

**REJECTED WITH EVIDENCE.** It can strand or ambiguate external effects and Celery explicitly treats forced process termination as a last-resort administrative operation.

> **HOSTILE REVIEWER:** Queueing pause/cancel as normal events would preserve one event model.

**REJECTED.** A control request could sit behind the work it is intended to stop. Control intent must be a direct durable run-state mutation.

> **HOSTILE REVIEWER:** Cancellation should erase a blocked effect so the user can move on.

**REJECTED.** That converts uncertainty into a false claim. Effect ambiguity must be reconciled before the run can become safely cancelled or paused.

> **HOSTILE REVIEWER:** Pause after a completed tool call may replay the tool on resume.

**ACCEPTED AS A DESIGN HAZARD.** Tool result + generation advance must commit before entering PAUSED; resume starts at the next generation. Pause before dispatch keeps the current event/generation.

## Claim ceiling

Source-level cooperative run control only. This does not prove an already-dispatched external operation was cancelled and does not terminate live host processes.
