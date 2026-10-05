from __future__ import annotations

# Increment this only when durable run semantics become incompatible with the
# previous contract. A bump must ship an explicit migration/resume path for
# older durable runs before those runs can continue.
RUN_CONTRACT_VERSION = 1


def validate_contract_version(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("run contract version must be an integer >= 1")
    return value
