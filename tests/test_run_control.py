from pathlib import Path

import pytest

from pre_active.store import Store


def test_run_control_request_is_durable_and_cancel_supersedes_pause(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    run_id = store.create_run_with_initial_step(
        task="controlled work",
        capabilities=set(),
        now=1.0,
    )

    store.request_run_control(
        run_id,
        action="PAUSE",
        reason="operator review",
        now=2.0,
    )
    paused_request = store.get_run(run_id)
    assert paused_request["status"] == "RUNNING"
    assert paused_request["control_action"] == "PAUSE"
    assert paused_request["control_reason"] == "operator review"
    assert paused_request["control_requested_at"] == 2.0

    store.request_run_control(
        run_id,
        action="CANCEL",
        reason="no longer needed",
        now=3.0,
    )
    cancelled_request = store.get_run(run_id)
    assert cancelled_request["control_action"] == "CANCEL"
    assert cancelled_request["control_reason"] == "no longer needed"
    assert cancelled_request["control_requested_at"] == 3.0

    journal = store.list_journal(subject_id=run_id)
    requested = [entry for entry in journal if entry["event_type"] == "RUN_CONTROL_REQUESTED"]
    assert [entry["payload"]["action"] for entry in requested] == ["PAUSE", "CANCEL"]


def test_terminal_run_rejects_new_control_request(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    run_id = store.create_run(task="already done", capabilities=set(), now=1.0)
    store.update_run(run_id, now=2.0, status="COMPLETED", final_text="done")

    with pytest.raises(RuntimeError, match="terminal"):
        store.request_run_control(
            run_id,
            action="PAUSE",
            reason="too late",
            now=3.0,
        )


def test_cancel_control_cannot_be_downgraded_to_pause(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    run_id = store.create_run(task="cancel me", capabilities=set(), now=1.0)
    store.request_run_control(run_id, action="CANCEL", reason="stop", now=2.0)

    with pytest.raises(RuntimeError, match="cannot replace CANCEL"):
        store.request_run_control(run_id, action="PAUSE", reason="actually pause", now=3.0)
