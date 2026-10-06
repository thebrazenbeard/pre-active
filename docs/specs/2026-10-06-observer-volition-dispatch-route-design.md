# Observer → Volition Dispatch Route V1 Design

## Status

Approved design, pending written-spec review.

Repository: `thebrazenbeard/pre-active`

Exact design base: `main@f22949f9e2eaeddab3854fec9c4c0634c9ec353e`

## Goal

Allow a durable observer whose initiative policy decides to emit to route that single
emission through canonical Volition instead of directly creating an
`autonomous.turn`.

The route is explicit and mutually exclusive:

```text
OBSERVER_CHANGE
    -> INITIATIVE_DECISION
        -> autonomous_turn
        XOR
        -> volition_signal
```

Existing observers retain their current direct `autonomous_turn` behavior unless
an operator explicitly configures `volition_signal`.

## Non-goals

V1 does not:

- infer a Volition drive kind, target, magnitude, confidence, or provenance from
  arbitrary observation text;
- emit both a direct autonomous turn and a Volition signal for one initiative
  decision;
- let an observer grant capabilities or effect authority through Volition;
- map observer priority or Volition urgency into effect authority;
- add new observer adapters;
- redesign Volition's motive/choice/goal engine;
- make the existing observer `task` field part of Volition motive scoring.

## Architecture

Pre-Active remains the durable observer, event-queue, and run owner. Volition
remains the motive/choice/goal engine.

The observer pipeline remains:

```text
sample
  -> detect snapshot change
  -> durable initiative policy
  -> emit / suppress
```

V1 adds a durable dispatch decision after `emit`:

```text
emit
  -> observer_dispatch.route_kind
       autonomous_turn  -> Store.request_autonomous_turn(...)
       volition_signal  -> VolitionBridge.enqueue_signal(...)
```

The two branches are mutually exclusive. The dispatch route does not participate
in initiative scoring; it only determines which durable event is created after
the initiative gate has already decided to emit.

This keeps two independent questions separate:

```text
INITIATIVE_POLICY = SHOULD_THIS_CHANGE_EMIT?
DISPATCH_ROUTE    = WHERE_DOES_THE_EMISSION_GO?
```

## Durable configuration

Add a separate table rather than widening the existing `observers` table:

```sql
CREATE TABLE IF NOT EXISTS observer_dispatch (
    observer_id TEXT PRIMARY KEY REFERENCES observers(id) ON DELETE CASCADE,
    route_kind TEXT NOT NULL,
    config_json TEXT NOT NULL,
    updated_at REAL NOT NULL
);
```

On `ObserverManager` initialization, every existing observer without a dispatch
row is backfilled as:

```json
{
  "route_kind": "autonomous_turn",
  "config": {}
}
```

This preserves current behavior for existing databases.

New observer creation inserts the observer row, initiative row, and dispatch row
inside the same transaction.

## Dispatch route contract

Supported V1 routes are exactly:

- `autonomous_turn`
- `volition_signal`

Unknown routes fail closed.

### autonomous_turn

This is the compatibility/default route.

Its dispatch config must be an empty object. The existing observer fields retain
their current meaning:

- `task` becomes the autonomous-turn task plus read-only observation evidence;
- `capabilities` are forwarded unchanged;
- `priority` is forwarded unchanged;
- source remains `EXTERNAL`;
- the existing deterministic observer-change dedup identity is preserved.

### volition_signal

This route requires an explicit typed motive mapping.

Required dispatch config:

```json
{
  "target": "investigate-ci",
  "kind": "open_loop",
  "magnitude": 0.8,
  "confidence": 1.0,
  "provenance": "current_observation"
}
```

Optional typed signal fields already supported by the bridge may also be present:

- `expected_information_gain`
- `learning_progress`
- `controllability`
- `predicted_deficit_reduction`
- `current_reappraisal`

The route config must not contain:

- `source`
- `effect_authority`
- `capabilities`
- `priority`

For `volition_signal`, the observer's durable `capabilities` set must be empty.
Configuration fails closed otherwise.

The signal source is derived by Pre-Active:

```text
observer:<observer_id>
```

`effect_authority` is always synthesized as `false`.

The existing observer `priority` value is not forwarded into the Volition
route. `VolitionBridge.enqueue_signal` retains its existing fixed event
priority. Volition urgency remains evidence, not queue/effect authority.

The existing observer `task` field remains stored for schema and CLI
compatibility but is not consumed by the `volition_signal` route in V1.

## Typed observation context

Canonical Volition `Signal` has no arbitrary evidence field. V1 therefore does
not put observation evidence into canonical motive semantics.

