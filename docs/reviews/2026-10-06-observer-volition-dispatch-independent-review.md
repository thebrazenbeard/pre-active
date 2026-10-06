# Observer → Volition Dispatch Route V1 — Independent Review Record

Date: 2026-10-06

Review class: **INDEPENDENT REVIEW EVIDENCE**

Repository: `thebrazenbeard/pre-active`

Feature branch: `feat/observer-volition-dispatch-route-v1-20261006`

Base: `main@f22949f9e2eaeddab3854fec9c4c0634c9ec353e`

Current reviewed implementation head at the time of this record:
`3814cbe5b29124cb7b5ebe58646cbe9cd53ef633`

This file records independent-review evidence separately from the internal hostile
review. It does not convert incomplete reviewer runs into a clean independent
PASS.

## Review 1 — broad read-only Codex review

Frozen feature head reviewed:
`ddb4b37765b3ea3828151227b76eb38101b749f2`

The reviewer was a separate Codex CLI process running with a read-only sandbox.
It independently reproduced three defects before its local Codex runtime later
panicked with an OS resource error and failed to emit a terminal verdict.

### Finding A — missing dispatch row could silently fall back to direct routing

Deleting one `observer_dispatch` row and reconstructing `ObserverManager`
caused initialization to backfill that individual row as `autonomous_turn`.
That could transform a configured Volition route into the direct EXTERNAL route
after state corruption.

**Accepted and fixed.**

The implementation now distinguishes genuine first schema migration from a
missing row after the dispatch table already exists. Once the table exists, an
individual missing row fails closed and is never silently converted into the
default direct route.

Regression:
`test_missing_dispatch_row_is_not_backfilled_after_dispatch_schema_exists`

### Finding B — cross-goal observation context contamination

A low-magnitude signal for target B could leave target A as the active Volition
goal while still attaching B's `observation_context` to the cognition request
for A.

**Accepted and fixed.**

The bridge now attaches observation context only when the signal target that
carried the context equals the cognition request target.

Regression:
`test_bridge_does_not_attach_context_for_different_active_goal`

### Finding C — corrupt dispatch JSON impaired repair operations

Malformed dispatch JSON caused list/remove to fail and could make disable mutate
state but then fail while reading the corrupt record.

**Accepted and fixed.**

List now has a tolerant diagnostic surface. Manager-level enable/disable/remove
use the observer identity without requiring successful dispatch decoding.

Regression:
`test_corrupt_dispatch_can_be_listed_disabled_and_removed`

The same broad independent run also executed 113 guarded in-memory cases covering
XOR routing, capability/effect-authority boundaries, fixed Volition priority,
rollback after enqueue/journal failure, replay, corruption isolation, dependency
loss, API/CLI validation parity, malformed-context rejection, context-neutral
motive scoring, and exact-base migration behavior. Those cases passed. The
review process itself then failed before a terminal verdict, so this evidence is
not labeled a PASS.

Task outcome: **REVIEW PROCESS FAILED AFTER REPRODUCTIONS; FINDINGS ACTIONED.**

## Review 2 — focused read-only Codex review of first remediation

Review range:
`ddb4b37765b3ea3828151227b76eb38101b749f2..4562d14c2a5c1c042f2ff43fcf6460e33c887e24`

This independent focused review confirmed that the three defects above were
resolved, then found three additional P2 repair-path defects.

### P2 — failed legacy migration could become unrecoverable

The first remediation used table existence as the migration-completion signal.
If table creation committed but the legacy backfill INSERT failed, a later restart
would see the table and skip the required legacy backfill.

Reviewer evidence described the defect at `ObserverManager.__init__` and
required schema creation plus migration completion to be atomic.

**Accepted and fixed in `3814cbe5b29124cb7b5ebe58646cbe9cd53ef633`.**

Schema creation and initial legacy backfill now run inside one explicit
transaction. Failure rolls back the table creation so a later restart can retry
the migration.

Regression:
`test_dispatch_legacy_migration_is_atomic_and_retries_after_backfill_failure`

### P2 — CLI disable still reported failure on corrupt dispatch JSON

Manager-level disable succeeded and journaled, but the CLI then performed a
strict `get()` for its output, causing `JSONDecodeError` after the mutation.

**Accepted and fixed in `3814cbe5b29124cb7b5ebe58646cbe9cd53ef633`.**

The CLI enable/disable readback uses the explicit tolerant dispatch surface.

Regression:
`test_cli_disable_reports_success_with_corrupt_dispatch_json`

### P2 — one missing dispatch row blocked observer listing

The tolerant list path handled malformed JSON but still raised when the entire
dispatch row was missing, hiding otherwise healthy observers from the diagnostic
list surface.

**Accepted and fixed in `3814cbe5b29124cb7b5ebe58646cbe9cd53ef633`.**

