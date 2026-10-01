# Runtime contract

Use this reference when operating a workstation-bound Pre-Active runtime.

## Expected current binding

The current source configuration expects:

- runtime root: `C:\ProgramData\PreActive`
- state database: `C:\ProgramData\PreActive\state\state.db`
- Python entrypoint: `python -m pre_active`
- local model endpoint: `http://127.0.0.1:18081/v1`
- scheduled-task prefix: `PreActive`

These are expectations from source, not proof of live installation.

## Discovery sequence

Prefer a read-only sequence:

1. Test whether the expected root exists.
2. Enumerate scheduled tasks whose names indicate the current runtime.
3. Inspect the process bound to the configured model port.
4. Inspect process command lines for the actual package/module and state path.
5. Run the installed runtime's status command only after the installed package and state path are known.

If the expected root is absent but a model endpoint or legacy daemon is active, classify the host as not yet migrated to the current source binding.

## Safe status semantics

A status response may establish durable queue/run state. It does not establish that:

- a model backend is healthy unless checked separately;
- a daemon is alive unless checked separately;
- a registered tool has authority to mutate an external system;
- a mutation actually occurred merely because a run completed.

## Effect discipline

For read-only inspection, prefer existing tools that cannot mutate state. For durable queue writes, bind the user's exact request and capability set. For external effects, preserve idempotency identity and require readback or reconciliation when outcome is ambiguous.

## Failure reporting

Report the narrowest observed failure. Examples:

- expected runtime root missing;
- daemon absent;
- model endpoint unavailable;
- durable event pending;
- run blocked on an ambiguous effect;
- runtime still bound to a predecessor installation.

Do not upgrade one failure into a claim that the whole machine or repository is broken.
