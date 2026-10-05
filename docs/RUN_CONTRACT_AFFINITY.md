# Run Contract Affinity

Pre-Active durable runs outlive individual model calls, daemon processes, and source deployments. A run therefore records the execution contract under which its durable state was created.

    RUN CONTRACT != CURRENT SOURCE VERSION
    SOURCE UPGRADE != RUN MIGRATION
    DURABLE STATE != SAFE TO REINTERPRET

## Contract version

`pre_active.contracts.RUN_CONTRACT_VERSION` is the current durable run-semantics version.

Every new run stores that version in `runs.contract_version`.

The contract version is intentionally not a Git commit SHA or package version. Most source changes do not change durable semantics. Increment it only when an older durable run could be interpreted differently by the new engine.

## Mismatch behavior

When the engine claims an exact `run.step`, it compares the run's stored contract version with the runtime contract version before model inference.

If they differ, Pre-Active fails closed:

    RUNNING / WAITING
           |
           | contract mismatch
           v
    BLOCKED_CONTRACT

The exact claimed event is moved to `PAUSED`, its lease is cleared, and the run records:

- the paused event ID;
- the prior run status (`RUNNING` or `WAITING`);
- the run contract version;
- the runtime contract version in the journal evidence;
- a durable incompatibility error.

No model call occurs after the mismatch is discovered.

## Operator control

A contract-blocked run can still be cancelled. Cancellation also cancels the exact paused event.

`PAUSE` is rejected because the run is already durably blocked.

There is deliberately no generic `resume` or `upgrade-run-to-current` command in this contract version.

## Migration rule

A future change that increments `RUN_CONTRACT_VERSION` must ship an explicit migration path for every older contract version it claims to support.

That migration must define:

- which durable fields/events are transformed;
- the allowed source contract version(s);
- the exact target contract version;
- how RUNNING versus WAITING resume semantics are preserved;
- how the paused exact event is rebound or replaced;
- rollback/failure behavior;
- verification proving the migrated run is semantically equivalent enough to continue.

Until such a migration is implemented, an older run remains `BLOCKED_CONTRACT` or may be cancelled.

This avoids a fake safety mechanism where an operator merely changes a version number and silently asks new code to reinterpret old durable state.

## CLI

Inspect one run:

    pre-active --state .pre-active/state.db contract show <run-id>

The output includes run/runtime contract versions, compatibility, status, the blocked event ID, and the prior resume state.

`pre-active ... status` also exposes the current runtime contract version.

## Relationship to verified progress

Contract affinity and verified progress solve different problems.

- contract affinity asks whether this runtime may safely interpret this durable run;
- verified progress asks which claims about task advancement have actually been accepted.

A future migration may use verified checkpoints as evidence for safe reconstruction, but a VERIFIED checkpoint does not itself authorize a contract migration.
