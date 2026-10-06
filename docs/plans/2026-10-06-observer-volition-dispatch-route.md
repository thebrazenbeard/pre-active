# Observer â†’ Volition Dispatch Route V1 Implementation Plan

> **For agentic workers:** Use the host's available task-by-task implementation workflow. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Add an opt-in, mutually exclusive observer dispatch route that emits a typed `volition.signal` instead of a direct `autonomous.turn`, while preserving existing observer behavior and all capability/effect-authority boundaries.

**Architecture:** Extend `ObserverManager` with a separate durable `observer_dispatch` configuration table. The existing initiative policy remains solely responsible for the emit/suppress decision; a validated dispatch route then chooses exactly one durable event path. Volition-routed observations carry motive fields from fixed operator configuration and read-only observation context in a separate envelope.

**Tech Stack:** Python 3.11+, SQLite/WAL, pytest, argparse CLI, existing Pre-Active observer/initiative/event queue, pinned Volition bridge.

## Global Constraints

- Existing observers and existing CLI invocations preserve current direct `autonomous_turn` behavior.
- Supported dispatch routes are exactly `autonomous_turn` and `volition_signal`; unknown routes fail closed.
- Dispatch is XOR: one initiative emission creates one direct autonomous turn or one Volition signal, never both.
- `volition_signal` requires `capabilities=[]`.
- Volition route configuration cannot supply `source`, `effect_authority`, `capabilities`, or `priority`.
- Volition signal source is derived as `observer:<observer_id>`; effect authority is synthesized as false.
- Observer priority is not forwarded into Volition routing.
- Observation context is a separate typed Pre-Active envelope and is not part of Volition motive scoring.
- Observer state, initiative state, dispatch enqueue, and observer journal evidence commit atomically.
- A corrupt dispatch row or unavailable Volition runtime must not make the manager silently fall back to the other route.
- Corruption/failure of one due observer must not block healthy due observers.
- Generated Volition cognition remains `ENDOGENOUS`, zero-capability, and effect-authority false.
- Existing ProgramData weekday autonomy window remains a deployment constraint and is not modified by this feature.

---

### Task 1: Durable observer dispatch configuration and compatibility surface

**Files:**
- Modify: `src/pre_active/observers.py:28-260`
- Test: `tests/test_observers.py`

**Interfaces:**
- Consumes: `ObserverManager.__init__(store, adapters=None)`, `ObserverManager.add(...)`, `ObserverManager.add_file(...)`, existing observer row decoding.
- Produces: `observer_dispatch(observer_id, route_kind, config_json, updated_at)`, additive `record["dispatch"]`, `dispatch_route="autonomous_turn"`, `dispatch_config=None` parameters on add/add_file.

- [x] **Step 1: Add focused failing tests**

Add tests proving:
- manager initialization backfills an existing observer with `{"route_kind":"autonomous_turn","config":{}}`;
- newly added default observer exposes the same dispatch object;
- unknown route is rejected before any observer row is inserted;
- `autonomous_turn` rejects non-empty dispatch config;
- `volition_signal` rejects a non-empty capability set;
- valid `volition_signal` config is persisted/read back unchanged;
- configuration cannot contain reserved keys `source`, `effect_authority`, `capabilities`, or `priority`;
- due-observer claiming does not decode dispatch JSON before the per-observer isolated path.

- [x] **Step 2: Verify the relevant failure**

Run:

```powershell
C:\ProgramData\PreActive\python\python.exe -m pytest tests/test_observers.py -k "dispatch or backfill" -q
```

Expected: failures show the missing `observer_dispatch` table/read surface and missing route-validation parameters, not import/setup errors.

- [x] **Step 3: Implement the minimum behavior**

In `observers.py`:
- add `_DISPATCH_SCHEMA` with the exact table from the approved spec;
- create/backfill it in `ObserverManager.__init__`;
- add `dispatch_route: str = "autonomous_turn"` and `dispatch_config: dict[str, Any] | None = None` to `add` and `add_file`;
- normalize route names to lowercase;
- validate:
  - `autonomous_turn` requires empty config;
  - `volition_signal` requires empty capabilities;
  - reserved configuration keys are rejected;
  - validate typed Volition fields by constructing a temporary payload with derived validation source `observer:validation` and forced `effect_authority=False`, then call `VolitionBridge.parse_signal_payload`;
- insert observer, initiative, and dispatch rows in the existing single `BEGIN IMMEDIATE` transaction;
- backfill preexisting observers with `autonomous_turn/{ }`;
- expose an additive `dispatch` object in normal observer records;
- preserve the existing `include_initiative=False` claim path pattern by adding an equivalent dispatch-decode guard so malformed dispatch JSON is not decoded during due-ID claiming.

- [x] **Step 4: Verify the focused pass**

Run the same focused command.

Expected: all dispatch/backfill validation tests pass.

