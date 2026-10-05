# Long-Horizon Runtime Donor Audit

Status: architecture/provenance record. These repositories are donors and comparison surfaces, not runtime dependencies.

Bound Pre-Active baseline for this audit:

- `thebrazenbeard/pre-active@dfa455646df72c169c981a2c6351bbaedcd1216a`

Donor heads inspected:

- `AMAP-ML/LongHorizon-Harness@a1dd930614972b92361c1b9cd6aac441a6db5a65`
- `vault-developer/event-loop-explorer@d9813277868c11aa8c10e36699812f19e1361e02`
- `LarsCowe/bmalph@9d2ab88c47838c79f08d5bf6ebf94be8cde72323`
- `damonwan1/AutoScholarLoop@8206c7581049a78cab9cf20723d70a846b43e7df`
- `inferablehq/inferable@c67acb8172cf0957ce872c818d8d4a8434f20f75`
- `ray-r-ren/agent-apprenticeship@4beafff2ff41da7d97a4faee9b516ccde466fb4b`
- `vxcozy/workflow-orchestration@547a0a0de6395eebed16f2e86043faf3cfb0863e`

## Governing distinction

Pre-Active already separates request, authority, attempt, external effect, and verified effect.

The next missing truth layer is analogous but concerns work progress:

```text
ATTEMPT != EVIDENCE != VERIFIED PROGRESS != COMPLETION
```

A model saying that work advanced is evidence. A tool returning successfully may be evidence. Neither automatically becomes trusted progress unless the relevant acceptance condition has actually been checked.

This is the strongest common lesson across the useful donors below.

## LongHorizon-Harness — ADOPT / ADAPT

Useful mechanisms:

- rebuild each round from original goal + verified state + failure evidence + remaining work;
- bounded next-step execution rather than one giant context;
- independent Manager / Executor / Auditor responsibilities;
- only accepted, independently checked results enter trusted task state;
- failed execution remains evidence and feeds recovery rather than being silently treated as progress;
- partial trajectory survives local episode timeout and later rounds inspect the real workspace again.

Pre-Active mapping:

- existing durable runs/events already supply the round ledger and crash recovery;
- Observer / Initiator / Critic should not collapse into executor self-approval;
- a future checkpoint primitive should distinguish candidate evidence from verified progress;
- a final answer should be based on verified state where the task has explicit acceptance checks.

Do not copy:

- the exact three-role topology as a mandatory runtime shape;
- fresh-context-per-round as a universal rule;
- any assumption that an auditor model is automatically independent merely because it is a separate call.

## Event Loop Explorer — ADAPT SYMBOLICALLY ONLY

Useful conceptual separation:

- call stack / active work;
- queued tasks;
- microtasks;
- rendering / lower-priority periodic work.

This is a useful mental model for Pre-Active wake semantics:

```text
resident host
  -> urgent external/control events
  -> admitted run steps
  -> endogenous follow-up
  -> periodic maintenance / observation
```

Do not copy:

- JavaScript event-loop timing rules;
- browser task/microtask ordering as if it were a general agent-runtime law;
- its simplified render timing.

This repository is primarily a visualization/teaching donor, not an execution-kernel donor.

## bmalph / Ralph — ADOPT LATER

Useful mechanisms:

- circuit-breaker state around repeated no-progress loops;
- separate counters for repeated failure classes such as same error and permission denial;
- cooldown / half-open recovery instead of permanently killing the whole system;
- tracking the last loop that produced real progress.

Pre-Active mapping:

- total step and autonomous-turn ceilings already stop infinite work;
- bounded endogenous dialogue already stops immediate recursive self-stimulation;
- a future no-progress breaker should be based on **verified progress**, not on model self-report;
- provider outage retry and model-loop no-progress should remain separate failure classes.

Do not add a no-progress breaker until a trustworthy progress signal exists. Otherwise the runtime would be measuring confidence, verbosity, or transcript churn and calling it progress.

## AutoScholarLoop — ADOPT / ADAPT

Useful mechanisms:

- explicit stage/round checkpoints;
- append-only progress index;
- resumability from the latest completed checkpoint;
- role-separated execution and review artifacts;
- quality gates and explicit fallback routes;
- criticism must produce actionable objections and revision requirements;
- canonical output may coexist with checkpoint history, but checkpoint history remains the audit trail.

Pre-Active mapping:

