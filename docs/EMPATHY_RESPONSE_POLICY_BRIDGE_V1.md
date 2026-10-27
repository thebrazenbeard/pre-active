# Empathy → Pre-Active response-policy bridge V1

Status: opt-in source integration, **not** a Lappy installation or verified
production effect.

Pre-Active may consult an already-admitted Empathy response-policy hint *only*
while preparing model input for a **zero-capability, no-autonomous-reentry** run.
Zero external capabilities alone is insufficient: `pre_active.request_turn` can
still be available internally, and advice is withheld while it is exposed. It cannot use that
hint to create a task, Volition want, Conations preference, autonomous wakeup,
tool capability, or external effect.

## Ownership

| Boundary | Owner |
| --- | --- |
| Person authentication, conversation scope, present evidence, revocation | Trusted private host |
| Perspective inference and policy candidate | Empathy `response_policy_hint()` |
| Persistence of goals and current wants | Volition |
| Historical directional tendencies | Conations (history is not standing consent) |
| Run/queue/tool authorization and model inference | Pre-Active |
| Temporal expiry and replay protection | Trusted host; not inferred from `source='current'` text |

The bridge exposes `EmpathyAdvisory(policy, focus_needed, ...)` and an
explicit `Engine(..., empathy_response_only=True, empathy_adviser=callback)`
opt-in. The default `empathy_response_only=False` leaves the existing runtime
and autonomous tool exposure unchanged, even if a callback was provided.
When opted in, a zero-capability run suppresses internal
`pre_active.request_turn` before model inference, with or without a returned
hint; the same `allow_autonomous_reentry=False` flag is used for both the
advice gate and the offered tool specs. The callback receives only an
opaque run ID, not a person model or task text, and may return `None`. The host
must map that run ID to an explicitly authenticated, current, properly admitted
Empathy hint. Nothing in the normal Pre-Active CLI config enables the callback.

If advice is returned, the engine verifies a seven-member policy allowlist,
boolean `focus_needed`, and exact `NONE`/`EPHEMERAL`/`RESPONSE_POLICY_ONLY`
authority markers before the model sees it. The model prompt contains only
the policy name and focus flag. It does **not** contain evidence, person IDs,
relationship history, free-form reason text, scores, or provenance identifiers.
This makes the code-level response route auditable while avoiding leakage of
private person-model contents through the prompt bridge.

When a run has any capabilities **or** its current turn permits internal
`pre_active.request_turn`, the callback is never invoked. The tool
registry, model tool specs, durable run capabilities, schedule, and Volition
bridge are unchanged. A malicious/unadmitted candidate is an error, not a
source of effect authority.

## Illustrative host-only wiring

```python
from pre_active.empathy_policy import EmpathyAdvisory

def admitted_response_policy(run_id: str) -> EmpathyAdvisory | None:
    # Pseudocode boundary: this host-owned service must authenticate the
    # person and current context before invoking Empathy. It does not exist
    # merely because Pre-Active or Empathy source is installed.
    admitted = private_host_admission_for_run(run_id)
    if admitted is None:
        return None
    hint = empathy_research.integration_contract.response_policy_hint(admitted)
    return EmpathyAdvisory(
        policy=hint.policy,
        focus_needed=hint.focus_needed,
        schema=hint.schema,
        consumer=hint.consumer,
        authority=hint.authority,
        persistence=hint.persistence,
        volition_want_authority=hint.volition_want_authority,
        effect_authority=hint.effect_authority,
    )

engine = Engine(
    ..., empathy_response_only=True, empathy_adviser=admitted_response_policy
)
```

The sample's `private_host_admission_for_run` and `Engine(...)` arguments are
intentionally incomplete; no such authenticated runtime is claimed here.

## Qualification and falsifiers

Run `python -m pytest tests/test_empathy_policy_integration.py` and the full
Pre-Active test suite with pinned optional Volition/MESO dependencies.
An optional cross-source test imports `empathy_research` when that package is
available on `PYTHONPATH`; otherwise it is reported as skipped, never as
verified live interoperability.

- No adviser means byte-for-byte normal response route.
- Capability-bearing runs and turns with `pre_active.request_turn` available
  must never consult private person-specific advice.
- The default mode retains `pre_active.request_turn` where previously enabled;
  explicit response-only mode suppresses it on zero-capability runs even if
  no hint is available.
- Invalid policy, authority, consumer, schema, focus type, or persistence fails.
- Response-policy advice must not alter the set of tool specs or run capabilities.
- The host must be separately tested for authentic person scope, freshness,
  correction/withdrawal, private evidence governance, and cross-person isolation.
- Human/performance evaluation is separately needed before claiming empathetic
  accuracy or useful downstream behavior; a prompt annotation is not proof.
