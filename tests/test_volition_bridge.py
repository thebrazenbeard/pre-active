from __future__ import annotations

from pathlib import Path
import json

import pytest

from pre_active.cli import main
from pre_active.context import ContextAssembler
from pre_active.engine import Engine, ModelResponse
from pre_active.store import Store
from pre_active.tools import ToolRegistry
from pre_active.volition_bridge import InvalidVolitionSignal, VolitionBridge
from volition import Policy, VolitionEngine


def test_volition_state_round_trip_uses_revision_cas(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")

    assert store.get_volition_state() is None

    revision = store.save_volition_state(
        {"schema": "VOLITION_STATE_V2", "active_goal": None},
        expected_revision=0,
        now=1.0,
    )
    assert revision == 1
    assert store.get_volition_state() == {
        "revision": 1,
        "snapshot": {"active_goal": None, "schema": "VOLITION_STATE_V2"},
        "updated_at": 1.0,
    }

    with pytest.raises(RuntimeError, match="volition state revision changed"):
        store.save_volition_state(
            {"schema": "VOLITION_STATE_V2", "active_goal": {"goal_id": "stale"}},
            expected_revision=0,
            now=2.0,
        )


def test_volition_signal_receipt_round_trip_is_unique_by_source_event(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")

    store.record_volition_signal_receipt(
        source_event_id="signal-event-1",
        state_revision=3,
        cognition_event_id="cognition-event-1",
        goal_id="goal-0001",
        target="investigate",
        urgency=0.75,
        provenance="current_observation",
        signal_source="observer:file",
        choice_id="choice-0001",
        choice_class="POLICY_DERIVED",
        choice_source="volition:policy",
        now=4.0,
    )

    assert store.get_volition_signal_receipt("signal-event-1") == {
        "source_event_id": "signal-event-1",
        "state_revision": 3,
        "cognition_event_id": "cognition-event-1",
        "goal_id": "goal-0001",
        "target": "investigate",
        "urgency": 0.75,
        "provenance": "current_observation",
        "signal_source": "observer:file",
        "choice_id": "choice-0001",
        "choice_class": "POLICY_DERIVED",
        "choice_source": "volition:policy",
        "created_at": 4.0,
    }

    with pytest.raises(RuntimeError, match="volition signal receipt already exists"):
        store.record_volition_signal_receipt(
            source_event_id="signal-event-1",
            state_revision=4,
            cognition_event_id=None,
            goal_id=None,
            target="different",
            urgency=None,
            provenance="inference",
            signal_source="different",
            choice_id=None,
            choice_class=None,
            choice_source=None,
            now=5.0,
        )


def _apply_signal(
    store: Store,
    bridge: VolitionBridge,
    *,
    source_event_id: str,
    payload: dict[str, object],
    now: float,
) -> dict[str, object]:
    store.connection.execute("BEGIN IMMEDIATE")
    try:
        receipt = bridge.process_signal_event(
            source_event_id=source_event_id,
            payload=payload,
            now=now,
        )
        store.connection.execute("COMMIT")
        return receipt
    except BaseException:
        store.connection.execute("ROLLBACK")
        raise


def _open_loop_payload(target: str = "investigate") -> dict[str, object]:
    return {
        "target": target,
        "kind": "open_loop",
        "magnitude": 0.8,
        "confidence": 1.0,
        "provenance": "current_observation",
        "source": "observer:file",
        "effect_authority": False,
    }


def test_bridge_enqueues_zero_capability_endogenous_turn_with_provenance(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "state.db")
    bridge = VolitionBridge(store)

    receipt = _apply_signal(
        store,
        bridge,
        source_event_id="signal-1",
        payload=_open_loop_payload(),
        now=1.0,
    )

    assert receipt["state_revision"] == 1
    assert receipt["goal_id"] == "goal-0001"
    assert receipt["choice_class"] == "POLICY_DERIVED"
    assert receipt["cognition_event_id"] is not None
    [event] = store.list_events(kind="autonomous.turn")
    assert event["id"] == receipt["cognition_event_id"]
    assert event["payload"]["source"] == "ENDOGENOUS"
    assert event["payload"]["capabilities"] == []
    assert event["payload"]["volition"]["effect_authority"] is False
    assert event["payload"]["volition"]["signal_provenance"] == "current_observation"
    assert event["payload"]["volition"]["signal_source"] == "observer:file"


def test_bridge_replay_does_not_advance_state_or_duplicate_cognition(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "state.db")
    bridge = VolitionBridge(store)
    payload = _open_loop_payload()

    first = _apply_signal(
        store, bridge, source_event_id="signal-1", payload=payload, now=1.0
    )
    repeated = _apply_signal(
        store, bridge, source_event_id="signal-1", payload=payload, now=2.0
    )

    assert repeated == first
    assert store.get_volition_state()["revision"] == 1
    assert len(store.list_events(kind="autonomous.turn")) == 1


def test_bridge_persists_volition_cognition_budget_across_signals(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    engine = VolitionEngine(Policy(endogenous_turn_budget=1))
    assert store.save_volition_state(engine.snapshot(), expected_revision=0, now=0.0) == 1
    bridge = VolitionBridge(store)

    first = _apply_signal(
        store, bridge, source_event_id="signal-1", payload=_open_loop_payload(), now=1.0
    )

    second = _apply_signal(
        store, bridge, source_event_id="signal-2", payload=_open_loop_payload(), now=2.0
    )

    assert first["cognition_event_id"] is not None
    assert second["cognition_event_id"] is None
    assert store.get_volition_state()["revision"] == 3
    assert len(store.list_events(kind="autonomous.turn")) == 1


def test_bridge_rejects_effect_authority_claim_before_state_change(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    bridge = VolitionBridge(store)
    payload = _open_loop_payload()
    payload["effect_authority"] = True

    store.connection.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(ValueError, match="effect authority"):
            bridge.process_signal_event(
                source_event_id="signal-authority",
                payload=payload,
                now=1.0,
            )
    finally:
        store.connection.execute("ROLLBACK")

    assert store.get_volition_state() is None
    assert store.list_events(kind="autonomous.turn") == []


class _FinalModel:
    def respond(self, *, messages, tools):
        return ModelResponse(final_text="VOLITION_ENGINE_OK")


def _engine(store: Store) -> Engine:
    return Engine(
        store=store,
        model=_FinalModel(),
        tools=ToolRegistry(store),
        context=ContextAssembler(store),
        system_prompt="Run.",
        worker_id="volition-bridge-test",
    )


def test_engine_consumes_volition_signal_then_grants_endogenous_run(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    bridge = VolitionBridge(store)
    signal_event_id = bridge.enqueue_signal(
        payload=_open_loop_payload(),
        now=1.0,
        dedup_key="volition-signal:engine",
    )
    engine = _engine(store)

    assert engine.run_once(now=2.0) is None
    [signal_event] = store.list_events(kind="volition.signal")
    assert signal_event["id"] == signal_event_id
    assert signal_event["status"] == "DONE"

    [cognition] = store.list_events(kind="autonomous.turn")
    assert cognition["status"] == "PENDING"
    assert cognition["payload"]["source"] == "ENDOGENOUS"
    assert cognition["payload"]["capabilities"] == []

    run_id = engine.run_once(now=3.0)
    assert run_id is not None
    run = store.get_run(run_id)
    assert run["capabilities"] == set()
    assert "Source: ENDOGENOUS" in run["task"]
    assert "Volition requested cognition" in run["task"]

    assert engine.run_once(now=4.0) == run_id
    completed = store.get_run(run_id)
    assert completed["status"] == "COMPLETED"
    assert completed["final_text"] == "VOLITION_ENGINE_OK"


def test_engine_rejects_malformed_volition_signal_without_retry(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    event_id = store.enqueue_event(
        kind="volition.signal",
        payload={**_open_loop_payload(), "effect_authority": True},
        dedup_key="volition-signal:invalid",
        now=1.0,
    )
    engine = _engine(store)

    assert engine.run_once(now=2.0) is None
    [event] = store.list_events(kind="volition.signal")
    assert event["id"] == event_id
    assert event["status"] == "DONE"
    assert store.get_volition_state() is None
    assert store.list_events(kind="autonomous.turn") == []
    journal = store.list_journal(subject_id=event_id)
    assert any(item["event_type"] == "EVENT_REJECTED" for item in journal)


def test_cli_enqueues_typed_volition_signal_idempotently(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    state = tmp_path / "state.db"
    monkeypatch.setattr("pre_active.cli.time.time", lambda: 10.0)
    args = [
        "--state", str(state),
        "volition-signal", "investigate",
        "--kind", "open_loop",
        "--magnitude", "0.8",
        "--source", "observer:file",
        "--provenance", "current_observation",
        "--dedup-key", "cli-signal-1",
    ]

    assert main(args) == 0
    first = json.loads(capsys.readouterr().out)
    assert main(args) == 0
    second = json.loads(capsys.readouterr().out)
    assert first["event_id"] == second["event_id"]

    store = Store(state)
    [event] = store.list_events(kind="volition.signal")
    assert event["payload"]["target"] == "investigate"
    assert event["payload"]["kind"] == "open_loop"
    assert event["payload"]["effect_authority"] is False
    store.close()


def test_engine_retries_volition_execution_failure_immediately(
    tmp_path: Path, monkeypatch
) -> None:
    store = Store(tmp_path / "state.db")
    event_id = store.enqueue_event(
        kind="volition.signal",
        payload=_open_loop_payload(),
        dedup_key="volition-signal:runtime-failure",
        now=1.0,
    )
    engine = _engine(store)

    def unavailable():
        raise RuntimeError("volition unavailable")

    monkeypatch.setattr("pre_active.volition_bridge._load_volition", unavailable)

    with pytest.raises(RuntimeError, match="volition unavailable"):
        engine.run_once(now=2.0)

    [event] = store.list_events(kind="volition.signal")
    assert event["id"] == event_id
    assert event["status"] == "PENDING"
    assert event["attempts"] == 1
    assert "volition unavailable" in event["last_error"]


def test_engine_retries_corrupt_persisted_volition_state_instead_of_rejecting_signal(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "state.db")
    store.save_volition_state(
        {"schema": "BROKEN_VOLITION_STATE"},
        expected_revision=0,
        now=0.0,
    )
    event_id = store.enqueue_event(
        kind="volition.signal",
        payload=_open_loop_payload(),
        dedup_key="volition-signal:corrupt-state",
        now=1.0,
    )
    engine = _engine(store)

    with pytest.raises(ValueError, match="unsupported or missing Volition state schema"):
        engine.run_once(now=2.0)

    [event] = store.list_events(kind="volition.signal")
    assert event["id"] == event_id
    assert event["status"] == "PENDING"
    assert event["attempts"] == 1
    assert "unsupported or missing Volition state schema" in event["last_error"]
    assert not any(
        item["event_type"] == "EVENT_REJECTED"
        for item in store.list_journal(subject_id=event_id)
    )


def test_bridge_advances_persisted_clock_and_renews_budget_after_reappraisal(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "state.db")
    bridge = VolitionBridge(store)
    payload = _open_loop_payload("reappraise-me")

    for index, now in enumerate((1.0, 2.0, 3.0), start=1):
        receipt = _apply_signal(
            store,
            bridge,
            source_event_id=f"signal-{index}",
            payload=payload,
            now=now,
        )
        assert receipt["cognition_event_id"] is not None

    exhausted = store.get_volition_state()
    assert exhausted is not None
    assert exhausted["snapshot"]["endogenous_turns"] == 3
    assert exhausted["snapshot"]["elapsed_seconds"] == pytest.approx(2.0)
    assert exhausted["snapshot"]["active_goal"]["revision"] == 1

    renewed = _apply_signal(
        store,
        bridge,
        source_event_id="signal-after-horizon",
        payload=payload,
        now=21603.0,
    )

    assert renewed["cognition_event_id"] is not None
    restored = store.get_volition_state()
    assert restored is not None
    assert restored["snapshot"]["elapsed_seconds"] == pytest.approx(21602.0)
    assert restored["snapshot"]["active_goal"]["revision"] == 2
    assert restored["snapshot"]["endogenous_turns"] == 1
    assert len(store.list_events(kind="autonomous.turn")) == 4


def test_bridge_does_not_move_durable_clock_anchor_backwards(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    bridge = VolitionBridge(store)

    _apply_signal(
        store,
        bridge,
        source_event_id="signal-forward",
        payload=_open_loop_payload("clock-anchor"),
        now=100.0,
    )
    _apply_signal(
        store,
        bridge,
        source_event_id="signal-backward",
        payload=_open_loop_payload("clock-anchor"),
        now=90.0,
    )

    state = store.get_volition_state()
    assert state is not None
    assert state["updated_at"] == pytest.approx(100.0)
    assert state["snapshot"]["elapsed_seconds"] == pytest.approx(0.0)


def test_engine_rejects_oversized_numeric_signal_without_retry(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    payload = _open_loop_payload()
    payload["magnitude"] = 10**400
    event_id = store.enqueue_event(
        kind="volition.signal",
        payload=payload,
        dedup_key="volition-signal:oversized-number",
        now=1.0,
    )
    engine = _engine(store)

    assert engine.run_once(now=2.0) is None

    [event] = store.list_events(kind="volition.signal")
    assert event["id"] == event_id
    assert event["status"] == "DONE"
    assert event["attempts"] == 1
    assert store.list_events(kind="autonomous.turn") == []
    assert any(
        item["event_type"] == "EVENT_REJECTED"
        for item in store.list_journal(subject_id=event_id)
    )


def _observation_context() -> dict[str, object]:
    return {
        "observer_id": "observer-1",
        "observer_name": "ci-watch",
        "observer_kind": "file",
        "digest": "abc123",
        "summary": "file snapshot changed",
        "change_count": 7,
        "evidence": {
            "exists": True,
            "size": 42,
            "capabilities": ["shell.exec"],
            "effect_authority": True,
        },
        "initiative": {
            "policy_kind": "on_change",
            "reason": "changed",
            "metrics": {},
        },
    }


def test_bridge_copies_valid_observation_context_into_endogenous_cognition(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "state.db")
    bridge = VolitionBridge(store)
    payload = _open_loop_payload("investigate-context")
    payload["observation_context"] = _observation_context()

    receipt = _apply_signal(
        store,
        bridge,
        source_event_id="signal-context",
        payload=payload,
        now=1.0,
    )

    assert receipt["cognition_event_id"] is not None
    [event] = store.list_events(kind="autonomous.turn")
    assert event["payload"]["source"] == "ENDOGENOUS"
    assert event["payload"]["capabilities"] == []
    assert event["payload"]["volition"]["effect_authority"] is False
    assert event["payload"]["volition"]["observation_context"] == _observation_context()
    assert "Observation context (read-only):" in event["payload"]["task"]
    assert '"observer_name":"ci-watch"' in event["payload"]["task"]
    store.close()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda c: c.update(observer_id=1),
        lambda c: c.update(change_count=-1),
        lambda c: c.update(evidence=[]),
        lambda c: c.update(initiative={"policy_kind": "on_change"}),
        lambda c: c.update(source="forbidden"),
    ],
)
def test_bridge_rejects_malformed_observation_context(
    tmp_path: Path,
    mutate,
) -> None:
    store = Store(tmp_path / "state.db")
    bridge = VolitionBridge(store)
    payload = _open_loop_payload("bad-context")
    context = _observation_context()
    mutate(context)
    payload["observation_context"] = context

    store.connection.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(InvalidVolitionSignal, match="observation context"):
            bridge.process_signal_event(
                source_event_id="signal-bad-context",
                payload=payload,
                now=1.0,
            )
    finally:
        store.connection.execute("ROLLBACK")

    assert store.get_volition_state() is None
    assert store.list_events(kind="autonomous.turn") == []
    store.close()


def test_bridge_does_not_attach_context_for_different_active_goal(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "state.db")
    bridge = VolitionBridge(store)

    first_payload = _open_loop_payload("goal-A")
    first_payload["magnitude"] = 0.9
    first_context = _observation_context()
    first_context["observer_id"] = "observer-A"
    first_context["observer_name"] = "observer-A"
    first_context["digest"] = "digest-A"
    first_context["summary"] = "Only goal A changed"
    first_payload["observation_context"] = first_context

    first = _apply_signal(
        store,
        bridge,
        source_event_id="signal-A",
        payload=first_payload,
        now=1.0,
    )
    assert first["cognition_event_id"] is not None

    second_payload = _open_loop_payload("goal-B")
    second_payload["magnitude"] = 0.1
    second_context = _observation_context()
    second_context["observer_id"] = "observer-B"
    second_context["observer_name"] = "observer-B"
    second_context["digest"] = "digest-B"
    second_context["summary"] = "Only goal B changed"
    second_payload["observation_context"] = second_context

    second = _apply_signal(
        store,
        bridge,
        source_event_id="signal-B",
        payload=second_payload,
        now=2.0,
    )

    assert second["cognition_event_id"] is not None
    event = next(
        item
        for item in store.list_events(kind="autonomous.turn")
        if item["id"] == second["cognition_event_id"]
    )
    assert event["payload"]["volition"]["target"] == "goal-A"
    assert event["payload"]["volition"]["signal_event_id"] == "signal-B"
    assert "observation_context" not in event["payload"]["volition"]
    assert "Observation context (read-only):" not in event["payload"]["task"]
    assert "observer-B" not in event["payload"]["task"]
    store.close()


def _static_signal_config(target: str = "scheduled-goal") -> dict[str, object]:
    return {
        "target": target,
        "kind": "open_loop",
        "magnitude": 0.8,
        "confidence": 1.0,
        "provenance": "current_observation",
    }


def test_static_signal_config_validation_derives_authority_sensitive_fields() -> None:
    config = {
        **_static_signal_config(),
        "expected_information_gain": 0.4,
        "learning_progress": 0.2,
        "controllability": 0.9,
        "predicted_deficit_reduction": 0.7,
        "current_reappraisal": True,
    }

    payload = VolitionBridge.validate_static_signal_config(
        config,
        source="schedule:schedule-1",
    )

    assert payload == {
        **config,
        "source": "schedule:schedule-1",
        "effect_authority": False,
    }


@pytest.mark.parametrize(
    "bad_key",
    [
        "source",
        "effect_authority",
        "capabilities",
        "priority",
        "observation_context",
        "unknown_future_field",
    ],
)
def test_static_signal_config_validation_rejects_runtime_or_unknown_fields(
    bad_key: str,
) -> None:
    config = _static_signal_config()
    config[bad_key] = "forbidden"

    with pytest.raises(ValueError, match="static signal config contains unsupported field"):
        VolitionBridge.validate_static_signal_config(
            config,
            source="schedule:schedule-1",
        )


@pytest.mark.parametrize(
    "required_key",
    ["target", "kind", "magnitude", "confidence", "provenance"],
)
def test_static_signal_config_validation_requires_complete_motive_config(
    required_key: str,
) -> None:
    config = _static_signal_config()
    del config[required_key]

    with pytest.raises(
        ValueError,
        match=f"static signal config missing required field: {required_key}",
    ):
        VolitionBridge.validate_static_signal_config(
            config,
            source="schedule:schedule-1",
        )


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("kind", "not-a-drive", "kind is unsupported"),
        ("provenance", "not-provenance", "provenance is unsupported"),
        ("magnitude", float("nan"), "magnitude must be finite"),
        ("confidence", True, "confidence must be numeric"),
    ],
)
def test_static_signal_config_validation_reuses_signal_semantics(
    key: str,
    value: object,
    message: str,
) -> None:
    config = _static_signal_config()
    config[key] = value

    with pytest.raises(InvalidVolitionSignal, match=message):
        VolitionBridge.validate_static_signal_config(
            config,
            source="schedule:schedule-1",
        )
