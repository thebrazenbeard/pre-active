from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from pre_active.cli import main
from pre_active.context import ContextAssembler
from pre_active.contracts import RUN_CONTRACT_VERSION
from pre_active.engine import Engine, ModelResponse
from pre_active.store import Store
from pre_active.tools import ToolRegistry


class NeverCalledModel:
    def __init__(self) -> None:
        self.calls = 0

    def respond(self, *, messages, tools):  # type: ignore[no-untyped-def]
        self.calls += 1
        raise AssertionError("incompatible run must be blocked before inference")


def test_new_run_is_stamped_with_current_contract(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    run_id = store.create_run(
        task="versioned work",
        capabilities=set(),
        now=1.0,
    )

    run = store.get_run(run_id)
    assert run["contract_version"] == RUN_CONTRACT_VERSION
    assert run["contract_event_id"] is None
    assert run["contract_resume_status"] is None


def test_incompatible_running_run_blocks_before_model_call(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    model = NeverCalledModel()
    engine = Engine(
        store=store,
        model=model,
        tools=ToolRegistry(store),
        context=ContextAssembler(store),
        system_prompt="Run.",
        worker_id="worker-a",
        runtime_contract_version=2,
    )
    run_id = store.create_run_with_initial_step(
        task="old contract",
        capabilities=set(),
        now=1.0,
        contract_version=1,
    )
    [event] = store.list_events(kind="run.step")

    assert engine.run_once(now=2.0) == run_id

    run = store.get_run(run_id)
    assert run["status"] == "BLOCKED_CONTRACT"
    assert run["contract_version"] == 1
    assert run["contract_event_id"] == event["id"]
    assert run["contract_resume_status"] == "RUNNING"
    assert "runtime contract version 2" in run["last_error"]
    assert model.calls == 0

    [blocked_event] = store.list_events(kind="run.step")
    assert blocked_event["id"] == event["id"]
    assert blocked_event["status"] == "PAUSED"
    journal = store.list_journal(subject_id=run_id)
    assert any(item["event_type"] == "RUN_CONTRACT_BLOCKED" for item in journal)


def test_incompatible_waiting_run_preserves_waiting_resume_state(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "state.db")
    model = NeverCalledModel()
    engine = Engine(
        store=store,
        model=model,
        tools=ToolRegistry(store),
        context=ContextAssembler(store),
        system_prompt="Run.",
        worker_id="worker-a",
        runtime_contract_version=2,
    )
    run_id = store.create_run(
        task="sleeping old contract",
        capabilities=set(),
        now=1.0,
        contract_version=1,
    )
    store.defer_run_for_autonomous_turn(
        run_id=run_id,
        expected_step=0,
        reason="wake later",
        delay_seconds=0,
        priority=0,
        now=2.0,
    )
    assert store.get_run(run_id)["status"] == "WAITING"

    assert engine.run_once(now=3.0) == run_id

    run = store.get_run(run_id)
    assert run["status"] == "BLOCKED_CONTRACT"
    assert run["contract_resume_status"] == "WAITING"
    assert model.calls == 0
    [event] = [
        item
        for item in store.list_events(kind="run.step")
        if item["payload"].get("step") == 1
    ]
    assert event["status"] == "PAUSED"


def test_contract_block_can_be_cancelled_without_migration(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    engine = Engine(
        store=store,
        model=NeverCalledModel(),
        tools=ToolRegistry(store),
        context=ContextAssembler(store),
        system_prompt="Run.",
        worker_id="worker-a",
        runtime_contract_version=2,
    )
    run_id = store.create_run_with_initial_step(
        task="cancel incompatible work",
        capabilities=set(),
        now=1.0,
        contract_version=1,
    )
    engine.run_once(now=2.0)
    blocked = store.get_run(run_id)
    event_id = blocked["contract_event_id"]
    assert isinstance(event_id, str)

    with pytest.raises(RuntimeError, match="already blocked"):
        store.request_run_control(
            run_id,
            action="PAUSE",
            reason="redundant",
            now=3.0,
        )

    store.request_run_control(
        run_id,
        action="CANCEL",
        reason="do not migrate this run",
        now=4.0,
    )

    cancelled = store.get_run(run_id)
    assert cancelled["status"] == "CANCELLED"
    assert cancelled["contract_event_id"] is None
    assert cancelled["contract_resume_status"] is None
    [event] = store.list_events(kind="run.step")
    assert event["id"] == event_id
    assert event["status"] == "CANCELLED"


def test_compatible_run_continues_normally(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")

    class FinalModel:
        def __init__(self) -> None:
            self.calls = 0

        def respond(self, *, messages, tools):  # type: ignore[no-untyped-def]
            self.calls += 1
            return ModelResponse(final_text="compatible")

    model = FinalModel()
    engine = Engine(
        store=store,
        model=model,
        tools=ToolRegistry(store),
        context=ContextAssembler(store),
        system_prompt="Run.",
        worker_id="worker-a",
        runtime_contract_version=RUN_CONTRACT_VERSION,
    )
    run_id = engine.submit_task("current contract", set(), now=1.0)

    assert engine.run_once(now=2.0) == run_id
    run = store.get_run(run_id)
    assert run["status"] == "COMPLETED"
    assert run["final_text"] == "compatible"
    assert model.calls == 1


def test_contract_show_cli_reports_compatibility(
    tmp_path: Path, capsys
) -> None:
    state = tmp_path / "state.db"
    store = Store(state)
    run_id = store.create_run(
        task="inspect contract",
        capabilities=set(),
        now=1.0,
    )
    store.close()

    assert main([
        "--state",
        str(state),
        "contract",
        "show",
        run_id,
    ]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["run_contract_version"] == RUN_CONTRACT_VERSION
    assert payload["runtime_contract_version"] == RUN_CONTRACT_VERSION
    assert payload["compatible"] is True
    assert payload["blocked_event_id"] is None


def test_legacy_run_schema_migrates_to_contract_version_one(
    tmp_path: Path,
) -> None:
    state = tmp_path / "legacy.db"
    db = sqlite3.connect(state)
    db.execute(
        """
        CREATE TABLE runs (
            id TEXT PRIMARY KEY,
            task TEXT NOT NULL,
            status TEXT NOT NULL,
            capabilities_json TEXT NOT NULL,
            step_count INTEGER NOT NULL DEFAULT 0,
            final_text TEXT,
            last_error TEXT,
            blocked_request_id TEXT,
            source_event_id TEXT UNIQUE,
            failed_event_id TEXT,
            control_action TEXT,
            control_reason TEXT,
            control_requested_at REAL,
            paused_event_id TEXT,
            paused_at REAL,
            cancelled_at REAL,
            autonomous_turn_count INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
        """
    )
    db.execute(
        """
        INSERT INTO runs (
            id, task, status, capabilities_json, step_count,
            autonomous_turn_count, created_at, updated_at
        ) VALUES ('legacy-run', 'legacy', 'RUNNING', '[]', 0, 0, 1.0, 1.0)
        """
    )
    db.commit()
    db.close()

    store = Store(state)
    run = store.get_run("legacy-run")
    assert run["contract_version"] == 1
    assert run["contract_event_id"] is None
    assert run["contract_resume_status"] is None
