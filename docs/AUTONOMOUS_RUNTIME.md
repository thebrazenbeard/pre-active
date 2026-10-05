# Autonomous Resident Runtime Contract

Status: executable core contract.

## Purpose

Pre-Active is a **continuous resident runtime for self-initiating LLM agency**.

Its core purpose is to remove the requirement that a human must provide every model turn. While a Pre-Active daemon is actually running, the host can continuously monitor every observation source it has explicitly connected and authorized, preserve durable goals and open loops, and grant the model a new cognition turn when something materially warrants attention.

A user prompt is therefore only one possible cause of a model turn.

```text
USER_PROMPT != MODEL_TURN
CONTINUOUS_RUNTIME != CONTINUOUS_INFERENCE
AUTONOMOUS_TURN != EFFECT_AUTHORITY
OBSERVATION != IMPORTANCE
INITIATIVE != PERMISSION
CRITIQUE != AUTHORITY
```

Pre-Active is continuously resident; the model need not infer continuously. The runtime should remain cheap and mostly idle when nothing warrants cognition.

## Sources of autonomous turns

The runtime recognizes four semantic sources:
- `EXTERNAL` — an authorized observer reports a meaningful change in the outside world.
- `TEMPORAL` — a clock, schedule, deadline, or model/host-created timer matures.
- `OPEN_LOOP` — a previously unresolved goal or dependency becomes actionable.
- `ENDOGENOUS` — the model itself requested a later cognition turn.

These source labels describe why cognition was granted. They do not grant tool capabilities or effect authority.

## Endogenous re-entry

Every model run receives the reserved core tool `pre_active.request_turn`.

The tool does not call an external system. It asks Pre-Active to preserve the current run, enter `WAITING`, and schedule the next model turn for the same durable run after a requested delay.

The next turn keeps the same run identity and capability set, receives the durable transcript including the re-entry reason, is generated without a new human prompt, and consumes the run's bounded autonomous-turn budget.

The model cannot use `pre_active.request_turn` to create new filesystem, network, credential, or mutation authority.

```text
RUNNING
   |
   | model: pre_active.request_turn(reason, delay)
   v
WAITING
   |
   | timer/event matures
   v
RUNNING
```

Operator pause/cancel outranks the wait. A `WAITING` run can be paused or cancelled immediately.
## External observation ingress

Hosts and observers can grant promptless model turns through the durable `autonomous.turn` event.

The library surface is `Store.request_autonomous_turn(...)`. The CLI surface is:

```text
pre-active autonomous-turn <task> --source EXTERNAL --reason <reason>
```

Temporal autonomous turns can also be scheduled with `pre-active schedule ... --autonomous --reason ...`.

This is the monitoring seam. Pre-Active does not magically observe an unconnected machine, inbox, repository, sensor, or network. A host adapter must observe an authorized source and provide durable evidence or deltas to Pre-Active.

"Monitor everything 24/7" therefore means **everything the host has deliberately connected and authorized**, not omniscience.

## Observer, Initiator, Critic

A mature continuous runtime needs three cognitive functions. They may be implemented by one model in phases or by separate bounded components.

**Observer:** What changed since the last relevant cognition turn? Preserve provenance and distinguish raw observation from interpretation.

**Initiator:** Given current goals, open loops, changes, and time conditions, is anything worth another model turn? This converts continuous monitoring into selective cognition instead of continuous token generation.

**Critic:** Is this proposed interpretation, re-entry, or action actually justified? The Critic is intentionally Cricket-like: adversarial about semantic drift, self-generated urgency, evidence laundering, authority inflation, and recursive self-stimulation.

The Critic is not a second sovereign identity, moral oracle, or authority source. It can challenge or narrow a bad inference according to host policy; it cannot manufacture permission.
## Self-stimulation boundary

A model that can request its own future turns can otherwise create an infinite loop:

```text
think -> request another turn -> think -> request another turn -> ...
```

Pre-Active therefore applies a durable per-run autonomous-turn budget. Exceeding the configured budget fails the run rather than silently consuming inference forever.

Future initiative policies may add information-gain, novelty, cooldown, or resource budgets, but another model turn should be justified by expected progress, new evidence, a matured dependency, or an explicitly bounded reconsideration policy.

## Authority boundary

Autonomy concerns **who or what may cause cognition to happen**. Authority concerns **what effects that cognition may cause**. They are deliberately separate.

```text
TURN_GRANTED
    != CAPABILITY_GRANTED
    != EFFECT_AUTHORIZED
    != EFFECT_ATTEMPTED
    != EFFECT_VERIFIED
```

An autonomous turn receives only the capabilities already attached to its run or explicitly supplied by the host that created a new autonomous run.

## Local cognitive target

The resident runtime and the LLM are separate lifecycle objects. Pre-Active resolves a user-selected local model target from durable state and uses that target whenever cognition is required. Changing the active target does not change run/effect authority and does not redefine Pre-Active itself.

The selected target is currently a loopback OpenAI-compatible endpoint plus model identifier. A bundled Qwen host is one optional way to satisfy that interface; another local model server may be selected instead. Model availability is therefore a dependency state, not the runtime's liveness state.

## Continuous host lifecycle

The daemon is the resident host loop. Operating-system supervision should keep that process alive across ordinary process failure and reboot.

No source document or configured intent proves that such a process is currently running. Claims of 24/7 monitoring require runtime observation of the installed daemon/service and the observation adapters it is supposed to consume.

The runtime exists before the next prompt; it does not imply hidden model activity when the runtime is stopped.
