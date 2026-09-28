from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).parents[1] / "ops" / "windows" / "tool_protocol.py"
SPEC = importlib.util.spec_from_file_location("tool_protocol", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
tool_protocol = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tool_protocol)


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "math.double",
            "description": "Double an integer",
            "parameters": {
                "type": "object",
                "properties": {"value": {"type": "integer"}},
                "required": ["value"],
                "additionalProperties": False,
            },
        },
    }
]


def test_native_qwen_tool_call_becomes_structured_openai_call() -> None:
    result = tool_protocol.parse_qwen_response(
        "I should use the tool.\n<tool_call>\n"
        "<function=math.double>\n"
        "<parameter=value>\n6\n</parameter>\n"
        "</function>\n</tool_call>",
        tools=TOOLS,
        messages=[{"role": "user", "content": "Double 6."}],
    )

    assert result["kind"] == "tool_call"
    assert result["name"] == "math.double"
    assert result["arguments"] == {"value": 6}
    assert result["request_id"].startswith("call_")


def test_independent_identical_tool_calls_get_distinct_request_ids() -> None:
    raw = (
        "<tool_call><function=math.double>"
        "<parameter=value>6</parameter>"
        "</function></tool_call>"
    )
    first = tool_protocol.parse_qwen_response(
        raw, tools=TOOLS, messages=[{"role": "user", "content": "Double 6."}]
    )
    second = tool_protocol.parse_qwen_response(
        raw, tools=TOOLS, messages=[{"role": "user", "content": "Double 6."}]
    )

    assert first["request_id"].startswith("call_")
    assert second["request_id"].startswith("call_")
    assert first["request_id"] != second["request_id"]


def test_tool_call_rejects_additional_tool_markup_or_unknown_parameter() -> None:
    with pytest.raises(tool_protocol.ToolProtocolError, match="additional tool markup"):
        tool_protocol.parse_qwen_response(
            "<tool_call><function=math.double>"
            "<parameter=value>6</parameter>"
            "</function></tool_call>"
            "<function=math.double><parameter=value>7</parameter></function>",
            tools=TOOLS,
            messages=[],
        )

    with pytest.raises(tool_protocol.ToolProtocolError, match="unknown parameter"):
        tool_protocol.parse_qwen_response(
            "<tool_call><function=math.double>"
            "<parameter=value>6</parameter>"
            "<parameter=extra>7</parameter>"
            "</function></tool_call>",
            tools=TOOLS,
            messages=[],
        )


def test_required_parameter_and_integer_coercion_fail_closed() -> None:
    with pytest.raises(tool_protocol.ToolProtocolError, match="required"):
        tool_protocol.parse_qwen_response(
            "<tool_call><function=math.double></function></tool_call>",
            tools=TOOLS,
            messages=[],
        )

    with pytest.raises(tool_protocol.ToolProtocolError, match="integer"):
        tool_protocol.parse_qwen_response(
            "<tool_call><function=math.double>"
            "<parameter=value>six</parameter>"
            "</function></tool_call>",
            tools=TOOLS,
            messages=[],
        )


def test_plain_text_remains_plain_text_and_stray_tool_markup_fails() -> None:
    result = tool_protocol.parse_qwen_response(
        "The answer is twelve.", tools=TOOLS, messages=[]
    )
    assert result == {"kind": "text", "content": "The answer is twelve."}

    with pytest.raises(tool_protocol.ToolProtocolError, match="malformed"):
        tool_protocol.parse_qwen_response(
            "<function=math.double><parameter=value>6</parameter></function>",
            tools=TOOLS,
            messages=[],
        )



def test_structured_tool_result_renders_openai_tool_call_message() -> None:
    parsed = {
        "kind": "tool_call",
        "request_id": "call_abc",
        "name": "math.double",
        "arguments": {"value": 6},
    }

    message, finish_reason = tool_protocol.to_openai_message(parsed)

    assert finish_reason == "tool_calls"
    assert message["role"] == "assistant"
    assert message["content"] is None
    assert message["tool_calls"] == [
        {
            "id": "call_abc",
            "type": "function",
            "function": {
                "name": "math.double",
                "arguments": '{"value":6}',
            },
        }
    ]


def test_text_result_renders_normal_assistant_message() -> None:
    message, finish_reason = tool_protocol.to_openai_message(
        {"kind": "text", "content": "twelve"}
    )

    assert finish_reason == "stop"
    assert message == {"role": "assistant", "content": "twelve"}


def test_native_qwen_suffix_after_complete_tool_call_is_discarded() -> None:
    raw = (
        "<tool_call>\n"
        "<function=math.double>\n"
        "<parameter=value>\n6\n</parameter>\n"
        "</function>\n</tool_call>\n"
        "user\n\nuser\nassistant\n<think>\n\n</think>\n\nTOOL_LOOP_OK:12"
    )

    result = tool_protocol.parse_qwen_response(
        raw,
        tools=TOOLS,
        messages=[{"role": "user", "content": "Double 6, then report the result."}],
    )

    assert result["kind"] == "tool_call"
    assert result["name"] == "math.double"
    assert result["arguments"] == {"value": 6}