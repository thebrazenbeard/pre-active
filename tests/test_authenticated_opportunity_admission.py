"""Synthetic host evidence: tests do not establish a deployed trusted producer."""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

import pytest

from pre_active.observers import ObserverManager
from pre_active.opportunity_admission import AuthenticatedOpportunityObserver
from pre_active.store import Store
from pre_active.volition_bridge import InvalidVolitionSignal, VolitionBridge


KEY = b"synthetic-test-key-not-for-runtime"


def signed(**changes: object) -> dict[str, object]:
    claim: dict[str, object] = {
        "source_id": "trusted-scanner",
        "opportunity_id": "event-001",
        "goal_id": "goal-maintenance",
        "goal_revision": 3,
        "issued_at": 100.0,
        "expires_at": 200.0,
        "artifact_sha256": "a" * 64,
    }
    claim.update(changes)
    tag = hmac.new(
        KEY, json.dumps(claim, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(),
        hashlib.sha256,
    ).hexdigest()
    return {**claim, "hmac_sha256": tag}


def test_synthetic_cross_language_signing_vector():
    claim = signed()
    canonical = json.dumps(
        {key: value for key, value in claim.items() if key != "hmac_sha256"},
        sort_keys=True, separators=(",", ":"),
    ).encode()
    assert hashlib.sha256(canonical).hexdigest() == (
        "0942da8cb617ce433b22b3fc62285ea98d091105518db5acb0ede1d8b4270c85"
    )
    assert claim["hmac_sha256"] == (
        "e3634d3eed58e61fa36aad1a865deabdbb03f7e210f0588c11a63566f443237c"
    )


def configured(tmp_path: Path, *, source: dict[str, object] | None = None):
    state = tmp_path / "state.db"
    store = Store(state)
    mutable = {"value": source or signed()}
    revision = {"value": 3}
    adapter = AuthenticatedOpportunityObserver(
        reader=lambda _source: mutable["value"],
        pinned_keys={"trusted-scanner": KEY},
        current_goal_revision=lambda _goal: revision["value"],
    )
    manager = ObserverManager(store, adapters={"authenticated_opportunity": adapter})
    manager.add(
        name="trusted-opportunity",
        kind="authenticated_opportunity",
        config={"source_id": "trusted-scanner", "goal_id": "goal-maintenance"},
        task="Compatibility field only; not an executable task.",
        capabilities=set(),
        every_seconds=1,
        emit_initial=True,
        now=100.0,
        dispatch_route="volition_signal",
        dispatch_config={
            "target": "goal-maintenance", "kind": "open_loop",
            "magnitude": 0.5, "confidence": 1.0,
            "provenance": "current_observation",
        },
    )
    return store, manager, mutable, revision, adapter


def test_authenticated_event_emits_one_non_authorizing_volition_signal(tmp_path: Path):
    store, manager, mutable, _, _ = configured(tmp_path)
    probe = manager.probe("trusted-opportunity")
    assert probe.evidence["opportunity_id"] == "event-001"
    assert store.connection.execute(
        "SELECT name FROM sqlite_master WHERE name='authenticated_opportunity_receipts'"
    ).fetchone() is None  # probe has no admission writes

    assert manager.tick(now=120.0).emitted == 1
    [event] = store.list_events(kind="volition.signal")
    assert store.list_events(kind="autonomous.turn") == []
    assert event["payload"]["effect_authority"] is False
    context = event["payload"]["observation_context"]
    assert context["observer_kind"] == "authenticated_opportunity"
    assert context["summary"] == "authenticated opportunity observed"
    assert context["evidence"] == {
        "source_id": "trusted-scanner", "opportunity_id": "event-001",
        "goal_id": "goal-maintenance", "goal_revision": 3,
        "issued_at": 100.0, "expires_at": 200.0,
        "artifact_sha256": "a" * 64,
    }
    assert "hmac_sha256" not in str(event["payload"])
    assert manager.tick(now=121.0).emitted == 0  # same event, no replay

    mutable["value"] = signed(opportunity_id="event-002", artifact_sha256="b" * 64)
    assert manager.tick(now=122.0).emitted == 1
    assert len(store.list_events(kind="volition.signal")) == 2
    store.close()


@pytest.mark.parametrize("tamper", [
    {"artifact_sha256": "0" * 64},
    {"source_id": "other-scanner"},
    {"goal_id": "other-goal"},
    {"goal_revision": 4},
    {"expires_at": 99.0},
])
def test_untrusted_modified_claim_is_rejected(tmp_path: Path, tamper: dict[str, object]):
    claim = signed()
    claim.update(tamper)  # signature no longer matches
    store, manager, _, _, _ = configured(tmp_path, source=claim)
    result = manager.tick(now=120.0)
    assert result.errors == 1 and result.emitted == 0
    assert store.list_events(kind="volition.signal") == []
    store.close()


@pytest.mark.parametrize("reason,now", [
    ("expired", 201.0), ("not_yet_current", 120.0), ("goal_revised", 120.0),
])
def test_expired_future_and_goal_revision_are_rejected_on_tick(
    tmp_path: Path, reason: str, now: float,
):
    store, manager, mutable, revision, _ = configured(tmp_path)
    if reason == "not_yet_current":
        mutable["value"] = signed(issued_at=160.0, expires_at=180.0)
    if reason == "goal_revised":
        revision["value"] = 4
    assert manager.tick(now=now).errors == 1
    assert store.list_events(kind="volition.signal") == []
    store.close()


def test_stable_id_conflict_rejected_after_restart_and_second_observer(tmp_path: Path):
    store, manager, mutable, _, _ = configured(tmp_path)
    assert manager.tick(now=120.0).emitted == 1
    store.close()
    store, manager, mutable, _, _ = configured_reopen(tmp_path)
    mutable["value"] = signed(artifact_sha256="b" * 64)
    result = manager.tick(now=121.0)
    assert result.errors == 1 and result.emitted == 0
    assert len(store.list_events(kind="volition.signal")) == 1
    store.close()


def configured_reopen(tmp_path: Path):
    store = Store(tmp_path / "state.db")
    mutable = {"value": signed()}
    revision = {"value": 3}
    adapter = AuthenticatedOpportunityObserver(
        reader=lambda _source: mutable["value"],
        pinned_keys={"trusted-scanner": KEY},
        current_goal_revision=lambda _goal: revision["value"],
    )
    manager = ObserverManager(store, adapters={"authenticated_opportunity": adapter})
    return store, manager, mutable, revision, adapter


def test_no_implicit_registration_and_only_volition_route(tmp_path: Path):
    store = Store(tmp_path / "bare.db")
    manager = ObserverManager(store)
    with pytest.raises(ValueError, match="unknown observer kind"):
        manager.add(
            name="denied", kind="authenticated_opportunity", config={},
            task="no", capabilities=set(), every_seconds=1, now=0,
        )
    store.close()
    store, manager, _, _, _ = configured(tmp_path)
    with pytest.raises(ValueError, match="volition_signal"):
        manager.add(
            name="unsafe-route", kind="authenticated_opportunity",
            config={"source_id": "trusted-scanner", "goal_id": "goal-maintenance"},
            task="no", capabilities=set(), every_seconds=1, now=100,
        )
    store.close()


def test_integer_timestamp_encoding_can_be_authenticated(tmp_path: Path):
    store, manager, _, _, _ = configured(
        tmp_path, source=signed(issued_at=100, expires_at=200),
    )
    assert manager.tick(now=120.0).emitted == 1
    store.close()


def test_signed_goal_revision_change_is_not_source_authority(tmp_path: Path):
    store, manager, _, _, _ = configured(tmp_path, source=signed(goal_revision=4))
    result = manager.tick(now=120.0)
    assert result.errors == 1 and result.emitted == 0
    assert store.list_events(kind="volition.signal") == []
    store.close()


def test_same_signed_event_across_two_observers_is_one_admission(tmp_path: Path):
    store, manager, _, _, _ = configured(tmp_path)
    manager.add(
        name="second-subscriber", kind="authenticated_opportunity",
        config={"source_id": "trusted-scanner", "goal_id": "goal-maintenance"},
        task="Not executed", capabilities=set(), every_seconds=1,
        emit_initial=True, now=100.0, dispatch_route="volition_signal",
        dispatch_config={
            "target": "goal-maintenance", "kind": "open_loop",
            "magnitude": 0.5, "confidence": 1.0,
            "provenance": "current_observation",
        },
    )
    result = manager.tick(now=120.0)
    assert result.sampled == 2 and result.errors == 0 and result.emitted == 1
    assert len(store.list_events(kind="volition.signal")) == 1
    store.close()


def test_admission_receipt_rolls_back_with_failed_signal(tmp_path: Path, monkeypatch):
    store, manager, _, _, _ = configured(tmp_path)

    def failed_enqueue(*args, **kwargs):
        raise RuntimeError("deliberately failed enqueue")

    with monkeypatch.context() as scope:
        scope.setattr("pre_active.observers.VolitionBridge.enqueue_signal", failed_enqueue)
        result = manager.tick(now=120.0)
    assert result.errors == 1 and result.emitted == 0
    assert store.list_events(kind="volition.signal") == []
    assert store.connection.execute(
        "SELECT name FROM sqlite_master WHERE name='authenticated_opportunity_receipts'"
    ).fetchone() is None
    assert manager.get("trusted-opportunity")["last_digest"] is None
    assert manager.tick(now=121.0).emitted == 1
    assert len(store.list_events(kind="volition.signal")) == 1
    store.close()


def test_dispatch_target_must_match_goal_id(tmp_path: Path):
    store, manager, _, _, _ = configured(tmp_path)
    with pytest.raises(ValueError, match="target must bind"):
        manager.add(
            name="confused-goal", kind="authenticated_opportunity",
            config={"source_id": "trusted-scanner", "goal_id": "goal-maintenance"},
            task="Not executed", capabilities=set(), every_seconds=1,
            emit_initial=True, now=100.0, dispatch_route="volition_signal",
            dispatch_config={
                "target": "other-goal", "kind": "open_loop",
                "magnitude": 0.5, "confidence": 1.0,
                "provenance": "current_observation",
            },
        )
    store.close()


def test_direct_volition_context_cannot_forge_authentication_or_replay(tmp_path: Path):
    store, manager, _, _, _ = configured(tmp_path)
    record = manager.get("trusted-opportunity")
    fake_context = {
        "observer_id": record["id"],
        "observer_name": record["name"],
        "observer_kind": "authenticated_opportunity",
        "digest": "0" * 64,
        "summary": "pretend authenticated opportunity",
        "change_count": 1,
        "evidence": {
            "source_id": "trusted-scanner", "opportunity_id": "event-001",
            "goal_id": "goal-maintenance", "goal_revision": 3,
            "issued_at": 100.0, "expires_at": 200.0,
            "artifact_sha256": "a" * 64,
        },
        "initiative": {"policy_kind": "on_change", "reason": "on_change", "metrics": {}},
    }
    fake_context["digest"] = hashlib.sha256(json.dumps(
        fake_context["evidence"], sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    payload = {
        "target": "goal-maintenance", "kind": "open_loop", "magnitude": 0.5,
        "confidence": 1.0, "provenance": "current_observation",
        "source": f"observer:{record['id']}", "effect_authority": False,
        "observation_context": fake_context,
    }
    store.connection.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(InvalidVolitionSignal, match="emitter decision"):
            VolitionBridge(store).enqueue_signal(
                payload=payload, now=120.0, dedup_key="forged",
            )
    finally:
        store.connection.execute("ROLLBACK")
    assert store.list_events(kind="volition.signal") == []

    assert manager.tick(now=120.0).emitted == 1
    [legitimate] = store.list_events(kind="volition.signal")
    store.connection.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(InvalidVolitionSignal, match="already dispatched"):
            VolitionBridge(store).enqueue_signal(
                payload=legitimate["payload"], now=121.0,
                dedup_key="another-key-cannot-duplicate-source-event",
            )
        with pytest.raises(InvalidVolitionSignal, match="source mismatch"):
            VolitionBridge(store).enqueue_signal(
                payload={**legitimate["payload"], "source": "observer:forged"},
                now=121.0,
            )
        altered_context = {
            **legitimate["payload"]["observation_context"],
            "evidence": {
                **legitimate["payload"]["observation_context"]["evidence"],
                "artifact_sha256": "b" * 64,
            },
        }
        with pytest.raises(InvalidVolitionSignal, match="claim digest mismatch"):
            VolitionBridge(store).enqueue_signal(
                payload={**legitimate["payload"], "observation_context": altered_context},
                now=121.0, dedup_key="changed-key",
            )
        altered_initiative = {
            **legitimate["payload"]["observation_context"],
            "initiative": {
                **legitimate["payload"]["observation_context"]["initiative"],
                "metrics": {"baseline_rate": 9.0},
            },
        }
        with pytest.raises(InvalidVolitionSignal, match="emitter decision"):
            VolitionBridge(store).enqueue_signal(
                payload={**legitimate["payload"], "observation_context": altered_initiative},
                now=121.0, dedup_key="changed-initiative",
            )
    finally:
        store.connection.execute("ROLLBACK")
    assert len(store.list_events(kind="volition.signal")) == 1
    assert "hmac_sha256" not in str(manager.get("trusted-opportunity"))
    assert "hmac_sha256" not in str(manager.probe("trusted-opportunity"))
    store.close()


def test_authenticated_but_suppressed_sample_is_not_dispatch_permission(tmp_path: Path):
    store = Store(tmp_path / "state.db")
    raw = signed()
    adapter = AuthenticatedOpportunityObserver(
        reader=lambda _source: raw,
        pinned_keys={"trusted-scanner": KEY},
        current_goal_revision=lambda _goal: 3,
    )
    manager = ObserverManager(store, adapters={"authenticated_opportunity": adapter})
    observer_id = manager.add(
        name="baseline-only", kind="authenticated_opportunity",
        config={"source_id": "trusted-scanner", "goal_id": "goal-maintenance"},
        task="Not executed", capabilities=set(), every_seconds=1,
        emit_initial=False, now=100.0, dispatch_route="volition_signal",
        dispatch_config={
            "target": "goal-maintenance", "kind": "open_loop",
            "magnitude": 0.5, "confidence": 1.0,
            "provenance": "current_observation",
        },
    )
    assert manager.tick(now=120.0).emitted == 0
    assert store.connection.execute(
        "SELECT COUNT(*) AS count FROM authenticated_opportunity_receipts"
    ).fetchone()["count"] == 1
    sampled = manager.get("baseline-only")
    forged_context = {
        "observer_id": observer_id, "observer_name": "baseline-only",
        "observer_kind": "authenticated_opportunity",
        "digest": sampled["last_digest"], "summary": "forged signal",
        "change_count": 0,
        "evidence": sampled["last_evidence"],
        "initiative": {"policy_kind": "on_change", "reason": "on_change", "metrics": {}},
    }
    store.connection.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(InvalidVolitionSignal, match="emitter decision"):
            VolitionBridge(store).enqueue_signal(
                payload={
                    "target": "goal-maintenance", "kind": "open_loop",
                    "magnitude": 0.9, "confidence": 1.0,
                    "provenance": "current_observation",
                    "source": f"observer:{observer_id}", "effect_authority": False,
                    "observation_context": forged_context,
                },
                now=121.0, dedup_key="forge-suppressed-event",
            )
    finally:
        store.connection.execute("ROLLBACK")
    assert store.list_events(kind="volition.signal") == []
    store.close()