- [x] **Step 5: Run affected integration checks**

Run:

```powershell
C:\ProgramData\PreActive\python\python.exe -m pytest tests/test_observers.py tests/test_initiative.py -q
```

Expected: all observer/initiative tests pass with unchanged default behavior.

- [x] **Step 6: Commit the passing deliverable**

```bash
git add src/pre_active/observers.py tests/test_observers.py
git commit -m "feat: persist observer dispatch routes"
```

### Task 2: XOR event routing and observation-context propagation

**Files:**
- Modify: `src/pre_active/observers.py:432-606`
- Modify: `src/pre_active/volition_bridge.py:23-230`
- Test: `tests/test_observers.py`
- Test: `tests/test_volition_bridge.py`

**Interfaces:**
- Consumes: persisted `record["dispatch"]`, `VolitionBridge.enqueue_signal(payload, now, dedup_key)`, `VolitionBridge.process_signal_event(...)`.
- Produces: observer-created `volition.signal` envelopes with optional validated `observation_context`; resulting ENDOGENOUS cognition metadata/task containing that read-only context.

- [x] **Step 1: Add focused failing tests**

Observer tests:
- default route emits exactly one `autonomous.turn`, zero `volition.signal` events, preserving capability/priority/source/dedup semantics;
- valid Volition route emits exactly one `volition.signal` and zero direct autonomous turns;
- emitted motive fields equal the persisted dispatch config, with derived `source=observer:<id>` and forced `effect_authority=False`;
- `observation_context` contains observer id/name/kind, digest, summary, change_count, evidence, initiative policy/reason/metrics;
- Volition route dedup key equals `observer:<id>:volition:<change_count>:<digest>`;
- injected enqueue failure rolls back observation digest/change_count, initiative state, signal event, and journal write;
- corrupt dispatch JSON on one due observer increments that observer error path but does not block a healthy peer.

Bridge tests:
- `observation_context` is optional for ordinary CLI-created signals;
- malformed context is rejected as `InvalidVolitionSignal`;
- valid context is copied to generated cognition event metadata and a clearly marked read-only task section;
- context cannot introduce capabilities/effect authority and generated cognition remains `capabilities=[]`.

- [x] **Step 2: Verify the relevant failures**

Run:

```powershell
C:\ProgramData\PreActive\python\python.exe -m pytest tests/test_observers.py tests/test_volition_bridge.py -k "dispatch or observation_context or volition_route" -q
```

Expected: failures identify missing route dispatch and context propagation.

- [x] **Step 3: Implement the minimum behavior**

In `observers.py`:
- load/decode the dispatch row only inside `_record_observation`;
- on `emit=True`, switch on route:
  - `autonomous_turn`: keep existing `request_autonomous_turn` logic;
  - `volition_signal`: build motive payload from persisted config, add derived source/effect-authority false and the fixed observation-context object, then call `VolitionBridge(self.store).enqueue_signal`;
- use dedup key `observer:<id>:volition:<change_count>:<digest>`;
- never execute both branches;
- extend `OBSERVER_CHANGE_DETECTED` journal payload with `dispatch_route`, `event_kind`, and `event_id`;
- if route/config is corrupt or Volition unavailable, allow the per-observer tick isolation path to record the observer error; do not substitute routes.

In `volition_bridge.py`:
- validate optional `observation_context` as an object with the exact approved fields/types;
- strip it before constructing canonical Volition `Signal`;
- preserve the validated context in the generated cognition event's `volition` metadata;
- append a clearly labeled read-only observation-context JSON section to the cognition task only after Volition actually requests cognition.

- [x] **Step 4: Verify the focused pass**

Run the identical focused command.

Expected: route/context tests pass.

- [x] **Step 5: Run affected integration checks**

Run:

```powershell
C:\ProgramData\PreActive\python\python.exe -m pytest tests/test_observers.py tests/test_volition_bridge.py tests/test_engine.py tests/test_initiative.py -q
```

Expected: all affected tests pass; default route remains behavior-compatible.

- [x] **Step 6: Commit the passing deliverable**

```bash
git add src/pre_active/observers.py src/pre_active/volition_bridge.py tests/test_observers.py tests/test_volition_bridge.py
git commit -m "feat: route observer emissions through Volition"
```

### Task 3: CLI configuration and end-to-end daemon behavior

**Files:**
- Modify: `src/pre_active/cli.py:164-180,438-469`
- Modify: `README.md`
- Test: `tests/test_observers.py`
- Test: `tests/test_volition_bridge.py`

**Interfaces:**
- Consumes: `ObserverManager.add_file(... dispatch_route, dispatch_config ...)`.
- Produces: `observer add-file --dispatch-route --dispatch-config-json` and end-to-end file-change â†’ Volition â†’ ENDOGENOUS run behavior.

- [x] **Step 1: Add focused failing tests**

