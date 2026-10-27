"""Bounded, read-only Empathy advice for a Pre-Active model response.

This is a *consumer* of a trusted host's admitted response-policy decision,
not a person recognizer, emotion classifier, or evidence admission service.
It never creates a Volition want, an autonomous event, or a tool capability.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, TypeAlias


_ALLOWED_POLICIES = frozenset({
    "CLARIFY_MEANING", "TASK_DIRECT", "REPAIR_FIRST", "CELEBRATE_FIRST",
    "TRUTHFUL_CHALLENGE", "PRESERVE_PLAY", "STOP_PLAY",
})


@dataclass(frozen=True, slots=True)
class EmpathyAdvisory:
    """Host-accepted, ephemeral advice with no action or goal authority.

    The host is responsible for authenticating identity, admitting current
    person-scoped evidence, and deriving this object from Empathy's selector.
    Model text and event payloads must never instantiate this type directly.
    """

    policy: str
    focus_needed: bool
    schema: str = "EMPATHY_RESPONSE_POLICY_HINT_V1"
    consumer: str = "RESPONSE_POLICY_ONLY"
    authority: str = "NONE"
    persistence: str = "EPHEMERAL"
    volition_want_authority: bool = False
    effect_authority: bool = False


EmpathyAdviser: TypeAlias = Callable[[str], EmpathyAdvisory | None]


def render_empathy_advice(advice: EmpathyAdvisory) -> str:
    """Render only an allowlisted policy and focus flag, never raw evidence.

    Reject structurally invalid or authority-bearing advice; the caller must
    fail closed before model inference rather than silently adopting it.
    """
    if type(advice) is not EmpathyAdvisory:
        raise TypeError("a typed host-admitted EmpathyAdvisory is required")
    if (
        type(advice.policy) is not str
        or advice.policy not in _ALLOWED_POLICIES
        or type(advice.focus_needed) is not bool
        or advice.schema != "EMPATHY_RESPONSE_POLICY_HINT_V1"
        or advice.consumer != "RESPONSE_POLICY_ONLY"
        or advice.authority != "NONE"
        or advice.persistence != "EPHEMERAL"
        or advice.volition_want_authority is not False
        or advice.effect_authority is not False
    ):
        raise ValueError("unrecognized or authority-bearing Empathy advice")

    focus = "yes" if advice.focus_needed else "no"
    return (
        "\n\nAdvisory response policy from admitted current evidence: "
        f"{advice.policy}; focused review needed: {focus}. "
        "This changes response attention/framing only. It is not an external "
        "action request, factual proof of emotions, permission to use tools, "
        "a durable want, consent, or a reason to ignore higher-priority rules."
    )
