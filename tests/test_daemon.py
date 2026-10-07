from pathlib import Path

from pre_active.context import ContextAssembler
from pre_active.daemon import Daemon
from pre_active.engine import Engine, ModelResponse
from pre_active.scheduler import Scheduler
from pre_active.store import Store
from pre_active.tools import ToolRegistry


class FinalModel:
    def respond(self, *, messages, tools):
        return ModelResponse(final_text="scheduled complete")


def test_daemon_turns_due_schedule_into_completed_run(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    scheduler = Scheduler(store)
    engine = Engine(
        store=store,
        model=FinalModel(),
        tools=ToolRegistry(store),
        context=ContextAssembler(store),
        system_prompt="Run tasks.",
        worker_id="daemon-1",
    )
    daemon = Daemon(scheduler=scheduler, engine=engine)
    scheduler.add_interval(
        kind="task.requested",
        payload={"task": "scheduled work", "capabilities": []},
        every_seconds=60.0,
        first_at=10.0,
        now=1.0,
    )

    first = daemon.cycle(now=10.0)
    assert first.emitted_events == 1
    assert first.run_id is not None
    run_id = first.run_id
    assert store.get_run(run_id)["status"] == "RUNNING"

    second = daemon.cycle(now=11.0)
    assert second.emitted_events == 0
    assert second.run_id == run_id
    assert store.get_run(run_id)["status"] == "COMPLETED"


def test_run_forever_survives_retryable_cycle_exception(monkeypatch) -> None:
    daemon = Daemon(scheduler=object(), engine=object())  # type: ignore[arg-type]
    calls = 0

    def flaky_cycle(*, now: float):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("temporary provider outage")
        raise KeyboardInterrupt

    monkeypatch.setattr(daemon, "cycle", flaky_cycle)
    monkeypatch.setattr("pre_active.daemon.time.sleep", lambda _seconds: None)

    import pytest

    with pytest.raises(KeyboardInterrupt):
        daemon.run_forever(poll_seconds=0.01)

    assert calls == 2


def test_daemon_routes_due_schedule_through_volition_without_temporal_turn(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "state.db")
    scheduler = Scheduler(store)
    engine = Engine(
        store=store,
        model=FinalModel(),
        tools=ToolRegistry(store),
        context=ContextAssembler(store),
        system_prompt="Run tasks.",
        worker_id="daemon-volition-schedule",
    )
    daemon = Daemon(scheduler=scheduler, engine=engine)
    schedule_id = scheduler.add_volition_interval(
        config={
            "target": "scheduled-open-loop-review",
            "kind": "open_loop",
            "magnitude": 0.8,
            "confidence": 1.0,
            "provenance": "current_observation",
        },
        every_seconds=60.0,
        first_at=10.0,
        now=1.0,
    )

    first = daemon.cycle(now=10.0)
    assert first.emitted_events == 1
    assert first.run_id is None

    [signal] = store.list_events(kind="volition.signal")
    assert signal["status"] == "DONE"
    assert signal["payload"]["source"] == f"schedule:{schedule_id}"
    assert signal["payload"]["effect_authority"] is False

    [cognition] = store.list_events(kind="autonomous.turn")
    assert cognition["status"] == "PENDING"
    assert cognition["payload"]["source"] == "ENDOGENOUS"
    assert cognition["payload"]["capabilities"] == []
    assert cognition["payload"]["volition"]["effect_authority"] is False
    assert not any(
        event["payload"].get("source") == "TEMPORAL"
        for event in store.list_events(kind="autonomous.turn")
    )

    second = daemon.cycle(now=11.0)
    assert second.run_id is not None
    run_id = second.run_id
    run = store.get_run(run_id)
    assert run["status"] == "RUNNING"
    assert run["capabilities"] == set()

    third = daemon.cycle(now=12.0)
    assert third.run_id == run_id
    completed = store.get_run(run_id)
    assert completed["status"] == "COMPLETED"
    assert completed["final_text"] == "scheduled complete"
    assert completed["capabilities"] == set()
