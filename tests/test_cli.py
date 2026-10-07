import json
from pathlib import Path

import pytest

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



def test_autonomous_turn_cli_enqueues_promptless_model_turn(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    state = tmp_path / "state.db"
    monkeypatch.setattr("pre_active.cli.time.time", lambda: 100.0)

    assert main([
        "--state", str(state),
        "autonomous-turn", "Inspect the changed repository.",
        "--source", "OPEN_LOOP",
        "--reason", "A durable unresolved goal became actionable.",
        "--after", "30",
        "--capability", "repo.read",
    ]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == "autonomous.turn"
    assert payload["source"] == "OPEN_LOOP"
    assert payload["available_at"] == 130.0

    store = Store(state)
    [event] = store.list_events(kind="autonomous.turn")
    assert event["payload"]["task"] == "Inspect the changed repository."
    assert event["payload"]["source"] == "OPEN_LOOP"
    assert event["payload"]["capabilities"] == ["repo.read"]
    row = store.connection.execute(
        "SELECT available_at FROM events WHERE id=?", (event["id"],)
    ).fetchone()
    assert row is not None and float(row["available_at"]) == 130.0
    store.close()


def test_schedule_can_emit_temporal_autonomous_turns(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    state = tmp_path / "state.db"
    monkeypatch.setattr("pre_active.cli.time.time", lambda: 200.0)

    assert main([
        "--state", str(state),
        "schedule", "Reconsider unresolved architecture questions.",
        "--every", "60",
        "--autonomous",
        "--reason", "Periodic reconsideration was requested.",
    ]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["first_at"] == 260.0

    store = Store(state)
    row = store.connection.execute(
        "SELECT kind, payload_json, next_at FROM schedules WHERE id=?",
        (payload["schedule_id"],),
    ).fetchone()
    assert row is not None
    assert row["kind"] == "autonomous.turn"
    scheduled = json.loads(row["payload_json"])
    assert scheduled["source"] == "TEMPORAL"
    assert scheduled["reason"] == "Periodic reconsideration was requested."
    store.close()


def test_runtime_cli_exposes_autonomous_turn_budget() -> None:
    args = build_parser().parse_args([
        "run-once",
        "--base-url", "http://127.0.0.1:1/v1",
        "--model", "test-model",
        "--max-autonomous-turns-per-run", "7",
    ])
    assert args.max_autonomous_turns_per_run == 7


def _schedule_volition_config(target: str = "review-open-loops") -> dict[str, object]:
    return {
        "target": target,
        "kind": "open_loop",
        "magnitude": 0.5,
        "confidence": 1.0,
        "provenance": "current_observation",
    }


def test_schedule_parser_exposes_volition_mode() -> None:
    args = build_parser().parse_args([
        "schedule",
        "Compatibility-only task.",
        "--every",
        "60",
    ])

    assert args.volition is False
    assert args.volition_config_json is None


def test_schedule_cli_creates_volition_signal_schedule(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    state = tmp_path / "schedule-volition.db"
    monkeypatch.setattr("pre_active.cli.time.time", lambda: 200.0)
    config = _schedule_volition_config("scheduled-review")

    assert main([
        "--state", str(state),
        "schedule", "Compatibility-only task.",
        "--every", "60",
        "--volition",
        "--volition-config-json", json.dumps(config),
    ]) == 0

    created = json.loads(capsys.readouterr().out)
    assert created["first_at"] == 260.0
    store = Store(state)
    row = store.connection.execute(
        "SELECT kind, payload_json, every_seconds, next_at FROM schedules WHERE id=?",
        (created["schedule_id"],),
    ).fetchone()
    assert row is not None
    assert row["kind"] == "volition.signal"
    assert json.loads(row["payload_json"]) == {
        **config,
        "source": f"schedule:{created['schedule_id']}",
        "effect_authority": False,
    }
    assert float(row["every_seconds"]) == 60.0
    assert float(row["next_at"]) == 260.0
    store.close()


@pytest.mark.parametrize(
    ("extra_args", "message"),
    [
        (["--autonomous"], "--volition cannot be combined with --autonomous"),
        (["--capability", "files.read"], "--volition schedules cannot have capabilities"),
        (["--reason", "direct temporal reason"], "--reason is invalid with --volition"),
    ],
)
def test_schedule_cli_rejects_conflicting_volition_modes(
    tmp_path: Path,
    monkeypatch,
    extra_args: list[str],
    message: str,
) -> None:
    state = tmp_path / "schedule-volition-invalid.db"
    monkeypatch.setattr("pre_active.cli.time.time", lambda: 200.0)

    with pytest.raises(SystemExit, match=message):
        main([
            "--state", str(state),
            "schedule", "Compatibility-only task.",
            "--every", "60",
            "--volition",
            "--volition-config-json", json.dumps(_schedule_volition_config()),
            *extra_args,
        ])

    store = Store(state)
    count = store.connection.execute("SELECT COUNT(*) AS n FROM schedules").fetchone()
    assert count is not None
    assert int(count["n"]) == 0
    store.close()


@pytest.mark.parametrize(
    ("config_json", "message"),
    [
        ("{", "--volition-config-json must be valid JSON"),
        ("[]", "--volition-config-json must decode to a JSON object"),
    ],
)
def test_schedule_cli_rejects_malformed_volition_config_json(
    tmp_path: Path,
    monkeypatch,
    config_json: str,
    message: str,
) -> None:
    state = tmp_path / "schedule-volition-json.db"
    monkeypatch.setattr("pre_active.cli.time.time", lambda: 200.0)

    with pytest.raises(SystemExit, match=message):
        main([
            "--state", str(state),
            "schedule", "Compatibility-only task.",
            "--every", "60",
            "--volition",
            "--volition-config-json", config_json,
        ])


@pytest.mark.parametrize(
    "config_json",
    ["{}", json.dumps(_schedule_volition_config())],
)
def test_schedule_cli_rejects_volition_config_without_volition_mode(
    tmp_path: Path,
    monkeypatch,
    config_json: str,
) -> None:
    state = tmp_path / "schedule-volition-mode-mismatch.db"
    monkeypatch.setattr("pre_active.cli.time.time", lambda: 200.0)

    with pytest.raises(
        SystemExit,
        match="--volition-config-json requires --volition",
    ):
        main([
            "--state", str(state),
            "schedule", "This must not silently become ordinary scheduled work.",
            "--every", "60",
            "--volition-config-json", config_json,
        ])

    store = Store(state)
    count = store.connection.execute("SELECT COUNT(*) AS n FROM schedules").fetchone()
    assert count is not None
    assert int(count["n"]) == 0
    store.close()


def test_schedule_cli_requires_config_flag_in_volition_mode(
    tmp_path: Path,
    monkeypatch,
) -> None:
    state = tmp_path / "schedule-volition-config-required.db"
    monkeypatch.setattr("pre_active.cli.time.time", lambda: 200.0)

    with pytest.raises(
        SystemExit,
        match="--volition schedules require --volition-config-json",
    ):
        main([
            "--state", str(state),
            "schedule", "Compatibility-only task.",
            "--every", "60",
            "--volition",
        ])

    store = Store(state)
    count = store.connection.execute("SELECT COUNT(*) AS n FROM schedules").fetchone()
    assert count is not None
    assert int(count["n"]) == 0
    store.close()
