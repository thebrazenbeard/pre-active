# Schedule → Volition Dispatch V1 Implementation Plan

> **For agentic workers:** Use the host's available task-by-task implementation workflow. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an opt-in recurring schedule path that emits typed `volition.signal` events without changing existing ordinary or direct-autonomous schedules.

**Architecture:** Reuse the existing schedules table and occurrence emitter. Add one shared static Volition-config validator, a bounded `Scheduler.add_volition_interval` creation helper, and additive CLI flags. No database migration or temporal-context envelope is introduced.

**Tech Stack:** Python 3.11+, SQLite/WAL, pytest, argparse CLI, existing Pre-Active Scheduler and VolitionBridge.

## Global Constraints

- Existing ordinary and `--autonomous` schedule behavior remains unchanged.
- No new schedule table or migration.
- Static motive config uses one shared validator for observer and scheduler routes.
- Required motive fields: target, kind, magnitude, confidence, provenance.
- Runtime-only/authority fields are rejected from static config.
- Volition schedule source is derived as `schedule:<schedule_id>`.
- `effect_authority=false` is synthesized by Pre-Active.
- Schedule event priority remains 0.
- `--volition` is mutually exclusive with `--autonomous`.
- Volition schedules reject capabilities and `--reason`.
- Scheduler never falls back from Volition to direct autonomous cognition.
- No temporal-context envelope in V1.
- Generated cognition remains ENDOGENOUS and zero-capability.
- Existing Lappy autonomy window is not changed by this feature.

---

### Task 1: Shared static Volition signal configuration validator

**Files:**
- Modify: `src/pre_active/volition_bridge.py`
- Modify: `src/pre_active/observers.py`
- Test: `tests/test_volition_bridge.py`
- Test: `tests/test_observers.py`

**Interfaces:**
- Produces: `VolitionBridge.validate_static_signal_config(config, *, source) -> dict[str, Any]`.
- Consumes: existing `VolitionBridge.parse_signal_payload` semantic validation.

- [ ] **Step 1: Add focused failing tests**

Prove the new validator:
- returns a normalized payload containing the complete valid motive config plus derived source and false effect authority;
- rejects missing required fields;
- rejects `source`, `effect_authority`, `capabilities`, `priority`, `observation_context`, and unknown fields;
- rejects semantically invalid kind/provenance/numeric values;
- preserves all currently accepted optional motive fields.

Add a regression that observer Volition dispatch still accepts/rejects the same configs after switching to the shared helper.

- [ ] **Step 2: Verify the red phase**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_volition_bridge.py tests/test_observers.py -k "static_signal_config or volition_dispatch" -q
```

Expected: focused failures because the shared validator does not exist.

- [ ] **Step 3: Implement the minimum validator**

In `volition_bridge.py`:
- define the static allowlist and required field set once;
- implement `validate_static_signal_config`;
- reject all keys outside the allowlist;
- require all five required fields;
- call the existing parser against `{**config, source, effect_authority:false}`;
- return a copied normalized payload with only allowed fields plus derived source/false authority.

In `observers.py`:
- replace the duplicated allowlist/required-field logic and `__new__` parser call with the shared helper;
- retain the empty-capability requirement and direct-route behavior.

- [ ] **Step 4: Verify focused green**

Run the identical focused command.

Expected: all validator/observer dispatch tests pass.

- [ ] **Step 5: Run affected integration**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_observers.py tests/test_volition_bridge.py -q
```

Expected: all observer and bridge tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/pre_active/volition_bridge.py src/pre_active/observers.py tests/test_volition_bridge.py tests/test_observers.py
git commit -m "refactor: share static Volition signal validation"
```

### Task 2: Safe recurring Volition schedules

**Files:**
- Modify: `src/pre_active/scheduler.py`
- Test: `tests/test_scheduler.py`

**Interfaces:**
- Consumes: `VolitionBridge.validate_static_signal_config`.
- Produces: `Scheduler.add_volition_interval(*, config, every_seconds, first_at, now) -> str`.

- [ ] **Step 1: Add focused failing tests**

Prove:
- valid creation persists one schedule row with `kind=volition.signal`;
- payload source is exactly `schedule:<schedule_id>`;
- payload effect authority is false;
- stored config cannot contain capabilities/priority/runtime-only fields;
- invalid config writes no schedule row;
- due tick emits one priority-0 `volition.signal` with the existing schedule occurrence dedup key and `available_at=occurrence`;
- repeated tick at the same timestamp does not duplicate;
- catch-up over multiple intervals emits each occurrence once;
- generic `add_interval` schedule behavior remains unchanged.

- [ ] **Step 2: Verify red**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_scheduler.py -k "volition" -q
```

Expected: failures because `add_volition_interval` does not exist.

- [ ] **Step 3: Implement minimum scheduler helper**

