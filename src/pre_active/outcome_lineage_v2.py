"""Untrusted, read-only V2 lineage envelope over one historical correlation.

All sources and expected IDs are caller-supplied claims. Even a consistent
envelope is NOT an authenticated witness, verified causal chain, current need
observation, goal-completion receipt, or authority to execute an effect.
The optional prior snapshot is not a persistent or globally complete replay
ledger; durable admission remains a separate host responsibility.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .outcome_correlation import OutcomeCorrelation
from .outcomes import OutcomeState

_SHA = re.compile(r"[0-9a-f]{64}\Z")
_MAX_PRIOR = 256
_LINEAGE_TOKEN = object()


def _identifier(value: object, field: str) -> str:
    if (type(value) is not str or not 0 < len(value) <= 128
            or value != value.strip()
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ValueError(f"{field} requires bounded nonempty exact text")
    return value


def _revision(value: object, field: str) -> int:
    if type(value) is not int or not 1 <= value < 2**63:
        raise ValueError(f"{field} must be a positive exact revision")
    return value


def _digest(value: object, field: str) -> str:
    if type(value) is not str or _SHA.fullmatch(value) is None:
        raise ValueError(f"{field} must be lowercase SHA-256")
    return value


@dataclass(frozen=True, slots=True)
class ClaimedSourceLineageV2:
    candidate_event_id: str
    meso_source_event_id: str
    signal_event_id: str
    goal_id: str
    choice_id: str
    goal_revision: int
    need_id: str
    need_revision: int
    need_observation_id: str
    action_request_id: str
    attempt_id: str

    def __post_init__(self) -> None:
        for name in (
            "candidate_event_id", "meso_source_event_id", "signal_event_id",
            "goal_id", "choice_id", "need_id", "need_observation_id",
            "action_request_id", "attempt_id",
        ):
            _identifier(getattr(self, name), name)
        _revision(self.goal_revision, "goal revision")
        _revision(self.need_revision, "need revision")


@dataclass(frozen=True, slots=True)
class ClaimedPhysicalEffectV2:
    attempt_id: str
    physical_effect_id: str
    evidence_sha256: str

    def __post_init__(self) -> None:
        _identifier(self.attempt_id, "effect attempt")
        _identifier(self.physical_effect_id, "physical effect")
        _digest(self.evidence_sha256, "effect evidence digest")


@dataclass(frozen=True, slots=True, init=False)
class OutcomeLineageV2:
    source: ClaimedSourceLineageV2
    effect: ClaimedPhysicalEffectV2
    candidate_sha256: str

    def __init__(
        self, source: ClaimedSourceLineageV2, effect: ClaimedPhysicalEffectV2,
        candidate_sha256: str, *, _token: object | None = None,
    ) -> None:
        if _token is not _LINEAGE_TOKEN:
            raise TypeError("V2 lineage must be correlated by the source boundary")
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "effect", effect)
        object.__setattr__(self, "candidate_sha256", candidate_sha256)

    @property
    def historical_only(self) -> bool:
        return True

    @property
    def evidence_admitted(self) -> bool:
        return False

    @property
    def need_lineage_verified(self) -> bool:
        return False

    @property
    def choice_link_verified(self) -> bool:
        return False

    @property
    def physical_effect_verified(self) -> bool:
        return False

    @property
    def effect_authority(self) -> bool:
        return False


def correlate_outcome_lineage_v2(
    *, correlations: tuple[OutcomeCorrelation, ...],
    source: ClaimedSourceLineageV2, effect: ClaimedPhysicalEffectV2,
    expected_goal_revision: int, expected_need_revision: int,
    expected_attempt_id: str, expected_physical_effect_id: str,
    prior: tuple[OutcomeLineageV2, ...] = (),
) -> OutcomeLineageV2:
    """Cross-bind caller claims against one *unverified* historical V1 join.

    A caller may omit competing joins or previous attempts; success proves
    neither uniqueness in the real world nor authentication of any identifier.
    """
    if type(correlations) is not tuple or len(correlations) != 1:
        raise ValueError("exactly one caller-provided correlation is required")
    correlation = correlations[0]
    if type(correlation) is not OutcomeCorrelation or (
        correlation.candidate_status is not OutcomeState.CANDIDATE
        or correlation.historical_only is not True
        or any((
            correlation.evidence_admitted, correlation.candidate_choice_link_verified,
            correlation.need_lineage_verified, correlation.tool_effect_verified,
            correlation.effect_authority,
        ))
    ):
        raise ValueError("only inert historical outcome correlations are allowed")
    if type(source) is not ClaimedSourceLineageV2:
        raise ValueError("exact source lineage claim required")
    if type(effect) is not ClaimedPhysicalEffectV2:
        raise ValueError("exact physical effect claim required")
    _revision(expected_goal_revision, "expected goal revision")
    _revision(expected_need_revision, "expected need revision")
    _identifier(expected_attempt_id, "expected attempt")
    _identifier(expected_physical_effect_id, "expected physical effect")
    if type(prior) is not tuple or len(prior) > _MAX_PRIOR or any(
        type(item) is not OutcomeLineageV2 for item in prior
    ):
        raise ValueError("bounded prior lineage snapshot required")

    for field, actual, expected in (
        ("candidate", source.candidate_event_id, correlation.candidate_event_id),
        ("MESO source", source.meso_source_event_id, correlation.meso_source_event_id),
        ("signal", source.signal_event_id, correlation.signal_event_id),
        ("goal", source.goal_id, correlation.goal_id),
        ("choice", source.choice_id, correlation.choice_id),
        ("need", source.need_id, correlation.need_id_claimed),
        ("action", source.action_request_id, correlation.action_request_id_claimed),
    ):
        if actual != expected:
            raise ValueError(f"{field} source correlation mismatch")
    if source.goal_revision != expected_goal_revision:
        raise ValueError("goal revision mismatch (caller assertion)")
    if source.need_revision != expected_need_revision:
        raise ValueError("need revision mismatch (caller assertion)")
    if effect.attempt_id != source.attempt_id or (
        source.attempt_id != expected_attempt_id
    ):
        raise ValueError("attempt binding mismatch (caller assertion)")
    if effect.physical_effect_id != expected_physical_effect_id:
        raise ValueError("physical effect binding mismatch (caller assertion)")
    if effect.evidence_sha256 != correlation.evidence_sha256_claimed:
        raise ValueError("effect evidence digest mismatch (unverified claim)")
    if any(
        entry.source.candidate_event_id == source.candidate_event_id
        or entry.source.attempt_id == source.attempt_id
        or entry.effect.physical_effect_id == effect.physical_effect_id
        for entry in prior
    ):
        raise ValueError("replay in caller-supplied prior snapshot")
    return OutcomeLineageV2(
        source, effect, _digest(correlation.candidate_sha256, "candidate digest"),
        _token=_LINEAGE_TOKEN,
    )
