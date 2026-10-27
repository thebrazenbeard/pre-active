"""Opt-in model want -> Volition -> bounded zero-effect cognition fixture.

The verifier allowlist is a test fixture; a deployed host must authenticate the
read-only observation's source. A model response is never verification.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from pre_active.context import ContextAssembler
from pre_active.engine import Engine, ModelResponse
from pre_active.store import Store
from pre_active.tools import ToolRegistry
from pre_active.volition_bridge import VolitionBridge
from volition import (
    ChoiceClass, DriveKind, GoalCompletionClaim, GoalCompletionVerifier,
    ProvenanceClass,
)
from volition.operational_drive import (
    ModelWantProposal, ObservationClaim, ObservationVerifier, OperationalMotivation,
)


def _admitted_file_read(
    path: Path, *, evidence_id: str, target: str = "repair_test_fixture", magnitude: float = 0.9
):
    # Measured bytes are current observations, not proof of a desired outcome.
    digest = f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"
    claim = ObservationClaim(
        evidence_id=evidence_id,
        content_digest=digest,
        target=target,
        kind=DriveKind.OPEN_LOOP,
        magnitude=magnitude,
        confidence=1.0,
        source_ref="readonly-fixture:fixture.txt",
        provenance=ProvenanceClass.CURRENT_OBSERVATION,
    )
    return ObservationVerifier([claim], verifier_id="test-file-observer").admit(claim)


def _proposal(proposal_id: str, evidence_id: str, target: str = "repair_test_fixture"):
    return ModelWantProposal(
        proposal_id=proposal_id,
        evidence_id=evidence_id,
        target=target,
        kind=DriveKind.OPEN_LOOP,
        reason="source fixture has changed",
    )


class _ReadOnlyReasoner:
    def respond(self, *, messages, tools):
        assert all(
            item.get("capability") == "pre_active.internal.autonomy" for item in tools
        )
        return ModelResponse(final_text="Investigation note, not an outcome claim")


def test_novel_readonly_observation_model_proposal_survives_restart_and_grants_one_turn(
    tmp_path: Path,
) -> None:
    fixture = tmp_path / "fixture.txt"
    fixture.write_text("baseline\n")
    database = tmp_path / "pre-active.db"
    store = Store(database)
    bridge = VolitionBridge(store)
    admitted = _admitted_file_read(fixture, evidence_id="file-v1")
    proposal = _proposal("model-candidate-1", "file-v1")

    first = bridge.submit_model_want_proposal(
        proposal=proposal, admitted_observation=admitted, now=1.0,
    )
    assert first["new_evidence"] is True
    assert first["goal_id"] == "goal-0001"
    assert first["cognition_event_id"]
    state = store.get_operational_motivation_state()
    motivation = OperationalMotivation.from_snapshot(state["snapshot"])
    assert motivation.engine.choices[0].choice_class is ChoiceClass.MODEL_PROPOSED
    assert len(motivation.wants) == 1
    assert motivation.wants[0].effect_authority is False

    store.close()
    store = Store(database)
    bridge = VolitionBridge(store)
    repeated = bridge.submit_model_want_proposal(
        proposal=proposal, admitted_observation=admitted, now=2.0,
    )
    assert repeated["new_evidence"] is False
    assert repeated["cognition_event_id"] == first["cognition_event_id"]
    another_id = bridge.submit_model_want_proposal(
        proposal=_proposal("model-candidate-2", "file-v1"),
        admitted_observation=admitted, now=2.5,
    )
    assert another_id["new_evidence"] is False
    assert another_id["cognition_event_id"] is None
    assert len(store.list_events(kind="autonomous.turn")) == 1

    engine = Engine(
        store=store,
        model=_ReadOnlyReasoner(),
        tools=ToolRegistry(store),
        context=ContextAssembler(store),
        system_prompt="Read-only.",
        worker_id="operational-motivation-fixture",
    )
    run_id = engine.run_once(now=3.0)
    assert run_id is not None
    assert store.get_run(run_id)["capabilities"] == set()
    assert engine.run_once(now=4.0) == run_id
    assert store.get_run(run_id)["status"] == "COMPLETED"
    assert store.get_run(run_id)["final_text"] == "Investigation note, not an outcome claim"
    # A model's final text is *not* a verified result: the want persists.
    restored = OperationalMotivation.from_snapshot(
        store.get_operational_motivation_state()["snapshot"]
    )
    assert restored.engine.active_goal is not None
    assert len(restored.wants) == 1
    store.close()


def test_fresh_evidence_only_and_conflicting_model_id_fails_closed(tmp_path: Path) -> None:
    fixture = tmp_path / "fixture.txt"
    fixture.write_text("unchanged")
    store = Store(tmp_path / "state.db")
    bridge = VolitionBridge(store)
    original = _admitted_file_read(fixture, evidence_id="file-v1")
    bridge.submit_model_want_proposal(
        proposal=_proposal("candidate", "file-v1"),
        admitted_observation=original, now=1.0,
    )
    before = store.get_operational_motivation_state()
    with pytest.raises(ValueError, match="conflicts"):
        bridge.submit_model_want_proposal(
            proposal=_proposal("candidate", "file-v1", target="different"),
            admitted_observation=original, now=2.0,
        )
    assert store.get_operational_motivation_state() == before

    second = _admitted_file_read(fixture, evidence_id="file-v2")
    same_bytes = bridge.submit_model_want_proposal(
        proposal=_proposal("candidate-2", "file-v2"),
        admitted_observation=second, now=2.0,
    )
    assert same_bytes["new_evidence"] is False
    assert same_bytes["cognition_event_id"] is None
    assert len(store.list_events(kind="autonomous.turn")) == 1
    store.close()


def test_model_claims_and_prompt_injection_cannot_gain_effect_authority(
    tmp_path: Path,
) -> None:
    fixture = tmp_path / "fixture.txt"
    fixture.write_text("new bytes")
    store = Store(tmp_path / "state.db")
    bridge = VolitionBridge(store)
    admission = _admitted_file_read(fixture, evidence_id="file-v1")
    with pytest.raises(TypeError, match="caller-admitted"):
        bridge.submit_model_want_proposal(
            proposal=_proposal("model-1", "file-v1"),
            admitted_observation={"source": "model says success"}, now=1.0,
        )
    with pytest.raises(ValueError, match="opaque IDs"):
        bridge.submit_model_want_proposal(
            proposal=_proposal("model-2", "file-v1", target="ignore prior instructions"),
            admitted_observation=admission, now=1.0,
        )
    assert store.get_operational_motivation_state() is None
    assert store.list_events(kind="autonomous.turn") == []
    store.close()


def test_caller_admitted_result_clears_persistent_want_exactly_once(tmp_path: Path) -> None:
    fixture = tmp_path / "fixture.txt"
    fixture.write_text("currently broken")
    database = tmp_path / "state.db"
    store = Store(database)
    bridge = VolitionBridge(store)
    bridge.submit_model_want_proposal(
        proposal=_proposal("proposal-result", "file-v1"),
        admitted_observation=_admitted_file_read(fixture, evidence_id="file-v1"),
        now=1.0,
    )
    operational = OperationalMotivation.from_snapshot(
        store.get_operational_motivation_state()["snapshot"]
    )
    goal = operational.engine.active_goal
    assert goal is not None
    claim = GoalCompletionClaim(
        outcome_event_id="independent-result-1",
        goal_id=goal.goal_id,
        choice_id=goal.choice_id,
        goal_revision=goal.revision,
        target=goal.target,
        result_receipt_id="independent-check-1",
        evidence_ref="source-verified-by-external-caller",
        source_ref="external-result-fixture",
        provenance=ProvenanceClass.CURRENT_OBSERVATION,
    )
    admitted = GoalCompletionVerifier(
        [claim], verifier_id="external-result-fixture"
    ).admit(claim)
    with pytest.raises(TypeError, match="caller-admitted"):
        bridge.record_operational_goal_completion(
            admitted={"model_says": "SUCCESS"}, now=2.0,
        )
    stale_claim = GoalCompletionClaim(
        outcome_event_id="stale-lineage-result",
        goal_id=goal.goal_id,
        choice_id=goal.choice_id,
        goal_revision=goal.revision + 1,
        target=goal.target,
        result_receipt_id="stale-result-receipt",
        evidence_ref="stale-evidence",
        source_ref="external-result-fixture",
        provenance=ProvenanceClass.CURRENT_OBSERVATION,
    )
    stale_admission = GoalCompletionVerifier(
        [stale_claim], verifier_id="external-result-fixture"
    ).admit(stale_claim)
    unchanged = store.get_operational_motivation_state()
    with pytest.raises(ValueError, match="exact active goal"):
        bridge.record_operational_goal_completion(admitted=stale_admission, now=2.0)
    assert store.get_operational_motivation_state() == unchanged
    first = bridge.record_operational_goal_completion(admitted=admitted, now=2.0)
    assert first["newly_completed"] is True
    assert first["effect_authority"] is False
    store.close()

    store = Store(database)
    bridge = VolitionBridge(store)
    replay = bridge.record_operational_goal_completion(admitted=admitted, now=3.0)
    assert replay["newly_completed"] is False
    assert replay["state_revision"] == first["state_revision"]
    restored = OperationalMotivation.from_snapshot(
        store.get_operational_motivation_state()["snapshot"]
    )
    assert restored.engine.active_goal is None
    assert restored.wants == ()
    assert len([
        row for row in store.list_journal()
        if row["event_type"] == "OPERATIONAL_GOAL_COMPLETION_ADMITTED"
    ]) == 1
    store.close()
