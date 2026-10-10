import pytest

from pre_active.model_targets import ModelTarget


@pytest.mark.parametrize("url", [
    "http://localhost:bogus/v1",
    "http://127.0.0.1:65536/v1",
    "http://localhost:0/v1",
])
def test_model_target_rejects_invalid_local_port_before_network(url):
    with pytest.raises(ValueError, match="port"):
        ModelTarget(
            name="local",
            provider="openai-compatible",
            base_url=url,
            model="test-model",
            api_key_env=None,
        )


def test_valid_loopback_port_remains_supported():
    target = ModelTarget(
        name="local",
        provider="openai-compatible",
        base_url="http://127.0.0.1:11434/v1",
        model="test-model",
        api_key_env=None,
    )
    assert target.base_url.endswith(":11434/v1")
