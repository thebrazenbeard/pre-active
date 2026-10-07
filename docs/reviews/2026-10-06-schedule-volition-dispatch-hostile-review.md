# Schedule → Volition Dispatch V1 — Internal Hostile Review

Date: 2026-10-06

Review class: **INTERNAL_HOSTILE_REVIEW**

Repository: `thebrazenbeard/pre-active`

Base: `main@1f23a809d7274df506e03e2d9052525538c3bcf6`

Feature branch: `feat/schedule-volition-dispatch-v1-20261006`

This is internal engineering evidence, not independent review.

## Scope

Attack the load-bearing claims:

- static motive configuration cannot inject source, authority, capabilities, priority, or runtime context;
- a scheduled Volition occurrence does not also create a direct TEMPORAL autonomous turn;
- missed intervals do not amplify stale motives into a burst;
- dedup and occurrence timing remain deterministic;
- the shared static validator cannot drift between observer and schedule routes;
- dependency loss never causes fallback to another cognition route;
- caller mutation after schedule creation cannot mutate the durable motive;
- ordinary and direct-autonomous schedules preserve their prior behavior.

## Finding 1 — stale-occurrence motive amplification

> **HOSTILE REVIEWER:** Reusing ordinary scheduler catch-up semantics means every
> missed Volition interval is replayed when the runtime resumes. The existing
> daily autonomy window itself guarantees recurring downtime, so an hourly motive
> could resume with roughly ten stale signals each midnight and dozens after a
> weekend.

**ACCEPTED; FIXED.**

The first implementation inherited generic catch-up and a regression reproduced a
single overdue schedule emitting the full 100-event per-tick ceiling.

V1 now treats recurring `volition.signal` rows as a narrow scheduler exception:

- if multiple occurrences are overdue, compute the latest due occurrence;
- emit exactly that one occurrence;
- advance `next_at` to the following interval;
- create no events for the coalesced stale occurrences.

Ordinary task and direct-autonomous schedules retain existing catch-up behavior.

Regression:
`test_volition_interval_coalesces_missed_occurrences_to_latest_due`.

## Finding 2 — Volition config could be silently ignored by CLI mode selection

> **HOSTILE REVIEWER:** A user can supply `--volition-config-json` but forget
> `--volition`. Silently ignoring the motive config creates ordinary scheduled
> work, which is a materially different route and a fail-open mode error.

**ACCEPTED; FIXED.**

A non-default `--volition-config-json` supplied without `--volition` now exits
before any schedule row is written.

Regression:
`test_schedule_cli_rejects_volition_config_without_volition_mode`.

## Finding 3 — source/effect/capability/priority injection

> **HOSTILE REVIEWER:** Schedule configuration could smuggle authority-sensitive
> fields or future bridge-only context into the persisted signal.

**REJECTED WITH EVIDENCE.**

`VolitionBridge.validate_static_signal_config` is now the shared closed contract
for observer and schedule routes. It accepts only the canonical operator-authored
motive fields, requires target/kind/magnitude/confidence/provenance, derives the
source, forces `effect_authority=false`, and rejects capabilities, priority,
observation context, source, authority, and unknown future fields.

The scheduler stores only the returned validated copy. Event priority remains the
scheduler's fixed zero.

Evidence:
- static-validator tests in `tests/test_volition_bridge.py`;
- `test_volition_interval_rejects_invalid_config_before_insert`;
- `test_volition_interval_emits_priority_zero_occurrences_once`.

## Finding 4 — direct TEMPORAL + Volition dual dispatch

> **HOSTILE REVIEWER:** Volition mode could accidentally retain the existing
> `--autonomous` path and create both direct TEMPORAL cognition and a motive
> signal.

**REJECTED WITH EVIDENCE.**

CLI modes are mutually exclusive. Volition mode calls only
`Scheduler.add_volition_interval`. The daemon integration test observes one
`volition.signal`, then one ENDOGENOUS cognition event, and explicitly checks
that no TEMPORAL autonomous event exists.

Evidence:
- `test_schedule_cli_rejects_conflicting_volition_modes`;
- `test_daemon_routes_due_schedule_through_volition_without_temporal_turn`.

## Finding 5 — dependency loss could substitute a different route

> **HOSTILE REVIEWER:** If Volition is absent at configuration or runtime, the
> scheduler might silently fall back to direct autonomous work.

**REJECTED WITH EVIDENCE.**

Valid Volition schedule creation requires the Volition semantic validator before
the row is inserted. If the dependency is absent, configuration fails and no row
is written.

If the dependency disappears after configuration, `Scheduler.tick()` still
emits only the persisted `volition.signal`. It does not load Volition and has no
fallback branch. Ordinary event processing owns retry/dead-letter behavior.

Evidence:
- `test_volition_interval_dependency_required_at_configuration_time`;
- `test_volition_interval_runtime_dependency_loss_does_not_fallback_to_direct_turn`.

## Finding 6 — mutable caller config after creation

> **HOSTILE REVIEWER:** The caller could retain and mutate the config dict after
> schedule creation, changing future authority or motive fields without a durable
> write.

**REJECTED WITH EVIDENCE.**

Creation serializes a validated copied payload into `payload_json`. Mutating the
original caller dictionary afterward does not alter the durable schedule.

Evidence:
`test_volition_interval_copies_config_at_creation`.

## Finding 7 — generic low-level Scheduler.add_interval remains capable of storing volition.signal

> **HOSTILE REVIEWER:** The generic scheduler API still accepts an arbitrary event
> kind, including `volition.signal`, so the new helper cannot be described as the
> only possible way to persist such a row.

**ACCEPTED AS A CLAIM BOUNDARY; NO CODE CHANGE.**

`Scheduler.add_interval` predates this feature as a low-level generic durable
event primitive. V1 does not break that existing API. The safety claim is narrower:
the new documented/public Volition schedule helper and CLI derive and validate
authority-sensitive fields. Any raw generic-event producer remains responsible
for the event contract, and the Volition bridge still rejects invalid authority
claims when the event is processed.

No statement in the feature documentation should claim that arbitrary direct
database or generic-event construction is impossible.

## Finding 8 — existing schedule compatibility

> **HOSTILE REVIEWER:** Special-casing Volition catch-up could accidentally change
> ordinary or direct-autonomous scheduling.

**REJECTED WITH EVIDENCE.**

The coalescing branch is conditioned on `kind == "volition.signal"`. The
existing generic interval loop remains unchanged for all other recurring kinds.
The full pre-existing scheduler and CLI regression suites are required again at
the exact final feature head.

## Internal verdict

**SURVIVES after two accepted/fixed defects and one explicit claim-boundary
narrowing.**

No unresolved release-blocking internal finding remains at this stage.

This verdict does not substitute for exact-head local verification, GitHub matrix
CI, independent read-only review, or production deployment verification.
