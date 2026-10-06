> **License:** Source-visible, not open source. Original material is proprietary. Commercial use, redistribution, hosted-service use, and commercial derivative products require written permission. See [LICENSE](LICENSE) and [COMMERCIAL_LICENSE.md](COMMERCIAL_LICENSE.md). Separately identified third-party components retain their own licenses.

# Pre-Active

**Continuous resident runtime for self-initiating LLM agency.**

Pre-Active exists so an LLM does **not** require a human prompt to receive every turn. While a Pre-Active daemon is actually running, the host can continuously monitor every observation source it has deliberately connected and authorized, preserve durable goals and open loops, and grant the model another cognition turn when external change, time, an actionable open loop, or the model's own durable re-entry request warrants attention.

A user prompt is therefore only one possible cause of a model turn:

```text
USER_PROMPT != MODEL_TURN
CONTINUOUS_RUNTIME != CONTINUOUS_INFERENCE
AUTONOMOUS_TURN != EFFECT_AUTHORITY
```

The runtime can remain resident 24/7 while the model sleeps between meaningful turns. The model can request a later turn through the reserved `pre_active.request_turn` primitive; that preserves the same durable run and capability set rather than manufacturing new authority.

The name is deliberate: **Pre-Active** is a preemptive active runtime, and a play on being proactive—the runtime exists before the next prompt. Work can be observed, queued, resumed, reconsidered, and advanced by an active host process instead of requiring every step to begin with fresh human input.

Pre-Active still preserves the harder effect boundary: events and autonomous turns create opportunities for cognition, not permission for arbitrary action. External mutations remain fenced behind capability admission, idempotency, reconciliation, and verified-effect rules.

See [docs/AUTONOMOUS_RUNTIME.md](docs/AUTONOMOUS_RUNTIME.md) for the autonomous-turn contract and Observer/Initiator/Critic model.

## What V1 provides

- SQLite/WAL durable state with a lease-based event queue.
- Fresh fencing tokens on every event claim/reclaim so stale workers cannot acknowledge, renew, or reschedule work they no longer own.
- Bounded lease heartbeats keep healthy long model/tool operations owned without granting permanent ownership.
- Event deduplication bound to exact kind/payload/priority, plus recovery after expired worker leases.
- Bounded retry with configurable attempt ceilings, durable `DEAD` state, failure evidence, dead-letter inspection, and explicit single-event redrive.
- Interval schedules that emit idempotent events.
- Durable runs and idempotently keyed run transcripts across model turns.
- Run contract version affinity: durable work is stamped with its execution contract and fails closed as `BLOCKED_CONTRACT` before inference if a future runtime is incompatible.
- Durable progress checkpoints with explicit `CANDIDATE -> VERIFIED | REJECTED` transitions, preserving the distinction between evidence and trusted progress.
- Promptless `autonomous.turn` events classified as `EXTERNAL`, `TEMPORAL`, `OPEN_LOOP`, or `ENDOGENOUS`.
- Reserved `pre_active.request_turn` support so a model can put its own durable run into `WAITING` and receive a later cognition turn without a new human prompt.
- Bounded per-run autonomous-turn budgets to stop recursive self-stimulation from becoming an unbounded inference loop.
- Cooperative durable `pause`, `resume`, and `cancel` controls applied at fenced execution boundaries, including immediate control of `WAITING` autonomous turns; cancellation never claims an already-dispatched external effect stopped.
- Atomic run creation + initial-step scheduling, with source-event-to-run binding so redelivered wakeups reuse the same run.
- Per-step durable model-decision fencing before any tool dispatch.
- Salience/relevance-based memory selection under a bounded context budget.
- A provider-neutral model interface plus a minimal OpenAI-compatible adapter.
- Durable named local-model targets: the user selects which loopback OpenAI-compatible endpoint/model Pre-Active treats as its active cognitive target, without coupling the runtime to Qwen or any other specific model host.
- Typed provider failure semantics: known transient transport/HTTP failures retry with bounded backoff, stable jitter, and `Retry-After` support; known permanent client/protocol failures fail fast instead of burning the queue retry budget.
- Structured tool admission by explicit capability and a fail-closed JSON-Schema-compatible validation subset.
- Exactly one admitted tool call per model turn for deterministic effect ordering.
- A durable mutation ledger keyed by request ID and canonical request digest.
- Replay of already committed mutation results without re-executing the effect.
- `BLOCKED_EFFECT` recovery when a mutation outcome becomes ambiguous.
- Explicit reconciliation before retry of an ambiguous effect.
- Atomic run-generation advance + successor-event scheduling, including effect-recovery resumes.
- Append-only lifecycle journal entries for queue claims and recovery evidence.
- A small daemon and CLI for submitting, scheduling, inspecting, and running work.
- A provider-neutral operational snapshot covering backlog shape, claim expiry, retry/dead-letter pressure, run states, and due schedules without inventing a universal health verdict.
- Daemon polling survives ordinary cycle exceptions after durable retry/rejection handling; process-control exceptions still stop it.
- Malformed task-request envelopes are terminally rejected and journaled instead of becoming poison retry loops.

## Core invariant

