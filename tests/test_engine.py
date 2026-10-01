from pathlib import Path

from pre_active.context import ContextAssembler
from pre_active.engine import (
    Engine,
    ModelResponse,
    NonRetryableModelError,
    RetryableModelError,
    ToolCall,
)
from pre_active.lease import LeaseLost
from pre_active.store import Store
from pre_active.tools import ToolRegistry, ToolSpec


class ScriptedModel:
    def __init__(self) -> None:
        self.calls = 0

    def respond(self, *, messages, tools):
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                tool_call=ToolCall(
                    request_id="tool-1",
                    name="math.double",
                    arguments={"value": 6},
                )
            )
        assert any("12" in item["content"] for item in messages)
        return ModelResponse(final_text="done: 12")


def test_react_cycle_persists_tool_result_and_continues(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    tools = ToolRegistry(store)
    tools.register(
        ToolSpec(
            name="math.double",
            description="double an integer",
            input_schema={"type": "object", "required": ["value"]},
            capability="math.read",
            mutation=False,
        ),
        lambda args: {"value": int(args["value"]) * 2},
    )
    engine = Engine(
        store=store,
        model=ScriptedModel(),
        tools=tools,
        context=ContextAssembler(store, max_chars=4000),
        system_prompt="Use tools when needed.",
        worker_id="worker-1",
    )

    run_id = engine.submit_task("Double six and report it.", {"math.read"}, now=1.0)
    first = engine.run_once(now=2.0)
    assert first == run_id
    assert store.get_run(run_id)["status"] == "RUNNING"

    second = engine.run_once(now=3.0)
    assert second == run_id
    run = store.get_run(run_id)
    assert run["status"] == "COMPLETED"
    assert run["final_text"] == "done: 12"
    assert store.pending_event_count() == 0


def test_task_requested_event_spawns_a_durable_run(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    tools = ToolRegistry(store)

    class FinalModel:
        def respond(self, *, messages, tools):
            return ModelResponse(final_text="finished")

    engine = Engine(
        store=store,
        model=FinalModel(),
        tools=tools,
        context=ContextAssembler(store),
        system_prompt="Run.",
        worker_id="worker-1",
    )
    store.enqueue_event(
        kind="task.requested",
        payload={"task": "scheduled task", "capabilities": []},
        dedup_key="external-1",
        now=1.0,
    )

    run_id = engine.run_once(now=2.0)
    assert run_id is not None
    assert store.get_run(run_id)["task"] == "scheduled task"
    assert store.pending_event_count() == 1

    engine.run_once(now=3.0)
    assert store.get_run(run_id)["status"] == "COMPLETED"


def test_ambiguous_tool_effect_blocks_run_until_exact_reconciliation(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    tools = ToolRegistry(store)
    attempts = 0

    def flaky_write(args):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("lost response after dispatch")
        return {"written": args["value"]}

    tools.register(
        ToolSpec(
            name="record.write",
            description="write once",
            input_schema={"type": "object", "required": ["value"]},
            capability="record.write",
            mutation=True,
        ),
        flaky_write,
    )

    class RecoveryModel:
        def __init__(self):
            self.calls = 0

        def respond(self, *, messages, tools):
            self.calls += 1
            if self.calls == 1:
                return ModelResponse(
                    tool_call=ToolCall(
                        request_id="effect-1",
                        name="record.write",
                        arguments={"value": 9},
                    )
                )
            assert any("written" in item["content"] for item in messages)
            return ModelResponse(final_text="recovered")

    engine = Engine(
        store=store,
        model=RecoveryModel(),
        tools=tools,
        context=ContextAssembler(store),
        system_prompt="Run.",
        worker_id="worker-1",
    )
    run_id = engine.submit_task("write 9", {"record.write"}, now=1.0)

    first = engine.run_once(now=2.0)
    assert first == run_id
    blocked = store.get_run(run_id)
    assert blocked["status"] == "BLOCKED_EFFECT"
    assert blocked["blocked_request_id"] == "effect-1"
    assert store.pending_event_count() == 0

    tools.reconcile(
        request_id="effect-1",
        effect_occurred=False,
        evidence_digest="b" * 64,
        now=3.0,
    )
    engine.resume_blocked_effect(run_id, now=4.0)
    assert attempts == 2
    assert store.get_run(run_id)["status"] == "RUNNING"

    engine.run_once(now=5.0)
    assert store.get_run(run_id)["status"] == "COMPLETED"
    assert store.get_run(run_id)["final_text"] == "recovered"


def test_deterministic_tool_admission_error_fails_run_without_retry_loop(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    tools = ToolRegistry(store)

    class BadToolModel:
        def respond(self, *, messages, tools):
            return ModelResponse(
                tool_call=ToolCall(
                    request_id="bad-1",
                    name="missing.tool",
                    arguments={},
                )
            )

    engine = Engine(
        store=store,
        model=BadToolModel(),
        tools=tools,
        context=ContextAssembler(store),
        system_prompt="Run.",
        worker_id="worker-1",
    )
    run_id = engine.submit_task("call unavailable tool", set(), now=1.0)

    assert engine.run_once(now=2.0) == run_id
    run = store.get_run(run_id)
    assert run["status"] == "FAILED"
    assert "unknown tool" in run["last_error"]
    assert store.pending_event_count() == 0


def test_stale_run_step_redelivery_is_acked_without_advancing_model(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    tools = ToolRegistry(store)

    class TwoTurnModel:
        def __init__(self):
            self.calls = 0

        def respond(self, *, messages, tools):
            self.calls += 1
            if self.calls == 1:
                return ModelResponse(
                    tool_call=ToolCall(
                        request_id="read-1",
                        name="read.once",
                        arguments={},
                    )
                )
            return ModelResponse(final_text="done")

    model = TwoTurnModel()
    tools.register(
        ToolSpec(
            name="read.once",
            description="read",
            input_schema={"type": "object"},
            capability="read",
            mutation=False,
        ),
        lambda args: {"value": 1},
    )
    engine = Engine(
        store=store,
        model=model,
        tools=tools,
        context=ContextAssembler(store),
        system_prompt="Run.",
        worker_id="worker-1",
    )
    run_id = engine.submit_task("read then finish", {"read"}, now=1.0)
    engine.run_once(now=2.0)
    assert model.calls == 1

    old = store.list_events(kind="run.step")[0]
    store.connection.execute(
        "UPDATE events SET status='PENDING', available_at=2.5 WHERE id=?", (old["id"],)
    )

    engine.run_once(now=3.0)
    assert model.calls == 1
    assert store.get_run(run_id)["status"] == "RUNNING"

    engine.run_once(now=4.0)
    assert model.calls == 2
    assert store.get_run(run_id)["status"] == "COMPLETED"


def test_committed_mutation_is_not_duplicated_if_crash_happens_before_step_advance(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "state.db")
    tools = ToolRegistry(store)
    writes: list[int] = []

    def write_once(args):
        writes.append(int(args["value"]))
        return {"written": int(args["value"])}

    tools.register(
        ToolSpec(
            name="record.write",
            description="write a value",
            input_schema={"type": "object", "required": ["value"]},
            capability="record.write",
            mutation=True,
        ),
        write_once,
    )

    class ChangingRequestIdModel:
        def __init__(self):
            self.calls = 0

        def respond(self, *, messages, tools):
            self.calls += 1
            if any("record.write =>" in item["content"] for item in messages):
                return ModelResponse(final_text="done")
            return ModelResponse(
                tool_call=ToolCall(
                    request_id=f"effect-{self.calls}",
                    name="record.write",
                    arguments={"value": 11},
                )
            )

    model = ChangingRequestIdModel()
    engine = Engine(
        store=store,
        model=model,
        tools=tools,
        context=ContextAssembler(store),
        system_prompt="Run.",
        worker_id="worker-1",
    )
    run_id = engine.submit_task("write 11 once", {"record.write"}, now=1.0)

    original_record = store.record_run_message
    crash_once = True

    def crash_after_effect(*, run_id, role, content, now, message_key=None):
        nonlocal crash_once
        if crash_once and role == "tool":
            crash_once = False
            raise RuntimeError("simulated crash after committed tool effect")
        return original_record(
            run_id=run_id, role=role, content=content, now=now, message_key=message_key
        )

    store.record_run_message = crash_after_effect  # type: ignore[method-assign]

    import pytest

    with pytest.raises(RuntimeError, match="simulated crash"):
        engine.run_once(now=2.0)

    assert writes == [11]
    assert tools.effect_state("effect-1") == "COMMITTED"

    # Retry of the same durable run step must reuse the already-persisted decision,
    # including the original request_id, rather than asking the model to mint a new one.
    assert engine.run_once(now=5.0) == run_id
    assert writes == [11]
    assert model.calls == 1

    assert engine.run_once(now=6.0) == run_id
    assert store.get_run(run_id)["status"] == "COMPLETED"


def test_task_requested_redelivery_reuses_the_same_run_after_pre_ack_crash(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "state.db")
    tools = ToolRegistry(store)

    class FinalModel:
        def respond(self, *, messages, tools):
            return ModelResponse(final_text="done")

    engine = Engine(
        store=store,
        model=FinalModel(),
        tools=tools,
        context=ContextAssembler(store),
        system_prompt="Run.",
        worker_id="worker-1",
        lease_seconds=5.0,
    )
    source_event_id = store.enqueue_event(
        kind="task.requested",
        payload={"task": "exactly once", "capabilities": []},
        dedup_key="task-source-1",
        now=1.0,
    )

    original_ack = store.ack_event
    crash_once = True

    def crash_before_ack(event_id, *, worker_id, lease_token, now):
        nonlocal crash_once
        if crash_once and event_id == source_event_id:
            crash_once = False
            raise RuntimeError("simulated crash before source-event ack")
        return original_ack(
            event_id, worker_id=worker_id, lease_token=lease_token, now=now
        )

    store.ack_event = crash_before_ack  # type: ignore[method-assign]

    import pytest

    with pytest.raises(RuntimeError, match="simulated crash"):
        engine.run_once(now=2.0)

    first_run = store.connection.execute(
        "SELECT id FROM runs ORDER BY created_at, id LIMIT 1"
    ).fetchone()["id"]
    assert store.connection.execute("SELECT COUNT(*) AS n FROM runs").fetchone()["n"] == 1

    # The source event's lease expires and the same task.requested event is redelivered.
    assert engine.run_once(now=8.0) == first_run
    assert store.connection.execute("SELECT COUNT(*) AS n FROM runs").fetchone()["n"] == 1


def test_step_advance_and_next_event_are_atomic_across_enqueue_failure(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    tools = ToolRegistry(store)
    writes: list[int] = []
    tools.register(
        ToolSpec(
            name="record.write",
            description="write exactly once",
            input_schema={
                "type": "object",
                "properties": {"value": {"type": "integer"}},
                "required": ["value"],
                "additionalProperties": False,
            },
            capability="record.write",
            mutation=True,
        ),
        lambda args: writes.append(int(args["value"])) or {"written": int(args["value"])},
    )

    class OneWriteThenFinal:
        def __init__(self):
            self.calls = 0

        def respond(self, *, messages, tools):
            self.calls += 1
            if any("record.write =>" in item["content"] for item in messages):
                return ModelResponse(final_text="done")
            return ModelResponse(
                tool_call=ToolCall(
                    request_id="advance-effect-1",
                    name="record.write",
                    arguments={"value": 3},
                )
            )

    engine = Engine(
        store=store,
        model=OneWriteThenFinal(),
        tools=tools,
        context=ContextAssembler(store),
        system_prompt="Run.",
        worker_id="worker-1",
    )
    run_id = engine.submit_task("write 3", {"record.write"}, now=1.0)

    original_enqueue = store.enqueue_event
    crash_once = True

    def crash_on_next_step(*, kind, payload, priority=0, dedup_key=None, now, available_at=None):
        nonlocal crash_once
        if crash_once and kind == "run.step" and payload.get("step") == 1:
            crash_once = False
            raise RuntimeError("simulated crash while scheduling next step")
        return original_enqueue(
            kind=kind,
            payload=payload,
            priority=priority,
            dedup_key=dedup_key,
            now=now,
            available_at=available_at,
        )

    store.enqueue_event = crash_on_next_step  # type: ignore[method-assign]

    import pytest

    with pytest.raises(RuntimeError, match="scheduling next step"):
        engine.run_once(now=2.0)

    assert writes == [3]
    # A failed next-step enqueue must not durably advance the generation by itself.
    assert store.get_run(run_id)["step_count"] == 0

    assert engine.run_once(now=5.0) == run_id
    assert writes == [3]
    assert store.get_run(run_id)["step_count"] == 1
    transcript = store.list_run_messages(run_id)
    assert len([m for m in transcript if m["role"] == "assistant"]) == 1
    assert len([m for m in transcript if m["role"] == "tool"]) == 1
    assert engine.run_once(now=6.0) == run_id
    assert store.get_run(run_id)["status"] == "COMPLETED"


def test_initial_run_creation_and_first_step_event_are_atomic(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    tools = ToolRegistry(store)

    class FinalModel:
        def respond(self, *, messages, tools):
            return ModelResponse(final_text="done")

    engine = Engine(
        store=store,
        model=FinalModel(),
        tools=tools,
        context=ContextAssembler(store),
        system_prompt="Run.",
        worker_id="worker-1",
    )

    original_enqueue = store.enqueue_event

    def crash_on_initial_step(*, kind, payload, priority=0, dedup_key=None, now, available_at=None):
        if kind == "run.step" and payload.get("step") == 0:
            raise RuntimeError("simulated crash while scheduling initial step")
        return original_enqueue(
            kind=kind,
            payload=payload,
            priority=priority,
            dedup_key=dedup_key,
            now=now,
            available_at=available_at,
        )

    store.enqueue_event = crash_on_initial_step  # type: ignore[method-assign]

    import pytest

    with pytest.raises(RuntimeError, match="scheduling initial step"):
        engine.submit_task("cannot strand me", set(), now=1.0)

    assert store.connection.execute("SELECT COUNT(*) AS n FROM runs").fetchone()["n"] == 0
    assert store.pending_event_count() == 0


def test_effect_recovery_resume_and_successor_event_are_atomic(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    run_id = store.create_run(task="recover", capabilities=set(), now=1.0)
    store.connection.execute(
        "UPDATE runs SET status='BLOCKED_EFFECT', blocked_request_id='effect-x' WHERE id=?",
        (run_id,),
    )

    original_enqueue = store.enqueue_event

    def crash_on_recovery_step(*, kind, payload, priority=0, dedup_key=None, now, available_at=None):
        if kind == "run.step" and payload.get("step") == 1:
            raise RuntimeError("simulated crash while scheduling recovery successor")
        return original_enqueue(
            kind=kind,
            payload=payload,
            priority=priority,
            dedup_key=dedup_key,
            now=now,
            available_at=available_at,
        )

    store.enqueue_event = crash_on_recovery_step  # type: ignore[method-assign]

    import pytest

    with pytest.raises(RuntimeError, match="scheduling recovery successor"):
        store.resume_run_after_effect(run_id=run_id, now=2.0)

    run = store.get_run(run_id)
    assert run["status"] == "BLOCKED_EFFECT"
    assert run["blocked_request_id"] == "effect-x"
    assert run["step_count"] == 0
    assert store.pending_event_count() == 0


def test_malformed_task_request_is_rejected_without_retry_loop(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    tools = ToolRegistry(store)

    class ShouldNotRunModel:
        def respond(self, *, messages, tools):
            raise AssertionError("malformed task request must not reach the model")

    engine = Engine(
        store=store,
        model=ShouldNotRunModel(),
        tools=tools,
        context=ContextAssembler(store),
        system_prompt="Run.",
        worker_id="worker-1",
    )
    event_id = store.enqueue_event(
        kind="task.requested",
        payload={"task": "bad capabilities", "capabilities": "not-a-list"},
        dedup_key="malformed-task-1",
        now=1.0,
    )

    assert engine.run_once(now=2.0) is None
    assert store.pending_event_count() == 0
    journal = store.list_journal(subject_id=event_id)
    assert any(entry["event_type"] == "EVENT_REJECTED" for entry in journal)



def test_successful_retry_clears_previous_provider_error(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    tools = ToolRegistry(store)

    class FlakyThenFinalModel:
        def __init__(self) -> None:
            self.calls = 0

        def respond(self, *, messages, tools):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("temporary provider outage")
            return ModelResponse(final_text="recovered")

    engine = Engine(
        store=store,
        model=FlakyThenFinalModel(),
        tools=tools,
        context=ContextAssembler(store),
        system_prompt="Run.",
        worker_id="worker-1",
    )
    run_id = engine.submit_task("recover after provider outage", set(), now=1.0)

    import pytest

    with pytest.raises(RuntimeError, match="temporary provider outage"):
        engine.run_once(now=2.0)

    failed = store.get_run(run_id)
    assert failed["status"] == "RUNNING"
    assert "temporary provider outage" in failed["last_error"]

    assert engine.run_once(now=10.0) == run_id
    recovered = store.get_run(run_id)
    assert recovered["status"] == "COMPLETED"
    assert recovered["final_text"] == "recovered"
    assert recovered["last_error"] is None


def test_successful_tool_step_clears_previous_provider_error(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    tools = ToolRegistry(store)
    tools.register(
        ToolSpec(
            name="read.value",
            description="read one value",
            input_schema={"type": "object"},
            capability="read.value",
            mutation=False,
        ),
        lambda args: {"value": 7},
    )

    class FlakyThenToolModel:
        def __init__(self) -> None:
            self.calls = 0

        def respond(self, *, messages, tools):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("temporary provider outage")
            return ModelResponse(
                tool_call=ToolCall(
                    request_id="read-after-recovery",
                    name="read.value",
                    arguments={},
                )
            )

    engine = Engine(
        store=store,
        model=FlakyThenToolModel(),
        tools=tools,
        context=ContextAssembler(store),
        system_prompt="Run.",
        worker_id="worker-1",
    )
    run_id = engine.submit_task("recover then read", {"read.value"}, now=1.0)

    import pytest

    with pytest.raises(RuntimeError, match="temporary provider outage"):
        engine.run_once(now=2.0)

    assert "temporary provider outage" in store.get_run(run_id)["last_error"]

    assert engine.run_once(now=10.0) == run_id
    recovered = store.get_run(run_id)
    assert recovered["status"] == "RUNNING"
    assert recovered["step_count"] == 1
    assert recovered["last_error"] is None


def test_long_model_call_keeps_event_lease_alive(tmp_path: Path) -> None:
    import threading
    import time

    state = tmp_path / "state.db"
    store = Store(state)
    tools = ToolRegistry(store)
    model_started = threading.Event()
    release_model = threading.Event()

    class SlowFinalModel:
        def respond(self, *, messages, tools):
            model_started.set()
            assert release_model.wait(timeout=3.0)
            return ModelResponse(final_text="slow but healthy")

    engine = Engine(
        store=store,
        model=SlowFinalModel(),
        tools=tools,
        context=ContextAssembler(store),
        system_prompt="Run.",
        worker_id="worker-primary",
        lease_seconds=0.40,
        lease_heartbeat_seconds=0.05,
    )
    run_id = engine.submit_task("slow model turn", set(), now=1.0)

    competing_claims = []
    contender_errors: list[BaseException] = []

    def compete() -> None:
        assert model_started.wait(timeout=2.0)
        competitor = Store(state)
        try:
            deadline = time.monotonic() + 2.0
            renewed_until = 0.0
            while time.monotonic() < deadline:
                row = competitor.connection.execute(
                    "SELECT lease_until FROM events WHERE status='CLAIMED' LIMIT 1"
                ).fetchone()
                if row is not None and row["lease_until"] is not None:
                    renewed_until = float(row["lease_until"])
                    if renewed_until > 2.45:
                        break
                time.sleep(0.01)
            assert renewed_until > 2.45

            # Test beyond the original 2.40 lease expiry using the queue's
            # explicit logical clock, not CI wall-clock timing.
            competing_claims.append(
                competitor.claim_event(
                    worker_id="worker-secondary",
                    now=2.41,
                    lease_seconds=1.0,
                )
            )
        except BaseException as exc:
            contender_errors.append(exc)
        finally:
            competitor.close()
            release_model.set()

    contender = threading.Thread(target=compete)
    contender.start()
    result = engine.run_once(now=2.0)
    contender.join(timeout=3.0)

    assert not contender.is_alive()
    assert contender_errors == []
    assert result == run_id
    assert competing_claims == [None]
    assert store.get_run(run_id)["status"] == "COMPLETED"


def test_retry_exhaustion_dead_letters_event_and_fails_run(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    tools = ToolRegistry(store)

    class AlwaysFailModel:
        def respond(self, *, messages, tools):
            raise RuntimeError("provider remains unavailable")

    engine = Engine(
        store=store,
        model=AlwaysFailModel(),
        tools=tools,
        context=ContextAssembler(store),
        system_prompt="Run.",
        worker_id="worker-1",
        max_event_attempts=2,
    )
    run_id = engine.submit_task("eventually stop retrying", set(), now=1.0)

    import pytest

    with pytest.raises(RuntimeError, match="provider remains unavailable"):
        engine.run_once(now=2.0)
    [first_event] = store.list_events(kind="run.step")
    assert first_event["status"] == "PENDING"
    assert store.get_run(run_id)["status"] == "RUNNING"

    with pytest.raises(RuntimeError, match="provider remains unavailable"):
        engine.run_once(now=5.0)

    [dead_event] = store.list_events(kind="run.step")
    assert dead_event["status"] == "DEAD"
    assert dead_event["attempts"] == 2
    assert "provider remains unavailable" in dead_event["last_error"]
    run = store.get_run(run_id)
    assert run["status"] == "FAILED"
    assert "provider remains unavailable" in run["last_error"]


def test_lease_extension_ceiling_blocks_stale_model_decision_persistence(tmp_path: Path) -> None:
    import time
    import pytest

    store = Store(tmp_path / "state.db")
    tools = ToolRegistry(store)

    class TooSlowModel:
        def respond(self, *, messages, tools):
            time.sleep(0.35)
            return ModelResponse(final_text="late response")

    engine = Engine(
        store=store,
        model=TooSlowModel(),
        tools=tools,
        context=ContextAssembler(store),
        system_prompt="Run.",
        worker_id="worker-1",
        lease_seconds=1.0,
        lease_heartbeat_seconds=0.05,
        max_lease_extension_seconds=0.15,
    )
    run_id = engine.submit_task("do not persist stale decision", set(), now=time.time())

    with pytest.raises(LeaseLost, match="maximum lease extension elapsed"):
        engine.run_once(now=time.time())

    assert store.get_run_step_decision(run_id=run_id, step=0) is None
    run = store.get_run(run_id)
    assert run["status"] == "RUNNING"
    assert "LeaseLost" in run["last_error"]


def test_final_progress_is_not_committed_after_claim_expires_during_persistence(
    tmp_path: Path,
) -> None:
    import time
    import pytest

    store = Store(tmp_path / "state.db")
    tools = ToolRegistry(store)

    class FinalModel:
        def respond(self, *, messages, tools):
            return ModelResponse(final_text="must not commit stale")

    engine = Engine(
        store=store,
        model=FinalModel(),
        tools=tools,
        context=ContextAssembler(store),
        system_prompt="Run.",
        worker_id="worker-1",
        lease_seconds=0.20,
        lease_heartbeat_seconds=0.05,
        max_lease_extension_seconds=0.12,
    )
    run_id = engine.submit_task("fence final commit", set(), now=time.time())

    original_record = store.record_run_message

    def delay_final_persistence(*, run_id, role, content, now, message_key=None):
        if role == "assistant" and content == "must not commit stale":
            time.sleep(0.35)
        return original_record(
            run_id=run_id,
            role=role,
            content=content,
            now=now,
            message_key=message_key,
        )

    store.record_run_message = delay_final_persistence  # type: ignore[method-assign]

    with pytest.raises((LeaseLost, RuntimeError)):
        engine.run_once(now=time.time())

    run = store.get_run(run_id)
    assert run["status"] == "RUNNING"
    assert run["step_count"] == 0
    assert run["final_text"] is None


def test_model_error_contract_distinguishes_retryable_and_permanent_failures() -> None:
    import pre_active.engine as engine_module

    assert hasattr(engine_module, "RetryableModelError")
    assert hasattr(engine_module, "NonRetryableModelError")

    retryable = engine_module.RetryableModelError(
        "rate limited",
        category="throttling",
        retry_after_seconds=12.0,
    )
    assert retryable.category == "throttling"
    assert retryable.retry_after_seconds == 12.0
    assert isinstance(engine_module.NonRetryableModelError("bad auth"), RuntimeError)


def test_non_retryable_model_error_fails_run_and_consumes_event(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    tools = ToolRegistry(store)

    class BadAuthModel:
        def respond(self, *, messages, tools):
            raise NonRetryableModelError("provider authentication rejected")

    engine = Engine(
        store=store,
        model=BadAuthModel(),
        tools=tools,
        context=ContextAssembler(store),
        system_prompt="Run.",
        worker_id="worker-1",
    )
    run_id = engine.submit_task("do not churn on bad auth", set(), now=1.0)

    assert engine.run_once(now=2.0) == run_id

    run = store.get_run(run_id)
    assert run["status"] == "FAILED"
    assert "NonRetryableModelError" in run["last_error"]
    [event] = store.list_events(kind="run.step")
    assert event["status"] == "DONE"
    assert event["attempts"] == 1
    assert store.pending_event_count() == 0
    assert any(
        entry["event_type"] == "MODEL_FAILURE_TERMINAL"
        for entry in store.list_journal(subject_id=run_id)
    )


def test_retryable_model_error_keeps_run_pending_for_retry(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    tools = ToolRegistry(store)

    class ThrottledModel:
        def respond(self, *, messages, tools):
            raise RetryableModelError(
                "provider throttled",
                category="throttling",
                retry_after_seconds=12.0,
            )

    engine = Engine(
        store=store,
        model=ThrottledModel(),
        tools=tools,
        context=ContextAssembler(store),
        system_prompt="Run.",
        worker_id="worker-1",
        max_event_attempts=4,
    )
    run_id = engine.submit_task("retry after throttle", set(), now=1.0)

    import pytest

    with pytest.raises(RetryableModelError, match="provider throttled"):
        engine.run_once(now=2.0)

    run = store.get_run(run_id)
    assert run["status"] == "RUNNING"
    assert "RetryableModelError" in run["last_error"]
    [event] = store.list_events(kind="run.step")
    assert event["status"] == "PENDING"
    assert event["attempts"] == 1


def test_retryable_model_error_honors_retry_after_as_minimum_delay(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    tools = ToolRegistry(store)

    class BusyModel:
        def respond(self, *, messages, tools):
            raise RetryableModelError(
                "come back later",
                category="throttling",
                retry_after_seconds=12.0,
            )

    engine = Engine(
        store=store,
        model=BusyModel(),
        tools=tools,
        context=ContextAssembler(store),
        system_prompt="Run.",
        worker_id="worker-1",
    )
    engine.submit_task("respect retry-after", set(), now=1.0)

    import pytest

    with pytest.raises(RetryableModelError):
        engine.run_once(now=2.0)

    row = store.connection.execute(
        "SELECT available_at, attempts FROM events WHERE kind='run.step'"
    ).fetchone()
    assert row is not None
    assert row["attempts"] == 1
    assert row["available_at"] == 14.0


def test_retryable_model_backoff_adds_stable_subsecond_jitter() -> None:
    import pre_active.engine as engine_module

    assert hasattr(engine_module, "_model_retry_delay_seconds")

    first = engine_module._model_retry_delay_seconds(
        event_id="event-a",
        attempts=1,
        retry_after_seconds=None,
    )
    repeated = engine_module._model_retry_delay_seconds(
        event_id="event-a",
        attempts=1,
        retry_after_seconds=None,
    )
    other = engine_module._model_retry_delay_seconds(
        event_id="event-b",
        attempts=1,
        retry_after_seconds=None,
    )

    assert first == repeated
    assert 2.0 <= first < 3.0
    assert 2.0 <= other < 3.0
    assert first != other
