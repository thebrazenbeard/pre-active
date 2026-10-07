from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

import pre_active.model_targets as model_targets
from pre_active.cli import build_parser, main
from pre_active.model_targets import ModelTarget, probe_model_target, resolve_model_target
from pre_active.store import Store


def test_model_provenance_surface_exists() -> None:
    assert callable(getattr(model_targets, "ModelProvenance", None))


def test_model_provenance_validates_exact_revision_and_adapter_digest() -> None:
    provenance = model_targets.ModelProvenance(
        base_model_revision="d61dd146c8fd44c9a49cdb7f59f34e17b61902d8",
        adapter_active=True,
        adapter_model_sha256=(
            "b2d6eec7befca3e18cf1fa793a7830197e27bab33bea8e4f42b5a318ee91d116"
        ),
    )

    assert provenance.to_mapping() == {
        "base_model_revision": "d61dd146c8fd44c9a49cdb7f59f34e17b61902d8",
        "adapter_active": True,
        "adapter_model_sha256": (
            "b2d6eec7befca3e18cf1fa793a7830197e27bab33bea8e4f42b5a318ee91d116"
        ),
    }


@pytest.mark.parametrize(
    ("base_revision", "adapter_active", "adapter_sha", "message"),
    [
        ("not-a-git-sha", False, None, "40 lowercase hex"),
        ("d61dd146c8fd44c9a49cdb7f59f34e17b61902d8", True, None, "required"),
        (
            "d61dd146c8fd44c9a49cdb7f59f34e17b61902d8",
            False,
            "b2d6eec7befca3e18cf1fa793a7830197e27bab33bea8e4f42b5a318ee91d116",
            "must be absent",
        ),
    ],
)
def test_model_provenance_rejects_invalid_bindings(
    base_revision: str,
    adapter_active: bool,
    adapter_sha: str | None,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        model_targets.ModelProvenance(
            base_model_revision=base_revision,
            adapter_active=adapter_active,
            adapter_model_sha256=adapter_sha,
        )


def test_model_target_rejects_non_loopback_endpoint() -> None:
    with pytest.raises(ValueError, match="loopback"):
        ModelTarget(
            name="remote",
            provider="openai-compatible",
            base_url="https://example.com/v1",
            model="x",
            api_key_env=None,
        )


def test_model_target_accepts_localhost_and_loopback() -> None:
    for url in (
        "http://127.0.0.1:11434/v1",
        "http://localhost:1234/v1",
        "http://[::1]:8080/v1",
    ):
        target = ModelTarget(
            name="local",
            provider="openai-compatible",
            base_url=url,
            model="model-a",
            api_key_env=None,
        )
        assert target.base_url == url.rstrip("/")


def test_store_persists_and_activates_named_model_targets(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    provenance = model_targets.ModelProvenance(
        base_model_revision="d61dd146c8fd44c9a49cdb7f59f34e17b61902d8",
        adapter_active=False,
    )
    store.upsert_model_target(
        name="qwen",
        provider="openai-compatible",
        base_url="http://127.0.0.1:18081/v1",
        model="qwen-local",
        api_key_env=None,
        provenance=provenance.to_mapping(),
        activate=True,
        now=10.0,
    )
    store.upsert_model_target(
        name="other",
        provider="openai-compatible",
        base_url="http://127.0.0.1:1234/v1",
        model="other-local",
        api_key_env="LOCAL_MODEL_TOKEN",
        activate=False,
        now=11.0,
    )

    active = store.get_active_model_target()
    assert active is not None
    assert active["name"] == "qwen"
    assert active["provenance"] == provenance.to_mapping()
    resolved = ModelTarget.from_record(active)
    assert resolved.provenance == provenance
    assert [item["name"] for item in store.list_model_targets()] == ["other", "qwen"]

    store.activate_model_target("other", now=12.0)
    active = store.get_active_model_target()
    assert active is not None
    assert active["name"] == "other"
    assert active["api_key_env"] == "LOCAL_MODEL_TOKEN"


def test_probe_requires_exact_expected_model_provenance(monkeypatch) -> None:
    provenance = model_targets.ModelProvenance(
        base_model_revision="d61dd146c8fd44c9a49cdb7f59f34e17b61902d8",
        adapter_active=False,
    )
    target = ModelTarget(
        name="qwen",
        provider="openai-compatible",
        base_url="http://127.0.0.1:18081/v1",
        model="qwen-local",
        api_key_env=None,
        provenance=provenance,
    )

    class Response(io.BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()
            return False

    def fake_urlopen(_request, timeout):
        assert timeout == 1.0
        return Response(
            json.dumps(
                {
                    "object": "list",
                    "data": [
                        {
                            "id": "qwen-local",
                            "object": "model",
                            "provenance": provenance.to_mapping(),
                        }
                    ],
                }
            ).encode("utf-8")
        )

    monkeypatch.setattr(model_targets.request, "urlopen", fake_urlopen)

    result = probe_model_target(target, env={}, timeout_seconds=1.0)

    assert result["ready"] is True
    assert result["provenance_verified"] is True
    assert result["expected_provenance"] == provenance.to_mapping()
    assert result["observed_provenance"] == provenance.to_mapping()


def test_probe_fails_closed_on_provenance_mismatch(monkeypatch) -> None:
    expected = model_targets.ModelProvenance(
        base_model_revision="d61dd146c8fd44c9a49cdb7f59f34e17b61902d8",
        adapter_active=False,
    )
    target = ModelTarget(
        name="qwen",
        provider="openai-compatible",
        base_url="http://127.0.0.1:18081/v1",
        model="qwen-local",
        api_key_env=None,
        provenance=expected,
    )

    class Response(io.BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()
            return False

    observed = {
        "base_model_revision": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "adapter_active": False,
        "adapter_model_sha256": None,
    }
    monkeypatch.setattr(
        model_targets.request,
        "urlopen",
        lambda *_args, **_kwargs: Response(
            json.dumps(
                {
                    "object": "list",
                    "data": [
                        {
                            "id": "qwen-local",
                            "object": "model",
                            "provenance": observed,
                        }
                    ],
                }
            ).encode("utf-8")
        ),
    )

    result = probe_model_target(target, env={}, timeout_seconds=1.0)

    assert result["reachable"] is True
    assert result["ready"] is False
    assert result["provenance_verified"] is False
    assert result["observed_provenance"] == observed


def test_target_cli_persists_expected_model_provenance(
    tmp_path: Path,
    capsys,
) -> None:
    state = tmp_path / "state.db"
    revision = "d61dd146c8fd44c9a49cdb7f59f34e17b61902d8"

    assert main([
        "--state", str(state),
        "target", "set", "qwen",
        "--base-url", "http://127.0.0.1:18081/v1",
        "--model", "qwen3.5-4b-local",
        "--base-model-revision", revision,
        "--activate",
    ]) == 0
    created = json.loads(capsys.readouterr().out)

    assert created["provenance"] == {
        "base_model_revision": revision,
        "adapter_active": False,
        "adapter_model_sha256": None,
    }


def test_target_cli_round_trip(tmp_path: Path, capsys) -> None:
    state = tmp_path / "state.db"

    assert main([
        "--state", str(state),
        "target", "set", "studio",
        "--base-url", "http://127.0.0.1:1234/v1",
        "--model", "local-model",
        "--activate",
    ]) == 0
    created = json.loads(capsys.readouterr().out)
    assert created["name"] == "studio"
    assert created["active"] is True

    assert main(["--state", str(state), "target", "show"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["active"]["name"] == "studio"
    assert shown["active"]["base_url"] == "http://127.0.0.1:1234/v1"

    assert main(["--state", str(state), "target", "list"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["targets"][0]["name"] == "studio"


def test_runtime_parser_accepts_named_target() -> None:
    args = build_parser().parse_args([
        "run-once",
        "--target", "studio",
    ])
    assert args.target == "studio"


def test_resolve_target_prefers_explicit_named_target_over_active(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    store.upsert_model_target(
        name="active",
        provider="openai-compatible",
        base_url="http://127.0.0.1:1111/v1",
        model="active-model",
        api_key_env=None,
        activate=True,
        now=1.0,
    )
    store.upsert_model_target(
        name="selected",
        provider="openai-compatible",
        base_url="http://127.0.0.1:2222/v1",
        model="selected-model",
        api_key_env=None,
        activate=False,
        now=2.0,
    )

    target = resolve_model_target(
        store=store,
        target_name="selected",
        base_url=None,
        model=None,
        api_key_env=None,
        env={},
    )
    assert target.name == "selected"
    assert target.model == "selected-model"


def test_resolve_target_uses_active_before_legacy_env(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    store.upsert_model_target(
        name="active",
        provider="openai-compatible",
        base_url="http://127.0.0.1:1111/v1",
        model="active-model",
        api_key_env=None,
        activate=True,
        now=1.0,
    )

    target = resolve_model_target(
        store=store,
        target_name=None,
        base_url=None,
        model=None,
        api_key_env=None,
        env={
            "PRE_ACTIVE_BASE_URL": "http://127.0.0.1:9999/v1",
            "PRE_ACTIVE_MODEL": "legacy",
        },
    )
    assert target.name == "active"
    assert target.model == "active-model"


def test_resolve_target_supports_legacy_env_when_no_persisted_target(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    target = resolve_model_target(
        store=store,
        target_name=None,
        base_url=None,
        model=None,
        api_key_env="PRE_ACTIVE_API_KEY",
        env={
            "PRE_ACTIVE_BASE_URL": "http://127.0.0.1:9999/v1",
            "PRE_ACTIVE_MODEL": "legacy",
        },
    )
    assert target.name == "legacy-env"
    assert target.model == "legacy"


def test_target_set_rejects_remote_url(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="loopback"):
        main([
            "--state", str(tmp_path / "state.db"),
            "target", "set", "bad",
            "--base-url", "https://api.example.com/v1",
            "--model", "remote",
        ])


def test_editing_active_target_does_not_silently_deactivate_it(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    store.upsert_model_target(
        name="local",
        provider="openai-compatible",
        base_url="http://127.0.0.1:1000/v1",
        model="before",
        api_key_env=None,
        activate=True,
        now=1.0,
    )
    store.upsert_model_target(
        name="local",
        provider="openai-compatible",
        base_url="http://127.0.0.1:1001/v1",
        model="after",
        api_key_env=None,
        activate=False,
        now=2.0,
    )
    active = store.get_active_model_target()
    assert active is not None
    assert active["name"] == "local"
    assert active["model"] == "after"


def test_resident_adapter_hot_switches_active_target_between_turns(
    tmp_path: Path, monkeypatch
) -> None:
    from pre_active.engine import ModelResponse
    from pre_active.model_targets import TargetResolvingModelAdapter

    store = Store(tmp_path / "state.db")
    provenance_a = model_targets.ModelProvenance(
        base_model_revision="d61dd146c8fd44c9a49cdb7f59f34e17b61902d8",
        adapter_active=False,
    )
    provenance_b = model_targets.ModelProvenance(
        base_model_revision="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        adapter_active=False,
    )
    store.upsert_model_target(
        name="a",
        provider="openai-compatible",
        base_url="http://127.0.0.1:1111/v1",
        model="model-a",
        api_key_env=None,
        provenance=provenance_a.to_mapping(),
        activate=True,
        now=1.0,
    )
    store.upsert_model_target(
        name="b",
        provider="openai-compatible",
        base_url="http://127.0.0.1:2222/v1",
        model="model-b",
        api_key_env=None,
        provenance=provenance_b.to_mapping(),
        activate=False,
        now=2.0,
    )

    def fake_respond(self, *, messages, tools):  # type: ignore[no-untyped-def]
        return ModelResponse(final_text=self.model)

    monkeypatch.setattr(
        "pre_active.model_targets.OpenAICompatibleAdapter.respond",
        fake_respond,
    )
    adapter = TargetResolvingModelAdapter(
        store=store,
        target_name=None,
        api_key_env=None,
        env={},
        timeout_seconds=1.0,
    )

    assert adapter.respond(messages=[], tools=[]).final_text == "model-a"
    store.activate_model_target("b", now=3.0)
    assert adapter.respond(messages=[], tools=[]).final_text == "model-b"

    bindings = [
        item
        for item in store.list_journal()
        if item["event_type"] == "MODEL_TARGET_BOUND"
    ]
    assert [item["subject_id"] for item in bindings] == ["a", "b"]
    assert bindings[0]["payload"]["provenance"] == provenance_a.to_mapping()
    assert bindings[1]["payload"]["provenance"] == provenance_b.to_mapping()