Reasoning is retryable. External effects are not assumed retryable.

```text
REQUEST != AUTHORITY != ATTEMPT != EFFECT != VERIFIED EFFECT
```

If a mutation handler loses its response after dispatch, Pre-Active records `ATTEMPTED_UNKNOWN`, stops that run, and refuses blind replay. A host must reconcile whether the effect occurred. If it did, the committed result is replayed; if it did not, Pre-Active retries the exact stored request rather than asking the model to invent a replacement call.

## Architecture

```text
 external event / schedule
          |
          v
  +------------------+
  | durable event DB |
  +------------------+
          |
       lease/claim
          |
          v
  +------------------+       +----------------+
  | execution engine |<----->| context/memory |
  +------------------+       +----------------+
          |
       model turn
          |
     final | tool call
           v
  +------------------+
  | capability gate  |
  +------------------+
          |
          v
  +------------------+      ambiguous       +------------------+
  | tool/effect      |---------------------->| BLOCKED_EFFECT   |
  | ledger           |                       | + reconciliation |
  +------------------+                       +------------------+
          |
       receipt
          v
  +------------------+
  | durable journal  |
  +------------------+
```

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), [docs/EFFECT_AND_RECOVERY.md](docs/EFFECT_AND_RECOVERY.md), [docs/VERIFIED_PROGRESS.md](docs/VERIFIED_PROGRESS.md), and [docs/RUN_CONTRACT_AFFINITY.md](docs/RUN_CONTRACT_AFFINITY.md).

## Portfolio-derived design

Before implementation, the then-accessible `thebrazenbeard` portfolio was swept at repository level: **70 repositories** were inventoried and their README surfaces inspected at that historical cut. The live portfolio has since grown; the 70-repository figure is provenance for the V1 donor sweep, not a current census. High-value public donors were then inspected more deeply. Pre-Active is self-contained; donor repositories are architecture/provenance inputs, not runtime dependencies.

The strongest donor mechanisms were:

- `project-runner`: work units, leases, deterministic deduplication, durable dispatch, exact-subject currentness, bounded retry.
- `wip`: crash recovery, checkpoints, recovery/effect lifecycle separation.
- `ccb-core` and `intranel`: canonical envelopes, routing, dedupe, journal/reconciliation discipline.
- `ingest`: deterministic intake, provenance, receipts, raw-vs-derived separation.
- `temporal`: simple append-only time/event semantics.
- `driftguard`: monotonic durable state, compare-and-swap thinking, evidence-bound recovery.
- `WorkBridgeMCP`: capability narrowing, bounded execution, identity checks around effects.
- `vera-mesh`: mutation-ledger idempotency and fail-closed replay safety.
- `vera-mono`: unresolved-effect recovery barrier and explicit effect/readback distinctions.

The complete public-safe audit is in [docs/PORTFOLIO_SWEEP_V1.md](docs/PORTFOLIO_SWEEP_V1.md). Private repositories were included in the internal sweep but are not named or reproduced in this public repository.

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
python -m pip install -e '.[dev]'
pytest
```

Submit a durable task:

```bash
pre-active --state .pre-active/state.db submit \
  "Summarize the queued work" \
  --capability files.read
```

Run one cycle against an OpenAI-compatible chat-completions endpoint:

Define the local model that Pre-Active should use as its cognitive target, then activate it:

```bash
pre-active --state .pre-active/state.db target set local-primary \
  --base-url "http://127.0.0.1:11434/v1" \
  --model "your-local-model" \
  --activate

pre-active --state .pre-active/state.db target probe
pre-active --state .pre-active/state.db run-once
```

Targets are loopback-only in V1. The target stores endpoint identity and an optional **environment-variable name** for a key; it never stores the key itself. Change models with `target activate <name>` rather than rewriting the daemon.

For backward compatibility, `PRE_ACTIVE_BASE_URL` and `PRE_ACTIVE_MODEL` are still accepted when no persisted target exists. Explicit `--base-url/--model` remains a one-run override.

Run continuously:

```bash
pre-active --state .pre-active/state.db daemon --poll-seconds 1

# Optional reliability controls:
# --lease-seconds 30
# --lease-heartbeat-seconds 10
# --max-lease-extension-seconds 900
# --max-event-attempts 16
# --max-autonomous-turns-per-run 16
# --max-consecutive-endogenous-turns 2
```

For the bundled OpenAI-compatible adapter, network/timeouts and HTTP `408`, `429`, `500`, `502`, `503`, and `504` are treated as retryable provider failures. Other HTTP `4xx` responses and malformed provider protocol/JSON are terminal for that run. Retryable provider failures retain the existing event-attempt ceiling, add deterministic per-event jitter, and honor a valid HTTP `Retry-After` value as a minimum delay.

Grant a model turn without a human prompt:

```bash
pre-active --state .pre-active/state.db autonomous-turn \
  "Inspect the changed CI state" \
  --source EXTERNAL \
  --reason "A monitored pull request changed from green to red"
