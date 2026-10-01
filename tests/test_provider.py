import json

from pre_active.providers.openai_compatible import OpenAICompatibleAdapter


def test_provider_parses_single_structured_tool_call() -> None:
    adapter = OpenAICompatibleAdapter(
        base_url="https://example.invalid/v1",
        model="test-model",
        api_key="secret",
    )
    payload = adapter.build_payload(
        messages=[{"role": "user", "content": "double 5"}],
        tools=[
            {
                "name": "math.double",
                "description": "double a number",
                "input_schema": {"type": "object", "required": ["value"]},
                "capability": "math.read",
                "mutation": False,
            }
        ],
    )
    assert payload["model"] == "test-model"
    assert payload["tools"][0]["function"]["name"] == "math.double"

    response = adapter.parse_response(
        {
            "choices": [
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {
                                    "name": "math.double",
                                    "arguments": json.dumps({"value": 5}),
                                },
                            }
                        ],
                    }
                }
            ]
        }
    )
    assert response.tool_call is not None
    assert response.tool_call.request_id == "call-1"
    assert response.tool_call.arguments == {"value": 5}


def test_provider_rejects_parallel_tool_calls_for_deterministic_effect_order() -> None:
    adapter = OpenAICompatibleAdapter(
        base_url="https://example.invalid/v1",
        model="test-model",
        api_key=None,
    )
    response = {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {"id": "a", "function": {"name": "x", "arguments": "{}"}},
                        {"id": "b", "function": {"name": "y", "arguments": "{}"}},
                    ],
                }
            }
        ]
    }

    import pytest

    with pytest.raises(ValueError, match="exactly one tool call"):
        adapter.parse_response(response)
