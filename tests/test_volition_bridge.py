from __future__ import annotations

from pathlib import Path
import json

import pytest

from meso_crct import (
    CircuitState,
    RewardState,
    SalienceState,
    TargetState,
    derive_action_tendency,
    propose_action_intent,
    select_target,
)

from pre_active.cli import main
from pre_active.context import ContextAssembler
from pre_active.engine import Engine, ModelResponse
from pre_active.store import Store
from pre_active.tools import ToolRegistry
from pre_active.volition_bridge import InvalidVolitionSignal, VolitionBridge
from volition import DriveKind, Policy, Signal, VolitionEngine


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


@pytest.mark.parametrize("corrupt", [float("inf"), float("-inf"), "invalid-clock"])
def test_existing_corrupt_volition_clock_fails_closed_without_rewriting_row(
    tmp_path: Path, corrupt: object
) -> None:
    store = Store(tmp_path / "state.db")
    store.save_volition_state(
        VolitionEngine().snapshot(), expected_revision=0, now=1.0
    )
    store.connection.execute(
        "UPDATE volition_state SET updated_at=? WHERE id=1", (corrupt,)
    )
    original = store.connection.execute(
        "SELECT revision, snapshot_json, updated_at FROM volition_state WHERE id=1"
    ).fetchone()

    with pytest.raises(RuntimeError, match="stored Volition updated_at is not finite"):
        store.get_volition_state()
    with pytest.raises(RuntimeError, match="stored Volition updated_at is not finite"):
        _apply_signal(
            store, VolitionBridge(store), source_event_id="valid-next-signal",
            payload=_open_loop_payload(), now=2.0,
        )

    restored = store.connection.execute(
        "SELECT revision, snapshot_json, updated_at FROM volition_state WHERE id=1"
    ).fetchone()
    assert tuple(original) == tuple(restored)
    assert store.get_volition_signal_receipt("valid-next-signal") is None
    assert store.list_events(kind="autonomous.turn") == []
    assert store.list_journal() == []
    store.close()


@pytest.mark.parametrize("persisted_clock", [500.0, 1e308])
def test_future_persisted_clock_beyond_rollback_budget_does_not_poison_next_signal(
    tmp_path: Path, persisted_clock: float
) -> None:
    store = Store(tmp_path / "state.db")
    store.save_volition_state(
        VolitionEngine().snapshot(), expected_revision=0, now=persisted_clock
    )
    original = store.connection.execute(
        "SELECT revision, snapshot_json, updated_at FROM volition_state WHERE id=1"
    ).fetchone()

    with pytest.raises(RuntimeError, match="clock rollback bound"):
        _apply_signal(
            store, VolitionBridge(store), source_event_id="future-poison",
            payload=_open_loop_payload(), now=2.0,
        )

    restored = store.connection.execute(
        "SELECT revision, snapshot_json, updated_at FROM volition_state WHERE id=1"
    ).fetchone()
    assert tuple(original) == tuple(restored)
    assert store.get_volition_signal_receipt("future-poison") is None
    assert store.list_events(kind="autonomous.turn") == []
    assert store.list_journal() == []
    store.close()


def test_clock_correction_at_bounded_rollback_limit_keeps_monotonic_anchor(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "state.db")
    store.save_volition_state(
        VolitionEngine().snapshot(), expected_revision=0, now=302.0
    )
    receipt = _apply_signal(
        store, VolitionBridge(store), source_event_id="allowed-backstep",
        payload=_open_loop_payload(), now=2.0,
    )
    assert receipt["source_event_id"] == "allowed-backstep"
    assert store.get_volition_state()["updated_at"] == pytest.approx(302.0)
    store.close()


@pytest.mark.parametrize("bad_now", [float("inf"), float("-inf"), float("nan"), True, "bad-clock"])
def test_direct_state_save_does_not_write_invalid_clock(
    tmp_path: Path, bad_now: object
) -> None:
    store = Store(tmp_path / "state.db")
    with pytest.raises(ValueError, match="Volition state now must be a finite timestamp"):
        store.save_volition_state(
            VolitionEngine().snapshot(), expected_revision=0, now=bad_now
        )
    assert store.get_volition_state() is None
    assert store.list_journal() == []
    store.close()


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


