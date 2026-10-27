from pathlib import Path

import pytest

from pre_active.scheduler import Scheduler
from pre_active.store import Store


def test_interval_schedule_emits_each_occurrence_once(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    scheduler = Scheduler(store)
    schedule_id = scheduler.add_interval(
        kind="task.requested",
        payload={"task": "heartbeat"},
        every_seconds=60.0,
        first_at=100.0,
        now=1.0,
    )

    assert scheduler.tick(now=99.0) == 0
    assert scheduler.tick(now=100.0) == 1
    assert scheduler.tick(now=100.0) == 0
    assert scheduler.tick(now=159.9) == 0
    assert scheduler.tick(now=160.0) == 1

    ids = [row["dedup_key"] for row in store.list_events(kind="task.requested")]
    assert ids == [f"schedule:{schedule_id}:100.000000", f"schedule:{schedule_id}:160.000000"]


@pytest.mark.parametrize(
    ("every_seconds", "first_at", "now"),
    [
        (float("nan"), 10.0, 0.0),
        (float("inf"), 10.0, 0.0),
        (1.0, float("inf"), 0.0),
        (1.0, 10.0, float("nan")),
        (1e-9, 1e12, 0.0),
    ],
)
def test_invalid_or_nonprogressing_interval_is_rejected(
    tmp_path: Path, every_seconds: float, first_at: float, now: float
) -> None:
    store = Store(tmp_path / "state.db")
    with pytest.raises(ValueError):
        Scheduler(store).add_interval(
            kind="task.requested",
            payload={"task": "heartbeat"},
            every_seconds=every_seconds,
            first_at=first_at,
            now=now,
        )
    assert store.connection.execute("SELECT COUNT(*) FROM schedules").fetchone()[0] == 0


def test_stalled_legacy_interval_fails_before_emitting(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    scheduler = Scheduler(store)
    schedule_id = scheduler.add_interval(
        kind="task.requested",
        payload={"task": "heartbeat"},
        every_seconds=60.0,
        first_at=1e12,
        now=0.0,
    )
    store.connection.execute(
        "UPDATE schedules SET every_seconds=? WHERE id=?", (1e-9, schedule_id)
    )
    with pytest.raises(ValueError, match="advance"):
        scheduler.tick(now=1e12, max_occurrences=3)
    assert store.list_events(kind="task.requested") == []
    stored = store.connection.execute(
        "SELECT next_at FROM schedules WHERE id=?", (schedule_id,)
    ).fetchone()
    assert stored["next_at"] == 1e12


@pytest.mark.parametrize("now", [float("nan"), float("inf")])
def test_nonfinite_tick_clock_is_rejected_without_emission(tmp_path: Path, now: float) -> None:
    store = Store(tmp_path / "state.db")
    scheduler = Scheduler(store)
    scheduler.add_interval(
        kind="task.requested", payload={"task": "heartbeat"},
        every_seconds=60.0, first_at=10.0, now=0.0,
    )
    with pytest.raises(ValueError, match="now"):
        scheduler.tick(now=now)
    assert store.list_events(kind="task.requested") == []


def test_submicrosecond_schedule_occurrences_have_unique_durable_keys(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    scheduler = Scheduler(store)
    scheduler.add_interval(
        kind="task.requested", payload={"task": "fast"},
        every_seconds=1e-7, first_at=0.0, now=0.0,
    )
    emitted = scheduler.tick(now=1e-6, max_occurrences=20)
    events = store.list_events(kind="task.requested")
    assert emitted == 11
    assert len(events) == emitted
    assert len({row["dedup_key"] for row in events}) == emitted
    assert scheduler.tick(now=1e-6, max_occurrences=20) == 0


def test_upgrade_reuses_exact_legacy_submicrosecond_event(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    scheduler = Scheduler(store)
    schedule_id = scheduler.add_interval(
        kind="task.requested", payload={"task": "legacy"},
        every_seconds=1.0, first_at=4e-7, now=0.0,
    )
    legacy_key = f"schedule:{schedule_id}:0.000000"
    original_id = store.enqueue_event(
        kind="task.requested", payload={"task": "legacy"},
        dedup_key=legacy_key, now=0.0, available_at=4e-7,
    )
    assert scheduler.tick(now=4e-7) == 1
    events = store.list_events(kind="task.requested")
    assert [(event["id"], event["dedup_key"]) for event in events] == [
        (original_id, legacy_key)
    ]


def test_upgrade_does_not_reuse_legacy_key_for_different_occurrence(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    scheduler = Scheduler(store)
    schedule_id = scheduler.add_interval(
        kind="task.requested", payload={"task": "legacy"},
        every_seconds=1.0, first_at=3e-7, now=0.0,
    )
    legacy_key = f"schedule:{schedule_id}:0.000000"
    store.enqueue_event(
        kind="task.requested", payload={"task": "legacy"},
        dedup_key=legacy_key, now=0.0, available_at=4e-7,
    )
    assert scheduler.tick(now=3e-7) == 1
    events = store.list_events(kind="task.requested")
    assert len(events) == 2
    assert len({row["dedup_key"] for row in events}) == 2
