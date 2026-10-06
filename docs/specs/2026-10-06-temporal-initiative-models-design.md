# Temporal Initiative Models V1 Design

## Status

Research-grounded implementation design for `thebrazenbeard/pre-active`, based on `feat/durable-observers-20261005@8a3ef44dbdc03a81335488514ee1f35ad5528812`.

## Problem

Durable observers currently implement a binary rule: a changed snapshot emits an `autonomous.turn`. That is a sound baseline but it cannot distinguish isolated change from bursty change, cannot express a refractory period, and has no durable seam for temporal evidence models.

The mathematical models considered for Pre-Active are not interchangeable. They require different data shapes and assumptions.

## Reality check

1. An exponential-kernel Hawkes intensity has a finite-dimensional recursive Markov state. This makes it practical for a resident runtime without replaying the full event history.
2. A linear Hawkes process is excitatory. Negative kernels can produce invalid negative intensities; inhibition belongs in a nonlinear/rectified model or a separate deterministic control. V1 therefore does **not** model cooldown by allowing negative Hawkes excitation.
3. For a univariate exponential Hawkes intensity `mu + excitation`, the integrated branching ratio is `alpha / beta`. V1 requires `alpha / beta < 1` to reject supercritical configurations.
4. ARIMA/SARIMA/ARIMAX, GARCH, OU/diffusion models and VAR require regularly sampled numeric observations or residual streams. The current `Observation` contract is evidence-oriented and not a regular numeric-series contract. They remain architectural candidates, not V1 implementations.
5. HMMs require an explicit state/emission model. They can fit the same future model seam, but adding generic HMM configuration before a concrete observer use case would add configuration surface without a validated runtime need.
6. Poisson is the zero-excitation special case of the Hawkes intensity used here; a separate Poisson implementation would duplicate V1 behavior.
7. Renewal/self-correcting models are useful for adaptive scheduling and refractory behavior, but observer polling is currently fixed-interval. V1 keeps refractory control explicit and deterministic rather than mislabeling it as a fitted stochastic process.

## Design

Create `src/pre_active/initiative.py` with a small policy contract:

- `InitiativeDecision`: immutable result containing `emit`, durable `state`, numeric `metrics`, and a machine-readable `reason`.
- `InitiativePolicy`: `decide(now, state) -> InitiativeDecision`.
- `OnChangePolicy`: preserves current behavior.
- `ExponentialHawkesThresholdPolicy`: updates exponential excitation recursively and emits only when post-event conditional intensity meets a configured threshold and the deterministic cooldown has elapsed.

The Hawkes policy is an evidence/initiative model, not an effect-authority mechanism and not a random event generator. It computes a conditional-intensity score from actual observed changes; a deterministic policy threshold decides whether a model turn is warranted.

## Hawkes state

Configuration:

- `baseline_rate >= 0`
- `excitation >= 0`
- `decay_rate > 0`
- `excitation / decay_rate < 1`
- `wake_threshold >= 0`
- `cooldown_seconds >= 0`

Durable state:

- `excitation`: accumulated post-event excitation
- `last_event_at`: latest admitted observation-change time
- `last_emit_at`: latest change that emitted an autonomous turn

For a new change at time `t`:

`decayed = prior_excitation * exp(-decay_rate * max(0, t - last_event_at))`

`post_excitation = decayed + excitation`

`post_intensity = baseline_rate + post_excitation`

The change emits when `post_intensity >= wake_threshold` and cooldown is satisfied. Suppressed changes still update excitation, so a genuine burst can cross the threshold later.

## Observer integration

Add a separate `observer_initiative` table instead of widening the existing observer table. It is keyed by observer ID and stores policy kind, JSON config, JSON state, last decision, and update time.

On manager initialization, existing observers are backfilled with `on_change`, preserving behavior. New observer configuration validates the policy before durable insertion.

A detected post-baseline change increments `change_count` whether emitted or suppressed. Suppression is journaled as `OBSERVER_CHANGE_SUPPRESSED`; emission keeps `OBSERVER_CHANGE_DETECTED`. Policy state and observation state commit atomically.

## CLI

Extend `observer add-file` with:

- `--initiative-policy` (default `on_change`)
- `--initiative-config-json` (default `{}`)

The JSON must decode to an object. Policy-specific validation happens in the initiative module.

## Boundaries

`TEMPORAL_MODEL != IMPORTANCE != MODEL_TURN != CAPABILITY != EFFECT_AUTHORITY`

No policy may add capabilities, execute tools, mutate external systems, or bypass autonomous-turn budgets.

## Testing

Use TDD. First establish red tests for Hawkes recursion, stability validation, cooldown, and observer suppression/escalation. Then implement the smallest policy module and observer integration. Existing observer tests must remain green. Full CI must pass on Python 3.11, 3.12, and 3.13.
