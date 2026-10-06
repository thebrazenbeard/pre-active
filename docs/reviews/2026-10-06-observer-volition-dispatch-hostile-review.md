# Observer → Volition Dispatch Route V1 — Internal Hostile Review

Date: 2026-10-06

Review class: **INTERNAL_HOSTILE_REVIEW**

Repository: `thebrazenbeard/pre-active`

Base: `main@f22949f9e2eaeddab3854fec9c4c0634c9ec353e`

Feature branch: `feat/observer-volition-dispatch-route-v1-20261006`

This review is internal engineering evidence. It is **not** independent review.

## Scope

Attack the load-bearing V1 claims:

- exactly one route per initiative emission;
- no observation/config path can manufacture capabilities or effect authority;
- observer state and route event are transactionally consistent;
- replay/unchanged observations do not amplify;
- corrupt observer dispatch state cannot block healthy peers;
- loss of Volition does not silently fall back to direct cognition;
- existing observers preserve their default direct behavior.

## Finding 1 — operator dispatch config was too open

> **HOSTILE REVIEWER:** The approved design says motive fields are fixed operator
> configuration and runtime observation context is separate. But if dispatch config
> accepts arbitrary keys, an operator/config corrupter can pre-populate
> `observation_context` or another future bridge field and collapse that boundary.

**ACCEPTED; FIXED.**

The initial implementation rejected only the four explicitly reserved authority
fields. That was insufficient because bridge payloads can grow over time.

Fix at commit `f503f379a596c7defef3cf79737e32d5e41e8ae7`:

- dispatch config now uses an explicit allowlist of canonical typed motive fields;
- unsupported keys fail closed;
- `observation_context` therefore cannot be supplied by durable motive config;
- the five V1 required motive fields — target, kind, magnitude, confidence, and
  provenance — must all be explicit rather than inherited from bridge defaults.

Regression evidence:

- `test_volition_dispatch_rejects_unknown_typed_config_field`;
- `test_volition_dispatch_requires_explicit_confidence`.

## Finding 2 — dual cognition path / amplification

> **HOSTILE REVIEWER:** One observer change could create both a direct EXTERNAL
> autonomous turn and a Volition signal, or repeated polling of an unchanged
> snapshot could repeatedly excite Volition.

**REJECTED WITH EVIDENCE.**

The route implementation uses one branch selected after the initiative decision:

- `autonomous_turn` calls `request_autonomous_turn`;
- `volition_signal` calls `VolitionBridge.enqueue_signal`;
- no fall-through executes the other route.

The Volition dedup identity is deterministic over observer id, change count, and
digest. Ordinary observer change detection also suppresses an unchanged snapshot.

Evidence:

- `test_default_dispatch_emits_only_external_autonomous_turn`;
- `test_volition_dispatch_emits_one_signal_and_no_direct_turn`;
- `test_volition_dispatch_unchanged_snapshot_does_not_amplify_signal`.

## Finding 3 — capability/effect-authority leakage

> **HOSTILE REVIEWER:** Observer priority, capabilities, or attacker-shaped
> observation evidence could be reinterpreted as authority by the Volition path.

**REJECTED WITH EVIDENCE.**

The route requires the observer capability set to be empty at configuration and
again validates the persisted capability set inside the emission transaction.
A corrupted persisted non-empty capability set fails closed before signal enqueue.

The observer priority is not forwarded. `VolitionBridge.enqueue_signal` stores
the route event at its fixed priority 0.

The bridge creates cognition with:

- `source="ENDOGENOUS"`;
- `capabilities=[]`;
- `volition.effect_authority=false`.

Observation context is separately validated and copied as read-only evidence only.
Nested evidence values named `capabilities` or `effect_authority` do not alter
the generated capability/effect fields.

Evidence:

- `test_volition_dispatch_validation_fails_closed_before_insert`;
- `test_volition_dispatch_corrupt_capabilities_fail_closed_without_signal`;
- `test_volition_dispatch_emits_one_signal_and_no_direct_turn` verifies event
  priority is 0 despite observer priority -99;
- `test_bridge_copies_valid_observation_context_into_endogenous_cognition` uses
  forged authority-looking nested evidence while asserting the generated cognition
  remains zero-capability and effect-authority false.

## Finding 4 — partial commit after route enqueue

> **HOSTILE REVIEWER:** A signal could commit while observer digest/change count or
> initiative state rolls back, causing the same physical change to be emitted again.

**REJECTED WITH TRANSACTIONAL EVIDENCE.**

Observer observation-state update, initiative-state update, route enqueue, and
`OBSERVER_CHANGE_DETECTED` journal append run under one explicit
`BEGIN IMMEDIATE` transaction.

Two injected failures cover both sides of signal creation:

- enqueue failure: no observer/initiative state or event persists;
- failure in the detection journal *after* enqueue: the newly enqueued
  `volition.signal` is also rolled back.

Evidence:

- `test_volition_dispatch_enqueue_failure_rolls_back_observation_state`;
- `test_volition_dispatch_journal_failure_rolls_back_enqueued_signal`.

## Finding 5 — corrupt dispatch state blocking unrelated observers

> **HOSTILE REVIEWER:** Eager dispatch JSON decoding during due-claim could crash
> the tick before the per-observer isolation boundary, repeating the earlier
> initiative-state failure class.

**REJECTED WITH EVIDENCE.**

Due claim uses `_row_to_record(... include_dispatch=False)`. Dispatch lookup and
JSON decoding happen only inside `_record_observation`, which is wrapped by the
per-observer error-isolated tick path.

Evidence:

- `test_due_claim_does_not_decode_corrupt_dispatch_json`;
- `test_corrupt_dispatch_does_not_block_healthy_due_observer`.

## Finding 6 — missing Volition dependency causing silent fallback

> **HOSTILE REVIEWER:** If Volition disappears after configuration, silently
> falling back to a direct EXTERNAL turn would violate XOR routing and could restore
> capabilities/priority semantics the operator explicitly did not choose.

**REJECTED WITH EVIDENCE.**

The implementation has no fallback path. A Volition runtime failure raises inside
the observer transaction; state/event changes roll back; the ordinary per-observer
error path records the failure; no direct autonomous turn is emitted.

Evidence:

- `test_volition_dispatch_does_not_fallback_when_runtime_becomes_unavailable`.

## Finding 7 — existing observer compatibility

> **HOSTILE REVIEWER:** Adding a second durable observer configuration table could
> accidentally change old databases or direct-route behavior.

**REJECTED WITH EVIDENCE.**

Initialization backfills missing dispatch rows to:

```json
{"route_kind":"autonomous_turn","config":{}}
```

New observers use the same default. Existing direct route task, capabilities,
priority, source, and dedup semantics remain in the direct branch.

Evidence:

- `test_observer_dispatch_backfills_existing_observer`;
- `test_observer_dispatch_defaults_to_autonomous_turn`;
- `test_default_dispatch_emits_only_external_autonomous_turn`;
- full regression suite is required again on the exact frozen head before push.

## Internal verdict

**SURVIVES after one accepted/fixed configuration-boundary defect.**

No unresolved release-blocking internal finding remains at this stage.

This verdict does not substitute for:

- exact-head local verification;
- GitHub matrix CI;
- independent read-only review;
- deployment/runtime verification.
