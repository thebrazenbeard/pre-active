from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import json
from typing import Mapping
from urllib import error, parse, request

from .store import Store
from .providers.openai_compatible import OpenAICompatibleAdapter


@dataclass(frozen=True)
class ModelTarget:
    name: str
    provider: str
    base_url: str
    model: str
    api_key_env: str | None

    def __post_init__(self) -> None:
        name = self.name.strip()
        provider = self.provider.strip()
        base_url = self.base_url.strip().rstrip("/")
        model = self.model.strip()
        if not name:
            raise ValueError("model target name is required")
        if provider != "openai-compatible":
            raise ValueError("only openai-compatible model targets are supported")
        if not model:
            raise ValueError("model target model is required")

        parsed = parse.urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("model target base_url must be an http(s) URL")
        host = parsed.hostname
        is_loopback = host == "localhost"
        if not is_loopback:
            try:
                is_loopback = ipaddress.ip_address(host).is_loopback
            except ValueError:
                is_loopback = False
        if not is_loopback:
            raise ValueError("model target base_url must use a loopback/local endpoint")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("model target base_url must not contain credentials, query, or fragment")

        object.__setattr__(self, "name", name)
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "base_url", base_url)
        object.__setattr__(self, "model", model)
        if self.api_key_env is not None:
            value = self.api_key_env.strip()
            object.__setattr__(self, "api_key_env", value or None)

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> "ModelTarget":
        return cls(
            name=str(record["name"]),
            provider=str(record["provider"]),
            base_url=str(record["base_url"]),
            model=str(record["model"]),
            api_key_env=(
                None
                if record.get("api_key_env") is None
                else str(record["api_key_env"])
            ),
        )


def resolve_model_target(
    *,
    store: Store,
    target_name: str | None,
    base_url: str | None,
    model: str | None,
    api_key_env: str | None,
    env: Mapping[str, str],
) -> ModelTarget:
    if bool(base_url) != bool(model):
        raise ValueError("--base-url and --model must be supplied together")
    if base_url and model:
        return ModelTarget(
            name="cli-override",
            provider="openai-compatible",
            base_url=base_url,
            model=model,
            api_key_env=api_key_env,
        )

    if target_name:
        record = store.get_model_target(target_name)
        if record is None:
            raise KeyError(f"unknown model target: {target_name}")
        return ModelTarget.from_record(record)

    active = store.get_active_model_target()
    if active is not None:
        return ModelTarget.from_record(active)

    legacy_url = env.get("PRE_ACTIVE_BASE_URL")
    legacy_model = env.get("PRE_ACTIVE_MODEL")
    if legacy_url and legacy_model:
        return ModelTarget(
            name="legacy-env",
            provider="openai-compatible",
            base_url=legacy_url,
            model=legacy_model,
            api_key_env=api_key_env,
        )

    raise ValueError(
        "no local model target configured; use 'pre-active target set ... --activate' "
        "or supply --base-url and --model"
    )


def probe_model_target(
    target: ModelTarget,
    *,
    env: Mapping[str, str],
    timeout_seconds: float = 5.0,
) -> dict[str, object]:
    headers = {
        "Accept": "application/json",
        "User-Agent": "pre-active/0.1",
    }
    if target.api_key_env:
        token = env.get(target.api_key_env)
        if token:
            headers["Authorization"] = f"Bearer {token}"
    req = request.Request(f"{target.base_url}/models", headers=headers, method="GET")
    try:
        with request.urlopen(req, timeout=timeout_seconds) as response:
            raw = response.read()
            status = int(getattr(response, "status", 200))
    except error.HTTPError as exc:
        return {
            "name": target.name,
            "reachable": True,
            "ready": False,
            "http_status": exc.code,
            "model": target.model,
            "error": f"HTTP {exc.code}: {exc.reason}",
        }
    except (error.URLError, TimeoutError, OSError) as exc:
        return {
            "name": target.name,
            "reachable": False,
            "ready": False,
            "model": target.model,
            "error": str(exc),
        }

    try:
        payload = json.loads(raw.decode("utf-8"))
        ids = [
            str(item["id"])
            for item in payload.get("data", [])
            if isinstance(item, dict) and "id" in item
        ]
    except (UnicodeDecodeError, json.JSONDecodeError, AttributeError, TypeError):
        return {
            "name": target.name,
            "reachable": True,
            "ready": False,
            "http_status": status,
            "model": target.model,
            "error": "models response was not valid OpenAI-compatible JSON",
        }
    return {
        "name": target.name,
        "reachable": True,
        "ready": target.model in ids,
        "http_status": status,
        "model": target.model,
        "advertised_models": ids,
    }


class TargetResolvingModelAdapter:
    """Resolve the selected persisted model target at each cognition turn.

    The daemon can therefore remain resident while an operator activates a
    different local target. A target switch takes effect between model calls,
    never in the middle of one provider request.
    """

    def __init__(
        self,
        *,
        store: Store,
        target_name: str | None,
        api_key_env: str | None,
        env: Mapping[str, str],
        timeout_seconds: float,
    ) -> None:
        self.store = store
        self.target_name = target_name
        self.api_key_env = api_key_env
        self.env = env
        self.timeout_seconds = timeout_seconds
        self._last_binding: tuple[str, str, str] | None = None

    def current_target(self) -> ModelTarget:
        return resolve_model_target(
            store=self.store,
            target_name=self.target_name,
            base_url=None,
            model=None,
            api_key_env=self.api_key_env,
            env=self.env,
        )

    def respond(self, *, messages, tools):  # type: ignore[no-untyped-def]
        target = self.current_target()
        binding = (target.name, target.base_url, target.model)
        if binding != self._last_binding:
            self.store.append_journal(
                event_type="MODEL_TARGET_BOUND",
                subject_id=target.name,
                payload={
                    "provider": target.provider,
                    "base_url": target.base_url,
                    "model": target.model,
                },
                now=__import__("time").time(),
            )
            self._last_binding = binding
        api_key = self.env.get(target.api_key_env) if target.api_key_env else None
        adapter = OpenAICompatibleAdapter(
            base_url=target.base_url,
            model=target.model,
            api_key=api_key,
            timeout_seconds=self.timeout_seconds,
        )
        return adapter.respond(messages=messages, tools=tools)
