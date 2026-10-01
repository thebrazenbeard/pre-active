# Durable Run Control Implementation Plan

> **For agentic workers:** Use the host's available task-by-task implementation workflow. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add durable pause, resume, and cancel semantics that stop future Pre-Active progress at safe boundaries without falsifying external-effect state.

**Architecture:** Persist control intent on each run, let the engine apply it cooperatively at fenced execution boundaries, and add PAUSED/CANCELLED event/run states. Preserve `BLOCKED_EFFECT` precedence and exact effect reconciliation.

**Tech Stack:** Python 3.11+, sqlite3/WAL, pytest.

## Global Constraints

- Branch is stacked on resilience PR #13 exact head `c4addb123ca5dd2b282c9b446b36273c439f1edd`.
- Do not merge or install/cut over live Lappy runtime.
- Never claim cancellation stopped an external mutation already dispatched.
- Never bypass `BLOCKED_EFFECT` or reconciliation.
- Existing databases migrate in place.
- Pause/resume/cancel must be journaled and idempotent where safe.

---

### Task 1: Durable run-control state

**Files:**
- Modify: `src/pre_active/store.py`
- Modify: `tests/test_engine.py`
- Modify: `tests/test_cli.py`

**Interfaces:**
- Produces `Store.request_run_control(run_id, action, reason, now)`.
- Produces `Store.resume_paused_run(run_id, reason, now)`.
- Extends `Store.get_run()` with control metadata.

- [ ] Add failing tests for PAUSE/CANCEL request persistence and terminal-state rejection.
- [ ] Add SQLite migrations for control action/reason/timestamp and paused event binding.
- [ ] Journal `RUN_CONTROL_REQUESTED`, `RUN_PAUSED`, `RUN_RESUMED`, and `RUN_CANCELLED`.

### Task 2: Engine safe-boundary control

**Files:**
- Modify: `src/pre_active/engine.py`
- Modify: `src/pre_active/store.py`
- Test: `tests/test_engine.py`

**Interfaces:**
- Engine checks durable control before model work, before tool dispatch, and after tool return.
- Store applies pause/cancel atomically under the active event claim.

- [ ] Test pause before inference prevents model invocation.
- [ ] Test cancel during model inference discards returned decision and cancels before tool dispatch.
- [ ] Test pause/cancel arriving during a tool call lets the call finish, records its result, then stops before successor work.
- [ ] Ensure paused pre-dispatch events resume with the same event identity/generation.
- [ ] Ensure post-tool pause advances generation once and resumes at the successor generation.

### Task 3: Ambiguous-effect precedence

**Files:**
- Modify: `src/pre_active/tools.py`
- Modify: `src/pre_active/engine.py`
- Test: `tests/test_engine.py`
- Test: `tests/test_tools.py`

**Interfaces:**
- Add a non-dispatching reconciled-effect inspection seam so CANCEL/PAUSE can honor reconciliation without accidentally retrying `RECONCILED_NO_EFFECT`.
- `Engine.resume_blocked_effect` applies pending control after reconciliation.

- [ ] Test CANCEL during `BLOCKED_EFFECT` remains blocked before reconciliation.
- [ ] Test confirmed effect records result then cancels.
- [ ] Test confirmed no-effect + CANCEL does not invoke the mutation handler.
- [ ] Test confirmed no-effect + PAUSE pauses before retry; resume preserves exact stored request identity.

### Task 4: Operator CLI and documentation

**Files:**
- Modify: `src/pre_active/cli.py`
- Modify: `README.md`
- Modify: `docs/ARCHITECTURE.md`
- Modify: `docs/EFFECT_AND_RECOVERY.md`
- Test: `tests/test_cli.py`

**Interfaces:**
- `pre-active pause <run-id> [--reason ...]`
- `pre-active resume <run-id> [--reason ...]`
- `pre-active cancel <run-id> [--reason ...]`

- [ ] Add CLI round-trip tests.
- [ ] Document cooperative-control and external-effect ceilings.
- [ ] Run package build, compileall, and pytest across Python 3.11/3.12/3.13.
- [ ] Verify stacked diff against PR #13 head and open a Draft PR targeting the resilience branch.
