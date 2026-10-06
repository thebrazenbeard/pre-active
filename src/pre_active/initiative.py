from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Mapping, Protocol


@dataclass(frozen=True)
class InitiativeDecision:
    emit: bool
    state: dict[str, Any]
    metrics: dict[str, float]
    reason: str


class InitiativePolicy(Protocol):
    def decide(
        self,
        *,
        now: float,
        state: Mapping[str, Any],
    ) -> InitiativeDecision: ...


def _finite_number(value: Any, *, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be numeric")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number


def _required_config_number(
    config: Mapping[str, Any],
    name: str,
) -> float:
    if name not in config:
        raise ValueError(f"hawkes_threshold requires {name}")
    return _finite_number(config[name], name=name)


class OnChangePolicy:
    def decide(
        self,
        *,
        now: float,
        state: Mapping[str, Any],
    ) -> InitiativeDecision:
        _finite_number(now, name="now")
        if state:
            raise ValueError("on_change initiative state must be empty")
        return InitiativeDecision(
            emit=True,
            state={},
            metrics={},
            reason="on_change",
        )


class ExponentialHawkesThresholdPolicy:
    _STATE_KEYS = {"excitation", "last_event_at", "last_emit_at"}

    def __init__(
        self,
        *,
        baseline_rate: float,
        excitation: float,
        decay_rate: float,
        wake_threshold: float,
        cooldown_seconds: float = 0.0,
    ) -> None:
        self.baseline_rate = _finite_number(
            baseline_rate,
            name="baseline_rate",
        )
        self.excitation = _finite_number(excitation, name="excitation")
        self.decay_rate = _finite_number(decay_rate, name="decay_rate")
        self.wake_threshold = _finite_number(
            wake_threshold,
            name="wake_threshold",
        )
        self.cooldown_seconds = _finite_number(
            cooldown_seconds,
            name="cooldown_seconds",
        )

        if self.baseline_rate < 0:
            raise ValueError("baseline_rate must be >= 0")
        if self.excitation < 0:
            raise ValueError("excitation must be >= 0")
        if self.decay_rate <= 0:
            raise ValueError("decay_rate must be > 0")
        if self.wake_threshold < 0:
            raise ValueError("wake_threshold must be >= 0")
        if self.cooldown_seconds < 0:
            raise ValueError("cooldown_seconds must be >= 0")
        if self.excitation / self.decay_rate >= 1.0:
            raise ValueError(
                "hawkes_threshold requires subcritical excitation / decay_rate < 1"
            )

    def _state_value(
        self,
        state: Mapping[str, Any],
        key: str,
        *,
        default: float | None,
    ) -> float | None:
        raw = state.get(key, default)
        if raw is None:
            return None
        return _finite_number(raw, name=f"state.{key}")

    def decide(
        self,
        *,
        now: float,
        state: Mapping[str, Any],
    ) -> InitiativeDecision:
        now_value = _finite_number(now, name="now")
        unknown_state = set(state) - self._STATE_KEYS
        if unknown_state:
            names = ", ".join(sorted(unknown_state))
            raise ValueError(f"unknown hawkes_threshold state keys: {names}")

        prior_excitation = self._state_value(
            state,
            "excitation",
            default=0.0,
        )
        last_event_at = self._state_value(
            state,
            "last_event_at",
            default=None,
        )
        last_emit_at = self._state_value(
            state,
            "last_emit_at",
            default=None,
        )
        assert prior_excitation is not None
        if prior_excitation < 0:
            raise ValueError("state.excitation must be >= 0")

        elapsed = (
            0.0
            if last_event_at is None
            else max(0.0, now_value - last_event_at)
        )
        decayed_excitation = prior_excitation * math.exp(
            -self.decay_rate * elapsed
        )
        post_excitation = decayed_excitation + self.excitation
        post_intensity = self.baseline_rate + post_excitation
        if not math.isfinite(post_intensity):
            raise ValueError("hawkes_threshold intensity became non-finite")

        threshold_met = post_intensity >= self.wake_threshold
        since_emit = (
            math.inf
            if last_emit_at is None
            else max(0.0, now_value - last_emit_at)
        )
        cooldown_remaining = max(
            0.0,
            self.cooldown_seconds - since_emit,
        )
        cooldown_met = cooldown_remaining == 0.0
        emit = threshold_met and cooldown_met

        event_at = (
            now_value
            if last_event_at is None
            else max(now_value, last_event_at)
        )
        next_last_emit_at = last_emit_at
        if emit:
            next_last_emit_at = (
                now_value
                if last_emit_at is None
                else max(now_value, last_emit_at)
            )

        if not threshold_met:
            reason = "below_threshold"
        elif not cooldown_met:
            reason = "cooldown"
        else:
            reason = "threshold_met"

        return InitiativeDecision(
            emit=emit,
            state={
                "excitation": post_excitation,
                "last_event_at": event_at,
                "last_emit_at": next_last_emit_at,
            },
            metrics={
                "baseline_rate": self.baseline_rate,
                "branching_ratio": self.excitation / self.decay_rate,
                "decayed_excitation": decayed_excitation,
                "post_excitation": post_excitation,
                "post_intensity": post_intensity,
                "wake_threshold": self.wake_threshold,
                "cooldown_remaining": cooldown_remaining,
            },
            reason=reason,
        )


def build_initiative_policy(
    kind: str,
    config: Mapping[str, Any],
) -> InitiativePolicy:
    normalized = kind.strip().lower()
    if normalized == "on_change":
        if config:
            raise ValueError("on_change initiative policy does not accept configuration")
        return OnChangePolicy()
    if normalized == "hawkes_threshold":
        allowed = {
            "baseline_rate",
            "excitation",
            "decay_rate",
            "wake_threshold",
            "cooldown_seconds",
        }
        unknown = set(config) - allowed
        if unknown:
            names = ", ".join(sorted(unknown))
            raise ValueError(f"unknown hawkes_threshold config keys: {names}")
        return ExponentialHawkesThresholdPolicy(
            baseline_rate=_required_config_number(config, "baseline_rate"),
            excitation=_required_config_number(config, "excitation"),
            decay_rate=_required_config_number(config, "decay_rate"),
            wake_threshold=_required_config_number(config, "wake_threshold"),
            cooldown_seconds=_finite_number(
                config.get("cooldown_seconds", 0.0),
                name="cooldown_seconds",
            ),
        )
    raise ValueError(f"unknown initiative policy: {kind}")
