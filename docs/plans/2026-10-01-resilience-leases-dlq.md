# Resilience Leases and Dead-Lettering Implementation Plan

> **For agentic workers:** Use the host's available task-by-task implementation workflow. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent healthy long-running work from losing queue ownership while ensuring stale workers and permanently failing events cannot make unbounded progress.

**Architecture:** Add per-claim fencing tokens and bounded lease renewal around blocking engine work, then bound the existing retry loop with a durable `DEAD` state. Preserve SQLite/WAL, current run/effect semantics, and the self-contained runtime.

**Tech Stack:** Python 3.11+, sqlite3/WAL, threading, pytest.

## Global Constraints

- Keep `pre-active` self-contained.
- Preserve the existing external-effect ambiguity barrier.
- Do not introduce distributed consensus or claim exactly-once effects.
- Existing state databases must migrate in place.
- Default max attempts must exceed the previously observed ten-attempt backend recovery.
- No live host install/cutover is part of this work.

---

### Task 1: Fenced event claims

**Files:**
- Modify: `src/pre_active/store.py`
- Modify: `tests/test_event_queue.py`

**Interfaces:**
- Produces: `Event.lease_token: str | None`
- Produces: `Store.renew_event_lease(event_id, *, worker_id, lease_token, now, lease_seconds) -> float`
- Changes: `Store.ack_event(..., lease_token, ...)`
- Changes: `Store.fail_event(..., lease_token, ...)`

- [ ] Add tests proving reclaim rotates the token and stale-token ack/fail/renew are rejected.
- [ ] Observe focused failure.
- [ ] Add `lease_token` migration and token-aware claim/renew/ack/fail behavior.
- [ ] Run focused queue tests.
- [ ] Run full suite.

### Task 2: Bounded lease heartbeat

**Files:**
- Create: `src/pre_active/lease.py`
- Modify: `src/pre_active/engine.py`
- Modify: `src/pre_active/cli.py`
- Test: `tests/test_lease.py`
- Test: `tests/test_engine.py`

**Interfaces:**
- Produces: `LeaseHeartbeat` context manager with `assert_owned()`
- Engine consumes `lease_heartbeat_seconds` and `max_lease_extension_seconds`.

- [ ] Add tests proving a long model call retains ownership and a stale/lost claim blocks further progress.
- [ ] Observe focused failure.
- [ ] Implement dedicated-connection heartbeat with bounded extension lifetime.
- [ ] Assert ownership after blocking model/tool boundaries before committing new run progress.
- [ ] Expose validated CLI controls.
- [ ] Run focused and full tests.

### Task 3: Bounded retry and durable dead-lettering

**Files:**
- Modify: `src/pre_active/store.py`
- Modify: `src/pre_active/engine.py`
- Modify: `src/pre_active/cli.py`
- Modify: `tests/test_event_queue.py`
- Modify: `tests/test_engine.py`

**Interfaces:**
- Changes: `Store.fail_event(..., max_attempts, error) -> bool`, returning whether the event was dead-lettered.
- Engine consumes `max_event_attempts`, default 16.

- [ ] Add tests proving retries below the ceiling remain pending and the ceiling transitions the event to `DEAD` with error evidence.
- [ ] Add engine test proving a dead-lettered `run.step` makes the run `FAILED`.
- [ ] Observe focused failure.
- [ ] Add `last_error` and `dead_lettered_at` migrations and journal transition.
- [ ] Wire engine and CLI.
- [ ] Run focused and full tests.

### Task 4: Documentation and regression verification

**Files:**
- Modify: `README.md`
- Modify: `docs/ARCHITECTURE.md`
- Modify: `docs/EFFECT_AND_RECOVERY.md`

**Interfaces:**
- Documents exact lease/dead-letter semantics and non-promotions.

- [ ] Document fenced claims, bounded renewal, attempt ceiling, and `DEAD` state.
- [ ] Run `python -m compileall -q src`.
- [ ] Run `pytest` on Python 3.11/3.12/3.13 through repository CI.
- [ ] Verify exact branch head and changed-file set before Draft PR readiness classification.

## Unresolved externally observable decisions

None. The user delegated selection and implementation of the strongest evidence-supported reliability improvement.
