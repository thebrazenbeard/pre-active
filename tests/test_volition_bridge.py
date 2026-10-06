from __future__ import annotations

from pathlib import Path

import pytest

from pre_active.store import Store


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
