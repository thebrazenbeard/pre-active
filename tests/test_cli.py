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
