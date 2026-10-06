import math

import pytest

from pre_active.initiative import build_initiative_policy


def test_on_change_policy_preserves_current_behavior() -> None:
    policy = build_initiative_policy("on_change", {})
    decision = policy.decide(now=10.0, state={})

    assert decision.emit is True
    assert decision.state == {}
    assert decision.metrics == {}
    assert decision.reason == "on_change"


def test_exponential_hawkes_threshold_accumulates_and_decays() -> None:
    policy = build_initiative_policy(
        "hawkes_threshold",
        {
            "baseline_rate": 0.1,
            "excitation": 0.4,
            "decay_rate": 1.0,
            "wake_threshold": 0.7,
            "cooldown_seconds": 0.0,
        },
    )

    first = policy.decide(now=0.0, state={})
    assert first.emit is False
    assert first.metrics["post_intensity"] == pytest.approx(0.5)
    assert first.state["excitation"] == pytest.approx(0.4)

    second = policy.decide(now=0.1, state=first.state)
    expected_second_excitation = 0.4 * math.exp(-0.1) + 0.4
    assert second.emit is True
    assert second.state["excitation"] == pytest.approx(expected_second_excitation)
    assert second.metrics["post_intensity"] == pytest.approx(
        0.1 + expected_second_excitation
    )

    third = policy.decide(now=10.1, state=second.state)
    expected_third_excitation = expected_second_excitation * math.exp(-10.0) + 0.4
    assert third.emit is False
    assert third.state["excitation"] == pytest.approx(expected_third_excitation)
    assert third.metrics["post_intensity"] == pytest.approx(
        0.1 + expected_third_excitation
    )


def test_hawkes_cooldown_suppresses_without_discarding_excitation() -> None:
    policy = build_initiative_policy(
        "hawkes_threshold",
        {
            "baseline_rate": 0.1,
            "excitation": 0.4,
            "decay_rate": 1.0,
            "wake_threshold": 0.4,
            "cooldown_seconds": 10.0,
        },
    )

    first = policy.decide(now=0.0, state={})
    assert first.emit is True

    second = policy.decide(now=0.1, state=first.state)
    assert second.emit is False
    assert second.reason == "cooldown"
    assert second.state["excitation"] > first.state["excitation"]
    assert second.state["last_emit_at"] == 0.0

    third = policy.decide(now=10.1, state=second.state)
    assert third.emit is True
    assert third.state["last_emit_at"] == 10.1


@pytest.mark.parametrize(
    "config",
    [
        {
            "baseline_rate": -0.1,
            "excitation": 0.1,
            "decay_rate": 1.0,
            "wake_threshold": 0.1,
        },
        {
            "baseline_rate": 0.1,
            "excitation": -0.1,
            "decay_rate": 1.0,
            "wake_threshold": 0.1,
        },
        {
            "baseline_rate": 0.1,
            "excitation": 0.1,
            "decay_rate": 0.0,
            "wake_threshold": 0.1,
        },
        {
            "baseline_rate": 0.1,
            "excitation": 1.0,
            "decay_rate": 1.0,
            "wake_threshold": 0.1,
        },
        {
            "baseline_rate": 0.1,
            "excitation": 0.1,
            "decay_rate": 1.0,
            "wake_threshold": -0.1,
        },
        {
            "baseline_rate": 0.1,
            "excitation": 0.1,
            "decay_rate": 1.0,
            "wake_threshold": 0.1,
            "cooldown_seconds": -1.0,
        },
    ],
)
def test_hawkes_policy_rejects_invalid_or_supercritical_configuration(
    config: dict[str, float],
) -> None:
    with pytest.raises(ValueError):
        build_initiative_policy("hawkes_threshold", config)


def test_unknown_initiative_policy_fails_closed() -> None:
    with pytest.raises(ValueError, match="unknown initiative policy"):
        build_initiative_policy("mystery", {})
