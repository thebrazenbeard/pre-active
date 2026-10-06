from __future__ import annotations

from pathlib import Path

import pytest

from pre_active.store import Store
from pre_active.volition_bridge import VolitionBridge
from volition import Policy, VolitionEngine


def test_volition_state_round_trip_uses_revision_cas(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")

    assert store.get_volition_state() is None

    revision = store.save_volition_state(
        {"schema": "VOLITION_STATE_V2", "active_goal": None},
        expected_revision=0,
        now=1.0,
    )
    assert revision == 1
    assert store.get_volition_state() == {
        "revision": 1,
        "snapshot": {"active_goal": None, "schema": "VOLITION_STATE_V2"},
        "updated_at": 1.0,
    }

    with pytest.raises(RuntimeError, match="volition state revision changed"):
        store.save_volition_state(
            {"schema": "VOLITION_STATE_V2", "active_goal": {"goal_id": "stale"}},
            expected_revision=0,
            now=2.0,
        )


def test_volition_signal_receipt_round_trip_is_unique_by_source_event(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")

    store.record_volition_signal_receipt(
        source_event_id="signal-event-1",
        state_revision=3,
        cognition_event_id="cognition-event-1",
        goal_id="goal-0001",
        target="investigate",
        urgency=0.75,
        provenance="current_observation",
        signal_source="observer:file",
        choice_id="choice-0001",
        choice_class="POLICY_DERIVED",
        choice_source="volition:policy",
        now=4.0,
    )

    assert store.get_volition_signal_receipt("signal-event-1") == {
        "source_event_id": "signal-event-1",
        "state_revision": 3,
        "cognition_event_id": "cognition-event-1",
        "goal_id": "goal-0001",
        "target": "investigate",
        "urgency": 0.75,
        "provenance": "current_observation",
        "signal_source": "observer:file",
        "choice_id": "choice-0001",
        "choice_class": "POLICY_DERIVED",
        "choice_source": "volition:policy",
        "created_at": 4.0,
    }

    with pytest.raises(RuntimeError, match="volition signal receipt already exists"):
        store.record_volition_signal_receipt(
            source_event_id="signal-event-1",
            state_revision=4,
            cognition_event_id=None,
            goal_id=None,
            target="different",
            urgency=None,
            provenance="inference",
            signal_source="different",
            choice_id=None,
            choice_class=None,
            choice_source=None,
            now=5.0,
        )


def _apply_signal(
    store: Store,
    bridge: VolitionBridge,
    *,
    source_event_id: str,
    payload: dict[str, object],
    now: float,
) -> dict[str, object]:
    store.connection.execute("BEGIN IMMEDIATE")
    try:
        receipt = bridge.process_signal_event(
            source_event_id=source_event_id,
            payload=payload,
            now=now,
        )
        store.connection.execute("COMMIT")
        return receipt
    except BaseException:
        store.connection.execute("ROLLBACK")
        raise


def _open_loop_payload(target: str = "investigate") -> dict[str, object]:
    return {
        "target": target,
        "kind": "open_loop",
        "magnitude": 0.8,
        "confidence": 1.0,
        "provenance": "current_observation",
        "source": "observer:file",
        "effect_authority": False,
    }


def test_bridge_enqueues_zero_capability_endogenous_turn_with_provenance(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "state.db")
    bridge = VolitionBridge(store)

    receipt = _apply_signal(
        store,
        bridge,
        source_event_id="signal-1",
        payload=_open_loop_payload(),
        now=1.0,
    )

    assert receipt["state_revision"] == 1
    assert receipt["goal_id"] == "goal-0001"
    assert receipt["choice_class"] == "POLICY_DERIVED"
    assert receipt["cognition_event_id"] is not None
    [event] = store.list_events(kind="autonomous.turn")
    assert event["id"] == receipt["cognition_event_id"]
    assert event["payload"]["source"] == "ENDOGENOUS"
    assert event["payload"]["capabilities"] == []
    assert event["payload"]["volition"]["effect_authority"] is False
    assert event["payload"]["volition"]["signal_provenance"] == "current_observation"
    assert event["payload"]["volition"]["signal_source"] == "observer:file"


def test_bridge_replay_does_not_advance_state_or_duplicate_cognition(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "state.db")
    bridge = VolitionBridge(store)
    payload = _open_loop_payload()

    first = _apply_signal(
        store, bridge, source_event_id="signal-1", payload=payload, now=1.0
    )
    repeated = _apply_signal(
        store, bridge, source_event_id="signal-1", payload=payload, now=2.0
    )

    assert repeated == first
    assert store.get_volition_state()["revision"] == 1
    assert len(store.list_events(kind="autonomous.turn")) == 1


def test_bridge_persists_volition_cognition_budget_across_signals(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    engine = VolitionEngine(Policy(endogenous_turn_budget=1))
    assert store.save_volition_state(engine.snapshot(), expected_revision=0, now=0.0) == 1
    bridge = VolitionBridge(store)

    first = _apply_signal(
        store, bridge, source_event_id="signal-1", payload=_open_loop_payload(), now=1.0
    )

    second = _apply_signal(
        store, bridge, source_event_id="signal-2", payload=_open_loop_payload(), now=2.0
    )

    assert first["cognition_event_id"] is not None
    assert second["cognition_event_id"] is None
    assert store.get_volition_state()["revision"] == 3
    assert len(store.list_events(kind="autonomous.turn")) == 1


def test_bridge_rejects_effect_authority_claim_before_state_change(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    bridge = VolitionBridge(store)
    payload = _open_loop_payload()
    payload["effect_authority"] = True

    store.connection.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(ValueError, match="effect authority"):
            bridge.process_signal_event(
                source_event_id="signal-authority",
                payload=payload,
                now=1.0,
            )
    finally:
        store.connection.execute("ROLLBACK")

    assert store.get_volition_state() is None
    assert store.list_events(kind="autonomous.turn") == []
