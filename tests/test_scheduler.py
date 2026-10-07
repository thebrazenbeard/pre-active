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
    assert scheduler.tick(now=220.0) == 1
    assert scheduler.tick(now=220.0) == 0

    rows = store.connection.execute(
        """
        SELECT kind, payload_json, priority, dedup_key, available_at
        FROM events
        ORDER BY available_at ASC, id ASC
        """
    ).fetchall()
    assert len(rows) == 2
    assert [row["kind"] for row in rows] == ["volition.signal"] * 2
    assert [int(row["priority"]) for row in rows] == [0, 0]
    assert [float(row["available_at"]) for row in rows] == [100.0, 220.0]
    assert [row["dedup_key"] for row in rows] == [
        f"schedule:{schedule_id}:100.000000",
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


def test_volition_interval_copies_config_at_creation(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    scheduler = Scheduler(store)
    config = _volition_schedule_config("immutable-schedule")
    schedule_id = scheduler.add_volition_interval(
        config=config,
        every_seconds=60.0,
        first_at=100.0,
        now=1.0,
    )

    config["target"] = "mutated-after-create"
    config["magnitude"] = 999.0

    row = store.connection.execute(
        "SELECT payload_json FROM schedules WHERE id=?",
        (schedule_id,),
    ).fetchone()
    assert row is not None
    payload = __import__("json").loads(row["payload_json"])
    assert payload["target"] == "immutable-schedule"
    assert payload["magnitude"] == 0.5
    store.close()


def test_volition_interval_coalesces_missed_occurrences_to_latest_due(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "state.db")
    scheduler = Scheduler(store)
    schedule_id = scheduler.add_volition_interval(
        config=_volition_schedule_config("bounded-catchup"),
        every_seconds=10.0,
        first_at=10.0,
        now=1.0,
    )

    assert scheduler.tick(now=1000.0, max_occurrences=100) == 1

    rows = store.connection.execute(
        """
        SELECT dedup_key, available_at
        FROM events
        WHERE kind='volition.signal'
        ORDER BY available_at ASC
        """
    ).fetchall()
    assert [(row["dedup_key"], float(row["available_at"])) for row in rows] == [
        (f"schedule:{schedule_id}:1000.000000", 1000.0),
    ]
    schedule = store.connection.execute(
        "SELECT next_at FROM schedules WHERE id=?",
        (schedule_id,),
    ).fetchone()
    assert schedule is not None
    assert float(schedule["next_at"]) == 1010.0
    store.close()

def test_volition_interval_runtime_dependency_loss_does_not_fallback_to_direct_turn(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = Store(tmp_path / "state.db")
    scheduler = Scheduler(store)
    scheduler.add_volition_interval(
        config=_volition_schedule_config("dependency-loss"),
        every_seconds=60.0,
        first_at=10.0,
        now=1.0,
    )

    def unavailable():
        raise RuntimeError("Volition unavailable")

    monkeypatch.setattr("pre_active.volition_bridge._load_volition", unavailable)

    assert scheduler.tick(now=10.0) == 1
    [signal] = store.list_events(kind="volition.signal")
    assert signal["status"] == "PENDING"
    assert store.list_events(kind="autonomous.turn") == []
    store.close()


def test_volition_interval_dependency_required_at_configuration_time(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = Store(tmp_path / "state.db")
    scheduler = Scheduler(store)

    def unavailable():
        raise RuntimeError("Volition unavailable")

    monkeypatch.setattr("pre_active.volition_bridge._load_volition", unavailable)

    with __import__("pytest").raises(RuntimeError, match="Volition unavailable"):
        scheduler.add_volition_interval(
            config=_volition_schedule_config("dependency-config"),
            every_seconds=60.0,
            first_at=10.0,
            now=1.0,
        )

    count = store.connection.execute(
        "SELECT COUNT(*) AS n FROM schedules"
    ).fetchone()
    assert count is not None
    assert int(count["n"]) == 0
    store.close()


@pytest.mark.parametrize("every_seconds", [float("nan"), float("inf")])
def test_volition_interval_rejects_non_finite_interval_before_insert(
    tmp_path: Path,
    every_seconds: float,
) -> None:
    store = Store(tmp_path / "state.db")
    scheduler = Scheduler(store)

    with pytest.raises(ValueError, match="every_seconds must be finite"):
        scheduler.add_volition_interval(
            config=_volition_schedule_config("finite-interval"),
            every_seconds=every_seconds,
            first_at=100.0,
            now=1.0,
        )

    row = store.connection.execute("SELECT COUNT(*) AS n FROM schedules").fetchone()
    assert row is not None
    assert int(row["n"]) == 0
    store.close()


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("first_at", float("nan"), "first_at must be finite"),
        ("first_at", float("inf"), "first_at must be finite"),
        ("first_at", float("-inf"), "first_at must be finite"),
        ("now", float("nan"), "now must be finite"),
        ("now", float("inf"), "now must be finite"),
        ("now", float("-inf"), "now must be finite"),
    ],
)
def test_volition_interval_rejects_non_finite_timestamps_before_insert(
    tmp_path: Path,
    field: str,
    value: float,
    message: str,
) -> None:
    store = Store(tmp_path / "state.db")
    scheduler = Scheduler(store)
    kwargs = {
        "config": _volition_schedule_config("finite-timestamps"),
        "every_seconds": 60.0,
        "first_at": 100.0,
        "now": 1.0,
    }
    kwargs[field] = value

    with pytest.raises(ValueError, match=message):
        scheduler.add_volition_interval(**kwargs)

    row = store.connection.execute("SELECT COUNT(*) AS n FROM schedules").fetchone()
    assert row is not None
    assert int(row["n"]) == 0
    store.close()


@pytest.mark.parametrize("every_seconds", [1e-9, 4e-7])
def test_volition_interval_rejects_sub_microsecond_occurrence_identity(
    tmp_path: Path,
    every_seconds: float,
) -> None:
    store = Store(tmp_path / "state.db")
    scheduler = Scheduler(store)

    with pytest.raises(
        ValueError,
        match="every_seconds must be at least 0.000001",
    ):
        scheduler.add_volition_interval(
            config=_volition_schedule_config("dedup-resolution"),
            every_seconds=every_seconds,
            first_at=1_800_000_000.0,
            now=1_800_000_000.0,
        )

    row = store.connection.execute("SELECT COUNT(*) AS n FROM schedules").fetchone()
    assert row is not None
    assert int(row["n"]) == 0
    store.close()


def test_volition_fractional_interval_coalesces_to_latest_due_occurrence(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "state.db")
    scheduler = Scheduler(store)
    schedule_id = scheduler.add_volition_interval(
        config=_volition_schedule_config("fractional-catchup"),
        every_seconds=0.1,
        first_at=100.0,
        now=1.0,
    )

    assert scheduler.tick(now=101.0, max_occurrences=1) == 1

    [event] = store.list_events(kind="volition.signal")
    row = store.connection.execute(
        "SELECT available_at, dedup_key FROM events WHERE id=?",
        (event["id"],),
    ).fetchone()
    assert row is not None
    assert float(row["available_at"]) == 101.0
    assert row["dedup_key"] == f"schedule:{schedule_id}:101.000000"

    schedule = store.connection.execute(
        "SELECT next_at FROM schedules WHERE id=?",
        (schedule_id,),
    ).fetchone()
    assert schedule is not None
    assert float(schedule["next_at"]) == pytest.approx(101.1)
    assert scheduler.tick(now=101.0, max_occurrences=1) == 0
    store.close()


def test_volition_schedule_budget_leaves_other_due_schedule_untouched(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "state.db")
    scheduler = Scheduler(store)
    first_id = scheduler.add_volition_interval(
        config=_volition_schedule_config("budget-first"),
        every_seconds=10.0,
        first_at=10.0,
        now=1.0,
    )
    second_id = scheduler.add_volition_interval(
        config=_volition_schedule_config("budget-second"),
        every_seconds=10.0,
        first_at=10.0,
        now=1.0,
    )

    assert scheduler.tick(now=100.0, max_occurrences=1) == 1

    rows = store.connection.execute(
        "SELECT id, next_at FROM schedules WHERE id IN (?, ?) ORDER BY id",
        (first_id, second_id),
    ).fetchall()
    next_by_id = {str(row["id"]): float(row["next_at"]) for row in rows}
    advanced = [schedule_id for schedule_id, next_at in next_by_id.items() if next_at == 110.0]
    still_due = [schedule_id for schedule_id, next_at in next_by_id.items() if next_at == 10.0]
    assert len(advanced) == 1
    assert len(still_due) == 1

    [event] = store.list_events(kind="volition.signal")
    emitted_source = event["payload"]["source"]
    assert emitted_source in {f"schedule:{first_id}", f"schedule:{second_id}"}
    assert f"schedule:{still_due[0]}" != emitted_source
    store.close()
