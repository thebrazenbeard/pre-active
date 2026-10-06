# Volition ↔ Pre-Active Bridge V1 Implementation Plan

> **For agentic workers:** Use the host's available task-by-task implementation workflow. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a durable, idempotent Volition-to-Pre-Active cognition bridge that turns explicit typed Volition signals into zero-capability ENDOGENOUS model turns.

**Architecture:** Add durable Volition snapshot/receipt storage to `Store`, a focused `VolitionBridge` module that owns typed signal parsing and Volition state transitions, and one Engine event branch for `volition.signal`. The existing `autonomous.turn` queue path remains the only way the bridge grants a model turn.

**Tech Stack:** Python 3.11+, SQLite, pytest, Volition `VOLITION_STATE_V2`, existing Pre-Active Engine/Store event queue.

## Global Constraints

- Explicit typed signals only; no motive inference from arbitrary prose.
- Generated cognition source is always `ENDOGENOUS`.
- Generated cognition capabilities are always empty.
- Input/output claiming `effect_authority=true` fails closed.
- Volition snapshot, receipt, cognition event, and source-event acknowledgement commit atomically.
- Duplicate/replayed source events cannot create duplicate cognition events.
- Volition's persisted endogenous cognition budget survives restart.
- Volition remains an optional runtime package: ordinary Pre-Active behavior must continue when no `volition.signal` event is processed.

---

### Task 1: Durable Volition snapshot and receipts

**Files:**
- Modify: `src/pre_active/store.py`
- Test: `tests/test_volition_bridge.py`

**Interfaces:**
- Produces: `Store.get_volition_state()`, `Store.save_volition_state(...)`, `Store.get_volition_signal_receipt(...)`, `Store.record_volition_signal_receipt(...)`

- [ ] **Step 1: Add the focused failing test**
- [ ] **Step 2: Verify the relevant failure**
- [ ] **Step 3: Implement the minimum behavior**
- [ ] **Step 4: Verify the focused pass**
- [ ] **Step 5: Run the affected integration check**
- [ ] **Step 6: Commit the passing deliverable**

### Task 2: Strict Volition adapter and idempotent cognition enqueue

**Files:**
- Create: `src/pre_active/volition_bridge.py`
- Modify: `tests/test_volition_bridge.py`

**Interfaces:**
- Consumes: Store Volition state/receipt methods, real or injected Volition engine types.
- Produces: `VolitionBridge.parse_signal_payload(...)`, `VolitionBridge.process_claimed_signal(...)`

- [ ] **Step 1: Add failing tests for valid request, no-request budget state, replay, provenance, and authority rejection**
- [ ] **Step 2: Verify the relevant failures**
- [ ] **Step 3: Implement the minimum adapter**
- [ ] **Step 4: Verify the focused pass**
- [ ] **Step 5: Run Store + bridge integration tests**
- [ ] **Step 6: Commit the passing deliverable**

### Task 3: Engine and CLI integration

**Files:**
- Modify: `src/pre_active/engine.py`
- Modify: `src/pre_active/cli.py`
- Modify: `tests/test_volition_bridge.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: `volition.signal` events and `VolitionBridge`.
- Produces: CLI signal ingestion and daemon processing through the existing `autonomous.turn` path.

- [ ] **Step 1: Add failing Engine/CLI end-to-end tests**
- [ ] **Step 2: Verify the relevant failures**
- [ ] **Step 3: Implement event dispatch and CLI ingestion**
- [ ] **Step 4: Verify the focused pass**
- [ ] **Step 5: Run full regression and a real Volition integration smoke**
- [ ] **Step 6: Commit the passing deliverable**

### Task 4: Hostile review and release evidence

**Files:**
- Create: `docs/reviews/2026-10-06-volition-preactive-bridge-hostile-review.md`
- Modify: `docs/plans/2026-10-06-volition-preactive-bridge.md`

**Interfaces:**
- Consumes: exact branch head, test evidence, live integration evidence.
- Produces: bounded claim/evidence record and completed implementation ledger.

- [ ] **Step 1: Attack authority, replay, crash-consistency, dependency, and amplification assumptions**
- [ ] **Step 2: Fix any surviving defect with a red-green regression**
- [x] **Step 3: Run full tests**
- [ ] **Step 4: Push branch and open Draft PR without merging**