Instead, a `volition.signal` event emitted by an observer contains a separate
Pre-Active envelope field named `observation_context`.

Its V1 shape is fixed:

```json
{
  "observer_id": "<uuid>",
  "observer_name": "<name>",
  "observer_kind": "file",
  "digest": "<snapshot digest>",
  "summary": "<adapter summary>",
  "change_count": 7,
  "evidence": {},
  "initiative": {
    "policy_kind": "on_change",
    "reason": "changed",
    "metrics": {}
  }
}
```

The context is read-only evidence. It cannot override or supply:

- Volition target;
- drive kind;
- magnitude or confidence;
- provenance;
- source;
- capabilities;
- queue priority;
- effect authority.

The bridge validates this optional envelope separately from the canonical
Volition `Signal`, then copies it into the generated ENDOGENOUS cognition event
under Volition metadata and into a clearly marked read-only observation-context
section of the cognition task.

Thus:

```text
OBSERVATION_CONTEXT != VOLITION_SIGNAL
OBSERVATION_CONTEXT != MOTIVE_SCORE
OBSERVATION_CONTEXT != CAPABILITY
OBSERVATION_CONTEXT != EFFECT_AUTHORITY
```

CLI-created `volition.signal` events may omit `observation_context` entirely.

## Emission transaction

Observer state, initiative state, route emission, and observer journal evidence
must commit atomically.

When an initiative decision emits:

### autonomous_turn route

The current `Store.request_autonomous_turn(...)` call remains inside the active
observer transaction.

### volition_signal route

The observer builds the exact signal envelope from:

- persisted typed dispatch config;
- derived `observer:<observer_id>` source;
- forced `effect_authority=false`;
- current observation context.

It calls `VolitionBridge(store).enqueue_signal(...)` inside the same observer
transaction.

The dedup key is deterministic:

```text
observer:<observer_id>:volition:<change_count>:<observation_digest>
```

A committed initiative emission therefore creates at most one durable route
event. Rollback removes both the state update and the route event.

The observer transaction does not process Volition synchronously. It only
enqueues the typed `volition.signal`; the ordinary Pre-Active event engine later
claims and processes it.

## Journal semantics

`OBSERVER_CHANGE_DETECTED` remains the emission journal event for both routes.

Its payload gains:

- `dispatch_route`
- `event_kind`
- `event_id`

Existing initiative evidence remains:

- `initiative_policy`
- `initiative_reason`
- `initiative_metrics`

For a `volition_signal` route, `event_kind` is `volition.signal`. For the
default route it is `autonomous.turn`.

Suppressed changes remain `OBSERVER_CHANGE_SUPPRESSED` and create no route
event.

## Observer read surface

`ObserverManager.list()` and `get()` expose an additive `dispatch` object:

```json
{
  "route_kind": "volition_signal",
  "config": {
    "target": "investigate-ci",
    "kind": "open_loop",
    "magnitude": 0.8,
    "confidence": 1.0,
    "provenance": "current_observation"
  },
  "updated_at": 0.0
}
```

As with initiative state, due-observer claiming must not eagerly decode dispatch
JSON before entering the per-observer isolated processing path. A corrupt
dispatch row must not prevent another healthy observer from running.

## CLI

Extend `observer add-file` additively with:

- `--dispatch-route` — default `autonomous_turn`
- `--dispatch-config-json` — default `{}`

The JSON must decode to an object.

Examples:

Existing behavior:

```bash
pre-active --state state.db observer add-file \
  ci-watch /tmp/ci.json "Inspect the CI change" \
  --every 30
```

Opt-in Volition route:

```bash
pre-active --state state.db observer add-file \
  ci-watch /tmp/ci.json "Inspect the CI change" \
  --every 30 \
  --dispatch-route volition_signal \
  --dispatch-config-json '{
    "target":"investigate-ci",
    "kind":"open_loop",
    "magnitude":0.8,
    "confidence":1.0,
    "provenance":"current_observation"
  }'
```

The positional `task` remains required in V1 to avoid a breaking CLI/schema
change. It is ignored by the `volition_signal` dispatch path.

## Validation and failure behavior

Dispatch configuration is validated before durable insertion.

For `volition_signal`, validation reuses the Volition bridge's typed signal
contract using the derived source and forced `effect_authority=false`. The
optional dependency is therefore required to configure or execute a
`volition_signal` observer route; ordinary observers continue to work without
Volition installed.

Malformed persisted dispatch state is handled within the individual observer's
error-isolated path. It must not crash the whole observer tick or block another
due observer.

If Volition becomes unavailable after a route was configured, the observer's
attempt to enqueue that route fails and the observer transaction rolls back.
The observer records its ordinary isolated error on the subsequent error path;
it does not fall back to a direct autonomous turn, because fallback would violate
the mutually exclusive route contract.

