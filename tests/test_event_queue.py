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
    assert event.lease_token
    store.ack_event(event.id, worker_id="w1", lease_token=event.lease_token, now=3.0)

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


def test_reclaimed_event_rotates_fencing_token_and_rejects_stale_claim(tmp_path: Path) -> None:
    import pytest

    store = Store(tmp_path / "state.db")
    event_id = store.enqueue_event(
        kind="probe",
        payload={"value": 1},
        dedup_key="fenced-probe",
        now=1.0,
    )

    first = store.claim_event(worker_id="worker-a", now=2.0, lease_seconds=5.0)
    assert first is not None
    assert first.lease_token

    renewed_until = store.renew_event_lease(
        first.id,
        worker_id="worker-a",
        lease_token=first.lease_token,
        now=4.0,
        lease_seconds=5.0,
    )
    assert renewed_until == 9.0
    assert store.claim_event(worker_id="worker-b", now=8.0, lease_seconds=5.0) is None

    recovered = store.claim_event(worker_id="worker-b", now=10.0, lease_seconds=5.0)
    assert recovered is not None
    assert recovered.id == event_id
    assert recovered.lease_token
    assert recovered.lease_token != first.lease_token

    with pytest.raises(RuntimeError, match="lost lease ownership"):
        store.renew_event_lease(
            first.id,
            worker_id="worker-a",
            lease_token=first.lease_token,
            now=10.5,
            lease_seconds=5.0,
        )
    with pytest.raises(RuntimeError, match="lost lease ownership"):
        store.ack_event(
            first.id,
            worker_id="worker-a",
            lease_token=first.lease_token,
            now=10.5,
        )
    with pytest.raises(RuntimeError, match="lost lease ownership"):
        store.fail_event(
            first.id,
            worker_id="worker-a",
            lease_token=first.lease_token,
            now=10.5,
        )

    store.ack_event(
        recovered.id,
        worker_id="worker-b",
        lease_token=recovered.lease_token,
        now=11.0,
    )