Tolerant dispatch reads represent a missing row as a diagnostic object with null
route/config/timestamp plus an error string; runtime routing remains strict and
fails closed.

Regression:
`test_missing_dispatch_row_is_visible_in_tolerant_list_surface`

The focused reviewer also reported 15 independent invariant cases and nine valid
CLI operations passing, including normal legacy migration, XOR routing,
authority/capability/priority constraints, sampling isolation, rollback, and
replay.

Review outcome: **NOT READY UNTIL THREE P2 FINDINGS WERE FIXED.**
All three findings were subsequently fixed with red-green regressions.

## Review 3 — focused read-only Codex re-review of second remediation

Review range:
`4562d14c2a5c1c042f2ff43fcf6460e33c887e24..3814cbe5b29124cb7b5ebe58646cbe9cd53ef633`

This was another separate read-only Codex process.

Before the reviewer process was cut short by its own harness error and account
usage limit, it independently reproduced the old-head failures and passed the
new head for all targeted remediation paths, including:

- atomic migration/backfill failure recovery;
- missing/corrupt dispatch listing while preserving a healthy peer;
- CLI disable with malformed dispatch JSON;
- injected commit failure rolling back the migration table and rows, followed by
  successful restart recovery;
- genuine one-time legacy backfill while preserving an existing Volition route
  and leaving a later missing row missing;
- missing/malformed/non-object dispatch JSON failing closed while healthy peers
  still emit;
- diagnostic list behavior, strict show behavior, and repair-safe
  enable/disable/remove;
- valid CLI add/show/disable/enable/remove lifecycle for both routes.

The reviewer's next custom harness incorrectly assumed that
`Store.list_events()` exposes the SQLite event priority field and raised
`KeyError: 'priority'`. Existing repository tests verify event priority through
the raw event row instead. The reviewer then hit its account usage limit before
it could emit a formal terminal verdict.

Review outcome:
**TARGETED REMEDIATIONS INDEPENDENTLY PASSED; NO TERMINAL REVIEW VERDICT DUE TO
REVIEWER-HARNESS FAILURE / USAGE LIMIT.**

This is positive independent verification evidence, but it is deliberately not
labeled a clean independent PASS.

## Other attempted independent surfaces

GitHub Copilot PR review was requested through both the GitHub API-compatible
review request and `gh pr edit --add-reviewer @copilot`. GitHub recorded no
review request, review submission, thread, or comment for PR #30. It is therefore
not counted as review evidence.

A separate local `ministral-3:14b` Ollama process was given the approved spec
and exact diff with no tools. It did not follow the requested defect/verdict
format and instead paraphrased test cases. That run was terminated and is not
counted as review evidence.

## Current verification state

At implementation head
`3814cbe5b29124cb7b5ebe58646cbe9cd53ef633`:

- local full suite: **212 passed in 8.78s**;
- local diff check: clean;
- GitHub test matrix: Python 3.11, 3.12, and 3.13 all successful;
- GitHub CodeQL checks successful;
- every concrete independent finding has a dedicated regression and a verified
  fix;
- no known unresolved release-blocking product defect remains.

## Claim ceiling

The strongest supported statement is:

**All concrete independent findings were resolved and independently exercised on
the remediated head; no known unresolved release-blocking defect remains.**

Do not strengthen that statement to:

**clean independent PASS**

because the final focused reviewer process did not emit a terminal PASS verdict.

This evidence is sufficient to distinguish the implementation state from both
the earlier NOT-READY state and from an unsupported claim of completed clean
independent certification.


## Final promotion readback

Final feature head:
`50090cdf54b97ea572b8f5d2de36e776ab5cfbd8`

Canonical merge:
`a0fb07b7a42f07d29bcbd9ce4e67607f6406814d` via PR #30.

Final observed verification:

- exact feature-head GitHub workflow run #354: success;
- canonical main push workflow run #355: success;
- ProgramData installed source: exact canonical merge;
- ProgramData full suite: **212 passed in 8.84s**;
- isolated real-model observer-to-Volition smoke: completed;
- smoke cognition source: `ENDOGENOUS`;
- smoke cognition capabilities: empty;
- smoke cognition `effect_authority=false`;
- duplicate user-local daemon: disabled with zero daemon processes;
- production daemon/Qwen outside the configured window: zero processes;
- autonomy window policy preserved;
- durable deployment receipt:
  `C:\ProgramData\PreActive\state\OBSERVER_VOLITION_DISPATCH_PROMOTION_20261006.json`.

The claim ceiling is unchanged: this evidence does not establish a terminal clean
independent PASS. It establishes that every concrete independent finding was
resolved and independently exercised, with no known unresolved release-blocking
defect remaining at promotion.
