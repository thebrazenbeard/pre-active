# Temporal Initiative Models V1 Implementation Plan

> **For agentic workers:** Use the host's available task-by-task implementation workflow. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Add a durable, research-grounded initiative-policy seam to Pre-Active observers and ship an exponential-Hawkes threshold policy without changing default observer behavior.

**Architecture:** A new `pre_active.initiative` module owns temporal initiative decisions. `ObserverManager` persists per-observer policy configuration/state in a separate table and consults the selected policy only when a real change would otherwise be eligible to emit. The default `on_change` policy is behavior-preserving.

**Tech Stack:** Python 3.11+, SQLite, stdlib `math/json/dataclasses/typing`, pytest, GitHub Actions.

## Global Constraints

- Base on `feat/durable-observers-20261005@8a3ef44dbdc03a81335488514ee1f35ad5528812`.
- Do not mutate `main` or the existing observer branch.
- Preserve `AUTONOMOUS_TURN != EFFECT_AUTHORITY`.
- Do not add SciPy, statsmodels, NumPy, or another numerical dependency in V1.
- The Hawkes policy is a conditional-intensity scoring mechanism over actual observed changes; it does not probabilistically generate changes.
- Reject supercritical exponential Hawkes configuration where `excitation / decay_rate >= 1`.
- Cooldown is deterministic control, not negative Hawkes excitation.
- Existing observers without explicit initiative policy retain on-change behavior.

---

### Task 1: Temporal initiative policy core

**Files:**
- Create: `src/pre_active/initiative.py`
- Create: `tests/test_initiative.py`

**Interfaces:**
- Consumes: policy configuration plus durable JSON-compatible state and observation time.
- Produces: `InitiativeDecision(emit, state, metrics, reason)`, `OnChangePolicy`, `ExponentialHawkesThresholdPolicy`, and `build_initiative_policy(kind, config)`.

- [x] **Step 1: Add the focused failing tests**

Test that `on_change` emits, exponential Hawkes excitation accumulates across nearby events and decays across distant events, cooldown suppresses emission without discarding excitation, and invalid/supercritical parameters raise `ValueError`.

- [x] **Step 2: Verify the relevant failure**

Run: `pytest -q tests/test_initiative.py`

Expected: collection/import failure because `pre_active.initiative` does not exist.

- [x] **Step 3: Implement the minimum behavior**

Use an immutable decision dataclass. Validate finite numeric parameters. Maintain `excitation`, `last_event_at`, and `last_emit_at` in JSON-compatible state. Clamp negative wall-clock deltas to zero so a clock correction cannot manufacture extra decay. Reject unknown policy kinds.

- [x] **Step 4: Verify the focused pass**

Run: `pytest -q tests/test_initiative.py`

Expected: all initiative tests pass.

- [x] **Step 5: Run the affected integration check**

Run: `pytest -q tests/test_observers.py`

Expected: existing observer tests pass unchanged.

- [x] **Step 6: Commit the passing deliverable**

Commit message: `feat: add temporal initiative policy core`

### Task 2: Durable observer policy state and gating

**Files:**
- Modify: `src/pre_active/observers.py`
- Modify: `tests/test_observers.py`

**Interfaces:**
- Consumes: `build_initiative_policy(kind, config)` and current `Observation`.
- Produces: durable `observer_initiative` state, additive `initiative` data in observer records, and gated `autonomous.turn` emission.

- [x] **Step 1: Add the focused failing test**

Configure a file observer with a Hawkes threshold where the first post-baseline change is suppressed and a second nearby change crosses threshold. Assert one autonomous turn, `change_count == 2`, persisted excitation state, and a suppression journal event.

- [x] **Step 2: Verify the relevant failure**

Run: `pytest -q tests/test_observers.py -k initiative`

Expected: failure because observer configuration has no initiative-policy arguments or persistence.

- [x] **Step 3: Implement the minimum behavior**

Create `observer_initiative` with `observer_id`, `policy_kind`, `config_json`, `state_json`, `last_decision_json`, and `updated_at`. Backfill existing observers to `on_change`. Validate policies on add. In `_record_observation`, update change count for every eligible detected change, evaluate the policy, atomically persist policy state and observation state, journal suppression, and emit only on `decision.emit`.

- [x] **Step 4: Verify the focused pass**

Run: `pytest -q tests/test_observers.py -k initiative`

Expected: initiative observer test passes.

- [x] **Step 5: Run the affected integration check**

Run: `pytest -q tests/test_observers.py`

Expected: all observer tests pass.

- [x] **Step 6: Commit the passing deliverable**

Commit message: `feat: gate observer turns with durable initiative policy`

### Task 3: CLI configuration and repository verification

**Files:**
- Modify: `src/pre_active/cli.py`
- Modify: `tests/test_observers.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: `--initiative-policy` and `--initiative-config-json` on `observer add-file`.
- Produces: validated observer policy configuration visible through existing `observer show/list` JSON.

- [x] **Step 1: Add the focused failing test**

Parse CLI arguments for Hawkes policy configuration and verify invalid non-object JSON exits with a clear message.

- [x] **Step 2: Verify the relevant failure**

Run: `pytest -q tests/test_observers.py -k cli`

Expected: failure because the new CLI flags do not exist.

- [x] **Step 3: Implement the minimum behavior**

Add the two generic CLI flags. Decode JSON to a dictionary, pass policy kind/config to `ObserverManager.add_file`, and document one Hawkes example plus the evidence/authority boundary.

- [x] **Step 4: Verify the focused pass**

Run: `pytest -q tests/test_observers.py -k cli`

Expected: CLI initiative tests pass.

- [x] **Step 5: Run the affected integration check**

Run: `python -m compileall -q src && pytest -q`

Expected: zero exit with the entire repository test suite passing.

- [x] **Step 6: Commit the passing deliverable**

Commit message: `docs: expose observer initiative policy configuration`

## Unresolved product decisions

None for V1. HMM, renewal/adaptive scheduling, ARIMA-family, GARCH, VAR, OU/diffusion, and Cox-process implementations remain separate future decisions because their required observer data contracts are not present in the current architecture.
