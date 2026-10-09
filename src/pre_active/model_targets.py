from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import json
import re
from typing import Mapping
from urllib import error, parse, request

from .store import Store
from .providers.openai_compatible import OpenAICompatibleAdapter


@dataclass(frozen=True)
class ModelProvenance:
    base_model_revision: str
    adapter_active: bool
    adapter_model_sha256: str | None = None

    def __post_init__(self) -> None:
        revision = self.base_model_revision.strip()
        if re.fullmatch(r"[0-9a-f]{40}", revision) is None:
            raise ValueError("base_model_revision must be 40 lowercase hex characters")
        adapter_sha = (
            self.adapter_model_sha256.strip()
            if isinstance(self.adapter_model_sha256, str)
            else None
        )
        if self.adapter_active:
            if adapter_sha is None or re.fullmatch(r"[0-9a-f]{64}", adapter_sha) is None:
                raise ValueError(
                    "adapter_model_sha256 is required when adapter_active is true "
                    "and must be 64 lowercase hex characters"
                )
        elif adapter_sha is not None:
            raise ValueError(
                "adapter_model_sha256 must be absent when adapter_active is false"
            )
        object.__setattr__(self, "base_model_revision", revision)
        object.__setattr__(self, "adapter_model_sha256", adapter_sha)

    def to_mapping(self) -> dict[str, object]:
        return {
            "base_model_revision": self.base_model_revision,
            "adapter_active": self.adapter_active,
            "adapter_model_sha256": self.adapter_model_sha256,
        }

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "ModelProvenance":
        adapter_active = payload.get("adapter_active")
        if not isinstance(adapter_active, bool):
            raise ValueError("adapter_active must be a boolean")
        adapter_sha = payload.get("adapter_model_sha256")
        if adapter_sha is not None and not isinstance(adapter_sha, str):
            raise ValueError("adapter_model_sha256 must be a string or null")
        return cls(
            base_model_revision=str(payload.get("base_model_revision", "")),
            adapter_active=adapter_active,
            adapter_model_sha256=adapter_sha,
        )


@dataclass(frozen=True)
class ModelTarget:
    name: str
    provider: str
    base_url: str
    model: str
    api_key_env: str | None
    provenance: ModelProvenance | None = None

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
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("model target base_url has an invalid port") from exc
        if port == 0:
            raise ValueError("model target base_url port must be nonzero")
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
        if self.provenance is not None and not isinstance(
            self.provenance,
            ModelProvenance,
        ):
            raise ValueError("model target provenance must be ModelProvenance or None")

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> "ModelTarget":
        raw_provenance = record.get("provenance")
        if raw_provenance is None:
            provenance = None
        elif isinstance(raw_provenance, Mapping):
            provenance = ModelProvenance.from_mapping(raw_provenance)
        else:
            raise ValueError("model target provenance must be an object or null")
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
            provenance=provenance,
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
        data = payload.get("data", [])
        records = [item for item in data if isinstance(item, dict) and "id" in item]
        ids = [str(item["id"]) for item in records]
    except (UnicodeDecodeError, json.JSONDecodeError, AttributeError, TypeError):
        return {
            "name": target.name,
            "reachable": True,
            "ready": False,
            "http_status": status,
            "model": target.model,
            "error": "models response was not valid OpenAI-compatible JSON",
        }

    matching = next(
        (item for item in records if str(item.get("id")) == target.model),
        None,
    )
    raw_observed_provenance = (
        matching.get("provenance")
        if isinstance(matching, dict)
        else None
    )
    observed_provenance: dict[str, object] | None = None
    if isinstance(raw_observed_provenance, Mapping):
        observed_provenance = dict(raw_observed_provenance)

    expected_provenance = (
        None if target.provenance is None else target.provenance.to_mapping()
    )
    provenance_verified = False
    provenance_ready = target.provenance is None
    if target.provenance is not None and observed_provenance is not None:
        try:
            observed = ModelProvenance.from_mapping(observed_provenance)
        except ValueError:
            observed = None
        provenance_verified = observed == target.provenance
        provenance_ready = provenance_verified

    return {
        "name": target.name,
        "reachable": True,
        "ready": target.model in ids and provenance_ready,
        "http_status": status,
        "model": target.model,
        "advertised_models": ids,
        "expected_provenance": expected_provenance,
        "observed_provenance": observed_provenance,
        "provenance_verified": provenance_verified,
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
        self._last_binding: tuple[str, str, str, str | None] | None = None

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
        provenance = (
            None if target.provenance is None else target.provenance.to_mapping()
        )
        provenance_key = (
            None
            if provenance is None
            else json.dumps(provenance, sort_keys=True, separators=(",", ":"))
        )
        binding = (target.name, target.base_url, target.model, provenance_key)
        if binding != self._last_binding:
            self.store.append_journal(
                event_type="MODEL_TARGET_BOUND",
                subject_id=target.name,
                payload={
                    "provider": target.provider,
                    "base_url": target.base_url,
                    "model": target.model,
                    "provenance": provenance,
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
