from __future__ import annotations

import hashlib
import json
import re
from typing import Any


class ToolProtocolError(ValueError):
    pass


_TOOL_CALL_RE = re.compile(
    r"<tool_call>\s*<function=([^>\r\n]+)>\s*(.*?)\s*</function>\s*</tool_call>",
    re.DOTALL,
)
_PARAMETER_RE = re.compile(
    r"<parameter=([^>\r\n]+)>\s*(.*?)\s*</parameter>",
    re.DOTALL,
)
_TOOL_MARKERS = ("<tool_call", "</tool_call", "<function=", "</function", "<parameter=", "</parameter")


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _function_specs(tools: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    specs: dict[str, dict[str, Any]] = {}
    for item in tools:
        if not isinstance(item, dict) or item.get("type") != "function":
            raise ToolProtocolError("tool declaration must be an OpenAI function tool")
        function = item.get("function")
        if not isinstance(function, dict):
            raise ToolProtocolError("tool function declaration must be an object")
        name = function.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ToolProtocolError("tool function name must be non-empty text")
        if name in specs:
            raise ToolProtocolError(f"duplicate tool name: {name}")
        specs[name] = function
    return specs


def _coerce_parameter(name: str, raw: str, schema: dict[str, Any]) -> Any:
    declared = schema.get("type", "string")
    value = raw.strip()

    if isinstance(declared, list):
        errors: list[str] = []
        for candidate in declared:
            try:
                return _coerce_parameter(name, value, {**schema, "type": candidate})
            except ToolProtocolError as exc:
                errors.append(str(exc))
        raise ToolProtocolError(
            f"parameter {name!r} does not match any declared type: {'; '.join(errors)}"
        )

    if declared == "string":
        return value
    if declared == "integer":
        if not re.fullmatch(r"[+-]?\d+", value):
            raise ToolProtocolError(f"parameter {name!r} must be an integer")
        return int(value)
    if declared == "number":
        try:
            parsed = float(value)
        except ValueError as exc:
            raise ToolProtocolError(f"parameter {name!r} must be a number") from exc
        if parsed != parsed or parsed in (float("inf"), float("-inf")):
            raise ToolProtocolError(f"parameter {name!r} must be a finite number")
        return parsed
    if declared == "boolean":
        lowered = value.lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
        raise ToolProtocolError(f"parameter {name!r} must be boolean true/false")
    if declared == "null":
        if value.lower() == "null":
            return None
        raise ToolProtocolError(f"parameter {name!r} must be null")
    if declared in {"object", "array"}:
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ToolProtocolError(
                f"parameter {name!r} must contain valid JSON for type {declared}"
            ) from exc
        if declared == "object" and not isinstance(parsed, dict):
            raise ToolProtocolError(f"parameter {name!r} must be a JSON object")
        if declared == "array" and not isinstance(parsed, list):
            raise ToolProtocolError(f"parameter {name!r} must be a JSON array")
        return parsed

    raise ToolProtocolError(f"unsupported parameter type for {name!r}: {declared!r}")


def parse_qwen_response(
    text: str,
    *,
    tools: list[dict[str, Any]],
    messages: list[dict[str, Any]],
) -> dict[str, Any]:
    if not isinstance(text, str):
        raise ToolProtocolError("model response must be text")

    matches = list(_TOOL_CALL_RE.finditer(text))
    has_tool_markup = any(marker in text for marker in _TOOL_MARKERS)

    if not matches:
        if has_tool_markup:
            raise ToolProtocolError("malformed Qwen tool-call markup")
        content = text.strip()
        if not content:
            raise ToolProtocolError("model returned empty text")
        return {"kind": "text", "content": content}

    if len(matches) != 1:
        raise ToolProtocolError("exactly one tool call is permitted")

    match = matches[0]
    suffix = text[match.end() :].strip()
    if suffix and any(marker in suffix for marker in _TOOL_MARKERS):
        raise ToolProtocolError("tool call suffix contains additional tool markup")

    name = match.group(1).strip()
    specs = _function_specs(tools)
    if name not in specs:
        raise ToolProtocolError(f"model called unknown tool: {name}")

    function = specs[name]
    parameters_schema = function.get("parameters") or {"type": "object"}
    if not isinstance(parameters_schema, dict):
        raise ToolProtocolError(f"tool {name} parameters schema must be an object")

    properties = parameters_schema.get("properties") or {}
    if not isinstance(properties, dict):
        raise ToolProtocolError(f"tool {name} properties schema must be an object")

    body = match.group(2)
    parameter_matches = list(_PARAMETER_RE.finditer(body))
    residue = _PARAMETER_RE.sub("", body).strip()
    if residue:
        raise ToolProtocolError("malformed content inside tool call")

    arguments: dict[str, Any] = {}
    for parameter_match in parameter_matches:
        parameter_name = parameter_match.group(1).strip()
        if parameter_name in arguments:
            raise ToolProtocolError(f"duplicate parameter: {parameter_name}")
        if parameter_name not in properties:
            if parameters_schema.get("additionalProperties", True) is False:
                raise ToolProtocolError(f"unknown parameter: {parameter_name}")
            parameter_schema: dict[str, Any] = {"type": "string"}
        else:
            parameter_schema = properties[parameter_name]
            if not isinstance(parameter_schema, dict):
                raise ToolProtocolError(
                    f"parameter schema for {parameter_name!r} must be an object"
                )
        arguments[parameter_name] = _coerce_parameter(
            parameter_name,
            parameter_match.group(2),
            parameter_schema,
        )

    required = parameters_schema.get("required") or []
    if not isinstance(required, list) or not all(isinstance(item, str) for item in required):
        raise ToolProtocolError(f"tool {name} required declaration must be a string array")
    missing = [item for item in required if item not in arguments]
    if missing:
        raise ToolProtocolError(
            "required parameter(s) missing: " + ", ".join(sorted(missing))
        )

    identity = {
        "messages": messages,
        "tool": name,
        "arguments": arguments,
    }
    request_id = "call_" + hashlib.sha256(
        _canonical_json(identity).encode("utf-8")
    ).hexdigest()[:24]

    return {
        "kind": "tool_call",
        "request_id": request_id,
        "name": name,
        "arguments": arguments,
    }



def to_openai_message(parsed: dict[str, Any]) -> tuple[dict[str, Any], str]:
    kind = parsed.get("kind")
    if kind == "text":
        content = parsed.get("content")
        if not isinstance(content, str) or not content:
            raise ToolProtocolError("text result requires non-empty content")
        return {"role": "assistant", "content": content}, "stop"

    if kind == "tool_call":
        request_id = parsed.get("request_id")
        name = parsed.get("name")
        arguments = parsed.get("arguments")
        if not isinstance(request_id, str) or not request_id:
            raise ToolProtocolError("tool result requires request_id")
        if not isinstance(name, str) or not name:
            raise ToolProtocolError("tool result requires name")
        if not isinstance(arguments, dict):
            raise ToolProtocolError("tool result arguments must be an object")
        return (
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": request_id,
                        "type": "function",
                        "function": {
                            "name": name,
                            "arguments": _canonical_json(arguments),
                        },
                    }
                ],
            },
            "tool_calls",
        )

    raise ToolProtocolError(f"unknown parsed response kind: {kind!r}")