```

### Observer initiative policies

Durable observers default to `on_change`: every eligible snapshot change grants an
external autonomous turn. An observer may instead attach a durable initiative policy
that scores real observed changes before a turn is emitted.

The first stochastic policy is `hawkes_threshold`, a configured subcritical
exponential-Hawkes conditional-intensity gate:

```bash
pre-active --state .pre-active/state.db observer add-file \
  source-watch /path/to/source.dat \
  "Review a meaningful burst of source changes." \
  --every 5 \
  --initiative-policy hawkes_threshold \
  --initiative-config-json \
  '{"baseline_rate":0.02,"excitation":0.2,"decay_rate":0.5,"wake_threshold":0.3,"cooldown_seconds":30}'
```

Rates and decay use seconds because observer timestamps are expressed in seconds.
`excitation / decay_rate` must be less than `1`; supercritical configurations
fail closed. Suppressed changes still update durable excitation state, allowing a
cluster of individually weak changes to cross the wake threshold. Cooldown is a
separate deterministic refractory control rather than negative Hawkes excitation.

This policy is not a fitted Hawkes estimator and does not generate synthetic events.
It computes a conditional-intensity score over changes actually reported by an
authorized observer. The default remains deterministic `on_change`.

```text
TEMPORAL MODEL != IMPORTANCE != MODEL TURN != CAPABILITY != EFFECT AUTHORITY
```

See
[docs/specs/2026-10-06-temporal-initiative-models-design.md](docs/specs/2026-10-06-temporal-initiative-models-design.md)
for the research boundary and deferred model families.

Schedule recurring autonomous cognition:

```bash
pre-active --state .pre-active/state.db schedule \
  "Reconsider unresolved architecture questions" \
  --every 300 \
  --autonomous \
  --reason "Periodic reconsideration was explicitly requested"
```

Ordinary scheduled tasks remain available:

```bash
pre-active --state .pre-active/state.db schedule \
  "Review the durable queue" \
  --every 300
```

Inspect runtime state and exhausted work:

```bash
pre-active --state .pre-active/state.db status
pre-active --state .pre-active/state.db dead
pre-active --state .pre-active/state.db redrive <event-id>
```

`status` keeps the existing `pending_events`, `dead_events`, and `runs` fields and adds ready/delayed/retry backlog counts, active/expired claims, oldest-wait ages, and enabled/due schedule counts. These are measurements, not a liveness or SLA verdict.

Redrive is intentionally one event at a time. It resets that event's attempt count and preserves its identity/dedup binding. A dead `run.step` may resume its run only when the event is recorded as the exact cause of that exact failed run generation.

Control a durable run:

```bash
pre-active --state .pre-active/state.db pause <run-id> --reason "inspect state"
pre-active --state .pre-active/state.db resume <run-id> --reason "continue"
pre-active --state .pre-active/state.db cancel <run-id> --reason "stop work"
```

Pause/cancel are cooperative durable control requests. If they arrive before tool dispatch, no new tool effect starts. If they arrive while a tool is already executing, Pre-Active lets that operation return or become ambiguous, then applies control at the next safe boundary. An unresolved mutation remains `BLOCKED_EFFECT` until reconciliation; cancel does not erase uncertainty.

The CLI intentionally does not expose arbitrary shell execution. Host applications register their own tools through `ToolRegistry`, with each tool bound to a named capability and an explicit `mutation` classification.

## Library sketch

```python
from pre_active.context import ContextAssembler
from pre_active.engine import Engine
from pre_active.store import Store
from pre_active.tools import ToolRegistry, ToolSpec

store = Store("state.db")
tools = ToolRegistry(store)

tools.register(
    ToolSpec(
        name="inventory.read",
        description="Read inventory state",
        input_schema={"type": "object", "required": ["sku"]},
        capability="inventory.read",
        mutation=False,
    ),
    lambda args: {"sku": args["sku"], "quantity": 4},
)

# Supply any object implementing ModelAdapter, then create Engine(...).
```

Mutating handlers should return JSON-serializable dictionaries. If a mutation throws after ledger admission, Pre-Active treats its outcome as ambiguous rather than assuming nothing happened.

## Repository map

```text
src/pre_active/
  cli.py                     CLI entrypoint
  context.py                 bounded context + memory assembly
  daemon.py                  scheduler/engine continuous loop
  engine.py                  durable ReAct-style execution loop
  lease.py                   bounded fenced-claim heartbeat
  scheduler.py               interval event triggers
  store.py                   SQLite queue, runs, memory, journal
  tools.py                   capability gate + effect/idempotency ledger
  providers/
    openai_compatible.py     provider adapter
schemas/                     wire/data contracts
tests/                       deterministic behavioral tests
docs/                        architecture, effect model, donor audit
```

## Non-goals

V1 is not a distributed consensus system, a universal authorization service, a sandbox for untrusted code, a secret manager, or proof that an external effect occurred merely because a tool handler returned success. It does not force-kill in-flight tool handlers or claim a cancellation stopped an already-dispatched external operation. It also does not make a model continuously active unless a host process is running the daemon.

## License

Source-visible proprietary. Noncommercial evaluation/research rights are described in [LICENSE](LICENSE). Commercial use requires a separate written license; see [COMMERCIAL_LICENSE.md](COMMERCIAL_LICENSE.md).