# Operational Snapshot Implementation Plan

**Goal:** Expose a deterministic operational snapshot for queue/run/schedule diagnosis and project it through `pre-active status`.

**Architecture:** Derive bounded aggregate measurements directly from the existing SQLite state in one read transaction. Preserve existing status fields and avoid per-event high-cardinality telemetry.

**Tech Stack:** Python 3.11+, sqlite3/WAL, pytest.

## Constraints

- Stack on provider-retry PR #15 exact head `51315f002f54bc73e138ea4883c5540c232d7d5d`.
- No telemetry backend dependency.
- No universal health thresholds.
- No event/run IDs in aggregate metric keys.
- No liveness claims.
- Empty age populations return `None`.
- Existing `status` top-level compatibility fields remain.

### Task 1: Aggregate snapshot contract

**Files:**
- Modify: `src/pre_active/store.py`
- Create: `tests/test_operational_snapshot.py`

- [ ] Add red test spanning ready/delayed/retry/active/expired/dead event populations.
- [ ] Add run-state and due-schedule assertions.
- [ ] Implement one read-transaction snapshot.

### Task 2: Status CLI projection

**Files:**
- Modify: `src/pre_active/cli.py`
- Modify: `tests/test_cli.py`

- [ ] Add red CLI test for new aggregate fields and compatibility fields.
- [ ] Replace ad-hoc status SQL with `Store.operational_snapshot()`.

### Task 3: Documentation and verification

**Files:**
- Modify: `README.md`
- Modify: `docs/ARCHITECTURE.md`

- [ ] Document measurement semantics and non-liveness ceiling.
- [ ] Verify build, compileall, and tests across Python 3.11/3.12/3.13.
- [ ] Verify clean stack relative to PR #15 and open a Draft PR.
