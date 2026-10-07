# Schedule → Volition Dispatch V1 — Independent Read-Only Review

Review executed: 2026-10-07

Review class: **INDEPENDENT_READ_ONLY_REVIEW**

Repository: `thebrazenbeard/pre-active`

Reviewed PR: #32

Reviewed head: `a8ca7500b4ebda66ff88df6c58b97ebfd2dd1b7c`

Reviewed base: `main@1f23a809d7274df506e03e2d9052525538c3bcf6`

Reviewer: separate Codex CLI session, GPT-6 Astra, xhigh reasoning, read-only sandbox,
ephemeral session. The reviewer was not given implementation authority and made no
source or external-state changes.

## Verdict at reviewed head

**FAIL — three P2 release blockers remained.**

The reviewer independently confirmed that the authority boundary itself was sound:
the static config allowlist was closed, source was derived, effect authority was
false, event priority was zero, and downstream cognition was ENDOGENOUS and
zero-capability. It also exercised mixed schedule ordering/budget behavior without
finding an additional authority or fallback defect.

The following release blockers remained at `a8ca750...`.

### P2 — infinite interval poisons scheduler processing

`Scheduler.add_volition_interval(... every_seconds=float("inf"))` accepted the
interval. The coalescing path later computed an invalid occurrence and SQLite
rejected `events.available_at`. Because the schedule remained due, subsequent
cycles failed again and could block later due schedules.

Required correction: Volition schedule timing inputs must reject non-finite values
before insertion.

### P2 — fractional interval can split one due instant into two signals

With:

- `first_at=100.0`
- `every_seconds=60.1`
- `now=701.0`

the first tick emitted `640.9` and left `next_at=701.0`; a second tick at the
same `now` emitted another signal at `701.0`. This violated latest-due
coalescing and repeated-tick idempotence.

Required correction: latest-due calculation must not rely on binary-float floor
division that can undercount an exact decimal cadence boundary.

### P2 — explicit empty Volition config bypasses mode guard

`--volition-config-json '{}'` without `--volition` was indistinguishable from
the parser default and silently created an ordinary schedule. Combined with
`--autonomous --reason ...`, it could instead create a direct TEMPORAL schedule.

Required correction: distinguish flag omission from an explicitly supplied JSON
string; any explicit Volition config without Volition mode must fail closed.

## Additional reviewer observations

The reviewer reproduced inherited scheduler precision/dedup limitations for
extremely small intervals on both base and feature head. Because the Volition
helper is a new safe public seam and schedule dedup identity is formatted to six
decimal places, the repair narrows Volition cadence to at least one microsecond
and requires timing values that can advance at the current timestamp scale.

The reviewer ran 18 focused existing tests and 36 mixed schedule ordering/budget
cases. Its broader pytest run was blocked by read-only sandbox temporary-storage
constraints, not by a test failure. `git diff --check` passed. The reviewed
worktree remained clean.

The GitHub “Code scanning AI findings” check on the same head also failed, but its
workflow log showed HTTP 402 / monthly quota exhaustion while creating the AI
review session. That failure produced no security finding. CodeQL itself passed.

## Repair status

A separate isolated repair worktree was created from the exact reviewed head so
the reviewer target remained frozen.

TDD regressions were added for:

- non-finite interval rejection;
- non-finite `first_at` / `now` rejection;
- sub-microsecond Volition interval rejection;
- fractional latest-due coalescing;
- explicit empty config without Volition mode;
- missing config flag in Volition mode;
- multiple equally-due Volition schedules under `max_occurrences=1`.

All focused repair tests are green. The full local Python 3.12 suite is green on
the repair worktree.

This document records the review at `a8ca750...`; it does **not** by itself
constitute independent verification of the repaired successor head. A fresh
read-only follow-up review is required after the repair is committed onto PR #32.
