from __future__ import annotations

import math
from typing import Any

from .store import Store


class InvalidVolitionSignal(ValueError):
    """Explicit input-contract violation for a volition.signal event."""


def _load_volition() -> tuple[Any, Any, Any, Any]:
    try:
        from volition import DriveKind, ProvenanceClass, Signal, VolitionEngine
    except ImportError as exc:
        raise RuntimeError(
            "Volition runtime is required to process volition.signal events"
        ) from exc
    return DriveKind, ProvenanceClass, Signal, VolitionEngine


class VolitionBridge:
    def __init__(self, store: Store) -> None:
        self.store = store

    def enqueue_signal(
        self,
        *,
        payload: dict[str, Any],
        now: float,
        dedup_key: str | None = None,
    ) -> str:
        self.parse_signal_payload(payload)
        normalized = dict(payload)
        normalized["effect_authority"] = False
        return self.store.enqueue_event(
            kind="volition.signal",
            payload=normalized,
            priority=0,
            dedup_key=dedup_key,
            now=now,
        )

    @staticmethod
    def _text(payload: dict[str, Any], key: str) -> str:
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip():
            raise InvalidVolitionSignal(f"volition signal {key} must be non-empty text")
        return value.strip()

    @staticmethod
    def _number(
        payload: dict[str, Any],
        key: str,
        *,
        default: float | None = None,
    ) -> float:
        value = payload.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise InvalidVolitionSignal(f"volition signal {key} must be numeric")
        number = float(value)
        if not math.isfinite(number):
            raise InvalidVolitionSignal(f"volition signal {key} must be finite")
        return number

    def parse_signal_payload(self, payload: dict[str, Any]) -> Any:
        if not isinstance(payload, dict):
            raise InvalidVolitionSignal("volition signal payload must be an object")
        if payload.get("effect_authority", False) is not False:
            raise InvalidVolitionSignal("volition signal cannot claim effect authority")

        DriveKind, ProvenanceClass, Signal, _ = _load_volition()
        target = self._text(payload, "target")
        source = self._text(payload, "source")

        try:
            kind = DriveKind(self._text(payload, "kind"))
        except ValueError as exc:
            raise InvalidVolitionSignal("volition signal kind is unsupported") from exc
        try:
            provenance = ProvenanceClass(self._text(payload, "provenance"))
        except ValueError as exc:
            raise InvalidVolitionSignal("volition signal provenance is unsupported") from exc

        current_reappraisal = payload.get("current_reappraisal", False)
        if not isinstance(current_reappraisal, bool):
            raise InvalidVolitionSignal("volition signal current_reappraisal must be boolean")

        return Signal(
            target=target,
            kind=kind,
            magnitude=self._number(payload, "magnitude"),
            confidence=self._number(payload, "confidence", default=1.0),
            provenance=provenance,
            source=source,
            expected_information_gain=self._number(
                payload, "expected_information_gain", default=0.0
            ),

            learning_progress=self._number(payload, "learning_progress", default=0.0),
            controllability=self._number(payload, "controllability", default=1.0),
            predicted_deficit_reduction=self._number(
                payload, "predicted_deficit_reduction", default=1.0
            ),
            current_reappraisal=current_reappraisal,
        )

    def process_signal_event(
        self,
        *,
        source_event_id: str,
        payload: dict[str, Any],
        now: float,
    ) -> dict[str, Any]:
        if not source_event_id:
            raise ValueError("source_event_id is required")
        if not self.store.connection.in_transaction:
            raise RuntimeError(
                "volition signal processing requires an active store transaction"
            )
        existing = self.store.get_volition_signal_receipt(source_event_id)
        if existing is not None:
            return existing

        signal = self.parse_signal_payload(payload)
        _, _, _, VolitionEngine = _load_volition()
        state = self.store.get_volition_state()

        if state is None:
            engine = VolitionEngine()
            expected_revision = 0
        else:
            engine = VolitionEngine.from_snapshot(state["snapshot"])
            expected_revision = int(state["revision"])

        goal = engine.tick([signal])
        request = engine.request_cognition()
        active_goal = engine.active_goal
        choice = None
        if active_goal is not None:
            choice = next(
                (
                    item
                    for item in engine.choices
                    if item.choice_id == active_goal.choice_id
                ),
                None,
            )

        if request is not None:
            if request.effect_authority is not False:
                raise RuntimeError("Volition cognition request claimed effect authority")
            if request.source != "ENDOGENOUS":
                raise RuntimeError("Volition cognition request source must be ENDOGENOUS")
            if active_goal is None:
                raise RuntimeError("Volition cognition request has no active goal")

            if request.goal_id != active_goal.goal_id or request.target != active_goal.target:
                raise RuntimeError("Volition cognition request does not match active goal")
            if not math.isfinite(float(request.urgency)):
                raise RuntimeError("Volition cognition request urgency must be finite")

        revision = self.store.save_volition_state(
            engine.snapshot(),
            expected_revision=expected_revision,
            now=now,
        )

        cognition_event_id: str | None = None
        if request is not None:
            volition_meta = {
                "goal_id": request.goal_id,
                "target": request.target,
                "urgency": float(request.urgency),
                "signal_event_id": source_event_id,
                "signal_provenance": signal.provenance.value,
                "signal_source": signal.source,
                "choice_id": active_goal.choice_id,
                "choice_class": choice.choice_class.value if choice else None,
                "choice_source": choice.source if choice else None,
                "effect_authority": False,
            }

            cognition_event_id = self.store.enqueue_event(
                kind="autonomous.turn",
                payload={
                    "task": (
                        "Continue bounded cognition on the active Volition goal. "
                        "Do not assume external effect authority.\n\n"
                        f"Goal: {request.target}"
                    ),
                    "capabilities": [],
                    "source": "ENDOGENOUS",
                    "reason": (
                        f"Volition requested cognition for {request.goal_id}: "
                        f"{request.target}"
                    ),
                    "volition": volition_meta,
                },
                priority=0,
                dedup_key=f"volition-cognition:{source_event_id}",
                now=now,
            )

        self.store.record_volition_signal_receipt(
            source_event_id=source_event_id,
            state_revision=revision,
            cognition_event_id=cognition_event_id,
            goal_id=active_goal.goal_id if active_goal else None,
            target=signal.target,
            urgency=float(request.urgency) if request is not None else None,

            provenance=signal.provenance.value,
            signal_source=signal.source,
            choice_id=active_goal.choice_id if active_goal else None,
            choice_class=choice.choice_class.value if choice else None,
            choice_source=choice.source if choice else None,
            now=now,
        )
        self.store.append_journal(
            event_type="VOLITION_SIGNAL_APPLIED",
            subject_id=source_event_id,
            payload={
                "state_revision": revision,
                "target": signal.target,
                "provenance": signal.provenance.value,
                "signal_source": signal.source,
                "cognition_event_id": cognition_event_id,
                "goal_id": active_goal.goal_id if active_goal else None,
                "effect_authority": False,
            },
            now=now,
        )
        if cognition_event_id is not None:
            self.store.append_journal(
                event_type="VOLITION_COGNITION_REQUESTED",
                subject_id=cognition_event_id,
                payload={
                    "source_event_id": source_event_id,

                    "goal_id": active_goal.goal_id,
                    "target": request.target,
                    "urgency": float(request.urgency),
                    "choice_id": active_goal.choice_id,
                    "choice_class": choice.choice_class.value if choice else None,
                    "choice_source": choice.source if choice else None,
                    "signal_provenance": signal.provenance.value,
                    "signal_source": signal.source,
                    "effect_authority": False,
                },
                now=now,
            )
        receipt = self.store.get_volition_signal_receipt(source_event_id)
        assert receipt is not None
        return receipt