def _reappraised_snapshot(*, legacy_v2: bool) -> dict[str, object]:
    engine = VolitionEngine(Policy(goal_reappraisal_seconds=1.0))
    engine.tick([
        Signal(target="migration-rest", kind=DriveKind.OPEN_LOOP, magnitude=0.7)
    ])
    engine.advance(2.0)
    engine.tick([
        Signal(target="migration-rest", kind=DriveKind.OPEN_LOOP, magnitude=0.8)
    ])
    snapshot = engine.snapshot()
    assert snapshot["active_goal"]["revision"] == 2
    if legacy_v2:
        snapshot["schema"] = "VOLITION_STATE_V2"
        for event in snapshot["events"]:
            if event["kind"] == "GOAL_REAPPRAISED":
                event["details"] = [
                    (key, value) for key, value in event["details"]
                    if key != "score"
                ]
    return snapshot


def test_v3_reappraised_goal_roundtrip_through_durable_signal_bridge(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    store.save_volition_state(
        _reappraised_snapshot(legacy_v2=False), expected_revision=0, now=2.0
    )
    receipt = _apply_signal(
        store, VolitionBridge(store), source_event_id="v3-resume",
        payload=_open_loop_payload("migration-rest"), now=3.0,
    )
    assert receipt["state_revision"] == 2
    state = store.get_volition_state()
    assert state["snapshot"]["schema"] == "VOLITION_STATE_V3"
    restored = VolitionEngine.from_snapshot(state["snapshot"])
    assert restored.active_goal.revision == 3
    assert [
        detail for event in restored.events if event.kind == "GOAL_REAPPRAISED"
        for detail in event.details if detail[0] == "score"
    ]
    store.close()


def test_legacy_v2_unrevised_goal_migrates_only_after_valid_signal(tmp_path: Path) -> None:
    engine = VolitionEngine()
    engine.tick([
        Signal(target="migration-rest", kind=DriveKind.OPEN_LOOP, magnitude=0.7)
    ])
    snapshot = engine.snapshot()
    snapshot["schema"] = "VOLITION_STATE_V2"
    store = Store(tmp_path / "state.db")
    store.save_volition_state(snapshot, expected_revision=0, now=1.0)
    receipt = _apply_signal(
        store, VolitionBridge(store), source_event_id="v2-rev1-resume",
        payload=_open_loop_payload("migration-rest"), now=2.0,
    )
    assert receipt["state_revision"] == 2
    assert store.get_volition_state()["snapshot"]["schema"] == "VOLITION_STATE_V3"
    store.close()


def test_legacy_v2_inactive_state_migrates_on_next_signal(tmp_path: Path) -> None:
    snapshot = VolitionEngine().snapshot()
    snapshot["schema"] = "VOLITION_STATE_V2"
    assert snapshot["active_goal"] is None
    store = Store(tmp_path / "state.db")
    store.save_volition_state(snapshot, expected_revision=0, now=1.0)
    receipt = _apply_signal(
        store, VolitionBridge(store), source_event_id="v2-inactive-resume",
        payload=_open_loop_payload("migration-rest"), now=2.0,
    )
    assert receipt["state_revision"] == 2
    current = store.get_volition_state()["snapshot"]
    assert current["schema"] == "VOLITION_STATE_V3"
    assert VolitionEngine.from_snapshot(current).active_goal is not None
    store.close()


def test_legacy_v2_revised_goal_fails_without_overwriting_durable_state(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    store.save_volition_state(
        _reappraised_snapshot(legacy_v2=True), expected_revision=0, now=2.0
    )
    row_before = store.connection.execute(
        "SELECT revision, snapshot_json, updated_at FROM volition_state WHERE id=1"
    ).fetchone()
    with pytest.raises(ValueError, match="legacy V2 reappraisal score is unverifiable"):
        _apply_signal(
            store, VolitionBridge(store), source_event_id="legacy-unverified",
            payload=_open_loop_payload("migration-rest"), now=3.0,
        )
    row_after = store.connection.execute(
        "SELECT revision, snapshot_json, updated_at FROM volition_state WHERE id=1"
    ).fetchone()
    assert tuple(row_before) == tuple(row_after)
    assert store.get_volition_signal_receipt("legacy-unverified") is None
    assert store.list_events(kind="autonomous.turn") == []
    assert store.list_journal() == []
    store.close()


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


# MESO-CRCT → Volition → Pre-Active: explicit durable ingress.

def _meso_intent(*, kind: str = "approach"):
    if kind == "approach":
        state = CircuitState(salience=SalienceState(incentive_salience=0.9))
    elif kind == "inspect":
        state = CircuitState(salience=SalienceState(epistemic_value=0.9))
    elif kind == "withdraw":
        state = CircuitState(reward=RewardState(hazard=1.0, avoidance=1.0))
    elif kind == "hold":
        state = CircuitState()
    else:
        raise AssertionError(kind)
    selection = select_target([TargetState("meso-target", state)])
    return propose_action_intent(derive_action_tendency(selection))


class _MesoFinalModel:
    def respond(self, *, messages, tools):
        return ModelResponse(final_text="MESO_ZERO_CAPABILITIES_OK")


def _meso_engine(store: Store) -> Engine:
    return Engine(
        store=store,
        model=_MesoFinalModel(),
        tools=ToolRegistry(store),
        context=ContextAssembler(store),
        system_prompt="Run.",
        worker_id="meso-ingress-test",
    )


def test_real_meso_approach_survives_restart_and_produces_zero_capability_cognition(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "state.db"
    store = Store(state_path)
    proposal = _meso_intent()
    assert proposal.can_execute is False
    assert proposal.effect_authorized is False

    [signal_id] = VolitionBridge(store).enqueue_meso_intent(
        proposal=proposal, source_event_id="meso-cycle-1", now=1.0
    )
    [signal_event] = store.list_events(kind="volition.signal")
    assert signal_event["id"] == signal_id
    assert signal_event["payload"]["kind"] == "open_loop"
    assert signal_event["payload"]["provenance"] == "model_generated"
    assert signal_event["payload"]["source"].startswith("meso-crct:intent:approach:")
    assert signal_event["payload"]["meso_source_event_id"] == "meso-cycle-1"
    assert signal_event["payload"]["effect_authority"] is False

    store.close()
    store = Store(state_path)
    assert VolitionBridge(store).enqueue_meso_intent(
        proposal=proposal, source_event_id="meso-cycle-1", now=2.0
    ) == (signal_id,)
    assert len(store.list_events(kind="volition.signal")) == 1

    engine = _meso_engine(store)
    assert engine.run_once(now=3.0) is None
    [cognition] = store.list_events(kind="autonomous.turn")
    assert cognition["payload"]["source"] == "ENDOGENOUS"
    assert cognition["payload"]["capabilities"] == []
    assert cognition["payload"]["volition"]["effect_authority"] is False
    assert cognition["payload"]["volition"]["signal_provenance"] == "model_generated"
    assert cognition["payload"]["volition"]["signal_source"].startswith("meso-crct:intent:")

    run_id = engine.run_once(now=4.0)
    assert run_id is not None
    assert store.get_run(run_id)["capabilities"] == set()
    assert engine.run_once(now=5.0) == run_id
    assert store.get_run(run_id)["status"] == "COMPLETED"
    store.close()


def test_meso_hold_is_durable_no_signal_and_identity_cannot_change(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.db"
    store = Store(path)
    bridge = VolitionBridge(store)
    assert bridge.enqueue_meso_intent(
        proposal=_meso_intent(kind="hold"), source_event_id="meso-cycle-hold", now=1.0
    ) == ()
    store.close()

    store = Store(path)
    bridge = VolitionBridge(store)
    assert bridge.enqueue_meso_intent(
        proposal=_meso_intent(kind="hold"), source_event_id="meso-cycle-hold", now=2.0
    ) == ()
    with pytest.raises(RuntimeError, match="already bound"):
        bridge.enqueue_meso_intent(
            proposal=_meso_intent(), source_event_id="meso-cycle-hold", now=3.0
        )
    assert store.list_events(kind="volition.signal") == []
    assert store.get_volition_state() is None
    store.close()


@pytest.mark.parametrize("kind,expected", [
    ("inspect", "epistemic"),
    ("withdraw", "protection"),
])
def test_meso_other_typed_intents_enter_as_evidence_only(
    tmp_path: Path, kind: str, expected: str
) -> None:
    store = Store(tmp_path / "state.db")
    [signal_id] = VolitionBridge(store).enqueue_meso_intent(
        proposal=_meso_intent(kind=kind), source_event_id=f"meso-{kind}", now=1.0
    )
    [event] = store.list_events(kind="volition.signal")
    assert event["id"] == signal_id
    assert event["payload"]["kind"] == expected
    assert event["payload"]["provenance"] == "model_generated"
    assert event["payload"]["effect_authority"] is False
    assert "capabilities" not in event["payload"]
    store.close()


def test_meso_source_identity_is_mandatory_and_collision_is_rejected(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "state.db")
    bridge = VolitionBridge(store)
    for bad in ("", "   ", " leading", "trailing "):
        with pytest.raises(ValueError, match="source_event_id"):
            bridge.enqueue_meso_intent(proposal=_meso_intent(), source_event_id=bad, now=1.0)
    bridge.enqueue_meso_intent(
        proposal=_meso_intent(), source_event_id="stable-meso-id", now=1.0
    )
    with pytest.raises(RuntimeError, match="already bound"):
        bridge.enqueue_meso_intent(
            proposal=_meso_intent(kind="inspect"), source_event_id="stable-meso-id", now=2.0
        )
    assert len(store.list_events(kind="volition.signal")) == 1
    store.close()


@pytest.mark.parametrize("bad_now", [float("inf"), float("-inf"), float("nan"), True, "bad-clock"])
@pytest.mark.parametrize("kind", ["approach", "hold"])
def test_meso_ingress_rejects_invalid_clock_without_durable_writes(
    tmp_path: Path, bad_now: object, kind: str
) -> None:
    store = Store(tmp_path / "state.db")
    with pytest.raises(ValueError, match="now must be a finite timestamp"):
        VolitionBridge(store).enqueue_meso_intent(
            proposal=_meso_intent(kind=kind),
            source_event_id="meso-invalid-clock",
            now=bad_now,
        )
    assert store.get_meso_intent_ingress("meso-invalid-clock") is None
    assert store.list_events(kind="volition.signal") == []
    store.close()


@pytest.mark.parametrize("bad_now", [float("inf"), float("nan"), True])
def test_direct_volition_signal_rejects_invalid_clock_before_enqueue(
    tmp_path: Path, bad_now: object
) -> None:
    store = Store(tmp_path / "state.db")
    with pytest.raises(ValueError, match="now must be a finite timestamp"):
        VolitionBridge(store).enqueue_signal(
            payload=_open_loop_payload(), now=bad_now,
        )
    assert store.list_events(kind="volition.signal") == []
    store.close()


@pytest.mark.parametrize(
    "bad_now", [float("inf"), float("-inf"), float("nan"), True, "bad-clock"]
)
def test_direct_volition_signal_processing_rejects_invalid_clock_before_state_writes(
    tmp_path: Path, bad_now: object
) -> None:
    store = Store(tmp_path / "state.db")
    with pytest.raises(ValueError, match="now must be a finite timestamp"):
        _apply_signal(
            store, VolitionBridge(store), source_event_id="direct-invalid-clock",
            payload=_open_loop_payload(), now=bad_now,
        )
    assert store.get_volition_state() is None
    assert store.get_volition_signal_receipt("direct-invalid-clock") is None
    assert store.list_events(kind="autonomous.turn") == []
    assert store.list_journal() == []
    store.close()


def test_reject_forged_authorized_intent_without_writes(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    bridge = VolitionBridge(store)
    forged = _meso_intent()
    object.__setattr__(forged, "effect_authorized", True)
    with pytest.raises(ValueError, match="authorized"):
        bridge.enqueue_meso_intent(
            proposal=forged, source_event_id="tampered", now=1.0
        )
    with pytest.raises((TypeError, ValueError)):
        bridge.enqueue_meso_intent(
            proposal={
                "target_id": "meso-target",
                "kind": "approach",
                "strength": 0.9,
                "effect_authorized": False,
                "can_execute": False,
            },
            source_event_id="dict-spoof",
            now=1.0,
        )
    assert store.list_events(kind="volition.signal") == []
    assert store.connection.execute("SELECT COUNT(*) FROM meso_intent_ingress").fetchone()[0] == 0
    store.close()


def test_meso_ingress_rolls_back_signal_on_receipt_failure(
    tmp_path: Path, monkeypatch
) -> None:
    store = Store(tmp_path / "state.db")

    def fail_receipt(**kwargs):
        raise RuntimeError("receipt unavailable")

    monkeypatch.setattr(store, "record_meso_intent_ingress", fail_receipt)
    with pytest.raises(RuntimeError, match="receipt unavailable"):
        VolitionBridge(store).enqueue_meso_intent(
            proposal=_meso_intent(), source_event_id="rollback-meso", now=1.0
        )
    assert store.list_events(kind="volition.signal") == []
    assert store.get_meso_intent_ingress("rollback-meso") is None
    store.close()
