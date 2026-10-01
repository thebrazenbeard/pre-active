from pathlib import Path

import pytest

from pre_active.context import ContextAssembler
from pre_active.engine import Engine, ModelResponse, ToolCall
from pre_active.store import Store
from pre_active.tools import ToolRegistry, ToolSpec


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


def test_pause_before_inference_prevents_model_call_and_resume_reuses_event(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state.db"
    store = Store(state)
    tools = ToolRegistry(store)

    class FinalModel:
        def __init__(self) -> None:
            self.calls = 0

        def respond(self, *, messages, tools):
            self.calls += 1
            return ModelResponse(final_text="done")

    model = FinalModel()
    engine = Engine(
        store=store,
        model=model,
        tools=tools,
        context=ContextAssembler(store),
        system_prompt="Run.",
        worker_id="worker-1",
    )
    run_id = engine.submit_task("pause before inference", set(), now=1.0)
    [original_event] = store.list_events(kind="run.step")

    store.request_run_control(run_id, action="PAUSE", reason="review", now=2.0)
    assert engine.run_once(now=3.0) == run_id

    run = store.get_run(run_id)
    assert run["status"] == "PAUSED"
    assert run["paused_event_id"] == original_event["id"]
    assert model.calls == 0
    [paused_event] = store.list_events(kind="run.step")
    assert paused_event["id"] == original_event["id"]
    assert paused_event["status"] == "PAUSED"

    store.resume_paused_run(run_id, reason="continue", now=4.0)
    [resumed_event] = store.list_events(kind="run.step")
    assert resumed_event["id"] == original_event["id"]
    assert resumed_event["status"] == "PENDING"

    assert engine.run_once(now=5.0) == run_id
    assert model.calls == 1
    assert store.get_run(run_id)["status"] == "COMPLETED"


def test_cancel_arriving_during_inference_prevents_tool_dispatch(tmp_path: Path) -> None:
    state = tmp_path / "state.db"
    store = Store(state)
    tools = ToolRegistry(store)
    tool_calls: list[int] = []
    run_id_holder: dict[str, str] = {}

    tools.register(
        ToolSpec(
            name="read.value",
            description="read",
            input_schema={"type": "object"},
            capability="read",
            mutation=False,
        ),
        lambda args: tool_calls.append(1) or {"value": 1},
    )

    class CancellingModel:
        def respond(self, *, messages, tools):
            operator = Store(state)
            try:
                operator.request_run_control(
                    run_id_holder["run_id"],
                    action="CANCEL",
                    reason="operator changed course",
                    now=2.5,
                )
            finally:
                operator.close()
            return ModelResponse(
                tool_call=ToolCall(
                    request_id="must-not-dispatch",
                    name="read.value",
                    arguments={},
                )
            )

    engine = Engine(
        store=store,
        model=CancellingModel(),
        tools=tools,
        context=ContextAssembler(store),
        system_prompt="Run.",
        worker_id="worker-1",
    )
    run_id = engine.submit_task("cancel during model", {"read"}, now=1.0)
    run_id_holder["run_id"] = run_id

    assert engine.run_once(now=2.0) == run_id
    run = store.get_run(run_id)
    assert run["status"] == "CANCELLED"
    assert tool_calls == []
    assert store.get_run_step_decision(run_id=run_id, step=0) is None
    [event] = store.list_events(kind="run.step")
    assert event["status"] == "CANCELLED"


def test_pause_arriving_during_tool_finishes_result_then_stops_successor(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state.db"
    store = Store(state)
    tools = ToolRegistry(store)
    run_id_holder: dict[str, str] = {}

    def read_and_request_pause(args):
        operator = Store(state)
        try:
            operator.request_run_control(
                run_id_holder["run_id"],
                action="PAUSE",
                reason="inspect tool result",
                now=2.5,
            )
        finally:
            operator.close()
        return {"value": 7}

    tools.register(
        ToolSpec(
            name="read.value",
            description="read",
            input_schema={"type": "object"},
            capability="read",
            mutation=False,
        ),
        read_and_request_pause,
    )

    class ToolThenFinal:
        def __init__(self) -> None:
            self.calls = 0

        def respond(self, *, messages, tools):
            self.calls += 1
            if self.calls == 1:
                return ModelResponse(
                    tool_call=ToolCall(
                        request_id="read-1",
                        name="read.value",
                        arguments={},
                    )
                )
            return ModelResponse(final_text="continued")

    model = ToolThenFinal()
    engine = Engine(
        store=store,
        model=model,
        tools=tools,
        context=ContextAssembler(store),
        system_prompt="Run.",
        worker_id="worker-1",
    )
    run_id = engine.submit_task("pause after tool", {"read"}, now=1.0)
    run_id_holder["run_id"] = run_id

    assert engine.run_once(now=2.0) == run_id
    paused = store.get_run(run_id)
    assert paused["status"] == "PAUSED"
    assert paused["step_count"] == 1
    assert paused["paused_event_id"] is None
    assert store.pending_event_count() == 0
    transcript = store.list_run_messages(run_id)
    assert any(message["role"] == "tool" and "7" in message["content"] for message in transcript)

    store.resume_paused_run(run_id, reason="continue after inspection", now=3.0)
    assert store.pending_event_count() == 1
    assert engine.run_once(now=4.0) == run_id
    assert model.calls == 2
    assert store.get_run(run_id)["status"] == "COMPLETED"


def test_cancel_waits_for_effect_reconciliation_and_no_effect_does_not_retry(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state.db"
    store = Store(state)
    tools = ToolRegistry(store)
    attempts = 0

    def ambiguous_write(args):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("response lost after dispatch")
        return {"written": args["value"]}

    tools.register(
        ToolSpec(
            name="record.write",
            description="write",
            input_schema={"type": "object", "required": ["value"]},
            capability="write",
            mutation=True,
        ),
        ambiguous_write,
    )

    class WriteModel:
        def respond(self, *, messages, tools):
            return ModelResponse(
                tool_call=ToolCall(
                    request_id="cancel-blocked-effect",
                    name="record.write",
                    arguments={"value": 9},
                )
            )

    engine = Engine(
        store=store,
        model=WriteModel(),
        tools=tools,
        context=ContextAssembler(store),
        system_prompt="Run.",
        worker_id="worker-1",
    )
    run_id = engine.submit_task("write then cancel", {"write"}, now=1.0)
    assert engine.run_once(now=2.0) == run_id
    assert store.get_run(run_id)["status"] == "BLOCKED_EFFECT"
    assert attempts == 1

    store.request_run_control(
        run_id,
        action="CANCEL",
        reason="operator stop",
        now=3.0,
    )
    pending = store.get_run(run_id)
    assert pending["status"] == "BLOCKED_EFFECT"
    assert pending["control_action"] == "CANCEL"

    with pytest.raises(Exception):
        engine.resume_blocked_effect(run_id, now=3.5)
    assert store.get_run(run_id)["status"] == "BLOCKED_EFFECT"
    assert attempts == 1

    tools.reconcile(
        request_id="cancel-blocked-effect",
        effect_occurred=False,
        evidence_digest="a" * 64,
        now=4.0,
    )
    engine.resume_blocked_effect(run_id, now=5.0)

    cancelled = store.get_run(run_id)
    assert cancelled["status"] == "CANCELLED"
    assert cancelled["blocked_request_id"] is None
    assert cancelled["control_action"] is None
    assert attempts == 1


def test_pause_after_no_effect_reconciliation_defers_exact_retry_until_resume(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state.db"
    store = Store(state)
    tools = ToolRegistry(store)
    attempts = 0

    def flaky_write(args):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("unknown first outcome")
        return {"written": args["value"]}

    tools.register(
        ToolSpec(
            name="record.write",
            description="write",
            input_schema={"type": "object", "required": ["value"]},
            capability="write",
            mutation=True,
        ),
        flaky_write,
    )

    class WriteThenFinal:
        def __init__(self) -> None:
            self.calls = 0

        def respond(self, *, messages, tools):
            self.calls += 1
            if self.calls == 1:
                return ModelResponse(
                    tool_call=ToolCall(
                        request_id="pause-blocked-effect",
                        name="record.write",
                        arguments={"value": 4},
                    )
                )
            return ModelResponse(final_text="done")

    model = WriteThenFinal()
    engine = Engine(
        store=store,
        model=model,
        tools=tools,
        context=ContextAssembler(store),
        system_prompt="Run.",
        worker_id="worker-1",
    )
    run_id = engine.submit_task("write then pause", {"write"}, now=1.0)
    assert engine.run_once(now=2.0) == run_id
    assert store.get_run(run_id)["status"] == "BLOCKED_EFFECT"

    store.request_run_control(run_id, action="PAUSE", reason="inspect", now=3.0)
    tools.reconcile(
        request_id="pause-blocked-effect",
        effect_occurred=False,
        evidence_digest="b" * 64,
        now=4.0,
    )
    engine.resume_blocked_effect(run_id, now=5.0)

    paused = store.get_run(run_id)
    assert paused["status"] == "PAUSED"
    assert paused["step_count"] == 0
    assert paused["blocked_request_id"] is None
    assert attempts == 1

    store.resume_paused_run(run_id, reason="continue", now=6.0)
    assert engine.run_once(now=7.0) == run_id
    assert attempts == 2
    assert store.get_run(run_id)["status"] == "RUNNING"
    assert store.get_run(run_id)["step_count"] == 1

    assert engine.run_once(now=8.0) == run_id
    assert model.calls == 2
    assert store.get_run(run_id)["status"] == "COMPLETED"


def test_cancel_after_confirmed_effect_records_result_without_redispatch(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state.db"
    store = Store(state)
    tools = ToolRegistry(store)
    attempts = 0

    def ambiguous_write(args):
        nonlocal attempts
        attempts += 1
        raise RuntimeError("response lost")

    tools.register(
        ToolSpec(
            name="record.write",
            description="write",
            input_schema={"type": "object", "required": ["value"]},
            capability="write",
            mutation=True,
        ),
        ambiguous_write,
    )

    class WriteModel:
        def respond(self, *, messages, tools):
            return ModelResponse(
                tool_call=ToolCall(
                    request_id="confirmed-cancel",
                    name="record.write",
                    arguments={"value": 11},
                )
            )

    engine = Engine(
        store=store,
        model=WriteModel(),
        tools=tools,
        context=ContextAssembler(store),
        system_prompt="Run.",
        worker_id="worker-1",
    )
    run_id = engine.submit_task("confirmed effect then cancel", {"write"}, now=1.0)
    assert engine.run_once(now=2.0) == run_id
    assert attempts == 1

    store.request_run_control(run_id, action="CANCEL", reason="stop", now=3.0)
    tools.reconcile(
        request_id="confirmed-cancel",
        effect_occurred=True,
        evidence_digest="c" * 64,
        result={"written": 11},
        now=4.0,
    )
    engine.resume_blocked_effect(run_id, now=5.0)

    run = store.get_run(run_id)
    assert run["status"] == "CANCELLED"
    assert run["step_count"] == 1
    assert run["blocked_request_id"] is None
    assert attempts == 1
    transcript = store.list_run_messages(run_id)
    assert any("written" in message["content"] for message in transcript)


def test_cancel_paused_run_applies_immediately_without_worker_cycle(tmp_path: Path) -> None:
    state = tmp_path / "state.db"
    store = Store(state)
    tools = ToolRegistry(store)

    class NeverModel:
        def respond(self, *, messages, tools):
            raise AssertionError("paused run must not call model")

    engine = Engine(
        store=store,
        model=NeverModel(),
        tools=tools,
        context=ContextAssembler(store),
        system_prompt="Run.",
        worker_id="worker-1",
    )
    run_id = engine.submit_task("pause then cancel", set(), now=1.0)
    store.request_run_control(run_id, action="PAUSE", reason="hold", now=2.0)
    assert engine.run_once(now=3.0) == run_id
    assert store.get_run(run_id)["status"] == "PAUSED"

    store.request_run_control(run_id, action="CANCEL", reason="stop permanently", now=4.0)

    run = store.get_run(run_id)
    assert run["status"] == "CANCELLED"
    assert run["control_action"] is None
    assert run["cancelled_at"] == 4.0
    [event] = store.list_events(kind="run.step")
    assert event["status"] == "CANCELLED"
    assert store.pending_event_count() == 0


def test_cancel_after_tool_decision_persistence_still_prevents_handler_dispatch(
    tmp_path: Path,
) -> None:
    import threading

    state = tmp_path / "state.db"
    store = Store(state)
    tools = ToolRegistry(store)
    run_id_holder: dict[str, str] = {}
    assistant_persisted = threading.Event()
    cancel_written = threading.Event()
    handler_calls: list[int] = []

    tools.register(
        ToolSpec(
            name="read.value",
            description="read",
            input_schema={"type": "object"},
            capability="read",
            mutation=False,
        ),
        lambda args: handler_calls.append(1) or {"value": 1},
    )

    class ToolModel:
        def respond(self, *, messages, tools):
            return ModelResponse(
                tool_call=ToolCall(
                    request_id="cancel-before-dispatch",
                    name="read.value",
                    arguments={},
                )
            )

    original_record = store.record_run_message

    def signal_after_assistant_persistence(*, run_id, role, content, now, message_key=None):
        result = original_record(
            run_id=run_id,
            role=role,
            content=content,
            now=now,
            message_key=message_key,
        )
        if role == "assistant" and "cancel-before-dispatch" in content:
            assistant_persisted.set()
        return result

    store.record_run_message = signal_after_assistant_persistence  # type: ignore[method-assign]

    original_execute = tools.execute

    def wait_for_cancel_before_dispatch(**kwargs):
        assert cancel_written.wait(timeout=2.0)
        return original_execute(**kwargs)

    tools.execute = wait_for_cancel_before_dispatch  # type: ignore[method-assign]

    def cancel_after_persist() -> None:
        assert assistant_persisted.wait(timeout=2.0)
        operator = Store(state)
        try:
            operator.request_run_control(
                run_id_holder["run_id"],
                action="CANCEL",
                reason="cancel before handler dispatch",
                now=2.5,
            )
        finally:
            operator.close()
            cancel_written.set()

    engine = Engine(
        store=store,
        model=ToolModel(),
        tools=tools,
        context=ContextAssembler(store),
        system_prompt="Run.",
        worker_id="worker-1",
    )
    run_id = engine.submit_task("cancel after decision persistence", {"read"}, now=1.0)
    run_id_holder["run_id"] = run_id

    operator_thread = threading.Thread(target=cancel_after_persist)
    operator_thread.start()
    assert engine.run_once(now=2.0) == run_id
    operator_thread.join(timeout=3.0)

    assert not operator_thread.is_alive()
    assert handler_calls == []
    assert store.get_run(run_id)["status"] == "CANCELLED"


def test_resume_withdraws_pending_pause_before_worker_applies_it(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    run_id = store.create_run_with_initial_step(
        task="change mind before pause",
        capabilities=set(),
        now=1.0,
    )
    store.request_run_control(
        run_id,
        action="PAUSE",
        reason="hold",
        now=2.0,
    )
    assert store.get_run(run_id)["status"] == "RUNNING"
    assert store.get_run(run_id)["control_action"] == "PAUSE"

    store.resume_paused_run(
        run_id,
        reason="never mind",
        now=2.5,
    )

    run = store.get_run(run_id)
    assert run["status"] == "RUNNING"
    assert run["control_action"] is None
    assert run["control_reason"] is None
    assert store.pending_event_count() == 1
    assert store.list_journal(subject_id=run_id)[-1]["event_type"] == "RUN_RESUMED"


def test_resume_cannot_withdraw_pending_cancel(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    run_id = store.create_run_with_initial_step(
        task="cancel is terminal intent",
        capabilities=set(),
        now=1.0,
    )
    store.request_run_control(
        run_id,
        action="CANCEL",
        reason="stop",
        now=2.0,
    )

    with pytest.raises(RuntimeError, match="CANCEL"):
        store.resume_paused_run(
            run_id,
            reason="changed mind",
            now=2.5,
        )
