from __future__ import annotations

import time
from pathlib import Path

from pre_active.context import ContextAssembler
from pre_active.engine import (
    AUTONOMOUS_TURN_TOOL_NAME,
    Engine,
    ModelResponse,
    ToolCall,
)
from pre_active.store import Store
from pre_active.tools import ToolRegistry


def test_model_can_request_future_turn_and_reenter_without_human_prompt(
    tmp_path: Path,
) -> None:
    base = time.time()
    store = Store(tmp_path / "state.db")
    registry = ToolRegistry(store)

    class InitiativeModel:
        def __init__(self) -> None:
            self.calls = 0

        def respond(self, *, messages, tools):  # type: ignore[no-untyped-def]
            self.calls += 1
            assert any(tool["name"] == AUTONOMOUS_TURN_TOOL_NAME for tool in tools)
            if self.calls == 1:
                return ModelResponse(
                    tool_call=ToolCall(
                        request_id="self-turn-1",
                        name=AUTONOMOUS_TURN_TOOL_NAME,
                        arguments={
                            "reason": "reconsider after a quiet interval",
                            "delay_seconds": 10,
                        },
                    )
                )
            assert any(
                "status" in item["content"] and "scheduled" in item["content"]
                for item in messages
            )
            return ModelResponse(final_text="autonomous follow-up complete")

    model = InitiativeModel()
    engine = Engine(
        store=store,
        model=model,
        tools=registry,
        context=ContextAssembler(store),
        system_prompt="Use initiative when it is useful.",
        worker_id="worker-a",
        max_autonomous_turns_per_run=2,
    )
    run_id = engine.submit_task("Watch this problem over time.", set(), now=base)

    assert engine.run_once(now=base + 1) == run_id
    waiting = store.get_run(run_id)
    assert waiting["status"] == "WAITING"
    assert waiting["step_count"] == 1
    assert waiting["autonomous_turn_count"] == 1

    assert engine.run_once(now=base + 5) is None
    assert engine.run_once(now=base + 20) == run_id

    completed = store.get_run(run_id)
    assert completed["status"] == "COMPLETED"
    assert completed["final_text"] == "autonomous follow-up complete"
    assert model.calls == 2
    journal = store.list_journal(subject_id=run_id)
    assert any(item["event_type"] == "AUTONOMOUS_TURN_REQUESTED" for item in journal)
    assert any(item["event_type"] == "AUTONOMOUS_TURN_GRANTED" for item in journal)


def test_external_observer_can_grant_model_turn_without_user_prompt(
    tmp_path: Path,
) -> None:
    base = time.time()
    store = Store(tmp_path / "state.db")
    registry = ToolRegistry(store)
    observed_messages: list[list[dict[str, str]]] = []

    class ObserverModel:
        def respond(self, *, messages, tools):  # type: ignore[no-untyped-def]
            observed_messages.append(messages)
            return ModelResponse(final_text="observation reviewed")

    engine = Engine(
        store=store,
        model=ObserverModel(),
        tools=registry,
        context=ContextAssembler(store),
        system_prompt="Monitor authorized inputs.",
        worker_id="worker-a",
    )
    store.request_autonomous_turn(
        task="Inspect the changed CI state.",
        capabilities=set(),
        source="EXTERNAL",
        reason="A monitored pull request changed from green to red.",
        now=base,
        dedup_key="observer:pr-ci:change-1",
    )

    run_id = engine.run_once(now=base + 1)
    assert run_id is not None
    assert engine.run_once(now=base + 2) == run_id

    run = store.get_run(run_id)
    assert run["status"] == "COMPLETED"
    rendered = "\n".join(item["content"] for item in observed_messages[0])
    assert "No human prompt caused this turn" in rendered
    assert "Source: EXTERNAL" in rendered
    assert "monitored pull request changed" in rendered


