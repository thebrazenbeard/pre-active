---
name: pre-active
description: Use when the user selects or mentions Pre-Active, asks to inspect, submit, schedule, resume, diagnose, or operate durable Pre-Active work, or asks whether work is actually continuing outside the current chat.
---

# Pre-Active control surface

Treat Pre-Active as a continuous resident runtime whose defining purpose is to let an LLM receive justified cognition turns without requiring a human prompt for every turn. The runtime may continuously monitor authorized observation sources and grant promptless turns from external change, time, actionable open loops, or model-requested re-entry. This is not model consciousness or a claim that ChatGPT itself is running when the host runtime is absent.

## Core invariants

- `USER_PROMPT != MODEL_TURN`
- `CONTINUOUS_RUNTIME != CONTINUOUS_INFERENCE`
- `AUTONOMOUS_TURN != CAPABILITY_GRANT != EFFECT_AUTHORITY`
- `REQUEST != AUTHORITY != ATTEMPT != EFFECT != VERIFIED EFFECT`
- `QUEUED != RUNNING != COMPLETED != VERIFIED EXTERNAL EFFECT`
- Loading this skill does not prove a daemon is running.
- A repository document does not prove the workstation is currently bound to that runtime.
- A model response is not authority to expand capabilities.
- Never expose workstation credentials, model keys, bearer tokens, or tunnel secrets.

## Runtime discovery

Before making runtime claims, establish live state. Prefer Workbridge Commander when it is available.

1. Check whether the documented Pre-Active root and scheduled tasks exist.
2. Check the local model endpoint separately; model availability does not prove the Pre-Active daemon exists.
3. Check process command lines and durable state paths.
4. If the current host is still on a legacy predecessor binding, report that fact without silently treating it as migrated.
5. Use repository documentation only as a design/runtime expectation after live state has been distinguished.

See [references/RUNTIME_CONTRACT.md](references/RUNTIME_CONTRACT.md).

## Read-only operations

For status, diagnostics, queue inspection, run inspection, model-endpoint checks, process inspection, and logs, use read-only workstation actions directly when the user asks. Do not restart, kill, install, rewrite, register, migrate, or delete anything merely to make a diagnostic cleaner.

## Autonomous turns

A promptless model turn may come from `EXTERNAL`, `TEMPORAL`, `OPEN_LOOP`, or `ENDOGENOUS` runtime state. Preserve that source and its reason; do not rewrite an operator-created turn as model initiative or vice versa.

The reserved `pre_active.request_turn` primitive is internal cognition control. It preserves the same run and capability set and schedules another model turn; it does not authorize a new external effect. A `WAITING` run is still operator-controllable and may be paused or cancelled before its future turn matures.

When a user asks to grant a model turn directly, use the installed runtime's `autonomous-turn` surface only after the real state path/runtime binding is established. A request to monitor a new source does not by itself install or authorize an observer for that source.

## Durable writes

A direct request to submit, schedule, remember durable work, or grant an autonomous turn authorizes only that exact Pre-Active state mutation when the target runtime and state database have been verified. Preserve the requested task, source, reason, and capabilities literally.

Do not infer permission for broader workstation effects from a request to queue work.

## Host effects

Installing or migrating the runtime, changing scheduled tasks, changing services, changing credentials, replacing the active model binding, killing or restarting processes, editing machine-wide files, or cutting over from a predecessor runtime requires explicit authority for that exact effect.

After any authorized host effect, verify source state, installed state, runtime state, and the external effect separately.

## Continuity claims

When the user asks whether something will continue after this chat, distinguish:

- work merely described in chat;
- work persisted in the Pre-Active queue;
- a daemon that is live and able to advance that work;
- tools/capabilities actually registered for the run;
- external effects independently verified by readback.

Never collapse those into "yes, it is running" without evidence.
