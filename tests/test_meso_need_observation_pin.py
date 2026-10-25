"""The optional MESO dependency exposes an inert, clock-bounded need update."""

from datetime import datetime, timezone
from pathlib import Path
import tomllib

import pytest

from meso_crct import (
    AdmissionClock,
    HomeostaticState,
    NeedAxis,
    NeedObservation,
    NeedObservationFuture,
    NeedObservationLedger,
    NeedObservationVerifier,
    Provenance,
    SourceKind,
    admit_need_observation,
)


MESO_SOURCE = "meso-crct @ git+https://github.com/thebrazenbeard/meso-crct.git@4ed527d31a68fc215158a520f1cd404e8f16b35b"


def test_meso_and_dev_extras_bind_the_reviewed_observation_source() -> None:
    config = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())
    extras = config["project"]["optional-dependencies"]
    assert [entry for entry in extras["meso"] if entry.startswith("meso-crct @ ")] == [MESO_SOURCE]
    assert [entry for entry in extras["dev"] if entry.startswith("meso-crct @ ")] == [MESO_SOURCE]


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
