import argparse
from pathlib import Path

from pre_active.context import ContextAssembler
from pre_active.daemon import Daemon
from pre_active.engine import Engine, ModelResponse
from pre_active.observers import Observation, ObserverManager
from pre_active.scheduler import Scheduler
from pre_active.store import Store
from pre_active.tools import ToolRegistry
from pre_active.cli import build_parser, main


def test_cli_exposes_observer_management_surface() -> None:
    parser = build_parser()
    subparsers = next(
        action
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    assert "observer" in subparsers.choices


def test_file_observer_baselines_then_emits_external_turn_on_change(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state.db"
    watched = tmp_path / "watched.txt"
    watched.write_text("alpha", encoding="utf-8")

    store = Store(state)
    observers = ObserverManager(store)
    observers.add_file(
        name="watched-file",
        path=str(watched),
        task="Inspect the monitored file change.",
        capabilities={"files.read"},
        every_seconds=10,
        now=0.0,
    )

    first = observers.tick(now=0.0)
    assert first.sampled == 1
    assert first.emitted == 0
    assert first.errors == 0
    assert store.list_events(kind="autonomous.turn") == []

    watched.write_text("TOP_SECRET_BETA", encoding="utf-8")
    assert observers.tick(now=5.0).sampled == 0

    changed = observers.tick(now=10.0)
    assert changed.sampled == 1
    assert changed.emitted == 1
    assert changed.errors == 0

    [event] = store.list_events(kind="autonomous.turn")
    assert event["payload"]["source"] == "EXTERNAL"
    assert "Observer watched-file detected change" in event["payload"]["reason"]
    assert "Observation evidence (read-only)" in event["payload"]["task"]
    assert "TOP_SECRET_BETA" not in event["payload"]["task"]
    assert event["payload"]["capabilities"] == ["files.read"]

    row = store.connection.execute(
        "SELECT priority FROM events WHERE id=?",
        (event["id"],),
    ).fetchone()
    assert row is not None and int(row["priority"]) == -10

    record = observers.get("watched-file")
    assert record["change_count"] == 1
    assert record["last_error"] is None
    assert record["last_evidence"]["sha256"]
    store.close()


def test_file_observer_can_explicitly_emit_initial_snapshot(tmp_path: Path) -> None:
    watched = tmp_path / "initial.txt"
    watched.write_text("initial", encoding="utf-8")
    store = Store(tmp_path / "state.db")
    observers = ObserverManager(store)
    observers.add_file(
        name="initial",
        path=str(watched),
        task="Review initial snapshot.",
        capabilities=set(),
        every_seconds=60,
        emit_initial=True,
        now=1.0,
    )

    result = observers.tick(now=1.0)
    assert result.emitted == 1
    [event] = store.list_events(kind="autonomous.turn")
    assert event["payload"]["source"] == "EXTERNAL"
    assert observers.get("initial")["change_count"] == 1
    store.close()


def test_probe_is_read_only_and_does_not_advance_baseline(tmp_path: Path) -> None:
    watched = tmp_path / "probe.txt"
    watched.write_text("probe", encoding="utf-8")
    store = Store(tmp_path / "state.db")
    observers = ObserverManager(store)
    observers.add_file(
        name="probe",
        path=str(watched),
        task="Probe only.",
        capabilities=set(),
        every_seconds=60,
        now=1.0,
    )

    before = observers.get("probe")
    observation = observers.probe("probe")
    after = observers.get("probe")

    assert observation.evidence["exists"] is True
    assert after["last_digest"] is None
    assert after["next_at"] == before["next_at"]
    assert store.list_events(kind="autonomous.turn") == []
    store.close()


def test_broken_observer_does_not_block_other_due_observers(tmp_path: Path) -> None:
    class Broken:
        def sample(self, config):  # type: ignore[no-untyped-def]
            raise RuntimeError("sensor offline")

    class Stable:
        def sample(self, config):  # type: ignore[no-untyped-def]
            return Observation(
                digest="stable-digest",
                summary="stable source changed",
                evidence={"value": 1},
            )

    store = Store(tmp_path / "state.db")
    observers = ObserverManager(
        store,
        adapters={"broken": Broken(), "stable": Stable()},
    )
    observers.add(
        name="broken",
        kind="broken",
        config={},
        task="Broken source.",
        capabilities=set(),
        every_seconds=10,
        emit_initial=True,
        now=0.0,
    )
    observers.add(
        name="stable",
        kind="stable",
        config={},
        task="Stable source.",
        capabilities=set(),
        every_seconds=10,
        emit_initial=True,
        now=0.0,
    )

    result = observers.tick(now=0.0)
    assert result.sampled == 2
    assert result.errors == 1
    assert result.emitted == 1
    assert "RuntimeError: sensor offline" == observers.get("broken")["last_error"]
    assert observers.get("broken")["consecutive_errors"] == 1
    [event] = store.list_events(kind="autonomous.turn")
    assert "Observer stable detected change" in event["payload"]["reason"]
    store.close()


class FinalModel:
    def respond(self, *, messages, tools):  # type: ignore[no-untyped-def]
        return ModelResponse(final_text="observer turn complete")


def test_daemon_consumes_observer_generated_turn_without_human_prompt(
    tmp_path: Path,
) -> None:
    watched = tmp_path / "daemon.txt"
    watched.write_text("ready", encoding="utf-8")
    store = Store(tmp_path / "state.db")
    observers = ObserverManager(store)
    observers.add_file(
        name="daemon-file",
        path=str(watched),
        task="Review this autonomous observation.",
        capabilities=set(),
        every_seconds=60,
        emit_initial=True,
        now=1.0,
    )
    daemon = Daemon(
        scheduler=Scheduler(store),
        observers=observers,
        engine=Engine(
            store=store,
            model=FinalModel(),
            tools=ToolRegistry(store),
            context=ContextAssembler(store),
            system_prompt="Run tasks.",
            worker_id="observer-daemon",
        ),
    )

    first = daemon.cycle(now=1.0)
    assert first.observer_samples == 1
    assert first.observer_events == 1
    assert first.emitted_events == 1
    assert first.run_id is not None
    assert store.get_run(first.run_id)["status"] == "RUNNING"

    second = daemon.cycle(now=2.0)
    assert second.observer_events == 0
    assert second.run_id == first.run_id
    assert store.get_run(first.run_id)["status"] == "COMPLETED"
    store.close()


def test_observer_cli_round_trip_and_status_snapshot(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    state = tmp_path / "state.db"
    watched = tmp_path / "cli.txt"
    watched.write_text("cli", encoding="utf-8")
    monkeypatch.setattr("pre_active.cli.time.time", lambda: 100.0)

    assert main([
        "--state", str(state),
        "observer", "add-file",
        "cli-file", str(watched),
        "Review CLI file changes.",
        "--every", "30",
        "--capability", "files.read",
    ]) == 0
    created = __import__("json").loads(capsys.readouterr().out)
    assert created["name"] == "cli-file"
    assert created["priority"] == -10
    assert created["capabilities"] == ["files.read"]

    assert main(["--state", str(state), "observer", "list"]) == 0
    listed = __import__("json").loads(capsys.readouterr().out)
    assert [item["name"] for item in listed["observers"]] == ["cli-file"]

    assert main(["--state", str(state), "status"]) == 0
    status = __import__("json").loads(capsys.readouterr().out)
    assert status["observers"] == {
        "enabled": 1,
        "due": 1,
        "with_errors": 0,
    }

    assert main(["--state", str(state), "observer", "disable", "cli-file"]) == 0
    disabled = __import__("json").loads(capsys.readouterr().out)
    assert disabled["enabled"] is False

    assert main(["--state", str(state), "observer", "remove", "cli-file"]) == 0
    removed = __import__("json").loads(capsys.readouterr().out)
    assert removed == {"name": "cli-file", "removed": True}
