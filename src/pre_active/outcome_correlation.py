"""Read-only, *untrusted* correlation of MESO, Volition, and outcome claims.

This is a source-level audit seam, not outcome admission, host attestation,
causal proof, need observation, effect approval, or goal completion. V1 outcome
claim serialization is deliberately unchanged. A candidate's ``need_id`` and
``action_request_id`` have no authenticated upstream link at this boundary.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass

from .outcomes import ClaimedOutcome, OutcomeLedger, OutcomeState
from .store import Store

_MESO_DRIVE_KIND = {
    "approach": "open_loop",
    "inspect": "epistemic",
    "withdraw": "protection",
}


@dataclass(frozen=True, slots=True)
class OutcomeCorrelation:
    """A historical, internally consistent join, with no granted authority.

    The trust indicators are deliberately *properties*, not dataclass fields:
    a frozen dataclass field with ``= False`` could still be constructed or
    ``dataclasses.replace``-ed with ``True`` by a caller. Even these properties
    are only a guard against accidental misuse; in-process objects never serve
    as cryptographic evidence or authorization tokens.
    """

    meso_source_event_id: str
    signal_event_id: str
    goal_id: str
    choice_id: str
    target_id: str
    candidate_event_id: str
    need_id_claimed: str
    action_request_id_claimed: str | None
    claimed_outcome: ClaimedOutcome
    claimed_need_change: float
    source_kind_claimed: str
    evidence_ref_claimed: str
    evidence_sha256_claimed: str
    candidate_sha256: str
    candidate_status: OutcomeState
    observed_at: float
    expires_at: float

    @property
    def historical_only(self) -> bool:
        """A successful join does not establish the currently active choice."""
        return True

    @property
    def evidence_admitted(self) -> bool:
        return False

    @property
    def candidate_choice_link_verified(self) -> bool:
        return False

    @property
    def need_lineage_verified(self) -> bool:
        return False

    @property
    def tool_effect_verified(self) -> bool:
        return False

    @property
    def effect_authority(self) -> bool:
        return False


def _identifier(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{name} requires a non-empty, trimmed string")
    return value


def _finite_now(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("now must be a finite nonnegative timestamp")
    try:
        normalized = float(value)
    except OverflowError as exc:
        raise ValueError("now must be a finite nonnegative timestamp") from exc
    if not math.isfinite(normalized) or normalized < 0:
        raise ValueError("now must be a finite nonnegative timestamp")
    return normalized


def correlate_outcome_candidate(
    *,
    store: Store,
    candidate_event_id: str,
    meso_source_event_id: str,
    now: float,
) -> OutcomeCorrelation:
    """Join durable receipts without writing, verifying, or applying anything.

    The MESO ingress must be non-HOLD and bind the exact persisted Volition
    signal, which must have a matching processed signal receipt. The outcome
    claim must name that signal's exact goal and be observed after the choice.
    ``need_id`` remains **claimant-supplied**: upstream MESO ``IntentProposal``
    currently has no need-observation ID or immutable need revision. Outcome
    candidate V1 has no ``choice_id`` or MESO source ID, so the caller-selected
    MESO source/choice association is *also unverified causation*, despite the
    exact goal/target match. A claimed action-request ID likewise does not
    prove execution or causation.

    Database-local correlation does not authenticate the producer, observer,
    digest, timestamps, tool effects, or real-world success. In particular,
    even ``SUCCESS`` and ``source_kind='observer'`` are untrusted claims.
    """
    if not isinstance(store, Store):
        raise TypeError("store must be a Store")
    candidate_event_id = _identifier(candidate_event_id, "candidate_event_id")
    meso_source_event_id = _identifier(meso_source_event_id, "meso_source_event_id")
    now = _finite_now(now)

    ingress = store.get_meso_intent_ingress(meso_source_event_id)
    if ingress is None:
        raise ValueError("MESO source event has no persisted ingress")
    signal_id = ingress["signal_event_id"]
    if not isinstance(signal_id, str) or not signal_id:
        raise ValueError("MESO HOLD has no Volition signal to correlate")
    intent = ingress["intent"]
    if (
        not isinstance(intent, dict)
        or intent.get("effect_authorized") is not False
        or intent.get("can_execute") is not False
        or not isinstance(intent.get("target_id"), str)
        or not intent["target_id"]
        or not isinstance(intent.get("kind"), str)
        or intent["kind"] not in _MESO_DRIVE_KIND
        or not isinstance(intent.get("source_tendency"), str)
        or not intent["source_tendency"]
        or not isinstance(intent.get("source"), str)
        or not intent["source"]
    ):
        raise ValueError("MESO ingress lacks the non-executable intent contract")

    signal_row = store.connection.execute(
        "SELECT kind, payload_json FROM events WHERE id=?", (signal_id,)
    ).fetchone()
    if signal_row is None or signal_row["kind"] != "volition.signal":
        raise ValueError("ingress is not bound to a persisted Volition signal")
    try:
        payload = json.loads(signal_row["payload_json"])
    except (TypeError, ValueError) as exc:
        raise ValueError("Volition signal payload cannot be decoded") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("meso_source_event_id") != meso_source_event_id
        or payload.get("target") != intent["target_id"]
        or payload.get("kind") != _MESO_DRIVE_KIND[intent["kind"]]
        or payload.get("source") != (
            f"meso-crct:intent:{intent['kind']}:"
            f"{intent['source_tendency']}:{intent['source']}"
        )
        or payload.get("effect_authority") is not False
        or payload.get("provenance") != "model_generated"
    ):
        raise ValueError("Volition signal mismatches the inert MESO source")

    receipt = store.get_volition_signal_receipt(signal_id)
    if (
        receipt is None
        or receipt["source_event_id"] != signal_id
        or receipt["target"] != intent["target_id"]
        or receipt["provenance"] != "model_generated"
        or receipt["signal_source"] != payload.get("source")
        or not isinstance(receipt["goal_id"], str)
        or not receipt["goal_id"]
        or not isinstance(receipt["choice_id"], str)
        or not receipt["choice_id"]
    ):
        raise ValueError("processed Volition choice/goal receipt is incomplete or mismatched")

    candidate_receipt = OutcomeLedger(store).get(candidate_event_id)
    if candidate_receipt is None or candidate_receipt.status is not OutcomeState.CANDIDATE:
        raise ValueError("outcome event is missing or is not an active candidate")
    claim = candidate_receipt.claim
    if claim.goal_id != receipt["goal_id"]:
        raise ValueError("candidate goal does not match Volition receipt")
    if not (
        ingress["created_at"] <= receipt["created_at"] <= claim.observed_at
        <= candidate_receipt.created_at <= now < claim.expires_at
    ):
        raise ValueError("candidate/intent chronology is inconsistent or expired")

    return OutcomeCorrelation(
        meso_source_event_id=meso_source_event_id,
        signal_event_id=signal_id,
        goal_id=claim.goal_id,
        choice_id=receipt["choice_id"],
        target_id=intent["target_id"],
        candidate_event_id=claim.event_id,
        need_id_claimed=claim.need_id,
        action_request_id_claimed=claim.action_request_id,
        claimed_outcome=claim.claimed_outcome,
        claimed_need_change=claim.claimed_need_change,
        source_kind_claimed=claim.source_kind,
        evidence_ref_claimed=claim.evidence_ref,
        evidence_sha256_claimed=claim.evidence_sha256,
        candidate_sha256=candidate_receipt.claim_sha256,
        candidate_status=candidate_receipt.status,
        observed_at=claim.observed_at,
        expires_at=claim.expires_at,
    )