In `scheduler.py`:
- import `VolitionBridge`;
- factor schedule-row insertion only if needed to avoid duplication;
- generate UUID before validation;
- validate config using source `schedule:<id>`;
- insert the existing schedule row with `kind="volition.signal"`;
- preserve existing `tick()` unchanged unless a test demonstrates a required fix.

- [ ] **Step 4: Verify focused green**

Run the identical command.

Expected: all Volition schedule tests pass.

- [ ] **Step 5: Run scheduler integration**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_scheduler.py tests/test_engine.py tests/test_volition_bridge.py -q
```

Expected: all scheduler/engine/bridge tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/pre_active/scheduler.py tests/test_scheduler.py
git commit -m "feat: schedule recurring Volition signals"
```

### Task 3: CLI and end-to-end temporal cognition

**Files:**
- Modify: `src/pre_active/cli.py`
- Modify: `README.md`
- Test: `tests/test_scheduler.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `Scheduler.add_volition_interval`.
- Produces: `schedule --volition --volition-config-json`.

- [ ] **Step 1: Add focused failing tests**

Prove:
- parser exposes `--volition` and `--volition-config-json`;
- default ordinary schedule output/row is unchanged;
- `--autonomous --volition` is rejected;
- Volition mode rejects capabilities;
- Volition mode rejects `--reason`;
- malformed JSON and non-object JSON get explicit errors;
- valid CLI creation persists the derived schedule source;
- end-to-end schedule occurrence → `volition.signal` → ENDOGENOUS cognition → completed zero-capability run;
- there is no direct TEMPORAL autonomous event in Volition mode.

- [ ] **Step 2: Verify red**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_scheduler.py tests/test_cli.py -k "volition and schedule" -q
```

Expected: failures because the CLI surface is absent.

- [ ] **Step 3: Implement minimum CLI/docs**

In `cli.py`:
- add `--volition`;
- add `--volition-config-json` defaulting to `{}`;
- before schedule creation, enforce mode/capability/reason mutual exclusions;
- parse config with explicit malformed/non-object errors;
- call `Scheduler.add_volition_interval` for Volition mode;
- leave existing ordinary/autonomous branches unchanged.

In `README.md`:
- document the three schedule modes;
- document the authority boundary and ignored compatibility task;
- include one Volition schedule example.

- [ ] **Step 4: Verify focused green**

Run the identical focused command.

Expected: all schedule CLI/end-to-end tests pass.

- [ ] **Step 5: Run full local suite**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest
```

Expected: complete suite passes.

- [ ] **Step 6: Commit**

```bash
git add src/pre_active/cli.py README.md tests/test_scheduler.py tests/test_cli.py
git commit -m "feat: expose scheduled Volition signals in CLI"
```

### Task 4: Review, promotion, and window-preserving deployment

**Files:**
- Create: `docs/reviews/2026-10-06-schedule-volition-dispatch-hostile-review.md`
- Create/Update: `docs/reviews/2026-10-06-schedule-volition-dispatch-independent-review.md`
- Modify: this implementation plan
- Deployment receipt: `C:\ProgramData\PreActive\state\SCHEDULE_VOLITION_DISPATCH_PROMOTION_20261006.json`

- [ ] **Step 1: Internal hostile review**

Attack:
- source/effect/capability/priority injection;
- accidental direct TEMPORAL plus Volition dual dispatch;
- schedule catch-up amplification;
- dedup collisions;
- static-validator drift between observers and scheduler;
- Volition dependency loss and fallback behavior;
- config mutation after creation;
- existing schedule compatibility.

Any surviving defect gets a failing regression before its fix.

- [ ] **Step 2: Exact-head local verification**

Run `git diff --check origin/main...HEAD` and full pytest.

Expected: clean diff and complete green suite.

- [ ] **Step 3: Push PR and independent read-only review**

Freeze the feature head, open a PR against exact current `main`, and run a
separate read-only Codex review. Record incomplete reviewer processes honestly;
fix every concrete finding with red-green regression evidence.

- [ ] **Step 4: Verify exact-head CI**

Require Python 3.11, 3.12, and 3.13 success on the frozen PR head and re-read
head/base/mergeability immediately before promotion.

- [ ] **Step 5: Promote**

Merge with `expected_head_sha` fencing and read back canonical `main`.

- [ ] **Step 6: Deploy without violating the existing autonomy window**

Fast-forward ProgramData to the exact canonical merge while no production
daemon/Qwen process is active outside the existing window. Run the full production
Python suite and an isolated real-model schedule→Volition smoke. Do not create a
persistent production Volition schedule in V1; this deploys the capability, not
a new recurring motive policy. Preserve the existing 00:00–14:00 weekday window
and write the deployment receipt.

## Unresolved product decisions

None. V1 intentionally deploys the capability without choosing a persistent
production motive/schedule on Patrick's behalf.
