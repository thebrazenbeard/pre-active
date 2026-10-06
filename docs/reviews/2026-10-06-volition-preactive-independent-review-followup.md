# Volition ↔ Pre-Active Bridge — Independent Review Follow-up

Date: 2026-10-06

Review class: **INDEPENDENT READ-ONLY REVIEW**

Reviewer surface: separate Codex CLI process, read-only sandbox, supervised through the local Project Runner task lane.

Original reviewed feature range:

- base: `a6900dc2d2fb65f4ea66db95fca1b9c5b022450b`
- bridge head: `538f55c50d95b84423216c1220a02fa6674f6d30`
- merged main before follow-up: `3749ae0a67fe6e4df8df5c8a50eecb17e5650ce6`

The reviewer did not modify files.

## Findings

### P2 — persisted Volition clock never advanced

**Accepted.**

The bridge restored `VOLITION_STATE_V2` and immediately called `tick()` /
`request_cognition()` without advancing Volition time. Because goal reappraisal
and endogenous cognition-budget renewal depend on `elapsed_seconds`, a retained
goal permanently exhausted its default cognition budget after three requests.

Independent reproduction showed signals at one day and one week later still
produced no cognition with `elapsed_seconds=0`; explicitly advancing the engine
past the six-hour reappraisal horizon renewed cognition and incremented the goal
revision.

Fix:

- use the durable `volition_state.updated_at` timestamp as the previous clock anchor;
- compute `state_now = max(previous_updated_at, event_now)`;
- call `engine.advance(state_now - previous_updated_at)` before `tick()`;
- persist the next snapshot with `updated_at=state_now` so wall-clock rollback
  cannot move the durable clock anchor backwards.

Regression coverage:

- `test_bridge_advances_persisted_clock_and_renews_budget_after_reappraisal`
- `test_bridge_does_not_move_durable_clock_anchor_backwards`

### P3 — oversized integers entered retry/dead-letter handling

**Accepted.**

The bridge accepted integer values as numeric, then called `float(value)` without
translating `OverflowError`. An input such as `10**400` therefore escaped the
external-input validation class and was treated as an execution failure.

Fix:

- catch `OverflowError` during numeric conversion;
- raise `InvalidVolitionSignal` so the malformed signal is rejected and
  acknowledged rather than consuming retry/dead-letter budget.

Regression coverage:

- `test_engine_rejects_oversized_numeric_signal_without_retry`

## Findings not reproduced

The independent reviewer found no capability escalation and no crash/replay
amplification in the inspected paths. Its in-memory checks additionally verified:

- a forbidden mutation tool was not dispatched from the zero-capability Volition run;
- bridge transaction rollback removed snapshot, events, and journal writes after an
  injected receipt failure;
- source-event replay did not duplicate cognition events.

## TDD evidence

Focused red task: `volition-clock-hotfix-red`

All three new regressions failed for the expected reasons:

- persisted `elapsed_seconds` remained zero;
- persisted `updated_at` moved backward;
- oversized integer raised raw `OverflowError`.

Focused green task: `volition-clock-hotfix-green`

All three regressions passed after the minimum fix.

Full post-fix regression task: `volition-clock-hotfix-full`

**179 passed in 9.31s.**

## Runtime boundary

The production ProgramData runtime remains governed by the existing weekday
00:00–14:00 local autonomy window. The duplicate user-local daemon was disabled
during the preceding cutover. This hotfix must be promoted to canonical `main`
and redeployed to ProgramData before sustained production activation.
