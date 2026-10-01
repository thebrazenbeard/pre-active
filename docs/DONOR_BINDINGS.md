# Donor Architecture Bindings

Pre-Active is self-contained. The repositories below informed its design but are not runtime dependencies, submodules, imports, or automatic authorities.

| Donor | Mechanism admitted into Pre-Active | Representative source surface inspected |
|---|---|---|
| `thebrazenbeard/project-runner` | durable work orchestration, leases, deduplication, exact-subject discipline, bounded retries, ambiguous-write reconciliation | `docs/superpowers/specs/2026-09-17-project-runner-design.md`, `runner/durable_dispatch.py` |
| `thebrazenbeard/wip` | crash-recovery/checkpoint lifecycle and explicit effect recovery as a distinct phase | `protocol/WIP_PROTOCOL.md`, `protocol/CHECKPOINT_PROTOCOL.md`, `protocol/EFFECT_PROTOCOL.md`, `protocol/RECOVERY_PROTOCOL.md` |
| `thebrazenbeard/ccb-core` | canonical message/envelope thinking, dedupe, projection/reconciliation journaling, heartbeat/accounting discipline | `docs/protocol/PROTOCOL_V2.md`, `docs/protocol/RADAR_DEDUPE_V1.md`, `docs/protocol/RADAR_PROJECTION_JOURNAL_V1.md` |
| `thebrazenbeard/intranel` | deterministic machine-facing message contracts and idempotent coordination semantics | `schema/INTRANEL_MESSAGE_V1.schema.json` |
| `thebrazenbeard/ingest` | deterministic intake boundary, raw/provenance separation, receipt-bearing normalized records | `schemas/ingest-record-v1.schema.json`, `src/ingest/policy.py` |
| `thebrazenbeard/temporal` | minimal append-only time/event semantics | `events/`, `tests/test_temporal.py` |
| `thebrazenbeard/driftguard` | durable state generations, evidence binding, append-only recovery receipts, fail-closed admission | `docs/ARCHITECTURE_V1.md` |
| `thebrazenbeard/WorkBridgeMCP` | capability narrowing and bounded effect execution; executable/root identity checks and output/time limits | `internal/runner/runner.go`, `internal/policy/path.go` |
| `thebrazenbeard/vera-mesh` | mutation-ledger idempotency, admission-before-dispatch, fail-closed replay safety | `protocol/veraport/v1/mutation-ledger-profile.md` |
| `thebrazenbeard/vera-mono` | unknown-effect recovery barrier: unresolved mutations freeze automatic progress until reconciliation | `architecture/VERA_EFFECT_RECOVERY_BARRIER_V1.json` |

Secondary portfolio influences include public work on incident handling, provenance, persistent agents, qualification, salience/priority, and repair orchestration. Those ideas informed naming or extension seams but were not copied wholesale into the V1 kernel.

## Admission rule

A donor mechanism enters Pre-Active only when it solves a Pre-Active invariant directly. Domain-specific identity, cognition, product, research, and application logic stays out. This keeps the repository an execution substrate rather than a federation of unrelated projects.
