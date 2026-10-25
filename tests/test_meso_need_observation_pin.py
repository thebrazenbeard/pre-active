"""The optional MESO dependency exposes an inert, clock-bounded need update."""

from datetime import datetime, timezone
from itertools import repeat
from pathlib import Path
import tomllib

import pytest

from meso_crct import (
    AdmissionClock,
    HomeostaticState,
    NeedAxis,
    NeedObservation,
    NeedObservationCapacity,
    NeedObservationFuture,
    NeedObservationLedger,
    NeedObservationVerifier,
    Provenance,
    SourceKind,
    admit_need_observation,
)
from volition import (
    DriveKind,
    GoalCompletionClaim,
    GoalCompletionVerifier,
    ProvenanceClass,
    Signal,
    VolitionEngine,
)


MESO_SOURCE = "meso-crct @ git+https://github.com/thebrazenbeard/meso-crct.git@14bd3f6a359fc267a5246ccfc1682cb4c0c1b2e4"
VOLITION_SOURCE = "volition @ git+https://github.com/thebrazenbeard/volition.git@53570902890852845599df9b997bad5fa68ac2f3"


def test_meso_and_dev_extras_bind_the_reviewed_observation_source() -> None:
    config = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())
    extras = config["project"]["optional-dependencies"]
    assert [entry for entry in extras["meso"] if entry.startswith("meso-crct @ ")] == [MESO_SOURCE]
    assert [entry for entry in extras["dev"] if entry.startswith("meso-crct @ ")] == [MESO_SOURCE]


def test_all_affect_extras_bind_the_same_reviewed_volition_source() -> None:
    config = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())
    extras = config["project"]["optional-dependencies"]
    for extra in ("volition", "meso", "dev"):
        assert [entry for entry in extras[extra] if entry.startswith("volition @ ")] == [VOLITION_SOURCE]


def test_updated_volition_scalar_feedback_cannot_complete_an_active_goal() -> None:
    engine = VolitionEngine()
    active = engine.tick([Signal(target="rest", kind=DriveKind.OPEN_LOOP, magnitude=0.9)])
    assert active is not None
    engine.record_satisfaction("rest", 1.0)
    assert engine.active_goal == active
    assert not any(event.kind == "GOAL_COMPLETED" for event in engine.events)


@pytest.mark.parametrize("value", [True, "0.5", float("inf"), float("nan")])
def test_updated_meso_need_state_rejects_untyped_or_nonfinite_measurements(value: object) -> None:
    with pytest.raises(ValueError, match="finite numeric"):
        NeedAxis("rest", setpoint=value, current_level=0.2)


def test_optional_meso_need_observation_admission_is_clock_bounded_and_inert() -> None:
    now = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
    ledger = NeedObservationLedger(
        homeostasis=HomeostaticState((NeedAxis("rest", setpoint=0.9, current_level=0.1),))
    )

    def sample(event_id: str, when: datetime) -> NeedObservation:
        return NeedObservation(
            event_id=event_id,
            need_id="rest",
            measured_level=0.5,
            observed_at=when,
            provenance=Provenance(
                source_kind=SourceKind.ENVIRONMENT,
                source_id="source:example",
                source_revision="1",
            ),
            evidence_ref="sample:example",
        )

    far_future = sample("future", datetime(9999, 1, 1, tzinfo=timezone.utc))
    admitted_future = NeedObservationVerifier(
        (far_future,), verifier_id="test-caller"
    ).admit(far_future)
    with pytest.raises(NeedObservationFuture):
        admit_need_observation(ledger, admitted_future, clock=AdmissionClock(now))
    assert ledger.records == ()

    measured = sample("fresh", now)
    admitted = NeedObservationVerifier((measured,), verifier_id="test-caller").admit(measured)
    updated = admit_need_observation(ledger, admitted, clock=AdmissionClock(now))
    assert updated.homeostasis.axis("rest").deficit == pytest.approx(0.4)
    assert updated.effect_authorized is False
    assert ledger.homeostasis.axis("rest").current_level == 0.1


def test_current_meso_verifier_bounds_untrusted_claim_iterables() -> None:
    observation = NeedObservation(
        event_id="candidate", need_id="rest", measured_level=0.5,
        observed_at=datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc),
        provenance=Provenance(
            source_kind=SourceKind.ENVIRONMENT, source_id="sensor:example",
            source_revision="1",
        ),
        evidence_ref="sample:example",
    )
    with pytest.raises(NeedObservationCapacity, match="capacity exceeded"):
        NeedObservationVerifier(repeat(observation), verifier_id="test-caller")


def test_current_volition_verifier_bounds_untrusted_claim_iterables() -> None:
    claim = GoalCompletionClaim(
        outcome_event_id="outcome:1", goal_id="goal:1", choice_id="choice:1",
        goal_revision=1, target="rest", result_receipt_id="receipt:1",
        evidence_ref="sample:example", source_ref="sensor:example",
        provenance=ProvenanceClass.CURRENT_OBSERVATION,
    )
    with pytest.raises(ValueError, match="capacity exceeded"):
        GoalCompletionVerifier(repeat(claim), verifier_id="test-caller")
