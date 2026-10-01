import json
from pathlib import Path

from pre_active.cli import build_parser, main
from pre_active.store import Store


def test_cli_submit_creates_durable_run_and_event(tmp_path: Path, capsys) -> None:
    state = tmp_path / "state.db"
    code = main([
        "--state", str(state),
        "submit", "Do the thing",
        "--capability", "files.read",
    ])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    store = Store(state)
    run = store.get_run(payload["run_id"])
    assert run["task"] == "Do the thing"
    assert run["capabilities"] == {"files.read"}
    assert store.pending_event_count() == 1


def test_runtime_cli_exposes_bounded_lease_heartbeat_controls() -> None:
    args = build_parser().parse_args([
        "run-once",
        "--base-url", "http://127.0.0.1:1/v1",
        "--model", "test-model",
        "--lease-seconds", "30",
        "--lease-heartbeat-seconds", "7",
        "--max-lease-extension-seconds", "600",
    ])

    assert args.lease_seconds == 30.0
    assert args.lease_heartbeat_seconds == 7.0
    assert args.max_lease_extension_seconds == 600.0


def test_runtime_cli_exposes_event_attempt_ceiling() -> None:
    args = build_parser().parse_args([
        "run-once",
        "--base-url", "http://127.0.0.1:1/v1",
        "--model", "test-model",
        "--max-event-attempts", "20",
    ])

    assert args.max_event_attempts == 20


def test_status_reports_dead_lettered_event_count(tmp_path: Path, capsys) -> None:
    state = tmp_path / "state.db"
    store = Store(state)
    event_id = store.enqueue_event(
        kind="probe",
        payload={"value": 1},
        dedup_key="status-dead-probe",
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
        error="permanent failure",
    ) is True
    store.close()

    assert main(["--state", str(state), "status"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["pending_events"] == 0
    assert payload["dead_events"] == 1


def test_cli_exposes_dead_letter_inspection_and_single_event_redrive() -> None:
    import argparse

    parser = build_parser()
    subparsers = next(
        action
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    assert "dead" in subparsers.choices
    assert "redrive" in subparsers.choices


def test_dead_and_redrive_cli_round_trip(tmp_path: Path, capsys) -> None:
    state = tmp_path / "state.db"
    store = Store(state)
    event_id = store.enqueue_event(
        kind="probe",
        payload={"value": 42},
        dedup_key="cli-redrive",
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
        error="needs operator retry",
    ) is True
    store.close()

    assert main(["--state", str(state), "dead"]) == 0
    dead_payload = json.loads(capsys.readouterr().out)
    assert [item["id"] for item in dead_payload["events"]] == [event_id]
    assert dead_payload["events"][0]["last_error"] == "needs operator retry"

    assert main(["--state", str(state), "redrive", event_id]) == 0
    redrive_payload = json.loads(capsys.readouterr().out)
    assert redrive_payload == {"event_id": event_id, "status": "PENDING"}

    reopened = Store(state)
    [row] = reopened.list_events(kind="probe")
    assert row["status"] == "PENDING"
    assert row["attempts"] == 0
    reopened.close()


def test_cli_exposes_run_control_commands() -> None:
    import argparse

    parser = build_parser()
    subparsers = next(
        action
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    assert {"pause", "resume", "cancel"} <= set(subparsers.choices)


def test_pause_and_cancel_cli_persist_operator_control(tmp_path: Path, capsys) -> None:
    state = tmp_path / "state.db"
    store = Store(state)
    run_id = store.create_run_with_initial_step(
        task="operator controlled",
        capabilities=set(),
        now=1.0,
    )
    store.close()

    assert main([
        "--state", str(state),
        "pause", run_id,
        "--reason", "inspect state",
    ]) == 0
    pause_payload = json.loads(capsys.readouterr().out)
    assert pause_payload["run_id"] == run_id
    assert pause_payload["control_action"] == "PAUSE"

    check = Store(state)
    assert check.get_run(run_id)["control_action"] == "PAUSE"
    check.close()

    assert main([
        "--state", str(state),
        "cancel", run_id,
        "--reason", "stop work",
    ]) == 0
    cancel_payload = json.loads(capsys.readouterr().out)
    assert cancel_payload["run_id"] == run_id
    assert cancel_payload["control_action"] == "CANCEL"

    check = Store(state)
    assert check.get_run(run_id)["control_action"] == "CANCEL"
    check.close()


def test_resume_cli_requeues_paused_run(tmp_path: Path, capsys) -> None:
    state = tmp_path / "state.db"
    store = Store(state)
    run_id = store.create_run_with_initial_step(
        task="resume me",
        capabilities=set(),
        now=1.0,
    )
    [event] = store.list_events(kind="run.step")
    claim = store.claim_event(worker_id="w1", now=2.0, lease_seconds=10.0)
    assert claim is not None and claim.id == event["id"] and claim.lease_token
    store.request_run_control(run_id, action="PAUSE", reason="hold", now=2.5)
    with store.active_claim_transaction(
        claim.id,
        worker_id="w1",
        lease_token=claim.lease_token,
        now=3.0,
    ):
        store.apply_claimed_run_control(
            run_id=run_id,
            event_id=claim.id,
            worker_id="w1",
            lease_token=claim.lease_token,
            now=3.0,
        )
    assert store.get_run(run_id)["status"] == "PAUSED"
    store.close()

    assert main([
        "--state", str(state),
        "resume", run_id,
        "--reason", "continue",
    ]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"run_id": run_id, "status": "RUNNING"}

    reopened = Store(state)
    assert reopened.get_run(run_id)["status"] == "RUNNING"
    [resumed] = reopened.list_events(kind="run.step")
    assert resumed["id"] == event["id"]
    assert resumed["status"] == "PENDING"
    reopened.close()


def test_status_cli_emits_operational_snapshot_with_compatibility_fields(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    state = tmp_path / "state.db"
    store = Store(state)
    store.enqueue_event(
        kind="probe",
        payload={"value": "ready"},
        dedup_key="status-ready",
        now=1.0,
        available_at=1.0,
    )
    store.enqueue_event(
        kind="probe",
        payload={"value": "later"},
        dedup_key="status-later",
        now=2.0,
        available_at=20.0,
    )
    run_id = store.create_run(task="blocked", capabilities=set(), now=3.0)
    store.connection.execute(
        "UPDATE runs SET status='BLOCKED_EFFECT' WHERE id=?",
        (run_id,),
    )
    store.close()

    monkeypatch.setattr("pre_active.cli.time.time", lambda: 10.0)
    assert main(["--state", str(state), "status"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["observed_at"] == 10.0
    assert payload["pending_events"] == 2
    assert payload["dead_events"] == 0
    assert payload["runs"]["BLOCKED_EFFECT"] == 1
    assert payload["events"]["ready_pending"] == 1
    assert payload["events"]["delayed_pending"] == 1
    assert payload["schedules"] == {"due": 0, "enabled": 0}
