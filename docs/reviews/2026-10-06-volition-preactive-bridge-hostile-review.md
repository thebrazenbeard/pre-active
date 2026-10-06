# Volition ↔ Pre-Active Bridge V1 — Internal Hostile Review

Date: 2026-10-06
Review class: **INTERNAL_HOSTILE_REVIEW** — this is not independent review.
Reviewed code head: `773bf33168047feccbde08a61c61eaf0a884fc50`
Base: `pre-active/main@a6900dc2` at branch creation.
Canonical Volition binding: `thebrazenbeard/volition@dbc628d376515a0a523b1eecdf62129cca5d6b08`.

## Claim under review

V1 connects explicit typed Volition motive signals to Pre-Active's durable resident queue so a real Volition `CognitionRequest` can become a bounded Pre-Active `ENDOGENOUS` model turn while preserving the existing authority boundary.

It does **not** claim that arbitrary observations become wants, that Volition is continuously active between events, that a cognition request grants tools, or that motive urgency grants effect authority.

Invariant:

`VOLITION_SIGNAL != WANT != CHOICE != GOAL != COGNITION_REQUEST != CAPABILITY != EFFECT_AUTHORITY`

## Objections and disposition

> **HOSTILE REVIEWER:** This is a second scheduler in disguise. A weak event can become a model call and bypass Pre-Active's admission and authority controls.

**PARTIALLY ACCEPTED; BOUNDED.** The risk is real if the bridge infers motives from arbitrary event text or dispatches the model directly. V1 does neither. Only explicit `volition.signal` events are accepted. Volition decides whether a cognition request exists; Pre-Active remains the queue/run admission layer. Generated cognition events use `source=ENDOGENOUS`, `capabilities=[]`, and `effect_authority=false`. Urgency is preserved as evidence but does not alter priority or permissions.

> **HOSTILE REVIEWER:** Co-installing two packages is not integration. Persisting a snapshot does not prove the resident runtime consumes Volition output.

**REJECTED WITH RUNTIME EVIDENCE, within scope.** The isolated real-model smoke used the actual Lappy Ollama target `vera-local:latest` with a separate SQLite state. One explicit `volition.signal` produced one durable `autonomous.turn` with `source=ENDOGENOUS`, zero capabilities, and `effect_authority=false`. The resulting run executed model cognition. The model then used Pre-Active's existing bounded `pre_active.request_turn` mechanism, reaching `autonomous_turn_count=2` and `status=WAITING`; this is evidence of runtime consumption and bounded self-reentry, not evidence that the goal completed.

Observed readback:
- signal event: `dac8cb0a-c57d-407f-8f5a-9d5809844536`
- first cognition event: `b47e531e-2f53-47ee-869b-370b8c9414a6`
- Volition state revision: 1
- source: `ENDOGENOUS`
- capabilities: empty
- effect authority: false
- model run status after bounded reentry: `WAITING`
- autonomous turn count: 2

> **HOSTILE REVIEWER:** A transient Volition runtime failure occurs before the ordinary model-step retry block. It can leave the signal event CLAIMED until lease expiry instead of promptly rescheduling.

**ACCEPTED; FIXED.** Regression `test_engine_retries_volition_execution_failure_immediately` first failed with the event left `CLAIMED`. The engine now routes Volition execution failures through bounded event retry/dead-letter handling immediately. Red task: `volition-bridge-hostile-retry-red`; green task: `volition-bridge-hostile-retry-green`.

> **HOSTILE REVIEWER:** Catching every `ValueError` as malformed external input conflates invalid signal syntax with corruption/incompatibility in persisted `VOLITION_STATE_V2`. That could silently acknowledge a signal while hiding a damaged state store.

**ACCEPTED; FIXED.** The bridge now raises the dedicated `InvalidVolitionSignal` type only for external signal-contract violations. The engine reject-and-acks only that class. A ValueError raised while restoring persisted Volition state now follows retry/dead-letter handling. Regression `test_engine_retries_corrupt_persisted_volition_state_instead_of_rejecting_signal` was red before the distinction and green afterward.

> **HOSTILE REVIEWER:** Duplicate delivery or crash replay can consume Volition's endogenous cognition budget twice or emit duplicate cognition events.

**REJECTED WITH TEST EVIDENCE, within the single SQLite queue boundary.** Signal receipts are keyed by source event ID, cognition enqueue uses a deterministic source-event dedup key, and snapshot update + receipt + cognition enqueue occur inside the claimed-event transaction. Focused tests cover replay without a second state revision or second cognition event and budget persistence across subsequent signals.

> **HOSTILE REVIEWER:** The bridge silently depends on whatever Volition happens to be installed locally, making CI and deployment semantically unstable.

**ACCEPTED; FIXED.** The `volition` and `dev` extras pin the exact canonical Volition commit `dbc628d376515a0a523b1eecdf62129cca5d6b08`. A clean empty-venv install resolved and imported `volition-0.10.0` from that exact commit.

## Remaining limits

- V1 has an explicit typed signal ingress; it does not yet define which observers or higher-level policies should generate each motive signal.
- V1 persists one canonical Volition engine state in a Pre-Active store. Multi-agent/multi-identity Volition namespaces are not implemented.
- The bridge creates cognition, not external authority. Any later tool/effect request remains subject to existing Pre-Active capability and effect controls.
- The real-model smoke used an isolated state and the local Ollama provider. It did not modify or switch the resident production Pre-Active installation.
- This review is internal hostile review only. No independent reviewer has approved the change.

## Verification evidence

- Baseline before bridge work: 165 tests passed.
- Focused bridge suite after hostile fixes: 11 tests passed.
- Clean install from `.[dev]`: exact Volition commit resolved and imported successfully.
- Isolated real-model smoke: explicit Volition signal → durable ENDOGENOUS cognition → zero-capability model run; model used bounded reentry and remained WAITING.
- Pre-PR full regression after all hostile-review code fixes: **176 passed in 7.55s**.
