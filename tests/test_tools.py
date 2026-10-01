from pathlib import Path

from pre_active.store import Store
from pre_active.tools import ToolRegistry, ToolSpec


def test_mutation_tool_is_idempotent_across_retry(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    registry = ToolRegistry(store)
    calls: list[int] = []

    def handler(args: dict[str, object]) -> dict[str, object]:
        calls.append(int(args["value"]))
        return {"doubled": int(args["value"]) * 2}

    registry.register(
        ToolSpec(
            name="counter.write",
            description="test mutation",
            input_schema={"type": "object", "required": ["value"]},
            capability="counter.write",
            mutation=True,
        ),
        handler,
    )

    first = registry.execute(
        name="counter.write",
        arguments={"value": 4},
        request_id="req-1",
        allowed_capabilities={"counter.write"},
        now=10.0,
    )
    second = registry.execute(
        name="counter.write",
        arguments={"value": 4},
        request_id="req-1",
        allowed_capabilities={"counter.write"},
        now=11.0,
    )

    assert first.output == {"doubled": 8}
    assert second.output == first.output
    assert second.replayed is True
    assert calls == [4]


def test_ambiguous_mutation_must_be_reconciled_before_retry(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    registry = ToolRegistry(store)
    attempts = 0

    def handler(args: dict[str, object]) -> dict[str, object]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("transport died after dispatch")
        return {"written": args["value"]}

    registry.register(
        ToolSpec(
            name="record.write",
            description="test ambiguous mutation",
            input_schema={"type": "object", "required": ["value"]},
            capability="record.write",
            mutation=True,
        ),
        handler,
    )

    import pytest
    from pre_active.tools import AmbiguousEffect

    with pytest.raises(RuntimeError):
        registry.execute(
            name="record.write",
            arguments={"value": 7},
            request_id="req-ambiguous",
            allowed_capabilities={"record.write"},
            now=1.0,
        )

    with pytest.raises(AmbiguousEffect):
        registry.execute(
            name="record.write",
            arguments={"value": 7},
            request_id="req-ambiguous",
            allowed_capabilities={"record.write"},
            now=2.0,
        )

    registry.reconcile(
        request_id="req-ambiguous",
        effect_occurred=False,
        evidence_digest="a" * 64,
        now=3.0,
    )
    retried = registry.execute(
        name="record.write",
        arguments={"value": 7},
        request_id="req-ambiguous",
        allowed_capabilities={"record.write"},
        now=4.0,
    )

    assert retried.output == {"written": 7}
    assert attempts == 2


def test_tool_arguments_are_validated_against_json_schema_before_handler(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    registry = ToolRegistry(store)
    calls: list[dict[str, object]] = []
    registry.register(
        ToolSpec(
            name="typed.write",
            description="requires an integer and rejects extra fields",
            input_schema={
                "type": "object",
                "properties": {"value": {"type": "integer"}},
                "required": ["value"],
                "additionalProperties": False,
            },
            capability="typed.write",
            mutation=True,
        ),
        lambda args: calls.append(args) or {"ok": True},
    )

    import pytest
    from pre_active.tools import ToolError

    with pytest.raises(ToolError, match="arguments do not match input_schema"):
        registry.execute(
            name="typed.write",
            arguments={"value": "not-an-integer", "extra": True},
            request_id="typed-1",
            allowed_capabilities={"typed.write"},
            now=1.0,
        )

    assert calls == []
    assert registry.effect_state("typed-1") is None


def test_tool_registration_rejects_unsupported_schema_keywords(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    registry = ToolRegistry(store)

    import pytest

    with pytest.raises(ValueError, match="unsupported keyword"):
        registry.register(
            ToolSpec(
                name="unsupported.schema",
                description="must fail closed",
                input_schema={"type": "object", "format": "opaque-custom-format"},
                capability="schema.test",
            ),
            lambda args: {"ok": True},
        )
