from pathlib import Path

from pre_active.scheduler import Scheduler
from pre_active.store import Store


def test_operational_snapshot_distinguishes_backlog_claims_failures_and_due_work(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "state.db")

    ready = store.enqueue_event(
        kind="probe",
        payload={"name": "ready"},
        dedup_key="ready",
        now=1.0,
        available_at=1.0,
    )
    store.connection.execute(
        "UPDATE events SET attempts=2 WHERE id=?",
        (ready,),
    )
    store.enqueue_event(
        kind="probe",
        payload={"name": "delayed"},
        dedup_key="delayed",
        now=2.0,
        available_at=20.0,
    )
    expired = store.enqueue_event(
        kind="probe",
        payload={"name": "expired"},
        dedup_key="expired",
        now=3.0,
        available_at=3.0,
    )
    active = store.enqueue_event(
        kind="probe",
        payload={"name": "active"},
        dedup_key="active",
        now=4.0,
        available_at=4.0,
    )
    dead = store.enqueue_event(
        kind="probe",
        payload={"name": "dead"},
        dedup_key="dead",
        now=5.0,
        available_at=5.0,
    )
    store.connection.execute(
        """
        UPDATE events
        SET status='CLAIMED', lease_owner='old-worker', lease_token='expired-token',
            lease_until=8.0, attempts=1
        WHERE id=?
        """,
        (expired,),
    )
    store.connection.execute(
        """
        UPDATE events
        SET status='CLAIMED', lease_owner='live-worker', lease_token='active-token',
            lease_until=15.0, attempts=1
        WHERE id=?
        """,
        (active,),
    )
    store.connection.execute(
        """
        UPDATE events
        SET status='DEAD', dead_lettered_at=6.0, attempts=3,
            last_error='permanent failure'
        WHERE id=?
        """,
        (dead,),
    )

    running = store.create_run(task="running", capabilities=set(), now=1.0)
    blocked = store.create_run(task="blocked", capabilities=set(), now=2.0)
    paused = store.create_run(task="paused", capabilities=set(), now=3.0)
    store.connection.execute(
        "UPDATE runs SET status='BLOCKED_EFFECT' WHERE id=?",
        (blocked,),
    )
    store.connection.execute(
        "UPDATE runs SET status='PAUSED' WHERE id=?",
        (paused,),
    )

    scheduler = Scheduler(store)
    scheduler.add_interval(
        kind="probe",
        payload={"schedule": "due"},
        every_seconds=30.0,
        first_at=9.0,
        now=1.0,
    )
    scheduler.add_interval(
        kind="probe",
        payload={"schedule": "future"},
        every_seconds=30.0,
        first_at=12.0,
        now=1.0,
    )

    snapshot = store.operational_snapshot(now=10.0)

    assert snapshot["observed_at"] == 10.0
    assert snapshot["pending_events"] == 4
    assert snapshot["dead_events"] == 1

    events = snapshot["events"]
    assert events["by_status"]["PENDING"] == 2
    assert events["by_status"]["CLAIMED"] == 2
    assert events["by_status"]["DEAD"] == 1
    assert events["ready_pending"] == 1
    assert events["delayed_pending"] == 1
    assert events["retry_pending"] == 1
    assert events["active_claims"] == 1
    assert events["expired_claims"] == 1
    assert events["oldest_ready_age_seconds"] == 9.0
    assert events["oldest_expired_lease_age_seconds"] == 2.0
    assert events["oldest_dead_age_seconds"] == 4.0

    assert snapshot["runs"]["RUNNING"] == 1
    assert snapshot["runs"]["BLOCKED_EFFECT"] == 1
    assert snapshot["runs"]["PAUSED"] == 1
    assert snapshot["runs"]["COMPLETED"] == 0

    assert snapshot["schedules"] == {
        "enabled": 2,
        "due": 1,
    }

    assert store.get_run(running)["status"] == "RUNNING"


def test_operational_snapshot_uses_none_for_empty_age_populations(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")

    snapshot = store.operational_snapshot(now=100.0)

    assert snapshot["events"]["oldest_ready_age_seconds"] is None
    assert snapshot["events"]["oldest_expired_lease_age_seconds"] is None
    assert snapshot["events"]["oldest_dead_age_seconds"] is None
