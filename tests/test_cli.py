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
