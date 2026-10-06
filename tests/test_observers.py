import argparse
from pathlib import Path

import pytest

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



def test_hawkes_initiative_suppresses_first_change_then_emits_burst(
    tmp_path: Path,
) -> None:
    watched = tmp_path / "bursty.txt"
    watched.write_text("alpha", encoding="utf-8")
    store = Store(tmp_path / "state.db")
    observers = ObserverManager(store)
    observer_id = observers.add_file(
        name="bursty-file",
        path=str(watched),
        task="Review a meaningful burst of file changes.",
        capabilities={"files.read"},
        every_seconds=0.1,
        now=0.0,
        initiative_policy="hawkes_threshold",
        initiative_config={
            "baseline_rate": 0.1,
            "excitation": 0.4,
            "decay_rate": 1.0,
            "wake_threshold": 0.7,
            "cooldown_seconds": 0.0,
        },
    )

    baseline = observers.tick(now=0.0)
    assert baseline.sampled == 1
    assert baseline.emitted == 0
    assert store.list_events(kind="autonomous.turn") == []

    watched.write_text("beta", encoding="utf-8")
    first_change = observers.tick(now=0.1)
    assert first_change.sampled == 1
    assert first_change.emitted == 0
    assert store.list_events(kind="autonomous.turn") == []

    after_first = observers.get("bursty-file")
    assert after_first["change_count"] == 1
    assert after_first["initiative"]["policy_kind"] == "hawkes_threshold"
    assert after_first["initiative"]["state"]["excitation"] == pytest.approx(0.4)
    assert after_first["initiative"]["last_decision"]["reason"] == "below_threshold"

    watched.write_text("gamma", encoding="utf-8")
    second_change = observers.tick(now=0.2)
    assert second_change.sampled == 1
    assert second_change.emitted == 1

    [event] = store.list_events(kind="autonomous.turn")
    assert event["payload"]["source"] == "EXTERNAL"
    record = observers.get("bursty-file")
    assert record["change_count"] == 2
    assert record["initiative"]["state"]["excitation"] > 0.4
    assert record["initiative"]["last_decision"]["reason"] == "threshold_met"

    event_types = [
        item["event_type"]
        for item in store.list_journal(subject_id=observer_id)
    ]
    assert "OBSERVER_CHANGE_SUPPRESSED" in event_types
    assert "OBSERVER_CHANGE_DETECTED" in event_types
    store.close()