def test_autonomous_turn_budget_stops_recursive_self_stimulation(
    tmp_path: Path,
) -> None:
    base = time.time()
    store = Store(tmp_path / "state.db")
    registry = ToolRegistry(store)

    class LoopingModel:
        def respond(self, *, messages, tools):  # type: ignore[no-untyped-def]
            return ModelResponse(
                tool_call=ToolCall(
                    request_id=f"loop-{len(store.list_run_messages(run_id))}",
                    name=AUTONOMOUS_TURN_TOOL_NAME,
                    arguments={"reason": "think again", "delay_seconds": 0},
                )
            )

    engine = Engine(
        store=store,
        model=LoopingModel(),
        tools=registry,
        context=ContextAssembler(store),
        system_prompt="Run.",
        worker_id="worker-a",
        max_autonomous_turns_per_run=1,
    )
    run_id = engine.submit_task("Do not recurse forever.", set(), now=base)

    assert engine.run_once(now=base + 1) == run_id
    assert store.get_run(run_id)["status"] == "WAITING"

    assert engine.run_once(now=base + 2) == run_id
    failed = store.get_run(run_id)
    assert failed["status"] == "FAILED"
    assert failed["last_error"] == "autonomous turn budget exceeded"
    assert failed["autonomous_turn_count"] == 1
    assert store.pending_event_count() == 0
    assert any(
        item["event_type"] == "AUTONOMOUS_TURN_DENIED"
        for item in store.list_journal(subject_id=run_id)
    )




def test_invalid_self_turn_request_fails_once_instead_of_retrying(
    tmp_path: Path,
) -> None:
    base = time.time()
    store = Store(tmp_path / "state.db")
    registry = ToolRegistry(store)

    class InvalidInitiativeModel:
        def respond(self, *, messages, tools):  # type: ignore[no-untyped-def]
            return ModelResponse(
                tool_call=ToolCall(
                    request_id="bad-self-turn",
                    name=AUTONOMOUS_TURN_TOOL_NAME,
                    arguments={"reason": "", "delay_seconds": -1},
                )
            )

    engine = Engine(
        store=store,
        model=InvalidInitiativeModel(),
        tools=registry,
        context=ContextAssembler(store),
        system_prompt="Run.",
        worker_id="worker-a",
    )
    run_id = engine.submit_task("Bad request should not churn.", set(), now=base)

    assert engine.run_once(now=base + 1) == run_id
    run = store.get_run(run_id)
    assert run["status"] == "FAILED"
    assert "invalid autonomous turn request" in run["last_error"]
    assert store.pending_event_count() == 0


def test_malformed_external_autonomous_event_is_terminally_rejected(
    tmp_path: Path,
) -> None:
    base = time.time()
    store = Store(tmp_path / "state.db")
    registry = ToolRegistry(store)

    class ShouldNotRun:
        def respond(self, *, messages, tools):  # type: ignore[no-untyped-def]
            raise AssertionError("malformed autonomous event must not reach model")

    engine = Engine(
        store=store,
        model=ShouldNotRun(),
        tools=registry,
        context=ContextAssembler(store),
        system_prompt="Run.",
        worker_id="worker-a",
    )
    event_id = store.enqueue_event(
        kind="autonomous.turn",
        payload={
            "task": "invalid autonomous event",
            "capabilities": [],
            "source": "MADE_UP",
            "reason": "",
        },
        dedup_key="malformed-autonomy",
        now=base,
    )

    assert engine.run_once(now=base + 1) is None
    [event] = store.list_events(kind="autonomous.turn")
    assert event["id"] == event_id
    assert event["status"] == "DONE"
    assert any(
        item["event_type"] == "EVENT_REJECTED"
        for item in store.list_journal(subject_id=event_id)
    )


def test_reserved_autonomy_tool_name_cannot_be_overridden(tmp_path: Path) -> None:
    from pre_active.tools import ToolSpec

    store = Store(tmp_path / "state.db")
    registry = ToolRegistry(store)
    registry.register(
        ToolSpec(
            name=AUTONOMOUS_TURN_TOOL_NAME,
            description="host collision",
            input_schema={"type": "object"},
            capability="host.override",
            mutation=False,
        ),
        lambda args: {},
    )

    class NeverCalled:
        def respond(self, *, messages, tools):  # type: ignore[no-untyped-def]
            raise AssertionError

    import pytest

    with pytest.raises(ValueError, match="reserved"):
        Engine(
            store=store,
            model=NeverCalled(),
            tools=registry,
            context=ContextAssembler(store),
            system_prompt="Run.",
            worker_id="worker-a",
        )