- add durable run checkpoints with at least `CANDIDATE`, `VERIFIED`, and `REJECTED` states;
- verification must record evidence/verifier/reason instead of silently overwriting the candidate;
- future context assembly may privilege VERIFIED checkpoints while retaining rejected/candidate evidence for debugging;
- Critic output should narrow or reject candidate progress, not manufacture authority.

Do not copy:

- mandatory minimum discussion rounds for every domain;
- research-specific S00-S04 stage names;
- forced multi-agent deliberation when a mechanical verifier is stronger.

## Inferable — ADOPT / ADAPT

Useful mechanisms:

- durable interrupt/resume;
- workflow version affinity for already-running work;
- structured outputs with validation;
- memoized results for expensive or side-effecting operations;
- timeline observability.

Pre-Active already has strong equivalents for durable pause/cancel, structured tool admission, durable mutation/effect replay, and journal observability.

Missing high-value mechanism:

- **run contract version affinity**.

A long-running run should remember the runtime/workflow contract it began under. A source upgrade must not silently reinterpret an in-flight durable run under incompatible semantics.

Do not copy:

- managed-control-plane assumptions;
- network/cloud coordination that the single-host SQLite runtime does not need.

## Agent Apprenticeship — ADAPT

Useful mechanisms:

- verifier/mentor evaluation separated from apprentice execution;
- maximum iterations plus stop-on-verifier-pass;
- stop-on-no-improvement;
- reusable experience compilation from completed work.

Pre-Active mapping:

- verifier-pass is a better completion signal than model confidence where a verifier exists;
- no-improvement should eventually key off verified checkpoint movement;
- completed run traces may later be compiled into local lessons, but that is a distinct training/memory layer.

Do not copy:

- automatic public contribution of private run traces;
- shared-ecosystem assumptions;
- economic-value scoring as a kernel concern.

Private runtime evidence remains private unless explicitly exported.

## workflow-orchestration — ADOPT AS BEHAVIOR, NOT KERNEL

Useful mechanisms:

- verify before declaring completion;
- failed verification returns to planning;
- capture corrections as reusable lessons;
- avoid over-engineering.

These reinforce existing Pre-Active and Vera operating discipline, but they do not justify a new runtime subsystem by themselves.

## Resulting Pre-Active frontier

### P0 — finish live host requalification

Rebind Lappy to current `main`, register the direct-owned Windows daemon/model-host tasks, remove predecessor task definitions, and rerun a promptless autonomous-turn qualification.

### P1 — verified-progress checkpoint primitive

Add durable checkpoint records:

```text
CANDIDATE -> VERIFIED
          -> REJECTED
```

Required properties:

- run + step provenance;
- candidate summary;
- evidence payload/reference;
- verifier identity/type;
- verification/rejection reason;
- immutable history;
- no model self-assertion becomes VERIFIED merely because it was generated.

Core invariant:

```text
MODEL CLAIM != VERIFIED PROGRESS
```

### P2 — run contract version affinity

Persist the runtime/workflow contract version when a run begins.

An engine upgrade must either:

- continue under a compatible contract;
- explicitly migrate the run;
- or stop with a compatibility state that requires operator action.

It must not silently reinterpret durable state.

### P3 — verified-progress circuit breaker

Only after P1 exists, add a no-progress breaker informed by:

- number of run steps since last VERIFIED checkpoint;
- repeated identical error class/digest;
- repeated permission/authority denial;
- repeated rejected checkpoint reason.

Use `CLOSED -> OPEN -> HALF_OPEN` semantics or an equivalent explicit state machine. Cooldown may allow a later retry when external conditions can change.

### P4 — reusable experience compilation

Optionally compile completed, verified runs into local reusable lessons.

This is not automatically long-term memory and never implies public sharing.

## Hostile review

> **HOSTILE REVIEWER:** Most of these projects can be imitated with more tables, more roles, and more loops. That does not mean Pre-Active needs them. Its current effect ledger, durable queue, and bounded autonomy may already solve the real problem.

**Accepted in part.** The runtime should not acquire a subsystem merely because another agent project has one. The only new kernel primitive justified by the comparison is verified progress, because Pre-Active currently has a rigorous truth model for external effects but not for claims that the *task itself* advanced. Version affinity follows from durability across source upgrades. Circuit breaking and experience compilation remain deferred until those primitives exist.

The donor repositories therefore change the order of work, not Pre-Active's identity.
