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


def _volition_schedule_config(target: str = "review-open-loops") -> dict[str, object]:
    return {
        "target": target,
        "kind": "open_loop",
        "magnitude": 0.5,
        "confidence": 1.0,
        "provenance": "current_observation",
    }


def test_volition_interval_persists_derived_signal_payload(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    scheduler = Scheduler(store)
    schedule_id = scheduler.add_volition_interval(
        config=_volition_schedule_config(),
        every_seconds=60.0,
        first_at=100.0,
        now=1.0,
    )

    row = store.connection.execute(
        "SELECT * FROM schedules WHERE id=?",
        (schedule_id,),
    ).fetchone()
    assert row is not None
    assert row["kind"] == "volition.signal"
    payload = __import__("json").loads(row["payload_json"])
    assert payload == {
        **_volition_schedule_config(),
        "source": f"schedule:{schedule_id}",
        "effect_authority": False,
    }
    assert float(row["every_seconds"]) == 60.0
    assert float(row["next_at"]) == 100.0
    assert bool(row["enabled"]) is True
    store.close()


def test_volition_interval_rejects_invalid_config_before_insert(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    scheduler = Scheduler(store)
    config = _volition_schedule_config()
    config["priority"] = -100

    with __import__("pytest").raises(
        ValueError,
        match="static signal config contains unsupported field: priority",
    ):
        scheduler.add_volition_interval(
            config=config,
            every_seconds=60.0,
            first_at=100.0,
            now=1.0,
        )

    count = store.connection.execute(
        "SELECT COUNT(*) AS n FROM schedules"
    ).fetchone()
    assert count is not None
    assert int(count["n"]) == 0
    store.close()


def test_volition_interval_emits_priority_zero_occurrences_once(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    scheduler = Scheduler(store)
    schedule_id = scheduler.add_volition_interval(
        config=_volition_schedule_config("temporal-review"),
        every_seconds=60.0,
        first_at=100.0,
        now=1.0,
    )

    assert scheduler.tick(now=99.0) == 0
    assert scheduler.tick(now=100.0) == 1
    assert scheduler.tick(now=100.0) == 0
    assert scheduler.tick(now=220.0) == 2
    assert scheduler.tick(now=220.0) == 0

    rows = store.connection.execute(
        """
        SELECT kind, payload_json, priority, dedup_key, available_at
        FROM events
        ORDER BY available_at ASC, id ASC
        """
    ).fetchall()
    assert len(rows) == 3
    assert [row["kind"] for row in rows] == ["volition.signal"] * 3
    assert [int(row["priority"]) for row in rows] == [0, 0, 0]
    assert [float(row["available_at"]) for row in rows] == [100.0, 160.0, 220.0]
    assert [row["dedup_key"] for row in rows] == [
        f"schedule:{schedule_id}:100.000000",
        f"schedule:{schedule_id}:160.000000",
        f"schedule:{schedule_id}:220.000000",
    ]
    for row in rows:
        payload = __import__("json").loads(row["payload_json"])
        assert payload["source"] == f"schedule:{schedule_id}"
        assert payload["effect_authority"] is False
        assert "capabilities" not in payload
        assert "priority" not in payload

    schedule = store.connection.execute(
        "SELECT next_at FROM schedules WHERE id=?",
        (schedule_id,),
    ).fetchone()
    assert schedule is not None
    assert float(schedule["next_at"]) == 280.0
    store.close()