def test_waiting_autonomous_turn_can_be_paused_and_resumed_immediately(
    tmp_path: Path,
) -> None:
    base = time.time()
    store = Store(tmp_path / "state.db")
    registry = ToolRegistry(store)

    class PauseableModel:
        def __init__(self) -> None:
            self.calls = 0

        def respond(self, *, messages, tools):  # type: ignore[no-untyped-def]
            self.calls += 1
            if self.calls == 1:
                return ModelResponse(
                    tool_call=ToolCall(
                        request_id="pauseable-turn",
                        name=AUTONOMOUS_TURN_TOOL_NAME,
                        arguments={"reason": "check later", "delay_seconds": 3600},
                    )
                )
            return ModelResponse(final_text="resumed")

    model = PauseableModel()
    engine = Engine(
        store=store,
        model=model,
        tools=registry,
        context=ContextAssembler(store),
        system_prompt="Run.",
        worker_id="worker-a",
    )
    run_id = engine.submit_task("Wait safely.", set(), now=base)
    engine.run_once(now=base + 1)
    assert store.get_run(run_id)["status"] == "WAITING"

    store.request_run_control(run_id, action="PAUSE", reason="operator hold", now=base + 2)
    paused = store.get_run(run_id)
    assert paused["status"] == "PAUSED"
    [event] = [
        item
        for item in store.list_events(kind="run.step")
        if item["status"] == "PAUSED"
    ]
    assert event["payload"]["autonomous"] is True

    store.resume_paused_run(run_id, reason="continue", now=base + 3)
    assert store.get_run(run_id)["status"] == "RUNNING"
    assert engine.run_once(now=base + 4) == run_id
    assert store.get_run(run_id)["status"] == "COMPLETED"
    assert model.calls == 2


def test_waiting_autonomous_turn_can_be_cancelled_before_it_matures(
    tmp_path: Path,
) -> None:
    base = time.time()
    store = Store(tmp_path / "state.db")
    registry = ToolRegistry(store)

    class WaitingModel:
        def respond(self, *, messages, tools):  # type: ignore[no-untyped-def]
            return ModelResponse(
                tool_call=ToolCall(
                    request_id="cancel-wait",
                    name=AUTONOMOUS_TURN_TOOL_NAME,
                    arguments={"reason": "check tomorrow", "delay_seconds": 86400},
                )
            )

    engine = Engine(
        store=store,
        model=WaitingModel(),
        tools=registry,
        context=ContextAssembler(store),
        system_prompt="Run.",
        worker_id="worker-a",
    )
    run_id = engine.submit_task("Wait safely.", set(), now=base)
    engine.run_once(now=base + 1)
    assert store.get_run(run_id)["status"] == "WAITING"

    store.request_run_control(run_id, action="CANCEL", reason="stop", now=base + 2)
    cancelled = store.get_run(run_id)
    assert cancelled["status"] == "CANCELLED"
    assert store.pending_event_count() == 0
    [event] = [
        item
        for item in store.list_events(kind="run.step")
        if item["status"] == "CANCELLED"
    ]
    assert event["payload"]["autonomous"] is True


def test_standalone_autonomous_deferral_is_atomic_on_enqueue_failure(
    tmp_path: Path,
) -> None:
    base = time.time()
    store = Store(tmp_path / "state.db")
    run_id = store.create_run(task="atomic wait", capabilities=set(), now=base)

    original_enqueue = store.enqueue_event

    def fail_enqueue(**kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("injected enqueue failure")

    store.enqueue_event = fail_enqueue  # type: ignore[method-assign]

    import pytest

    with pytest.raises(RuntimeError, match="injected enqueue failure"):
        store.defer_run_for_autonomous_turn(
            run_id=run_id,
            expected_step=0,
            reason="try later",
            delay_seconds=10,
            priority=0,
            now=base + 1,
        )

    store.enqueue_event = original_enqueue  # type: ignore[method-assign]
    run = store.get_run(run_id)
    assert run["status"] == "RUNNING"
    assert run["step_count"] == 0
    assert run["autonomous_turn_count"] == 0
