# Schedule → Volition Dispatch V1 Design

## Status

Approved under the standing continuation authority for the current Pre-Active / Volition build.

Repository: `thebrazenbeard/pre-active`

Exact design base: `main@1f23a809d7274df506e03e2d9052525538c3bcf6`

## Goal

Allow a recurring Pre-Active interval schedule to emit a typed `volition.signal`
instead of a normal `task.requested` event or a direct `autonomous.turn`, while
preserving all existing schedule behavior by default.

## Why this is the next seam

The production runtime currently has no schedules or observers. Observer → Volition
routing now exists, but there is still no resident temporal source that can feed
Volition without a human or external producer.

Pre-Active already defines `TEMPORAL` as a legitimate autonomous source class and
already has a durable interval scheduler. Reusing that scheduler is the smallest
coherent way to let time create motive evidence without granting permissions.

## Approaches considered

### A. Add a separate scheduler-dispatch table

Rejected.

Observers needed a separate dispatch table because observer rows did not already
persist an event kind/payload. Schedules already persist exactly those fields.
Adding another table would duplicate route state and create reconciliation
questions with no new capability.

### B. Let callers schedule arbitrary `volition.signal` payloads through
`Scheduler.add_interval`

Rejected.

That would allow callers to persist forged `source`, `effect_authority`,
context fields, or future bridge-only keys without going through the bounded
static motive contract.

### C. Add `Scheduler.add_volition_interval(...)`

Chosen.

The helper creates the schedule ID first, derives `source=schedule:<id>`, forces
`effect_authority=false`, validates a closed static motive config, and stores a
normal `volition.signal` schedule in the existing table. `Scheduler.tick()`
then uses its existing durable occurrence dedup identity.

## Shared static motive contract

Observer dispatch and temporal dispatch need the same operator-authored motive
shape. V1 therefore moves that contract into one shared Volition bridge validator.

Add:

```python
VolitionBridge.validate_static_signal_config(
    config: dict[str, Any],
    *,
    source: str,
) -> dict[str, Any]
```

Required fields:

- `target`
- `kind`
- `magnitude`
- `confidence`
- `provenance`

Optional fields:

- `expected_information_gain`
- `learning_progress`
- `controllability`
- `predicted_deficit_reduction`
- `current_reappraisal`

Everything else is rejected.

The returned payload contains:

- the validated operator fields;
- the supplied derived `source`;
- `effect_authority=false`.

The shared validator must not accept runtime-only fields such as:

- `observation_context`;
- `source` from the config;
- `effect_authority`;
- `capabilities`;
- `priority`.

Observer dispatch switches to this helper, removing its current `__new__`
validation trick without changing observer behavior.

## Scheduler API

Add:

```python
Scheduler.add_volition_interval(
    *,
    config: dict[str, Any],
    every_seconds: float,
    first_at: float,
    now: float,
) -> str
```

Behavior:

1. require finite `every_seconds`, `first_at`, and `now`;
2. require `every_seconds >= 0.000001` seconds so occurrence identities remain distinct at the scheduler's six-decimal dedup precision, and require the interval to advance the current timestamp representation;
3. generate the schedule UUID;
4. derive `source=f"schedule:{schedule_id}"`;
5. validate/normalize the static motive config through
   `VolitionBridge.validate_static_signal_config`;
6. insert one schedule row:
   - `kind="volition.signal"`;
   - `payload_json=<validated payload>`;
   - existing interval/next_at/enabled timestamps unchanged.

The generic `add_interval` API remains behavior-compatible for existing callers.

## Occurrence behavior

`Scheduler.tick()` remains the sole occurrence emitter.

Ordinary and direct-autonomous schedules preserve their existing catch-up
semantics. Recurring `volition.signal` schedules deliberately do not replay
every missed occurrence after downtime. If more than one occurrence is overdue,
the scheduler coalesces them to the latest due occurrence and advances
`next_at` to the following interval. Coalescing uses decimal-stable arithmetic
from the persisted numeric values rather than binary-float floor division so an
exact fractional cadence boundary cannot be emitted one tick late.