There is no silent route substitution.

## Authority boundary

The route must preserve all existing boundaries:

```text
OBSERVATION != INITIATIVE_DECISION != VOLITION_SIGNAL
            != WANT != CHOICE != GOAL
            != COGNITION_REQUEST != CAPABILITY != EFFECT_AUTHORITY
```

Specific V1 rules:

1. `volition_signal` observers must have `capabilities=[]`.
2. The route synthesizes `effect_authority=false`; configuration cannot override it.
3. Generated Volition cognition remains `source=ENDOGENOUS`.
4. Generated Volition cognition remains `capabilities=[]`.
5. Observation evidence is context only.
6. Initiative metrics do not alter permissions.
7. Volition urgency does not alter permissions.
8. No dispatch route executes a tool or external effect by itself.

## Compatibility

Existing databases are behavior-preserving:

- dispatch rows are backfilled to `autonomous_turn`;
- current observer task/capability/priority semantics remain unchanged;
- initiative state and temporal models are unchanged;
- existing CLI invocations continue to parse and behave identically.

The new route is strictly opt-in.

## Testing strategy

Implementation uses TDD through the highest practical public seams.

Required red-green coverage:

1. Existing observer backfill exposes `dispatch.route_kind=autonomous_turn`.
2. Default observers still emit exactly one `autonomous.turn` with their
   existing capability and priority semantics.
3. A `volition_signal` observer requires an empty capability set.
4. Unknown dispatch route fails closed before insertion.
5. Malformed/non-object dispatch JSON is rejected by CLI.
6. A valid Volition dispatch emits exactly one `volition.signal` and no direct
   `autonomous.turn`.
7. The emitted signal contains only configured typed motive fields plus derived
   source, forced false effect authority, and typed `observation_context`.
8. The deterministic observer→Volition dedup key prevents duplicate route
   emission on replay.
9. Observer state, initiative state, and signal enqueue roll back together on an
   injected failure.
10. A corrupt dispatch row for one due observer does not block another healthy
    observer.
11. The Volition bridge copies validated observation context into the resulting
    ENDOGENOUS cognition event while preserving `capabilities=[]` and
    `effect_authority=false`.
12. End-to-end: file change → initiative emit → `volition.signal` →
    `ENDOGENOUS autonomous.turn` → zero-capability model run.
13. Existing observer, initiative, bridge, autonomy, and effect tests remain
    green.
14. Full Python 3.11/3.12/3.13 CI remains green.

## Hostile review

> **HOSTILE REVIEWER:** A route layer is unnecessary. Make Volition another
> initiative policy and avoid a new table.

**Rejected with architecture evidence.** Initiative policies answer whether a
change warrants emission and already compose temporal evidence such as Hawkes
state. Volition decides motive/goal/cognition after a signal exists. Treating
Volition as an initiative policy would conflate temporal emission scoring with
motive arbitration and prevent clean composition such as Hawkes-gated Volition
signals.

> **HOSTILE REVIEWER:** Emit the direct autonomous turn and the Volition signal;
> the second path adds useful redundancy.

**Rejected.** One observation would then have two independent cognition paths,
creating amplification and making budgets/deduplication harder to reason about.
V1 uses an XOR route.

> **HOSTILE REVIEWER:** Passing observation evidence through the signal lets
> arbitrary text manipulate Volition.

**Accepted and bounded.** Observation evidence is a separate typed Pre-Active
envelope, not a canonical Volition `Signal` field. The configured motive fields
are fixed before the observation occurs. The envelope is propagated only after
Volition has selected cognition and carries no authority.

> **HOSTILE REVIEWER:** Requiring empty observer capabilities makes the route less
> useful because the eventual cognition cannot act.

**Accepted by design.** This route is specifically motive-to-cognition
integration. Effect authority must be acquired through a separately authorized
mechanism; observer configuration must not manufacture it. V1 proves the safe
cognition seam first.

## Acceptance criteria

The feature is ready for merge only when all of the following are true:

- existing observers retain exact default behavior;
- `volition_signal` routing is explicit and mutually exclusive;
- typed motive configuration is validated before persistence;
- observer capabilities are empty for Volition routing;
- observation context is demonstrably separate from motive scoring and authority;
- observer→signal transaction and replay behavior are non-amplifying;
- one corrupt observer cannot block healthy peers;
- an end-to-end real bridge test proves the complete zero-capability path;
- full local suite passes;
- exact-head CI passes on Python 3.11, 3.12, and 3.13;
- internal hostile review has no unresolved release-blocking finding;
- any independent review is labeled separately from internal review.
