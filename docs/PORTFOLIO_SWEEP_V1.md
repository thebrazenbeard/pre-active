# Portfolio Full-Sweep Audit V1

Observed: 2026-09-25
Owner scope: `thebrazenbeard`
Target: `pre-active`

## Method

The accessible portfolio was enumerated from GitHub, yielding 70 repositories. The current default-branch README surface of every repository was inspected for mechanisms relevant to continuous execution: durable state, daemon/worker behavior, eventing, scheduling, queues, memory/context, structured tools, routing, idempotency, recovery, leases, heartbeats, effect boundaries, runtime policy, and verification.

Repositories with a strong match were then inspected at tree/source level for exact candidate mechanisms. The sweep is architecture discovery, not evidence that every donor is installed, current at runtime, or suitable for direct code copying.

Because `pre-active` is public, private repositories remain privacy-bound. The internal sweep included them, but this public artifact publishes only aggregate counts and public repository findings.

Inventory: **70 total = 54 public + 16 private** at the time of the sweep.

## Classification

- **PRIMARY** — directly informed V1 implementation invariants.
- **SECONDARY** — useful mechanism or operating pattern; incorporated narrowly or reserved as an extension seam.
- **REFERENCE** — conceptually related, but no V1 runtime dependency or direct mechanism admitted.
- **NONCORE** — domain-specific, historical, scaffold, or otherwise not needed for the execution kernel.
- **TARGET** — `pre-active` itself.

## Public repositories audited

| Repository | Class | Relevance to Pre-Active |
|---|---|---|
| `ingest` | PRIMARY | deterministic intake, provenance, receipt-oriented normalization |
| `voss` | SECONDARY | forensic review state and evidence checkpoints |
| `bt2` | REFERENCE | multi-module cognition/runtime workspace; too broad for core |
| `vera-synology` | NONCORE | deployment/platform scaffold, not execution-kernel logic |
| `axle` | NONCORE | automotive edge product domain |
| `bugops` | SECONDARY | incident evidence vs lifecycle tracking separation |
| `roots` | REFERENCE | provenance reconstruction and earliest-evidence discipline |
| `masamune` | REFERENCE | debugger/regression workflow discipline |
| `discovery` | SECONDARY | portfolio discovery/classification/implementation loop; Runner/WIP integrity research |
| `build-team-2.0` | SECONDARY | shared-state/capability coordination patterns |
| `intranel` | PRIMARY | canonical machine messages, admission, idempotency |
| `deepmemorystorage` | SECONDARY | provenance-preserving durable memory concepts |
| `empathy` | NONCORE | domain-specific appraisal/relationship architecture |
| `vera` | SECONDARY | cross-cutting runtime/schema/evidence architecture |
| `brigit-unbound` | REFERENCE | durable continuity/archive semantics, not execution core |
| `unbound-sol` | REFERENCE | cross-session durable workspace ideas |
| `semanticatlas` | NONCORE | semantic research domain |
| `god-brain` | REFERENCE | persistent runtime/state ideas embedded in speculative research |
| `personification` | NONCORE | identity/personification research |
| `vera-control-plane` | SECONDARY | control/effect-state separation and operator custody patterns |
| `vera-mesh` | PRIMARY | bounded transport/effect execution, request ledger, replay safety |
| `project-runner` | PRIMARY | orchestration, leases, dedup, work units, durable dispatch, reconciliation |
| `fuckup` | SECONDARY | structured failure-analysis/prevention loop |
| `vera_model_training` | NONCORE | model-training workbench rather than runtime execution fabric |
| `testament` | NONCORE | research/literary domain |
| `abil` | NONCORE | brownfield industrial intelligence product domain |
| `pre-active` | TARGET | blank target repository at audit start |
| `conditioning` | REFERENCE | consent/authority/runtime-effect distinction; historical experiment |
| `vera-mono` | PRIMARY | lifecycle/effect barriers, explicit source/runtime/effect separation |
| `project-lantern` | SECONDARY | qualification/orchestration workspace patterns |
| `unvtrslr` | NONCORE | semantic mediation domain |
| `vera-R9A0` | REFERENCE | historical architecture provenance |
| `conations` | REFERENCE | durable history vs present authority distinction |
| `Attune` | REFERENCE | persistent context/relationship architecture; too domain-specific for core |
| `transcendence` | NONCORE | modular cognitive architecture research |
| `project-achilles` | SECONDARY | security/consequence boundary review discipline |
| `sql-connectome` | REFERENCE | deterministic routing/translation with execution-authority separation |
| `hc-brain` | NONCORE | cognitive-organ architecture domain |
| `rezon` | REFERENCE | reasoning workspace; model-side reasoning remains replaceable in Pre-Active |
| `world-zero` | NONCORE | systems-modeling domain |
| `RepairTracker` | SECONDARY | durable repair orchestration and replay/idempotency concerns |
| `spm` | NONCORE | semantics/pragmatics model research |
| `noema` | SECONDARY | persistent predictive-agent architecture concepts |
| `temporal` | PRIMARY | timestamped append-only event logging |
| `driftguard` | PRIMARY | deterministic admission, evidence-bound recovery, durable generation discipline |
| `freerowcochkar` | NONCORE | adversarial legal reasoning domain |
| `on-theo` | NONCORE | comparative theology/history research domain |
| `meso-crct` | SECONDARY | salience/priority concepts relevant to future scheduler policy |
| `WorkBridgeMCP` | PRIMARY | bounded capability/tool execution and identity checks |
| `wip` | PRIMARY | crash recovery, checkpoints, effect and lifecycle recovery |
| `mosaic` | REFERENCE | persistent core + swappable model composition |
| `vera-habitat` | NONCORE | environment scaffold |
| `hephaestus` | SECONDARY | qualification/regression/release-control discipline |
| `ccb-core` | PRIMARY | durable bus/runtime contracts, dedupe, journal/reconciliation patterns |

## Admitted V1 architecture

The sweep did **not** produce a mega-runtime that imports the portfolio. It produced a smaller kernel with these admitted mechanisms:

1. one self-contained SQLite durability boundary;
2. lease-based durable event ownership;
3. deterministic schedule occurrence IDs and event dedupe;
4. persistent runs and bounded step counts;
5. explicit memory/context budget;
6. structured one-tool-call-per-turn model contract;
7. capability-scoped tool visibility and execution;
8. request-digest idempotency for mutations;
9. `BLOCKED_EFFECT` on ambiguous mutation outcomes;
10. exact-request recovery after external reconciliation;
11. append-only lifecycle journal;
12. provider and tool adapters as replaceable host seams.

## Deferred ideas

Useful portfolio mechanisms intentionally deferred from V1 include distributed workers/backends, multi-node heartbeats, recursive work decomposition, portfolio dependency graphs, external attestation, richer semantic retrieval, priority/salience learning, and provider-specific execution transports. They are extension candidates, not missing prerequisites for the V1 kernel.