def test_event_failure_dead_letters_at_attempt_ceiling(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    event_id = store.enqueue_event(
        kind="probe",
        payload={"value": 7},
        dedup_key="dead-letter-probe",
        now=1.0,
    )

    first = store.claim_event(worker_id="worker-a", now=2.0, lease_seconds=10.0)
    assert first is not None and first.lease_token
    assert store.fail_event(
        first.id,
        worker_id="worker-a",
        lease_token=first.lease_token,
        now=3.0,
        retry_at=4.0,
        max_attempts=2,
        error="first failure",
    ) is False

    retried = store.claim_event(worker_id="worker-b", now=5.0, lease_seconds=10.0)
    assert retried is not None and retried.lease_token
    assert retried.attempts == 2
    assert store.fail_event(
        retried.id,
        worker_id="worker-b",
        lease_token=retried.lease_token,
        now=6.0,
        retry_at=7.0,
        max_attempts=2,
        error="second failure",
    ) is True

    [row] = store.list_events(kind="probe")
    assert row["id"] == event_id
    assert row["status"] == "DEAD"
    assert row["attempts"] == 2
    assert row["last_error"] == "second failure"
    assert row["dead_lettered_at"] == 6.0
    assert store.pending_event_count() == 0

    journal_types = [
        entry["event_type"] for entry in store.list_journal(subject_id=event_id)
    ]
    assert "EVENT_RETRY_SCHEDULED" in journal_types
    assert journal_types[-1] == "EVENT_DEAD_LETTERED"


def test_expired_claim_cannot_ack_or_retry_without_reclaim(tmp_path: Path) -> None:
    import pytest

    store = Store(tmp_path / "state.db")

    ack_id = store.enqueue_event(kind="ack-expiry", payload={}, now=1.0)
    ack_claim = store.claim_event(worker_id="w1", now=2.0, lease_seconds=1.0)
    assert ack_claim is not None and ack_claim.id == ack_id and ack_claim.lease_token
    with pytest.raises(RuntimeError, match="lost lease ownership"):
        store.ack_event(
            ack_id,
            worker_id="w1",
            lease_token=ack_claim.lease_token,
            now=3.1,
        )

    store.close()

    retry_store = Store(tmp_path / "retry.db")
    retry_id = retry_store.enqueue_event(kind="retry-expiry", payload={}, now=4.0)
    retry_claim = retry_store.claim_event(
        worker_id="w2", now=5.0, lease_seconds=1.0
    )
    assert retry_claim is not None and retry_claim.id == retry_id and retry_claim.lease_token
    with pytest.raises(RuntimeError, match="lost lease ownership"):
        retry_store.fail_event(
            retry_id,
            worker_id="w2",
            lease_token=retry_claim.lease_token,
            now=6.1,
            error="late failure",
        )


def test_dead_letter_and_run_failure_commit_together(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    run_id = store.create_run(task="fail atomically", capabilities=set(), now=1.0)
    event_id = store.enqueue_event(
        kind="run.step",
        payload={"run_id": run_id, "step": 0},
        now=1.0,
    )
    event = store.claim_event(worker_id="w1", now=2.0, lease_seconds=10.0)
    assert event is not None and event.id == event_id and event.lease_token

    assert store.fail_event(
        event_id,
        worker_id="w1",
        lease_token=event.lease_token,
        now=3.0,
        max_attempts=1,
        error="permanent provider failure",
        failed_run_id=run_id,
    ) is True

    assert store.list_events(kind="run.step")[0]["status"] == "DEAD"
    run = store.get_run(run_id)
    assert run["status"] == "FAILED"
    assert run["last_error"] == "permanent provider failure"


def test_dead_event_can_be_explicitly_redriven_without_database_surgery(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "state.db")
    event_id = store.enqueue_event(
        kind="probe",
        payload={"value": 9},
        dedup_key="redrive-probe",
        now=1.0,
    )
    event = store.claim_event(worker_id="w1", now=2.0, lease_seconds=10.0)
    assert event is not None and event.lease_token
    assert store.fail_event(
        event_id,
        worker_id="w1",
        lease_token=event.lease_token,
        now=3.0,
        max_attempts=1,
        error="operator-fixable failure",
    ) is True

    assert hasattr(store, "redrive_event"), "Store.redrive_event is required"
    store.redrive_event(event_id, now=10.0)

    [row] = store.list_events(kind="probe")
    assert row["status"] == "PENDING"
    assert row["attempts"] == 0
    assert row["last_error"] is None
    assert row["dead_lettered_at"] is None
    assert store.pending_event_count() == 1
    assert store.dead_event_count() == 0
    assert store.list_journal(subject_id=event_id)[-1]["event_type"] == "EVENT_REDRIVEN"


def test_redriving_dead_run_step_resumes_only_its_exact_failed_run(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "state.db")
    run_id = store.create_run(task="recover me", capabilities=set(), now=1.0)
    event_id = store.enqueue_event(
        kind="run.step",
        payload={"run_id": run_id, "step": 0},
        dedup_key=f"run-step:{run_id}:0",
        now=1.0,
    )
    event = store.claim_event(worker_id="w1", now=2.0, lease_seconds=10.0)
    assert event is not None and event.lease_token
    assert store.fail_event(
        event_id,
        worker_id="w1",
        lease_token=event.lease_token,
        now=3.0,
        max_attempts=1,
        error="provider outage exhausted",
        failed_run_id=run_id,
    ) is True

    assert hasattr(store, "redrive_event"), "Store.redrive_event is required"
    store.redrive_event(event_id, now=20.0)

    run = store.get_run(run_id)
    assert run["status"] == "RUNNING"
    assert run["last_error"] is None
    [row] = store.list_events(kind="run.step")
    assert row["status"] == "PENDING"
    assert row["attempts"] == 0
