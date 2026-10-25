"""Inert evidence *claims* about outcomes, never evidence admission or satiation.

This intentionally exposes no VERIFIED state and never touches Volition, MESO,
the event queue, or any tool. Source names and digests supplied by a caller are
untrusted assertions until an independent future admission boundary exists.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields
from enum import StrEnum

from .store import Store

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
OUTCOME_CLAIM_SCHEMA_VERSION = 1
_CLAIM_V1_FIELDS = (
    "schema_version",
    "event_id",
    "need_id",
    "goal_id",
    "action_request_id",
    "claimed_outcome",
    "claimed_need_change",
    "source_kind",
    "source_actor_id",
    "source_revision",
    "evidence_ref",
    "evidence_sha256",
    "observed_at",
    "expires_at",
)
_SOURCE_KINDS = frozenset(
    {"observer", "tool_result", "operator_statement", "model_generated"}
)
_REJECTION_REASONS = frozenset(
    {
        "UNVERIFIED_SOURCE",
        "CONTRADICTORY_EVIDENCE",
        "EVIDENCE_MISSING",
        "STALE_REVISION",
        "EXPIRED",
    }
)


class ClaimedOutcome(StrEnum):
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    NO_EFFECT = "NO_EFFECT"
    UNKNOWN = "UNKNOWN"


class OutcomeState(StrEnum):
    CANDIDATE = "CANDIDATE"
    REJECTED = "REJECTED"


class OutcomeConflict(ValueError):
    """An immutable event or rejection decision was reused inconsistently."""


def _text(value: str, *, name: str, maximum: int = 2048) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > maximum
        or any(ord(ch) < 32 for ch in value)
    ):
        raise ValueError(
            f"{name} must be non-empty bounded text without control characters"
        )


def _time(value: float, *, name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        raise ValueError(f"{name} must be a finite timestamp")
    if value < 0:
        raise ValueError(f"{name} must not be negative")
    return float(value)


@dataclass(frozen=True, slots=True)
class OutcomeCandidate:
    """A typed assertion; its fields do not authenticate an observer or effect."""

    event_id: str
    need_id: str
    goal_id: str
    action_request_id: str | None
    claimed_outcome: ClaimedOutcome
    claimed_need_change: float
    source_kind: str
    source_actor_id: str
    source_revision: str
    evidence_ref: str
    evidence_sha256: str
    observed_at: float
    expires_at: float
    schema_version: int = OUTCOME_CLAIM_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != OUTCOME_CLAIM_SCHEMA_VERSION
        ):
            raise ValueError("unsupported outcome claim schema_version")
        for name in (
            "event_id",
            "need_id",
            "goal_id",
            "source_actor_id",
            "source_revision",
            "evidence_ref",
        ):
            _text(getattr(self, name), name=name)
        if self.action_request_id is not None:
            _text(self.action_request_id, name="action_request_id")
        if not isinstance(self.claimed_outcome, ClaimedOutcome):
            raise TypeError("claimed_outcome must be a ClaimedOutcome")
        if (
            not isinstance(self.source_kind, str)
            or self.source_kind not in _SOURCE_KINDS
        ):
            raise ValueError("source_kind must be one of the explicitly claimed kinds")
        if not isinstance(self.evidence_sha256, str) or not _DIGEST.fullmatch(
            self.evidence_sha256
        ):
            raise ValueError("evidence_sha256 must be lowercase SHA-256 hex")

        delta = self.claimed_need_change
        if (
            isinstance(delta, bool)
            or not isinstance(delta, (int, float))
            or not math.isfinite(delta)
            or not -1.0 <= delta <= 1.0
        ):
            raise ValueError("claimed_need_change must be a finite normalized delta")
        if self.claimed_outcome is ClaimedOutcome.NO_EFFECT and delta != 0:
            raise ValueError("NO_EFFECT cannot claim a need change")
        if self.claimed_outcome is ClaimedOutcome.UNKNOWN and delta != 0:
            raise ValueError("UNKNOWN cannot claim a need change")
        if self.claimed_outcome is ClaimedOutcome.FAILURE and delta > 0:
            raise ValueError("FAILURE cannot claim a positive need change")
        if self.claimed_outcome is ClaimedOutcome.SUCCESS and delta < 0:
            raise ValueError("SUCCESS cannot claim a negative need change")
        object.__setattr__(self, "claimed_need_change", float(delta))

        start = _time(self.observed_at, name="observed_at")
        end = _time(self.expires_at, name="expires_at")
        if end <= start:
            raise ValueError("expires_at must be after observed_at")
        object.__setattr__(self, "observed_at", start)
        object.__setattr__(self, "expires_at", end)


@dataclass(frozen=True, slots=True)
class OutcomeReceipt:
    claim: OutcomeCandidate
    claim_sha256: str
    status: OutcomeState
    reject_reason: str | None
    created_at: float
    rejected_at: float | None

    @property
    def event_id(self) -> str:
        return self.claim.event_id

    @property
    def claimed_outcome(self) -> ClaimedOutcome:
        return self.claim.claimed_outcome


def _serialize(claim: OutcomeCandidate) -> tuple[str, str]:
    # V1's canonical field set is frozen: adding optional dataclass fields in a
    # future release must not silently rewrite or truncate persisted V1 rows.
    if {field.name for field in fields(OutcomeCandidate)} != set(_CLAIM_V1_FIELDS):
        raise RuntimeError("outcome claim fields changed without a schema version bump")
    candidate_fields = asdict(claim)
    payload = {field: candidate_fields[field] for field in _CLAIM_V1_FIELDS}
    payload["claimed_outcome"] = claim.claimed_outcome.value
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return canonical, hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class OutcomeLedger:
    """Durable claim/reject transitions, with zero admission or effect authority."""

    def __init__(self, store: Store) -> None:
        self.store = store
        self.connection = store.connection

    @contextmanager
    def _atomic(self) -> Iterator[None]:
        """All-or-nothing even if our caller catches an error and commits later."""
        if self.connection.in_transaction:
            # SQLite nested BEGIN is invalid; a unique savepoint isolates the
            # ledger operation from its caller's still-open transaction.
            savepoint = f"outcome_{uuid.uuid4().hex}"
            self.connection.execute(f"SAVEPOINT {savepoint}")
            try:
                yield
            except BaseException:
                try:
                    self.connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                finally:
                    self.connection.execute(f"RELEASE SAVEPOINT {savepoint}")
                raise
            else:
                self.connection.execute(f"RELEASE SAVEPOINT {savepoint}")
            return
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise
        else:
            self.connection.execute("COMMIT")

    def get(self, event_id: str) -> OutcomeReceipt | None:
        row = self.connection.execute(
            "SELECT * FROM outcome_candidates WHERE event_id=?", (event_id,)
        ).fetchone()
        if row is None:
            return None
        claim = json.loads(row["claim_json"])
        if (
            not isinstance(claim, dict)
            or set(claim) != set(_CLAIM_V1_FIELDS)
            or claim.get("schema_version") != OUTCOME_CLAIM_SCHEMA_VERSION
        ):
            raise RuntimeError("unsupported stored outcome claim schema")
        claim["claimed_outcome"] = ClaimedOutcome(claim["claimed_outcome"])
        candidate = OutcomeCandidate(**claim)
        _, digest = _serialize(candidate)
        if (
            digest != row["claim_sha256"]
            or candidate.event_id != row["event_id"]
            or candidate.source_actor_id != row["source_actor_id"]
            or candidate.evidence_ref != row["evidence_ref"]
            or candidate.evidence_sha256 != row["evidence_sha256"]
            or candidate.expires_at != row["expires_at"]
        ):
            raise RuntimeError("stored outcome claim failed integrity check")
        return OutcomeReceipt(
            claim=candidate,
            claim_sha256=digest,
            status=OutcomeState(row["status"]),
            reject_reason=row["reject_reason"],
            created_at=float(row["created_at"]),
            rejected_at=None
            if row["rejected_at"] is None
            else float(row["rejected_at"]),
        )

    def count(self) -> int:
        return int(
            self.connection.execute(
                "SELECT COUNT(*) FROM outcome_candidates"
            ).fetchone()[0]
        )

    def record(self, claim: OutcomeCandidate, *, now: float) -> OutcomeReceipt:
        if not isinstance(claim, OutcomeCandidate):
            raise TypeError("claim must be an OutcomeCandidate")
        now = _time(now, name="now")
        canonical, digest = _serialize(claim)
        with self._atomic():
            existing = self.get(claim.event_id)
            if existing is not None:
                if existing.claim_sha256 != digest:
                    raise OutcomeConflict(
                        "outcome event_id reused for a different payload"
                    )
                return existing

            reason = (
                "FUTURE_OBSERVATION"
                if claim.observed_at > now
                else "EXPIRED_ON_ARRIVAL"
                if claim.expires_at <= now
                else None
            )
            status = OutcomeState.REJECTED if reason else OutcomeState.CANDIDATE
            self.connection.execute(
                """
                INSERT INTO outcome_candidates (
                    event_id, claim_json, claim_sha256, source_actor_id, evidence_ref,
                    evidence_sha256, expires_at, status, reject_reason, created_at, rejected_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    claim.event_id,
                    canonical,
                    digest,
                    claim.source_actor_id,
                    claim.evidence_ref,
                    claim.evidence_sha256,
                    float(claim.expires_at),
                    status.value,
                    reason,
                    now,
                    now if reason else None,
                ),
            )
            self.store.append_journal(
                event_type="OUTCOME_CANDIDATE_RECORDED"
                if reason is None
                else "OUTCOME_CANDIDATE_REJECTED",
                subject_id=claim.event_id,
                payload={
                    "claim_sha256": digest,
                    "status": status.value,
                    "reason": reason,
                },
                now=now,
            )
            result = self.get(claim.event_id)
            assert result is not None
            return result

    def reject(self, event_id: str, *, reason: str, now: float) -> OutcomeReceipt:
        _text(event_id, name="event_id")
        now = _time(now, name="now")
        if reason not in _REJECTION_REASONS:
            raise ValueError("rejection reason is unsupported")
        with self._atomic():
            existing = self.get(event_id)
            if existing is None:
                raise KeyError(event_id)
            if now < existing.created_at:
                raise ValueError("rejection timestamp predates the candidate")
            if reason == "EXPIRED" and existing.claim.expires_at > now:
                raise ValueError("EXPIRED requires the claim's expiry threshold")
            if existing.status is OutcomeState.REJECTED:
                if existing.reject_reason != reason:
                    raise OutcomeConflict("rejected claim cannot change its decision")
                return existing
            self.connection.execute(
                """
                UPDATE outcome_candidates
                SET status='REJECTED', reject_reason=?, rejected_at=?
                WHERE event_id=? AND status='CANDIDATE'
                """,
                (reason, now, event_id),
            )
            self.store.append_journal(
                event_type="OUTCOME_CANDIDATE_REJECTED",
                subject_id=event_id,
                payload={"claim_sha256": existing.claim_sha256, "reason": reason},
                now=now,
            )
            result = self.get(event_id)
            assert result is not None
            return result

    def reject_expired(self, *, now: float) -> tuple[str, ...]:
        now = _time(now, name="now")
        with self._atomic():
            ids = tuple(
                str(row["event_id"])
                for row in self.connection.execute(
                    """
                    SELECT event_id FROM outcome_candidates
                    WHERE status='CANDIDATE' AND expires_at<=?
                    ORDER BY event_id
                    """,
                    (now,),
                )
            )
            for event_id in ids:
                self.connection.execute(
                    """
                    UPDATE outcome_candidates
                    SET status='REJECTED', reject_reason='EXPIRED', rejected_at=?
                    WHERE event_id=? AND status='CANDIDATE'
                    """,
                    (now, event_id),
                )
                self.store.append_journal(
                    event_type="OUTCOME_CANDIDATE_REJECTED",
                    subject_id=event_id,
                    payload={"reason": "EXPIRED"},
                    now=now,
                )
            return ids
