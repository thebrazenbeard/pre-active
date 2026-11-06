"""Non-executing, untrusted opportunity advice stored only as an audit event.

Advisory route status is never an approval. The module neither enqueues work nor
invokes handlers. A real host must separately authenticate source and authority.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import re
from typing import Any, Mapping

from .store import Store


_ID = re.compile(r"[A-Za-z0-9:_-]{1,128}\Z")
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_HEAD = re.compile(r"[a-f0-9]{40}\Z")
_STATUSES = frozenset({"AVAILABLE", "NEEDS_APPROVAL", "NO_ROUTE", "INCOMPLETE_UNKNOWN"})
_PLAN_FIELDS = frozenset({
    "status", "action_ids", "total_cost", "missing_approvals", "blockers",
    "explored_states", "executed", "verified", "evidence_class", "provenance",
})
_SCHEMA = """
CREATE TABLE IF NOT EXISTS opportunity_advisories (
    proposal_id TEXT PRIMARY KEY,
    receipt_sha256 TEXT NOT NULL,
    receipt_json TEXT NOT NULL,
    journal_seq INTEGER NOT NULL,
    created_at REAL NOT NULL
);
"""


def _opaque(value: object, field: str) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise ValueError(f"{field} must be a bounded opaque identifier")
    return value


def _bounded_ids(value: object, field: str, *, limit: int = 32) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > limit:
        raise ValueError(f"{field} must be a bounded list")
    values = tuple(_opaque(v, field) for v in value)
    if len(set(values)) != len(values):
        raise ValueError(f"{field} must not repeat identifiers")
    return values


@dataclass(frozen=True, slots=True)
class GoalObservationLink:
    """Caller-supplied correlation, NOT provenance admission or permission."""

    goal_id: str
    goal_revision: int
    observation_event_id: str
    observation_sha256: str
    planner_head: str

    def __post_init__(self) -> None:
        _opaque(self.goal_id, "goal_id")
        _opaque(self.observation_event_id, "observation_event_id")
        if isinstance(self.goal_revision, bool) or not isinstance(self.goal_revision, int) or not 0 <= self.goal_revision < 2**63:
            raise ValueError("goal_revision must be a nonnegative integer")
        if not isinstance(self.observation_sha256, str) or _SHA.fullmatch(self.observation_sha256) is None:
            raise ValueError("observation_sha256 must be lowercase SHA256")
        if not isinstance(self.planner_head, str) or _HEAD.fullmatch(self.planner_head) is None:
            raise ValueError("planner_head must be lowercase 40-hex SHA")


def build_route_advisory(link: GoalObservationLink, plan: Mapping[str, Any]) -> dict[str, Any]:
    """Strip route advice to exact non-authoritative, bounded metadata.

    A SHA-256 here binds JSON bytes; it is not a signature or source attestation.
    The route plan is expected to match Lou-pole PR#13's ``to_dict`` schema.
    """
    if type(link) is not GoalObservationLink or type(plan) is not dict:
        raise TypeError("typed link and plain route-plan dict required")
    # Frozen/slots are convenience, not a trust boundary: object.__new__ plus
    # object.__setattr__ can bypass dataclass __post_init__ and forge fields.
    # Copy the five primitive values into a newly validated exact class and
    # serialize only that snapshot, never the potentially forged original.
    try:
        checked = GoalObservationLink(
            link.goal_id, link.goal_revision, link.observation_event_id,
            link.observation_sha256, link.planner_head,
        )
    except AttributeError as exc:
        raise ValueError("incomplete goal-observation link") from exc
    if set(plan) != _PLAN_FIELDS:
        raise ValueError("route plan requires exact keys; no effect/grant smuggling")
    status = plan["status"]
    if not isinstance(status, str) or status not in _STATUSES:
        raise ValueError("unknown advisory status")
    if plan["executed"] is not False or plan["verified"] is not False:
        raise ValueError("planner cannot claim actual execution or verification")
    if plan["evidence_class"] != "CALLER_SUPPLIED_UNVERIFIED":
        raise ValueError("planner evidence cannot be promoted")
    actions = _bounded_ids(plan["action_ids"], "action_ids")
    approvals = _bounded_ids(plan["missing_approvals"], "missing_approvals", limit=64)
    blockers = _bounded_ids(plan["blockers"], "blockers")
    provenance = plan["provenance"]
    if type(provenance) is not dict or len(provenance) > 32 or set(provenance) != set(actions):
        raise ValueError("provenance keys must equal selected action IDs")
    for key, origin in provenance.items():
        _opaque(key, "provenance ID")
        if not isinstance(origin, str) or not origin or len(origin) > 256:
            raise ValueError("invalid unverified provenance annotation")
    explored = plan["explored_states"]
    if isinstance(explored, bool) or not isinstance(explored, int) or not 0 <= explored <= 8192:
        raise ValueError("explored_states exceeds planner bound")
    cost = plan["total_cost"]
    if cost is not None and (isinstance(cost, bool) or not isinstance(cost, int) or cost != 0):
        raise ValueError("zero-budget advice requires declared cost zero or null")
    if status in {"AVAILABLE", "NEEDS_APPROVAL"} and cost is None:
        raise ValueError("selected route requires explicit declared zero cost")
    if (status == "AVAILABLE" and approvals) or (status == "NEEDS_APPROVAL" and not approvals):
        raise ValueError("route status and pending approvals disagree")
    if status == "NO_ROUTE" and actions:
        raise ValueError("NO_ROUTE cannot carry selected action IDs")

    result: dict[str, Any] = {
        "schema": "PRE_ACTIVE_OPPORTUNITY_ADVISORY_V1",
        "source": "UNTRUSTED_CALLER_PLAN",
        "goal_claim": {"id": checked.goal_id, "revision": checked.goal_revision},
        "observation_claim": {"event_id": checked.observation_event_id,
                              "sha256": checked.observation_sha256, "admission": "UNVERIFIED"},
        "planner_source_head_claim": checked.planner_head,
        "route": {"status": status, "action_ids": list(actions),
                  "missing_approvals": list(approvals), "blockers": list(blockers),
                  "declared_cost": cost, "explored_states": explored},
        "evidence_class": "CALLER_SUPPLIED_UNVERIFIED",
        "permission_authenticated": False,
        "effect_authority": "NONE",
        "may_execute": False,
        "verified_result": False,
    }
    encoded = json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    result["envelope_sha256"] = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    return result


class OpportunityAdvisoryLedger:
    """Audit-only durable receipt; never inserts an executable queue event."""

    def __init__(self, store: Store) -> None:
        if not isinstance(store, Store):
            raise TypeError("requires Pre-Active Store")
        if store.connection.in_transaction:
            raise RuntimeError("advisory table init cannot join an active transaction")
        self.store = store
        store.connection.executescript(_SCHEMA)

    def record(
        self, *, proposal_id: str, link: GoalObservationLink,
        route_plan: dict[str, Any], now: float,
    ) -> dict[str, Any]:
        """Idempotent per proposal ID; divergent replay fails without journal write."""
        proposal_id = _opaque(proposal_id, "proposal_id")
        if isinstance(now, bool) or not isinstance(now, (int, float)):
            raise ValueError("now must be finite")
        try:
            finite_now = float(now)
        except OverflowError as exc:
            raise ValueError("now must be finite") from exc
        if not math.isfinite(finite_now) or finite_now < 0:
            raise ValueError("now must be finite")
        receipt = build_route_advisory(link, route_plan)
        canonical = json.dumps(receipt, sort_keys=True, separators=(",", ":"))
        digest = receipt["envelope_sha256"]
        if self.store.connection.in_transaction:
            raise RuntimeError("advisory write cannot join an active transaction")
        self.store.connection.execute("BEGIN IMMEDIATE")
        try:
            existing = self.store.connection.execute(
                "SELECT receipt_sha256, receipt_json, journal_seq FROM opportunity_advisories WHERE proposal_id=?",
                (proposal_id,),
            ).fetchone()
            if existing is not None:
                if existing["receipt_sha256"] != digest or existing["receipt_json"] != canonical:
                    raise ValueError("proposal_id replay conflict: different route receipt")
                result = {"proposal_id": proposal_id, "journal_seq": int(existing["journal_seq"]),
                          "receipt": receipt, "newly_recorded": False, "effect_authority": "NONE"}
            else:
                journal_seq = self.store.append_journal(
                    event_type="OPPORTUNITY_ADVISORY_RECORDED", subject_id=proposal_id,
                    payload={"receipt_sha256": digest, "receipt": receipt,
                             "effect_authority": "NONE", "may_execute": False}, now=finite_now,
                )
                self.store.connection.execute(
                    "INSERT INTO opportunity_advisories "
                    "(proposal_id, receipt_sha256, receipt_json, journal_seq, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (proposal_id, digest, canonical, journal_seq, finite_now),
                )
                result = {"proposal_id": proposal_id, "journal_seq": journal_seq,
                          "receipt": receipt, "newly_recorded": True, "effect_authority": "NONE"}
            self.store.connection.execute("COMMIT")
            return result
        except BaseException:
            self.store.connection.execute("ROLLBACK")
            raise
