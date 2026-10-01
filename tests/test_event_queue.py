from pathlib import Path

from pre_active.store import Store


def test_event_claim_is_exclusive_and_recovers_expired_lease(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    event_id = store.enqueue_event(
        kind="task.requested",
        payload={"task": "inspect"},
        priority=3,
        dedup_key="task-1",
        now=100.0,
    )

    first = store.claim_event(worker_id="worker-a", now=101.0, lease_seconds=10.0)
    assert first is not None
    assert first.id == event_id

    assert store.claim_event(worker_id="worker-b", now=105.0, lease_seconds=10.0) is None

    recovered = store.claim_event(worker_id="worker-b", now=112.0, lease_seconds=10.0)
    assert recovered is not None
    assert recovered.id == event_id
    assert recovered.attempts == 2


def test_event_lifecycle_is_append_only_journaled(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    event_id = store.enqueue_event(
        kind="probe",
        payload={"value": 1},
        dedup_key="probe-1",
        now=1.0,
    )
    event = store.claim_event(worker_id="w1", now=2.0, lease_seconds=10.0)
    assert event is not None
    store.ack_event(event.id, worker_id="w1", now=3.0)

    entries = store.list_journal(subject_id=event_id)
    assert [entry["event_type"] for entry in entries] == [
        "EVENT_ENQUEUED",
        "EVENT_CLAIMED",
        "EVENT_ACKED",
    ]


def test_event_dedup_key_is_bound_to_exact_semantic_request(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    first = store.enqueue_event(
        kind="task.requested",
        payload={"task": "alpha", "capabilities": []},
        priority=2,
        dedup_key="same-key",
        now=1.0,
    )
    assert store.enqueue_event(
        kind="task.requested",
        payload={"task": "alpha", "capabilities": []},
        priority=2,
        dedup_key="same-key",
        now=2.0,
    ) == first

    import pytest

    with pytest.raises(RuntimeError, match="dedup_key is already bound"):
        store.enqueue_event(
            kind="task.requested",
            payload={"task": "beta", "capabilities": []},
            priority=2,
            dedup_key="same-key",
            now=3.0,
        )
