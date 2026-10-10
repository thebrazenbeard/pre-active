from __future__ import annotations

from datetime import timezone
from email.utils import parsedate_to_datetime
import json
import socket
import time
from typing import Any
from urllib import error, request

from ..local_transport import is_loopback_url, open_local_request
from ..engine import (
    ModelResponse,
    NonRetryableModelError,
    RetryableModelError,
    ToolCall,
)


_RETRYABLE_HTTP_STATUSES = {408, 429, 500, 502, 503, 504}


def _parse_retry_after(value: str | None, *, now: float | None = None) -> float | None:
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    if stripped.isdigit():
        return float(stripped)
    try:
        parsed = parsedate_to_datetime(stripped)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    current = time.time() if now is None else float(now)
    return max(0.0, parsed.timestamp() - current)


def _http_model_error(exc: error.HTTPError) -> RuntimeError:
    retry_after = _parse_retry_after(
        exc.headers.get("Retry-After") if exc.headers is not None else None
    )
    if exc.code in _RETRYABLE_HTTP_STATUSES:
        category = "throttling" if exc.code == 429 else "transient"
        return RetryableModelError(
            f"provider HTTP {exc.code}: {exc.reason}",
            category=category,
            retry_after_seconds=retry_after,
        )
    return NonRetryableModelError(f"provider HTTP {exc.code}: {exc.reason}")


class OpenAICompatibleAdapter:
    """Minimal Chat-Completions-compatible model boundary.

    Pre-Active deliberately admits one tool call per model turn. Parallel mutation
    planning can happen in reasoning, but effect ordering stays explicit and durable.
    """

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str | None,
        timeout_seconds: float = 120.0,
    ) -> None:
        if not base_url.strip() or not model.strip():
            raise ValueError("base_url and model are required")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds

    def build_payload(
        self, *, messages: list[dict[str, str]], tools: list[dict[str, Any]]
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
        }
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": item["name"],
                        "description": item["description"],
                        "parameters": item["input_schema"],
                    },
                }
                for item in tools
            ]
            payload["tool_choice"] = "auto"
            payload["parallel_tool_calls"] = False
        return payload

    def parse_response(self, payload: dict[str, Any]) -> ModelResponse:
        try:
            message = payload["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError("provider response does not contain choices[0].message") from exc

        calls = message.get("tool_calls") or []
        if calls:
            if len(calls) != 1:
                raise ValueError("provider must return exactly one tool call per turn")
            call = calls[0]
            try:
                request_id = str(call["id"])
                function = call["function"]
                name = str(function["name"])
                arguments = json.loads(function.get("arguments") or "{}")
            except (KeyError, TypeError, json.JSONDecodeError) as exc:
                raise ValueError("provider returned an invalid structured tool call") from exc
            if not isinstance(arguments, dict):
                raise ValueError("tool call arguments must decode to an object")
            return ModelResponse(
                tool_call=ToolCall(
                    request_id=request_id,
                    name=name,
                    arguments=arguments,
                )
            )

        content = message.get("content")
        if not isinstance(content, str):
            raise ValueError("provider response contains neither a tool call nor text")
        return ModelResponse(final_text=content)

    def respond(
        self, *, messages: list[dict[str, str]], tools: list[dict[str, Any]]
    ) -> ModelResponse:
        body = json.dumps(self.build_payload(messages=messages, tools=tools)).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "pre-active/0.1",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = request.Request(
            f"{self.base_url}/chat/completions",
            data=body,
            headers=headers,
            method="POST",
        )
        try:
            # Runtime ModelTargets are local-only. Preserve the generic
            # adapter's existing remote-provider behavior for direct callers.
            open_request = open_local_request if is_loopback_url(self.base_url) else request.urlopen
            with open_request(req, timeout=self.timeout_seconds) as response:
                raw = response.read()
        except error.HTTPError as exc:
            raise _http_model_error(exc) from exc
        except (error.URLError, TimeoutError, socket.timeout) as exc:
            raise RetryableModelError(
                f"provider transport failure: {exc}",
                category="transport",
            ) from exc

        try:
            parsed = json.loads(raw.decode("utf-8"))
            if not isinstance(parsed, dict):
                raise ValueError("provider response must be a JSON object")
            return self.parse_response(parsed)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise NonRetryableModelError(f"provider response invalid: {exc}") from exc
