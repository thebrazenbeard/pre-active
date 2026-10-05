from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from pre_active.context import ContextAssembler
from pre_active.engine import Engine, RetryableModelError
from pre_active.lease import LeaseHeartbeat
from pre_active.store import EventLeaseLost, Store
from pre_active.tools import ToolRegistry, ToolSpec


def test_active_claim_transaction_revalidates_lease_at_commit(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    event_id = store.enqueue_event(
        kind="probe", payload={}, dedup_key="commit-fence", now=1.0
    )
    event = store.claim_event(worker_id="worker-a", now=1.0, lease_seconds=5.0)
    assert event is not None and event.id == event_id and event.lease_token
    run_id = store.create_run(task="fenced", capabilities=set(), now=1.0)
    clock = iter([2.0, 7.0])

    with pytest.raises(EventLeaseLost):
        with store.active_claim_transaction(
            event.id,
            worker_id="worker-a",
            lease_token=event.lease_token,
            now=lambda: next(clock),
        ):
            store.update_run(run_id, now=2.0, status="COMPLETED", final_text="stale")

    run = store.get_run(run_id)
    assert run["status"] == "RUNNING"
    assert run["final_text"] is None


def test_heartbeat_never_extends_persisted_lease_past_maximum_deadline(
    tmp_path: Path, monkeypatch
) -> None:
    state = tmp_path / "state.db"
    store = Store(state)
    event_id = store.enqueue_event(
        kind="probe", payload={}, dedup_key="bounded-heartbeat", now=100.0
    )
    event = store.claim_event(worker_id="worker-a", now=100.0, lease_seconds=30.0)
    assert event is not None and event.id == event_id and event.lease_token

    heartbeat = LeaseHeartbeat(
        state_path=str(state),
        event_id=event.id,
        worker_id="worker-a",
        lease_token=event.lease_token,
        claimed_at=100.0,
        lease_seconds=30.0,
        heartbeat_seconds=10.0,
        max_extension_seconds=45.0,
    )
    heartbeat._started_monotonic = 0.0
    monkeypatch.setattr("pre_active.lease.time.monotonic", lambda: 20.0)

    connection = sqlite3.connect(state, timeout=5.0, isolation_level=None)
    try:
        heartbeat._renew_once(connection)
    finally:
        connection.close()

    row = store.connection.execute(
        "SELECT lease_until FROM events WHERE id=?", (event.id,)
    ).fetchone()
    assert row is not None
    assert float(row["lease_until"]) == 145.0


def test_retry_transition_updates_run_error_only_inside_fenced_failure_commit(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "state.db")
    run_id = store.create_run(task="retry", capabilities=set(), now=1.0)
    event_id = store.enqueue_event(
        kind="run.step",
        payload={"run_id": run_id, "step": 0},
        dedup_key="retry-step",
        now=1.0,
    )
    event = store.claim_event(worker_id="worker-a", now=2.0, lease_seconds=30.0)
    assert event is not None and event.id == event_id and event.lease_token

    dead = store.fail_event(
        event.id,
        worker_id="worker-a",
        lease_token=event.lease_token,
        now=3.0,
        retry_at=4.0,
        max_attempts=5,
        error="provider unavailable",
        failed_run_id=run_id,
    )

    assert dead is False
    run = store.get_run(run_id)
    assert run["status"] == "RUNNING"
    assert run["last_error"] == "provider unavailable"


def test_lost_retry_claim_cannot_write_run_error(tmp_path: Path) -> None:
    class LosingStore(Store):
        def fail_event(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            raise EventLeaseLost("simulated loss before fenced retry commit")

    class FailingModel:
        def respond(self, *, messages, tools):  # type: ignore[no-untyped-def]
            raise RetryableModelError("temporary", category="transport")

    store = LosingStore(tmp_path / "state.db")
    engine = Engine(
        store=store,
        model=FailingModel(),
        tools=ToolRegistry(store),
        context=ContextAssembler(store),
        system_prompt="Run.",
        worker_id="worker-a",
    )
    run_id = engine.submit_task("retry safely", set(), now=1.0)

    with pytest.raises(EventLeaseLost):
        engine.run_once(now=2.0)

    assert store.get_run(run_id)["last_error"] is None


def test_mutation_effect_admission_serializes_with_operator_control(tmp_path: Path) -> None:
    state = tmp_path / "state.db"
    store = Store(state)
    run_id = store.create_run(task="write", capabilities={"write"}, now=1.0)
    registry = ToolRegistry(store)
    registry.register(
        ToolSpec(
            name="record.write",
            description="write one value",
            input_schema={
                "type": "object",
                "properties": {"value": {"type": "integer"}},
                "required": ["value"],
                "additionalProperties": False,
            },
            capability="write",
            mutation=True,
        ),
        lambda args: {"written": args["value"]},
    )

    guard_entered = threading.Event()
    operator_started = threading.Event()
    operator_observed_effect: list[str | None] = []

    def operator() -> None:
        assert guard_entered.wait(timeout=2.0)
        operator_started.set()
        other = Store(state)
        try:
            other.request_run_control(
                run_id, action="CANCEL", reason="stop", now=2.5
            )
            row = other.connection.execute(
                "SELECT state FROM tool_effects WHERE request_id=?",
                ("atomic-admission",),
            ).fetchone()
            operator_observed_effect.append(None if row is None else str(row["state"]))
        finally:
            other.close()

    thread = threading.Thread(target=operator, daemon=True)
    thread.start()

    def admission_guard() -> None:
        guard_entered.set()
        assert operator_started.wait(timeout=2.0)
        time.sleep(0.1)

    result = registry.execute(
        name="record.write",
        arguments={"value": 7},
        request_id="atomic-admission",
        allowed_capabilities={"write"},
        now=2.0,
        admission_guard=admission_guard,
    )
    thread.join(timeout=3.0)

    assert not thread.is_alive()
    assert result.output == {"written": 7}
    assert operator_observed_effect
    assert operator_observed_effect[0] in {"EXECUTING", "COMMITTED"}



def test_engine_mutation_admission_guard_rechecks_database_claim(tmp_path: Path) -> None:
    from pre_active.engine import LeaseLost, ModelResponse, ToolCall
    from pre_active.tools import ToolExecution

    store = Store(tmp_path / "state.db")

    class ExpiringRegistry(ToolRegistry):
        def execute(self, **kwargs):  # type: ignore[no-untyped-def]
            self.store.connection.execute(
                "UPDATE events SET lease_until=0 WHERE status='CLAIMED'"
            )
            guard = kwargs.get("admission_guard")
            assert guard is not None
            guard()
            raise AssertionError("expired claim reached mutation admission")

    registry = ExpiringRegistry(store)
    registry.register(
        ToolSpec(
            name="record.write",
            description="write one value",
            input_schema={
                "type": "object",
                "properties": {"value": {"type": "integer"}},
                "required": ["value"],
                "additionalProperties": False,
            },
            capability="write",
            mutation=True,
        ),
        lambda args: {"written": args["value"]},
    )

    class WriteModel:
        def respond(self, *, messages, tools):  # type: ignore[no-untyped-def]
            return ModelResponse(
                tool_call=ToolCall(
                    request_id="lease-fenced-admission",
                    name="record.write",
                    arguments={"value": 9},
                )
            )

    engine = Engine(
        store=store,
        model=WriteModel(),
        tools=registry,
        context=ContextAssembler(store),
        system_prompt="Run.",
        worker_id="worker-a",
        lease_seconds=30.0,
        lease_heartbeat_seconds=29.0,
    )
    engine.submit_task("write safely", {"write"}, now=1.0)

    with pytest.raises(LeaseLost):
        engine.run_once(now=2.0)

    row = store.connection.execute(
        "SELECT state FROM tool_effects WHERE request_id=?",
        ("lease-fenced-admission",),
    ).fetchone()
    assert row is None



def test_no_effect_reconciliation_of_executing_attempt_requires_quiescence(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "state.db")
    registry = ToolRegistry(store)
    request_json = '{"arguments":{"value":1},"tool":"record.write"}'
    import hashlib

    request_sha = hashlib.sha256(request_json.encode("utf-8")).hexdigest()
    store.connection.execute(
        """
        INSERT INTO tool_effects (
            request_id, tool_name, request_sha256, request_json, state,
            result_json, result_sha256, created_at, updated_at
        ) VALUES (?, ?, ?, ?, 'EXECUTING', NULL, NULL, ?, ?)
        """,
        (
            "still-executing",
            "record.write",
            request_sha,
            request_json,
            1.0,
            1.0,
        ),
    )

    with pytest.raises(ValueError, match="quiesced"):
        registry.reconcile(
            request_id="still-executing",
            effect_occurred=False,
            evidence_digest="d" * 64,
            now=2.0,
        )

    registry.reconcile(
        request_id="still-executing",
        effect_occurred=False,
        evidence_digest="e" * 64,
        now=3.0,
        attempt_quiesced=True,
    )
    assert registry.effect_state("still-executing") == "RECONCILED_NO_EFFECT"



def test_concurrent_store_openers_serialize_legacy_schema_migration(
    tmp_path: Path, monkeypatch
) -> None:
    state = tmp_path / "legacy.db"
    original_connect = sqlite3.connect
    bootstrap = original_connect(state, timeout=5.0, isolation_level=None)
    bootstrap.execute("PRAGMA journal_mode=WAL")
    bootstrap.executescript(
        """
        CREATE TABLE events (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            priority INTEGER NOT NULL,
            dedup_key TEXT UNIQUE,
            status TEXT NOT NULL,
            available_at REAL NOT NULL,
            lease_owner TEXT,
            lease_until REAL,
            attempts INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE TABLE runs (
            id TEXT PRIMARY KEY,
            task TEXT NOT NULL,
            status TEXT NOT NULL,
            capabilities_json TEXT NOT NULL,
            step_count INTEGER NOT NULL DEFAULT 0,
            final_text TEXT,
            last_error TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        """
    )
    bootstrap.close()

    barrier = threading.Barrier(2)

    class BarrierConnection(sqlite3.Connection):
        def executescript(self, sql):  # type: ignore[no-untyped-def]
            cursor = super().executescript(sql)
            barrier.wait(timeout=3.0)
            return cursor

    def connect(*args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs["factory"] = BarrierConnection
        return original_connect(*args, **kwargs)

    monkeypatch.setattr("pre_active.store.sqlite3.connect", connect)
    errors: list[BaseException] = []

    def opener() -> None:
        try:
            Store(state).close()
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=opener) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=6.0)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []

    verify = original_connect(state)
    try:
        event_columns = {
            row[1] for row in verify.execute("PRAGMA table_info(events)").fetchall()
        }
        run_columns = {
            row[1] for row in verify.execute("PRAGMA table_info(runs)").fetchall()
        }
    finally:
        verify.close()
    assert {"lease_token", "last_error", "dead_lettered_at"} <= event_columns
    assert {
        "blocked_request_id",
        "source_event_id",
        "failed_event_id",
        "control_action",
        "control_reason",
        "control_requested_at",
        "paused_event_id",
        "paused_at",
        "cancelled_at",
    } <= run_columns
