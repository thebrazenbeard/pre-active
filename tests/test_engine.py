from pathlib import Path

from prorun.context import ContextAssembler
from prorun.engine import Engine, ModelResponse, ToolCall
from prorun.store import Store
from prorun.tools import ToolRegistry, ToolSpec


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

    def crash_before_ack(event_id, *, worker_id, now):
        nonlocal crash_once
        if crash_once and event_id == source_event_id:
            crash_once = False
            raise RuntimeError("simulated crash before source-event ack")
        return original_ack(event_id, worker_id=worker_id, now=now)

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
