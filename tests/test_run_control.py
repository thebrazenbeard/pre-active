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
