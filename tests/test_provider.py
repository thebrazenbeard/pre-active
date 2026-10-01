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


def test_provider_classifies_429_with_retry_after_as_retryable(monkeypatch) -> None:
    from email.message import Message
    from io import BytesIO
    from urllib.error import HTTPError

    from pre_active.engine import RetryableModelError

    headers = Message()
    headers["Retry-After"] = "17"

    def fail(_req, timeout):
        raise HTTPError(
            url="https://example.invalid/v1/chat/completions",
            code=429,
            msg="Too Many Requests",
            hdrs=headers,
            fp=BytesIO(b'{"error":"throttled"}'),
        )

    monkeypatch.setattr("pre_active.providers.openai_compatible.request.urlopen", fail)
    adapter = OpenAICompatibleAdapter(
        base_url="https://example.invalid/v1",
        model="test-model",
        api_key=None,
    )

    import pytest

    with pytest.raises(RetryableModelError) as raised:
        adapter.respond(messages=[{"role": "user", "content": "hi"}], tools=[])

    assert raised.value.category == "throttling"
    assert raised.value.retry_after_seconds == 17.0


def test_provider_classifies_transient_http_statuses_as_retryable(monkeypatch) -> None:
    from io import BytesIO
    from urllib.error import HTTPError

    from pre_active.engine import RetryableModelError

    statuses = [408, 500, 502, 503, 504]
    observed = []

    def fail(req, timeout):
        code = statuses[len(observed)]
        observed.append(code)
        raise HTTPError(
            url=req.full_url,
            code=code,
            msg="temporary",
            hdrs=None,
            fp=BytesIO(b"temporary"),
        )

    monkeypatch.setattr("pre_active.providers.openai_compatible.request.urlopen", fail)
    adapter = OpenAICompatibleAdapter(
        base_url="https://example.invalid/v1",
        model="test-model",
        api_key=None,
    )

    import pytest

    for code in statuses:
        with pytest.raises(RetryableModelError) as raised:
            adapter.respond(messages=[{"role": "user", "content": "hi"}], tools=[])
        assert raised.value.category == "transient"

    assert observed == statuses


def test_provider_classifies_other_4xx_as_non_retryable(monkeypatch) -> None:
    from io import BytesIO
    from urllib.error import HTTPError

    from pre_active.engine import NonRetryableModelError

    def fail(req, timeout):
        raise HTTPError(
            url=req.full_url,
            code=401,
            msg="Unauthorized",
            hdrs=None,
            fp=BytesIO(b"bad key"),
        )

    monkeypatch.setattr("pre_active.providers.openai_compatible.request.urlopen", fail)
    adapter = OpenAICompatibleAdapter(
        base_url="https://example.invalid/v1",
        model="test-model",
        api_key="bad",
    )

    import pytest

    with pytest.raises(NonRetryableModelError, match="401"):
        adapter.respond(messages=[{"role": "user", "content": "hi"}], tools=[])


def test_provider_classifies_transport_failure_as_retryable(monkeypatch) -> None:
    from urllib.error import URLError

    from pre_active.engine import RetryableModelError

    def fail(_req, timeout):
        raise URLError("connection reset")

    monkeypatch.setattr("pre_active.providers.openai_compatible.request.urlopen", fail)
    adapter = OpenAICompatibleAdapter(
        base_url="https://example.invalid/v1",
        model="test-model",
        api_key=None,
    )

    import pytest

    with pytest.raises(RetryableModelError) as raised:
        adapter.respond(messages=[{"role": "user", "content": "hi"}], tools=[])

    assert raised.value.category == "transport"


def test_provider_protocol_failure_is_non_retryable(monkeypatch) -> None:
    from io import BytesIO

    from pre_active.engine import NonRetryableModelError

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b'{"not_choices":true}'

    monkeypatch.setattr(
        "pre_active.providers.openai_compatible.request.urlopen",
        lambda _req, timeout: Response(),
    )
    adapter = OpenAICompatibleAdapter(
        base_url="https://example.invalid/v1",
        model="test-model",
        api_key=None,
    )

    import pytest

    with pytest.raises(NonRetryableModelError, match="provider response"):
        adapter.respond(messages=[{"role": "user", "content": "hi"}], tools=[])
