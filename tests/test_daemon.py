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