def test_observer_cli_configures_generic_initiative_policy(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    state = tmp_path / "initiative-cli.db"
    watched = tmp_path / "initiative-cli.txt"
    watched.write_text("alpha", encoding="utf-8")
    monkeypatch.setattr("pre_active.cli.time.time", lambda: 100.0)
    config_json = (
        '{"baseline_rate":0.1,"excitation":0.4,"decay_rate":1.0,'
        '"wake_threshold":0.7,"cooldown_seconds":2.0}'
    )

    assert main([
        "--state", str(state),
        "observer", "add-file",
        "initiative-cli", str(watched),
        "Review clustered changes.",
        "--every", "1",
        "--initiative-policy", "hawkes_threshold",
        "--initiative-config-json", config_json,
    ]) == 0
    created = __import__("json").loads(capsys.readouterr().out)
    assert created["initiative"]["policy_kind"] == "hawkes_threshold"
    assert created["initiative"]["config"]["wake_threshold"] == pytest.approx(0.7)

    with pytest.raises(
        SystemExit,
        match="--initiative-config-json must decode to a JSON object",
    ):
        main([
            "--state", str(state),
            "observer", "add-file",
            "bad-initiative", str(watched),
            "Reject invalid policy config.",
            "--every", "1",
            "--initiative-config-json", "[]",
        ])



def test_corrupt_initiative_policy_does_not_block_other_due_observers(
    tmp_path: Path,
) -> None:
    class Stable:
        def sample(self, config):  # type: ignore[no-untyped-def]
            value = config["value"]
            return Observation(
                digest=f"digest-{value}",
                summary=f"source {value} changed",
                evidence={"value": value},
            )

    store = Store(tmp_path / "state.db")
    observers = ObserverManager(store, adapters={"stable": Stable()})
    corrupt_id = observers.add(
        name="a-corrupt",
        kind="stable",
        config={"value": 1},
        task="Corrupt policy must fail closed.",
        capabilities=set(),
        every_seconds=10,
        emit_initial=True,
        now=0.0,
    )
    observers.add(
        name="z-stable",
        kind="stable",
        config={"value": 2},
        task="Stable policy should still run.",
        capabilities=set(),
        every_seconds=10,
        emit_initial=True,
        now=0.0,
    )
    store.connection.execute(
        "UPDATE observer_initiative SET policy_kind='missing-policy' "
        "WHERE observer_id=?",
        (corrupt_id,),
    )

    result = observers.tick(now=0.0)
    assert result.sampled == 2
    assert result.errors == 1
    assert result.emitted == 1
    assert observers.get("a-corrupt")["last_digest"] is None
    assert "unknown initiative policy" in observers.get("a-corrupt")["last_error"]
    [event] = store.list_events(kind="autonomous.turn")
    assert "Observer z-stable detected change" in event["payload"]["reason"]
    store.close()



def test_corrupt_initiative_json_does_not_block_other_due_observers(
    tmp_path: Path,
) -> None:
    class Stable:
        def sample(self, config):  # type: ignore[no-untyped-def]
            value = config["value"]
            return Observation(
                digest=f"digest-{value}",
                summary=f"source {value} changed",
                evidence={"value": value},
            )

    store = Store(tmp_path / "state.db")
    observers = ObserverManager(store, adapters={"stable": Stable()})
    corrupt_id = observers.add(
        name="a-corrupt-json",
        kind="stable",
        config={"value": 1},
        task="Corrupt initiative state must fail closed.",
        capabilities=set(),
        every_seconds=10,
        emit_initial=True,
        now=0.0,
    )
    observers.add(
        name="z-stable-json-peer",
        kind="stable",
        config={"value": 2},
        task="Stable policy should still run.",
        capabilities=set(),
        every_seconds=10,
        emit_initial=True,
        now=0.0,
    )
    store.connection.execute(
        "UPDATE observer_initiative SET state_json='{' WHERE observer_id=?",
        (corrupt_id,),
    )

    result = observers.tick(now=0.0)

    assert result.sampled == 2
    assert result.errors == 1
    assert result.emitted == 1
    assert "JSONDecodeError" in (
        store.connection.execute(
            "SELECT last_error FROM observers WHERE id=?",
            (corrupt_id,),
        ).fetchone()["last_error"]
    )
    [event] = store.list_events(kind="autonomous.turn")
    assert "Observer z-stable-json-peer detected change" in event["payload"]["reason"]
    store.close()


def _volition_dispatch_config(target: str = "investigate-dispatch") -> dict[str, object]:
    return {
        "target": target,
        "kind": "open_loop",
        "magnitude": 0.8,
        "confidence": 1.0,
        "provenance": "current_observation",
    }


def test_observer_dispatch_defaults_to_autonomous_turn(tmp_path: Path) -> None:
    watched = tmp_path / "dispatch-default.txt"
    watched.write_text("alpha", encoding="utf-8")
    store = Store(tmp_path / "state.db")
    observers = ObserverManager(store)
    observers.add_file(
        name="dispatch-default",
        path=str(watched),
        task="Review default route.",
        capabilities={"files.read"},
        every_seconds=10,
        now=0.0,
    )

    assert observers.get("dispatch-default")["dispatch"] == {
        "route_kind": "autonomous_turn",
        "config": {},
        "updated_at": 0.0,
    }
    store.close()


def test_observer_dispatch_backfills_existing_observer(tmp_path: Path) -> None:
    watched = tmp_path / "dispatch-backfill.txt"
    watched.write_text("alpha", encoding="utf-8")
    store = Store(tmp_path / "state.db")
    observers = ObserverManager(store)
    observers.add_file(
        name="dispatch-backfill",
        path=str(watched),
        task="Review backfilled route.",
        capabilities=set(),
        every_seconds=10,
        now=2.0,
    )

    store.connection.execute(
        "DELETE FROM observer_dispatch WHERE observer_id=?",
        (observers.get("dispatch-backfill")["id"],),
    )
    reloaded = ObserverManager(store)
    assert reloaded.get("dispatch-backfill")["dispatch"] == {
        "route_kind": "autonomous_turn",
        "config": {},
        "updated_at": 2.0,
    }
    store.close()


def test_observer_dispatch_validation_fails_closed_before_insert(tmp_path: Path) -> None:
    watched = tmp_path / "dispatch-invalid.txt"
    watched.write_text("alpha", encoding="utf-8")
    store = Store(tmp_path / "state.db")
    observers = ObserverManager(store)

    with pytest.raises(ValueError, match="unknown observer dispatch route"):
        observers.add_file(
            name="unknown-route",
            path=str(watched),
            task="Reject unknown route.",
            capabilities=set(),
            every_seconds=10,
            now=0.0,
            dispatch_route="missing",
        )

    with pytest.raises(ValueError, match="autonomous_turn dispatch config must be empty"):
        observers.add_file(
            name="direct-config",
            path=str(watched),
            task="Reject direct route config.",
            capabilities=set(),
            every_seconds=10,
            now=0.0,
            dispatch_config={"target": "not-used"},
        )

    with pytest.raises(ValueError, match="volition_signal observers cannot have capabilities"):
        observers.add_file(
            name="volition-capability",
            path=str(watched),
            task="Reject capability leakage.",
            capabilities={"files.read"},
            every_seconds=10,
            now=0.0,
            dispatch_route="volition_signal",
            dispatch_config=_volition_dispatch_config(),
        )

    assert observers.list() == []
    store.close()


@pytest.mark.parametrize("reserved_key", ["source", "effect_authority", "capabilities", "priority"])
def test_volition_dispatch_rejects_reserved_config_keys(
    tmp_path: Path,
    reserved_key: str,
) -> None:
    watched = tmp_path / f"dispatch-reserved-{reserved_key}.txt"
    watched.write_text("alpha", encoding="utf-8")
    store = Store(tmp_path / "state.db")
    observers = ObserverManager(store)
    config = _volition_dispatch_config()
    config[reserved_key] = "forbidden"

    with pytest.raises(ValueError, match=f"dispatch config cannot contain {reserved_key}"):
        observers.add_file(
            name=f"reserved-{reserved_key}",
            path=str(watched),
            task="Reject reserved route field.",
            capabilities=set(),
            every_seconds=10,
            now=0.0,
            dispatch_route="volition_signal",
            dispatch_config=config,
        )

    assert observers.list() == []
    store.close()


def test_volition_dispatch_persists_typed_config(tmp_path: Path) -> None:
    watched = tmp_path / "dispatch-volition.txt"
    watched.write_text("alpha", encoding="utf-8")
    store = Store(tmp_path / "state.db")
    observers = ObserverManager(store)
    config = _volition_dispatch_config()

    observers.add_file(
        name="dispatch-volition",
        path=str(watched),
        task="Compatibility-only task.",
        capabilities=set(),
        every_seconds=10,
        now=3.0,
        dispatch_route="volition_signal",
        dispatch_config=config,
    )

    assert observers.get("dispatch-volition")["dispatch"] == {
        "route_kind": "volition_signal",
        "config": config,
        "updated_at": 3.0,
    }
    store.close()


def test_due_claim_does_not_decode_corrupt_dispatch_json(tmp_path: Path) -> None:
    watched = tmp_path / "dispatch-claim.txt"
    watched.write_text("alpha", encoding="utf-8")
    store = Store(tmp_path / "state.db")
    observers = ObserverManager(store)
    observer_id = observers.add_file(
        name="dispatch-claim",
        path=str(watched),
        task="Claim without dispatch decode.",
        capabilities=set(),
        every_seconds=10,
        now=0.0,
    )
    store.connection.execute(
        "UPDATE observer_dispatch SET config_json='{' WHERE observer_id=?",
        (observer_id,),
    )

    claimed = observers._claim_due(observer_id, now=0.0)

    assert claimed is not None
    assert claimed["id"] == observer_id
    assert "dispatch" not in claimed
    store.close()


def test_default_dispatch_emits_only_external_autonomous_turn(tmp_path: Path) -> None:
    watched = tmp_path / "dispatch-direct.txt"
    watched.write_text("alpha", encoding="utf-8")
    store = Store(tmp_path / "state.db")
    observers = ObserverManager(store)
    observer_id = observers.add_file(
        name="dispatch-direct",
        path=str(watched),
        task="Review direct observer change.",
        capabilities={"files.read"},
        every_seconds=10,
        priority=-7,
        emit_initial=True,
        now=0.0,
    )

    result = observers.tick(now=0.0)

    assert result.emitted == 1
    [event] = store.list_events(kind="autonomous.turn")
    assert store.list_events(kind="volition.signal") == []
    assert event["payload"]["source"] == "EXTERNAL"
    assert event["payload"]["capabilities"] == ["files.read"]
    assert event["dedup_key"] == (
        f"observer:{observer_id}:change:1:{observers.get('dispatch-direct')['last_digest']}"
    )
    journal = [
        item for item in store.list_journal(subject_id=observer_id)
        if item["event_type"] == "OBSERVER_CHANGE_DETECTED"
    ]
    assert len(journal) == 1
    assert journal[0]["payload"]["dispatch_route"] == "autonomous_turn"
    assert journal[0]["payload"]["event_kind"] == "autonomous.turn"
    assert journal[0]["payload"]["event_id"] == event["id"]
    store.close()


def test_volition_dispatch_emits_one_signal_and_no_direct_turn(tmp_path: Path) -> None:
    watched = tmp_path / "dispatch-volition-route.txt"
    watched.write_text("alpha", encoding="utf-8")
    store = Store(tmp_path / "state.db")
    observers = ObserverManager(store)
    observer_id = observers.add_file(
        name="dispatch-volition-route",
        path=str(watched),
        task="Compatibility-only task.",
        capabilities=set(),
        every_seconds=10,
        priority=-99,
        emit_initial=True,
        now=0.0,
        dispatch_route="volition_signal",
        dispatch_config=_volition_dispatch_config("investigate-file-change"),
    )

    result = observers.tick(now=0.0)

    assert result.emitted == 1
    assert store.list_events(kind="autonomous.turn") == []
    [event] = store.list_events(kind="volition.signal")
    record = observers.get("dispatch-volition-route")
    assert event["dedup_key"] == (
        f"observer:{observer_id}:volition:1:{record['last_digest']}"
    )
    assert event["payload"]["target"] == "investigate-file-change"
    assert event["payload"]["kind"] == "open_loop"
    assert event["payload"]["magnitude"] == pytest.approx(0.8)
    assert event["payload"]["confidence"] == pytest.approx(1.0)
    assert event["payload"]["provenance"] == "current_observation"
    assert event["payload"]["source"] == f"observer:{observer_id}"
    assert event["payload"]["effect_authority"] is False
    context = event["payload"]["observation_context"]
    assert context["observer_id"] == observer_id
    assert context["observer_name"] == "dispatch-volition-route"
    assert context["observer_kind"] == "file"
    assert context["digest"] == record["last_digest"]
    assert context["summary"] == record["last_summary"]
    assert context["change_count"] == 1
    assert context["evidence"] == record["last_evidence"]
    assert context["initiative"] == {
        "policy_kind": "on_change",
        "reason": record["initiative"]["last_decision"]["reason"],
        "metrics": record["initiative"]["last_decision"]["metrics"],
    }
    journal = [
        item for item in store.list_journal(subject_id=observer_id)
        if item["event_type"] == "OBSERVER_CHANGE_DETECTED"
    ]
    assert len(journal) == 1
    assert journal[0]["payload"]["dispatch_route"] == "volition_signal"
    assert journal[0]["payload"]["event_kind"] == "volition.signal"
    assert journal[0]["payload"]["event_id"] == event["id"]
    store.close()


def test_volition_dispatch_enqueue_failure_rolls_back_observation_state(
    tmp_path: Path,
    monkeypatch,
) -> None:
    watched = tmp_path / "dispatch-rollback.txt"
    watched.write_text("alpha", encoding="utf-8")
    store = Store(tmp_path / "state.db")
    observers = ObserverManager(store)
    observers.add_file(
        name="dispatch-rollback",
        path=str(watched),
        task="Compatibility-only task.",
        capabilities=set(),
        every_seconds=10,
        emit_initial=True,
        now=0.0,
        dispatch_route="volition_signal",
        dispatch_config=_volition_dispatch_config("rollback-goal"),
    )

    def fail_enqueue(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("injected Volition enqueue failure")

    monkeypatch.setattr("pre_active.observers.VolitionBridge.enqueue_signal", fail_enqueue)

    result = observers.tick(now=0.0)

    assert result.sampled == 1
    assert result.emitted == 0
    assert result.errors == 1
    record = observers.get("dispatch-rollback")
    assert record["last_digest"] is None
    assert record["change_count"] == 0
    assert record["initiative"]["state"] == {}
    assert record["initiative"]["last_decision"] is None
    assert "injected Volition enqueue failure" in record["last_error"]
    assert store.list_events(kind="volition.signal") == []
    assert store.list_events(kind="autonomous.turn") == []
    assert not any(
        item["event_type"] == "OBSERVER_CHANGE_DETECTED"
        for item in store.list_journal(subject_id=record["id"])
    )
    store.close()


def test_corrupt_dispatch_does_not_block_healthy_due_observer(tmp_path: Path) -> None:
    class Stable:
        def sample(self, config):  # type: ignore[no-untyped-def]
            value = config["value"]
            return Observation(
                digest=f"digest-{value}",
                summary=f"source {value} changed",
                evidence={"value": value},
            )

    store = Store(tmp_path / "state.db")
    observers = ObserverManager(store, adapters={"stable": Stable()})
    corrupt_id = observers.add(
        name="a-corrupt-dispatch",
        kind="stable",
        config={"value": 1},
        task="Corrupt dispatch must fail closed.",
        capabilities=set(),
        every_seconds=10,
        emit_initial=True,
        now=0.0,
    )
    observers.add(
        name="z-healthy-dispatch",
        kind="stable",
        config={"value": 2},
        task="Healthy observer still emits.",
        capabilities=set(),
        every_seconds=10,
        emit_initial=True,
        now=0.0,
    )
    store.connection.execute(
        "UPDATE observer_dispatch SET config_json='{' WHERE observer_id=?",
        (corrupt_id,),
    )

    result = observers.tick(now=0.0)

    assert result.sampled == 2
    assert result.errors == 1
    assert result.emitted == 1
    corrupt = store.connection.execute(
        "SELECT last_digest, last_error FROM observers WHERE id=?",
        (corrupt_id,),
    ).fetchone()
    assert corrupt["last_digest"] is None
    assert "JSONDecodeError" in corrupt["last_error"]
    [event] = store.list_events(kind="autonomous.turn")
    assert "Observer z-healthy-dispatch detected change" in event["payload"]["reason"]
    store.close()


def test_observer_cli_configures_volition_dispatch_and_validates_json(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    state = tmp_path / "dispatch-cli.db"
    watched = tmp_path / "dispatch-cli.txt"
    watched.write_text("alpha", encoding="utf-8")
    monkeypatch.setattr("pre_active.cli.time.time", lambda: 100.0)

    assert main([
        "--state", str(state),
        "observer", "add-file",
        "dispatch-cli-default", str(watched),
        "Review default dispatch.",
        "--every", "30",
    ]) == 0
    default_created = __import__("json").loads(capsys.readouterr().out)
    assert default_created["dispatch"]["route_kind"] == "autonomous_turn"
    assert default_created["dispatch"]["config"] == {}

    config_json = __import__("json").dumps(_volition_dispatch_config("cli-volition"))
    assert main([
        "--state", str(state),
        "observer", "add-file",
        "dispatch-cli-volition", str(watched),
        "Compatibility-only task.",
        "--every", "30",
        "--dispatch-route", "volition_signal",
        "--dispatch-config-json", config_json,
    ]) == 0
    created = __import__("json").loads(capsys.readouterr().out)
    assert created["dispatch"]["route_kind"] == "volition_signal"
    assert created["dispatch"]["config"]["target"] == "cli-volition"

    with pytest.raises(SystemExit, match="--dispatch-config-json must be valid JSON"):
        main([
            "--state", str(state),
            "observer", "add-file",
            "dispatch-cli-bad-json", str(watched),
            "Reject malformed dispatch JSON.",
            "--every", "30",
            "--dispatch-route", "volition_signal",
            "--dispatch-config-json", "{",
        ])

    with pytest.raises(
        SystemExit,
        match="--dispatch-config-json must decode to a JSON object",
    ):
        main([
            "--state", str(state),
            "observer", "add-file",
            "dispatch-cli-bad-object", str(watched),
            "Reject non-object dispatch JSON.",
            "--every", "30",
            "--dispatch-route", "volition_signal",
            "--dispatch-config-json", "[]",
        ])


def test_daemon_routes_observer_through_volition_to_zero_capability_run(
    tmp_path: Path,
) -> None:
    watched = tmp_path / "volition-daemon.txt"
    watched.write_text("ready", encoding="utf-8")
    store = Store(tmp_path / "state.db")
    observers = ObserverManager(store)
    observer_id = observers.add_file(
        name="volition-daemon-file",
        path=str(watched),
        task="Compatibility-only task.",
        capabilities=set(),
        every_seconds=60,
        emit_initial=True,
        now=1.0,
        dispatch_route="volition_signal",
        dispatch_config=_volition_dispatch_config("investigate-daemon-file"),
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
            worker_id="volition-observer-daemon",
        ),
    )

    first = daemon.cycle(now=1.0)
    assert first.observer_samples == 1
    assert first.observer_events == 1
    assert first.run_id is None
    [signal] = store.list_events(kind="volition.signal")
    assert signal["status"] == "DONE"
    assert signal["payload"]["source"] == f"observer:{observer_id}"

    [cognition] = store.list_events(kind="autonomous.turn")
    assert cognition["status"] == "PENDING"
    assert cognition["payload"]["source"] == "ENDOGENOUS"
    assert cognition["payload"]["capabilities"] == []
    assert cognition["payload"]["volition"]["effect_authority"] is False
    assert store.list_events(kind="task.requested") == []

    second = daemon.cycle(now=2.0)
    assert second.observer_events == 0
    assert second.run_id is not None
    run_id = second.run_id
    run = store.get_run(run_id)
    assert run["capabilities"] == set()
    assert run["status"] == "RUNNING"
    assert "Observation context (read-only):" in run["task"]
    assert "volition-daemon-file" in run["task"]

    third = daemon.cycle(now=3.0)
    assert third.run_id == run_id
    completed = store.get_run(run_id)
    assert completed["status"] == "COMPLETED"
    assert completed["final_text"] == "observer turn complete"
    assert completed["capabilities"] == set()
    assert len(store.list_events(kind="autonomous.turn")) == 1
    store.close()