This prevents the normal daily autonomy-window shutdown (and longer outages)
from replaying a burst of stale motive signals when the runtime resumes.

Each emitted Volition occurrence creates one durable event:

```text
kind = volition.signal
payload.source = schedule:<schedule_id>
payload.effect_authority = false
priority = 0
available_at = scheduled occurrence timestamp
dedup_key = schedule:<schedule_id>:<occurrence timestamp>
```

The existing scheduler dedup identity remains unchanged for the occurrence
that is actually emitted. Coalesced stale occurrences intentionally create no
events.

No direct `autonomous.turn` is created by the scheduler in Volition mode.
The ordinary engine later processes the `volition.signal`; if Volition requests
cognition, that cognition remains `ENDOGENOUS`, zero-capability, and
`effect_authority=false`.

## Temporal context

V1 intentionally does **not** add a free-form `temporal_context` envelope.

The durable event already carries:

- the schedule-derived source;
- the exact occurrence identity in the dedup key;
- `available_at` equal to the scheduled occurrence.

Adding another context shape is unnecessary for the first temporal seam and would
expand the cross-goal context problem class that observer routing just had to
harden.

## CLI

Extend the existing `schedule` command additively:

```text
--volition
--volition-config-json '{}'
```

Rules:

- existing schedule behavior is unchanged when no Volition-specific option is supplied;
- providing `--volition-config-json` at all without `--volition` fails closed, including an explicit `'{}'`;
- `--volition` requires the `--volition-config-json` flag to be explicitly present;
- `--volition` and `--autonomous` are mutually exclusive;
- `--volition` requires `--volition-config-json` to decode to an object
  containing the complete required static motive fields;
- `--volition` rejects every `--capability`;
- `--reason` is invalid with `--volition`;
- the positional `task` remains required for CLI compatibility but is ignored
  in Volition mode;
- CLI output remains the existing JSON object with `schedule_id` and
  `first_at`.

Example:

```bash
pre-active --state state.db schedule "Compatibility-only task." \
  --every 3600 \
  --volition \
  --volition-config-json '{
    "target":"review-open-loops",
    "kind":"open_loop",
    "magnitude":0.5,
    "confidence":1.0,
    "provenance":"current_observation"
  }'
```

## Authority boundary

Temporal scheduling must preserve:

```text
CLOCK_OCCURRENCE
  != VOLITION_SIGNAL
  != WANT
  != CHOICE
  != GOAL
  != COGNITION_REQUEST
  != CAPABILITY
  != EFFECT_AUTHORITY
```

Specific rules:

1. schedule config cannot supply capabilities;
2. schedule config cannot supply event priority;
3. schedule config cannot supply source;
4. schedule config cannot supply effect authority;
5. schedule event priority remains 0;
6. generated cognition remains ENDOGENOUS;
7. generated cognition remains zero-capability;
8. no schedule occurrence performs an external effect by itself.

## Failure behavior

Configuration validation occurs before schedule insertion.

If Volition is unavailable while configuring a Volition schedule, configuration
fails and no schedule row is written.

If Volition becomes unavailable later, the schedule occurrence still durably
enqueues its already-validated `volition.signal`; ordinary event processing then
uses the existing bounded retry/dead-letter semantics for runtime dependency
failure. The scheduler does not fall back to a direct autonomous turn.

A malformed/corrupt persisted Volition schedule payload is treated like any other
queued malformed `volition.signal` when its occurrence is processed; it is not
rewritten by the scheduler.

## Compatibility

Existing:

- ordinary `task.requested` schedules;
- `--autonomous` TEMPORAL schedules;
- schedule dedup keys;
- scheduler catch-up behavior;
- max-occurrence bounding;
- schedule table schema

remain unchanged.

No database migration is required.

