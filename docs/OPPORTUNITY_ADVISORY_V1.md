# Opportunity Advisory V1 — source-only, audit-only

## Why this exists

A suggested route is not permission to execute it. `lou-pole` Draft PR #13
finds possible paths through a **caller-supplied** action graph and explicitly
returns `executed=false`, `verified=false`, and
`evidence_class=CALLER_SUPPLIED_UNVERIFIED`. The status `AVAILABLE` only means
no declared approval is missing; it does not authenticate the approver,
host capability, price, or effect.

Pre-Active already has a durable Observer → Volition signal route, goal
completion, an effect ledger, replay defenses, and outcome correlation. It
does not need a second agent scheduler or a direct Obsession-to-executor route.

This candidate adds an opt-in **Python call surface**, not a background worker:

```python
from pre_active.store import Store
from pre_active.opportunity_advisory import (
    GoalObservationLink, OpportunityAdvisoryLedger,
)

# `plan` must be an explicit caller-supplied `RoutePlan.to_dict()` from the
# separate lou-pole planner; this example does NOT authenticate it.
store = Store(":memory:")
ledger = OpportunityAdvisoryLedger(store)
link = GoalObservationLink(
    goal_id="research_goal_1", goal_revision=1,
    observation_event_id="observation_1", observation_sha256="a" * 64,
    planner_head="c19ef17a9f93514910a01be9df07c74210252496",
)
plan = {
    "status": "AVAILABLE", "action_ids": ["inspect"], "total_cost": 0,
    "missing_approvals": [], "blockers": [], "explored_states": 1,
    "executed": False, "verified": False,
    "evidence_class": "CALLER_SUPPLIED_UNVERIFIED",
    "provenance": {"inspect": "caller_supplied_unverified"},
}
receipt = ledger.record(
    proposal_id="proposal_1", link=link, route_plan=plan, now=1.0,
)
assert receipt["effect_authority"] == "NONE"
assert receipt["receipt"]["may_execute"] is False
store.close()
```

The ledger creates one separate `opportunity_advisories` audit table and one
`OPPORTUNITY_ADVISORY_RECORDED` **journal** row per proposal identity. Same ID
plus same bytes returns the prior journal identity; divergent replay fails
within a transaction. It never inserts an executable `events` row, creates a
`runs` row, calls providers/tools, or changes existing observer dispatch.

## Evidence/authority boundaries

| Stage | Current source owner | What is *not* proven |
| --- | --- | --- |
| Observation | Tattler sampled network/process telemetry; Pre-Active observer evidence | Human identity, messages, model consent, source authentication |
| Goal evaluation | Volition/MESO typed state and Pre-Active VolitionBridge | Effect grants or user wishes inferred from observation text |
| Possible route | Lou-pole Draft PR #13, caller-supplied action graph | Authenticated capabilities, grants, real price, route executability |
| Advisory record | This isolated, untrusted Pre-Active journal candidate | Permission, approval, effect, goal completion, installed runtime |
| Execution | Separate Pre-Active effect admission and exact request ledger | Invocation without independently admitted authority |
| Verified result | External outcome verifier and correlated receipts | Trust from journal, model text, `GoalCompletionVerifier` type alone |

Important: `sha256` is a content fingerprint, not a signature. This API checks
grammar and accidental idempotency but cannot authenticate the caller, source
revision, goal, receipt, or device. The `planner_head` is also a **caller claim**
until independently checked. No human/person target text, recipient, phone
number, source credentials or private data is recorded by this contract.

## Security invariants

- Exact input schema; no `effect_authority`, `granted_approvals`, or `dispatch_task`
  values can be smuggled through an extra plan key.
- Treat frozen dataclasses as untrusted at the API boundary: recopy every
  `GoalObservationLink` primitive into a newly validated exact class, rejecting
  forged post-init bypasses before any SQLite transaction.
- Bounded opaque IDs; zero-dollar declared route cost only.
- A selected `AVAILABLE`/`NEEDS_APPROVAL` route must explicitly declare zero
  cost; unknown cost is not silently treated as free. Negative timestamps fail.
- `AVAILABLE` leaves `permission_authenticated=false`, `may_execute=false`,
  `verified_result=false`, `effect_authority=NONE`.
- `INCOMPLETE_UNKNOWN` is retained; bounded search exhaustion is not `NO_ROUTE`.
- Exact duplicate proposal reuses journal row; divergent retry raises.
- Invalid input creates no journal row or executable event.

### Hostile falsifiers

> A forged input can still lie about the observed event or approved actions,
> and can recompute its SHA. The Python class is not a security principal.

> A Tattler TCP listener is not an authenticated communication endpoint;
> it cannot be used to obtain a person-directed consent signal.

> Even if Lou-pole finds `AVAILABLE`, execution and verified success remain
> different state transitions. Do not wire this audit API to an executor.

## Qualification and status

Execute the scoped tests without installing anything:

```sh
PYTHONPATH=src python -B -m unittest discover -s tests \
  -p 'test_opportunity_advisory.py' -v
```

**Observed locally:** 18 synthetic Python tests passed (including actual
in-memory and reopened disk-backed Pre-Active Store journal writes, dedup, conflict, and no queue/run
creation), 2026-11-06. These are SOURCE/TEST, not a real host adapter or
external verifier. No host service activation, native Project install,
autonomous outreach, protected effect or canonical memory write is implied.

**Live refs used for architecture orientation:** pre-active `main@e306c251a4da44125ee58c311a35120084da8e4b`, lou-pole Draft PR #13
`c19ef17a9f93514910a01be9df07c74210252496`, volition
`main@f622e564bf257b0ffc0b3c05130b62db1c201d3d`, meso-crct
`main@86c15a633daafd27cb4c3273e228f786dd54dc41`, tattler
`main@bda4500c6627cdd70734ecc620140e4298873ce6`, obsession Draft PR #1
`54c5b7d2f6ef040330b88836c049c004ffd14c50` at inspection,
vera-stack Draft PR #1 `a58988fc6b2da3b5c97d21925b9205e8fb3b38ea`, vera-mono
`main@2cf4c42525998cf298e465f294fb45c4624e1a7c`.

**Next frontier:** A *separately approved* trusted source admission adapter
for observation and host permission evaluation, followed by an exact effect
ledger request where authorized. Person-directed research mode remains HOLD;
no new implicit worker has been installed.