Add tests proving:
- parser exposes `--dispatch-route` defaulting to `autonomous_turn` and `--dispatch-config-json` defaulting to `{}`;
- malformed JSON produces the explicit `--dispatch-config-json must be valid JSON` exit;
- non-object JSON produces `--dispatch-config-json must decode to a JSON object`;
- CLI Volition-route creation round-trips the stored `dispatch` object;
- CLI default route remains unchanged;
- daemon end-to-end with a real file observer and test model produces:
  `file change -> volition.signal -> ENDOGENOUS autonomous.turn -> completed zero-capability run`,
  with no direct EXTERNAL observer turn.

- [x] **Step 2: Verify the relevant failure**

Run:

```powershell
C:\ProgramData\PreActive\python\python.exe -m pytest tests/test_observers.py tests/test_volition_bridge.py -k "cli and dispatch or daemon and volition" -q
```

Expected: failures show missing CLI flags and missing end-to-end route.

- [x] **Step 3: Implement the minimum behavior**

In `cli.py`:
- add the two additive observer flags;
- parse dispatch JSON independently of initiative JSON with the exact error messages above;
- pass `dispatch_route` and parsed config to `ObserverManager.add_file`.

In `README.md`:
- document default vs opt-in route;
- state XOR dispatch, empty-capability rule, separate observation context, and no authority escalation;
- include one CLI example matching the approved spec.

- [x] **Step 4: Verify the focused pass**

Run the identical focused command.

Expected: CLI/end-to-end focused tests pass.

- [x] **Step 5: Run full local regression**

Run:

```powershell
C:\ProgramData\PreActive\python\python.exe -m pytest
```

Expected: complete suite passes with no regression.

- [x] **Step 6: Commit the passing deliverable**

```bash
git add src/pre_active/cli.py README.md tests/test_observers.py tests/test_volition_bridge.py
git commit -m "feat: expose observer Volition dispatch in CLI"
```

### Task 4: Adversarial verification, independent review, promotion, and window-preserving deployment

**Files:**
- Create: `docs/reviews/2026-10-06-observer-volition-dispatch-hostile-review.md`
- Modify: `docs/plans/2026-10-06-observer-volition-dispatch-route.md`
- Deployment receipt: `C:\ProgramData\PreActive\state\OBSERVER_VOLITION_DISPATCH_PROMOTION_20261006.json`

**Interfaces:**
- Consumes: exact feature head, Project Runner task evidence, GitHub CI, separate read-only reviewer output, existing ProgramData deployment/window policy.
- Produces: reviewed Draft/ready PR, canonical merge if evidence is green, exact-head ProgramData deployment, durable deployment receipt.

- [x] **Step 1: Internal hostile review**

Attack at minimum:
- accidental dual dispatch;
- dispatch corruption blocking healthy observers;
- observation-context injection into motive/authority fields;
- non-empty capability leakage;
- replay amplification;
- observer transaction partial commit;
- missing Volition dependency causing silent fallback;
- existing-observer compatibility.

Any surviving defect must receive a red regression before its fix.

- [x] **Step 2: Run exact-head local verification**

Run:

```powershell
git diff --check origin/main...HEAD
C:\ProgramData\PreActive\python\python.exe -m pytest
```

Expected: clean diff and complete green suite.

- [x] **Step 3: Push and obtain independent read-only review**

Push the exact feature branch and open a PR against the exact current `main`.

Run a separate Codex CLI reviewer in a read-only sandbox against the frozen feature diff. Record independent findings separately from internal hostile review. If it finds Important/Critical defects, add red regressions and fix them before promotion; rerun focused independent review of the fix range.

- [ ] **Step 4: Verify exact-head GitHub CI**

Require green matrix results for Python 3.11, 3.12, and 3.13 on the exact PR head. Re-read PR head/base/mergeability before merge.

- [ ] **Step 5: Promote to canonical main**

Merge with `expected_head_sha` fencing only when the frozen head and CI/review evidence remain valid. Read back the merge commit and `main`.

- [ ] **Step 6: Deploy without bypassing the existing autonomy window**

At `C:\ProgramData\PreActive`:
- verify no production daemon/Qwen process is unexpectedly active outside the existing window;
- back up state metadata;
- fast-forward source to the exact canonical merge commit;
- install the exact package/dependency set if packaging changed;
- run the full production-Python suite;
- run an isolated real-model observerâ†’Volition smoke database proving zero-capability ENDOGENOUS cognition;
- do not start the production daemon outside the existing Monday-Friday 00:00-14:00 window;
- preserve `LAPPY_AUTONOMY_WINDOW.json` and supervisor/close tasks;
- write a promotion receipt with canonical SHA, CI/review evidence, smoke IDs, task/process states, and activation status.

## Unresolved product decisions

None. The approved specification settles the externally observable V1 behavior.
