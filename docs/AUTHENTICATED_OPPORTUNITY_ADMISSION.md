# Authenticated opportunity admission (source-only candidate)

Status: **opt-in API and synthetic tests only**. No producer, host key, observer,
timer, Volition runtime, account or executor is installed or activated by this
change. This is separate from file-snapshot diagnostics and from Lou-pole's
planner/advisory proposals.

## Why a separate ingress exists

`FileSnapshotObserver` proves that local bytes changed; it does **not** prove
that a real opportunity arose, a goal is still current, a producer is trusted,
or a result occurred. Tattler-style sampled diagnostics similarly cannot
promote themselves into authenticated opportunity evidence. Existing
`ObserverManager` `volition_signal` routing keeps capabilities empty and effect
authority false, but previously only supported a sanitized **file** envelope.

`AuthenticatedOpportunityObserver` is a new explicit host-injected adapter. It
does not appear in the default adapter registry or CLI. A host supplies:

1. A read-only `reader(source_id)` for an independently designated producer.
2. A **host-pinned** per-source HMAC-SHA256 key, never stored in observer
   config or version control. The trusted producer must sign the exact claim.
3. A live `current_goal_revision(goal_id)` oracle from the actual goal owner,
   not from the model/producer/observer's own claim.

An accepted claim has **exactly** these signed fields:

| Field | Purpose |
|---|---|
| `source_id` | Host-configured producer binding, opaque ID |
| `opportunity_id` | Stable producer event ID, scoped by source |
| `goal_id` | Exact observer config and Volition signal target binding |
| `goal_revision` | Must match trusted live owner revision at admission |
| `issued_at`, `expires_at` | Finite seconds, host clock inside interval, bounded lifetime |
| `artifact_sha256` | Signed digest of producer artifact; not proof that the artifact exists |
| `hmac_sha256` | Lowercase HMAC-SHA256 hex over sorted compact JSON of all fields above |

Only opaque IDs, revision, bounded time window, and artifact digest are forwarded
in the sanitized Volition observation context. The HMAC tag, raw artifact contents, paths,
summaries and arbitrary producer fields do not enter the derived model context.
The tag is also removed from `probe` output and durable observer snapshots after
the transactional verification. The envelope parser is a **shape validator**;
only the receipt-bound `enqueue_signal` path supplies admission evidence.
The code will not infer an action, capability, priority or effect authority from
any of these fields. The signal's fixed target, kind, magnitude, confidence,
and provenance remain host-configured and pass existing Volition validation.

## Admission flow and replay boundary

1. `probe` / `sample`: read and verify the source tag and exact binding.
   **Diagnostic only**; it does not check live time/goal or persist a receipt.
2. Explicit `ObserverManager.tick(now=...)`: validate dispatch and empty
   capabilities. Under the same SQLite transaction that enqueues Volition,
   re-verify the tag, clock interval, live goal revision, and observation digest.
3. Insert a unique durable receipt keyed by `(source_id, opportunity_id)` with
   the canonical claim SHA-256. Exact repeats are no-ops; same ID with altered
   signed content is an error, even after reopening the local state database or
   when a second observer samples the event. A receipt means **admitted**, not
   **authorized to emit**.
4. Only a new admitted event can advance initiative and enqueue one
   non-executable `volition.signal` (`effect_authority=false`, no capabilities).
   If downstream validation/enqueue fails, transaction rollback also removes
   the admission receipt. No partial promotion is committed.

   Direct `enqueue_signal` calls carrying an `authenticated_opportunity` context
   must run in an admission transaction and match a real observer/dispatch,
   source, goal, canonical claim digest, and durable admission receipt. They
   must also match the observer's *current positive initiative decision*,
   last digest, change count, and entire configured dispatch signal. A
   baselined/initiative-suppressed signed sample **cannot** create a signal.
   The receipt atomically binds the dispatched event ID; later calls are
   rejected, including when the original event was deleted. A stable
   source/opportunity key is forced for event deduplication; a caller's
   arbitrary dedup key cannot produce a second motive.

The host may choose not to register the adapter, add any observer or emit an
initial event. The module never imports a real producer or reaches the network.

## Required host integration (not implemented here)

The host must independently prove the signing key belongs to the intended
producer; protect/rotate/revoke it; defend against producer compromise; provide
an authoritative goal-revision read; authenticate the actual artifact behind
`artifact_sha256`; bind a trusted clock; and establish end-to-end result/outcome
receipts. The synthetic tests deliberately **cannot** establish these facts.

**Currentness limits:** replay receipts survive ordinary database close/reopen
but **not rollback to a previous SQLite snapshot**. Restoring an older database
reopens a consumed source ID unless a separate host-owned monotonic/append-only
witness verifies replay history and backup generation. A goal revision obtained
from an external store can also change immediately after the resolver returns;
atomic goal/currentness guarantees require integration with that goal owner's
commit/reconciliation protocol. This candidate neither supplies that external
authority nor claims it. Even an authenticated source event is not a verified
outcome, a new self-authored goal, or a permit to act.
The receipt table is a new source-only schema with no deployed predecessor or
upgrade/migration contract. A future host release must qualify migrations before
it allows an existing candidate database to consume this module.

**Signing interoperability:** HMAC covers UTF-8 bytes of a JSON object with
exact claim fields, sorted lexicographic keys, no spaces (`separators=(",", ":")`),
and no NaN/infinities. Numeric representation matters (`100` and `100.0` have
different signatures). A non-Python producer must match the exact bytes; using
integer epoch seconds is the simplest cross-language convention. This synthetic
test vector uses only a publicly known throwaway key, not a runtime secret:

```text
key UTF-8: synthetic-test-key-not-for-runtime
claim JSON: {"artifact_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","expires_at":200.0,"goal_id":"goal-maintenance","goal_revision":3,"issued_at":100.0,"opportunity_id":"event-001","source_id":"trusted-scanner"}
claim SHA-256: 0942da8cb617ce433b22b3fc62285ea98d091105518db5acb0ede1d8b4270c85
HMAC-SHA256: e3634d3eed58e61fa36aad1a865deabdbb03f7e210f0588c11a63566f443237c
```

Tattler's current `README.md` describes its TCP/UDP lifecycle as sampled
observations and diagnostic correlations, not source-authenticated opportunity
events or proven root causes. Volition's `README.md` likewise warns that caller
allowlists, SHA-256 and verifier IDs alone do not authenticate source evidence.

There is currently no claim of deployed Tattler, Volition, VN1/VN2 or Bus
ingress, human verification, real-world opportunity, or authenticated outcome.
An HMAC on a fabricated claim is not real-world evidence. The scanner's
transport and source of truth remain separate integration assignments.

## Developer check

Run `pytest -q tests/test_authenticated_opportunity_admission.py` and the full
suite. Tests cover missing registration, sample/probe non-admission, tamper,
source and goal binding, stale and future timestamps, live revision mismatch,
integer timestamp signatures, one-off replay across observers/restarts,
conflicting stable IDs, and atomic receipt rollback on failed enqueue.