## Testing

Required coverage:

1. shared static validator accepts the complete V1 motive contract;
2. it rejects unknown/runtime-only fields and missing required fields;
3. existing observer dispatch still uses the same accepted/rejected config
   semantics after refactor;
4. `add_volition_interval` stores exactly one `volition.signal` schedule with
   derived source and forced false effect authority;
5. no caller-supplied capability/priority/source/effect field can enter that
   payload;
6. due Volition occurrences preserve existing dedup identity while coalescing
   missed recurring occurrences to the latest due occurrence;
7. repeated tick at the same time does not duplicate events;
8. Volition schedule event priority is 0;
9. existing generic scheduler tests remain unchanged and green;
10. CLI default/ordinary/autonomous behavior remains unchanged;
11. CLI Volition mode validates mutual exclusions and JSON object shape;
12. end-to-end:
    interval occurrence → `volition.signal` → ENDOGENOUS cognition →
    completed zero-capability model run;
13. full local suite and Python 3.11/3.12/3.13 CI remain green.

## Hostile review

> **HOSTILE REVIEWER:** A second helper is needless; callers can already schedule
> `kind="volition.signal"`.

**Accepted as a capability fact, rejected as the safe public seam.** The generic
scheduler is intentionally low-level. The CLI and documented integration need a
closed contract that derives authority-sensitive fields rather than trusting
arbitrary payloads.

> **HOSTILE REVIEWER:** Preserving ordinary catch-up semantics will replay every
> missed motive after the daily autonomy-window shutdown and can amplify stale
> temporal intent into a burst.

**Accepted and fixed.** Recurring Volition schedules coalesce all missed
occurrences to the latest due occurrence. Ordinary and direct-autonomous schedule
catch-up remains unchanged.

> **HOSTILE REVIEWER:** A temporal signal with no dynamic context is too weak to
> be useful.

**Partially accepted.** V1 is useful for stable recurring motives such as
periodic open-loop review. Dynamic telemetry belongs in observers or a later
typed producer. Adding arbitrary temporal context now would recreate context
binding risks without evidence that it is needed.

> **HOSTILE REVIEWER:** Reusing the existing schedule dedup key may conflate a
> route change with a prior occurrence.

**Rejected for V1.** A schedule row has one immutable persisted kind/payload in
the current API. Occurrence identity is already schedule ID plus occurrence
timestamp. V1 adds no update/mutation API that can change a schedule route in
place.

> **HOSTILE REVIEWER:** Supplying `--volition-config-json` without
> `--volition` could silently create ordinary scheduled work instead of the
> intended motive route.

**Accepted and fixed.** Any explicitly supplied Volition config, including an
empty JSON object, fails closed without Volition mode. Volition mode also
requires the config flag to be explicitly present.

> **HOSTILE REVIEWER:** Volition schedules could accidentally inherit
> `TEMPORAL` direct-turn semantics and capabilities from the CLI.

**Accepted and bounded.** Volition mode is mutually exclusive with
`--autonomous`, rejects capabilities and `--reason`, stores
`kind=volition.signal`, and depends on Volition to create any later cognition.
No direct TEMPORAL autonomous turn is emitted.

## Acceptance criteria

Ready for promotion only when:

- shared static config validation replaces observer validation duplication
  without behavior drift;
- existing schedule tests remain green;
- Volition schedules emit only `volition.signal`;
- source is schedule-derived;
- effect authority is false;
- event priority is fixed at 0;
- occurrence dedup remains deterministic and stale Volition occurrences are coalesced with fractional-boundary correctness;
- Volition timing values are finite and cadence is at least one microsecond;
- mode-specific Volition config cannot be silently ignored;
- no direct autonomous fallback exists;
- end-to-end cognition is ENDOGENOUS and zero-capability;
- exact-head local suite is green;
- exact-head GitHub matrix is green;
- hostile and independent reviews have no unresolved release-blocking defect.
