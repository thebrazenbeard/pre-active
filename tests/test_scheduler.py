from pathlib import Path

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
