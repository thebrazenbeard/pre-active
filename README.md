> **License:** Source-visible, not open source. Original material is proprietary. Commercial use, redistribution, hosted-service use, and commercial derivative products require written permission. See [LICENSE](LICENSE) and [COMMERCIAL_LICENSE.md](COMMERCIAL_LICENSE.md). Separately identified third-party components retain their own licenses.

# Pre-Active

**Durable continuous execution for tool-using language models.**

The name is deliberate: **Pre-Active** is a preemptive active runtime, and a play on being proactive—work can be durably queued, resumed, and advanced by an active host process instead of requiring every step to begin with a fresh interactive prompt.

Pre-Active turns a stateless model call into a recoverable execution process: events wake work, durable state survives process restarts, relevant memory is injected into context, model turns are constrained to structured tool calls or a final answer, and external mutations are fenced behind idempotency and reconciliation rules.

The repository description calls this a continuous execution environment for LLM autonomy. In concrete terms, Pre-Active provides **process-level autonomy while a Pre-Active daemon is actually running**. It does not imply hidden activity when no process is running, model consciousness, unrestricted authority, or permission to perform effects a host has not granted.

## What V1 provides

- SQLite/WAL durable state with a lease-based event queue.
- Fresh fencing tokens on every event claim/reclaim so stale workers cannot acknowledge, renew, or reschedule work they no longer own.
- Bounded lease heartbeats keep healthy long model/tool operations owned without granting permanent ownership.
- Event deduplication bound to exact kind/payload/priority, plus recovery after expired worker leases.
- Bounded retry with configurable attempt ceilings, durable `DEAD` state, failure evidence, dead-letter inspection, and explicit single-event redrive.
- Interval schedules that emit idempotent events.
- Durable runs and idempotently keyed run transcripts across model turns.
- Cooperative durable `pause`, `resume`, and `cancel` controls applied at fenced execution boundaries; cancellation never claims an already-dispatched external effect stopped.
- Atomic run creation + initial-step scheduling, with source-event-to-run binding so redelivered wakeups reuse the same run.
- Per-step durable model-decision fencing before any tool dispatch.
- Salience/relevance-based memory selection under a bounded context budget.
- A provider-neutral model interface plus a minimal OpenAI-compatible adapter.
- Structured tool admission by explicit capability and a fail-closed JSON-Schema-compatible validation subset.
- Exactly one admitted tool call per model turn for deterministic effect ordering.
- A durable mutation ledger keyed by request ID and canonical request digest.
- Replay of already committed mutation results without re-executing the effect.
- `BLOCKED_EFFECT` recovery when a mutation outcome becomes ambiguous.
- Explicit reconciliation before retry of an ambiguous effect.
- Atomic run-generation advance + successor-event scheduling, including effect-recovery resumes.
- Append-only lifecycle journal entries for queue claims and recovery evidence.
- A small daemon and CLI for submitting, scheduling, inspecting, and running work.
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

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and [docs/EFFECT_AND_RECOVERY.md](docs/EFFECT_AND_RECOVERY.md).

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

```bash
export PRE_ACTIVE_BASE_URL="http://localhost:11434/v1"
export PRE_ACTIVE_MODEL="your-model"
# export PRE_ACTIVE_API_KEY="..."  # only when your endpoint requires one

pre-active --state .pre-active/state.db run-once
```

Run continuously:

```bash
pre-active --state .pre-active/state.db daemon --poll-seconds 1

# Optional reliability controls:
# --lease-seconds 30
# --lease-heartbeat-seconds 10
# --max-lease-extension-seconds 900
# --max-event-attempts 16
```

Schedule a recurring task:

```bash
pre-active --state .pre-active/state.db schedule \
  "Review the durable queue" \
  --every 300
```

Inspect and explicitly redrive exhausted work:

```bash
pre-active --state .pre-active/state.db dead
pre-active --state .pre-active/state.db redrive <event-id>
```

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