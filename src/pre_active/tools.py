from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import re
from typing import Any, Callable

from .store import Store


_TOOL_LEDGER_SCHEMA = """
CREATE TABLE IF NOT EXISTS tool_effects (
    request_id TEXT PRIMARY KEY,
    tool_name TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    request_json TEXT NOT NULL,
    state TEXT NOT NULL,
    result_json TEXT,
    result_sha256 TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tool_reconciliations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL,
    effect_occurred INTEGER NOT NULL,
    evidence_digest TEXT NOT NULL,
    result_json TEXT,
    reconciled_at REAL NOT NULL,
    FOREIGN KEY (request_id) REFERENCES tool_effects(request_id)
);
"""


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    capability: str
    mutation: bool = False


@dataclass(frozen=True)
class ToolExecution:
    request_id: str
    tool_name: str
    output: dict[str, Any]
    replayed: bool
    result_sha256: str


class ToolError(RuntimeError):
    pass


class CapabilityDenied(ToolError):
    pass


class IdempotencyConflict(ToolError):
    pass


class AmbiguousEffect(ToolError):
    pass


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


_SUPPORTED_SCHEMA_KEYS = frozenset({
    "$schema", "$id", "title", "description", "default", "examples",
    "type", "enum", "const",
    "properties", "required", "additionalProperties", "minProperties", "maxProperties",
    "items", "minItems", "maxItems", "uniqueItems",
    "minLength", "maxLength", "pattern",
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
    "allOf", "anyOf", "oneOf", "not",
})
_JSON_TYPES = frozenset({"object", "array", "string", "integer", "number", "boolean", "null"})
_ANNOTATION_KEYS = frozenset({"$schema", "$id", "title", "description", "default", "examples"})


def _check_supported_schema(schema: dict[str, Any], *, path: str = "$schema") -> None:
    if not isinstance(schema, dict):
        raise ValueError(f"{path} must be an object")
    unknown = sorted(set(schema) - _SUPPORTED_SCHEMA_KEYS)
    if unknown:
        raise ValueError(f"{path} uses unsupported keyword(s): {', '.join(unknown)}")
    declared_type = schema.get("type")
    if declared_type is not None:
        values = [declared_type] if isinstance(declared_type, str) else declared_type
        if not isinstance(values, list) or not values or not all(isinstance(v, str) and v in _JSON_TYPES for v in values):
            raise ValueError(f"{path}.type must name supported JSON type(s)")
    if "required" in schema:
        required = schema["required"]
        if not isinstance(required, list) or not all(isinstance(v, str) for v in required):
            raise ValueError(f"{path}.required must be an array of strings")
    if "properties" in schema:
        properties = schema["properties"]
        if not isinstance(properties, dict) or not all(isinstance(k, str) for k in properties):
            raise ValueError(f"{path}.properties must be an object")
        for name, child in properties.items():
            _check_supported_schema(child, path=f"{path}.properties.{name}")
    if "additionalProperties" in schema:
        extra = schema["additionalProperties"]
        if not isinstance(extra, (bool, dict)):
            raise ValueError(f"{path}.additionalProperties must be boolean or schema")
        if isinstance(extra, dict):
            _check_supported_schema(extra, path=f"{path}.additionalProperties")
    if "items" in schema:
        _check_supported_schema(schema["items"], path=f"{path}.items")
    for key in ("allOf", "anyOf", "oneOf"):
        if key in schema:
            branches = schema[key]
            if not isinstance(branches, list) or not branches:
                raise ValueError(f"{path}.{key} must be a non-empty array")
            for index, child in enumerate(branches):
                _check_supported_schema(child, path=f"{path}.{key}[{index}]")
    if "not" in schema:
        _check_supported_schema(schema["not"], path=f"{path}.not")
    if "pattern" in schema:
        if not isinstance(schema["pattern"], str):
            raise ValueError(f"{path}.pattern must be a string")
        try:
            re.compile(schema["pattern"])
        except re.error as exc:
            raise ValueError(f"{path}.pattern is invalid: {exc}") from exc
    for key in ("minProperties", "maxProperties", "minItems", "maxItems", "minLength", "maxLength"):
        if key in schema and (not isinstance(schema[key], int) or isinstance(schema[key], bool) or schema[key] < 0):
            raise ValueError(f"{path}.{key} must be a non-negative integer")
    for key in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf"):
        if key in schema and (not isinstance(schema[key], (int, float)) or isinstance(schema[key], bool) or not math.isfinite(float(schema[key]))):
            raise ValueError(f"{path}.{key} must be a finite number")
    if "multipleOf" in schema and float(schema["multipleOf"]) <= 0:
        raise ValueError(f"{path}.multipleOf must be > 0")
    if "uniqueItems" in schema and not isinstance(schema["uniqueItems"], bool):
        raise ValueError(f"{path}.uniqueItems must be boolean")
    if "enum" in schema and (not isinstance(schema["enum"], list) or not schema["enum"]):
        raise ValueError(f"{path}.enum must be a non-empty array")


def _json_type_matches(value: Any, declared: str) -> bool:
    if declared == "object":
        return isinstance(value, dict)
    if declared == "array":
        return isinstance(value, list)
    if declared == "string":
        return isinstance(value, str)
    if declared == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if declared == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))
    if declared == "boolean":
        return isinstance(value, bool)
    if declared == "null":
        return value is None
    return False


def _schema_accepts(schema: dict[str, Any], value: Any, path: str) -> bool:
    try:
        _validate_schema_value(schema, value, path=path)
    except ValueError:
        return False
    return True


def _validate_schema_value(schema: dict[str, Any], value: Any, *, path: str) -> None:
    declared_type = schema.get("type")
    if declared_type is not None:
        types = [declared_type] if isinstance(declared_type, str) else declared_type
        if not any(_json_type_matches(value, item) for item in types):
            raise ValueError(f"{path} has wrong type")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{path} is not in enum")
    if "const" in schema and value != schema["const"]:
        raise ValueError(f"{path} does not equal const")
    if "allOf" in schema:
        for child in schema["allOf"]:
            _validate_schema_value(child, value, path=path)
    if "anyOf" in schema and not any(_schema_accepts(child, value, path) for child in schema["anyOf"]):
        raise ValueError(f"{path} does not satisfy anyOf")
    if "oneOf" in schema and sum(_schema_accepts(child, value, path) for child in schema["oneOf"]) != 1:
        raise ValueError(f"{path} does not satisfy exactly one oneOf branch")
    if "not" in schema and _schema_accepts(schema["not"], value, path):
        raise ValueError(f"{path} matches forbidden not schema")

    if isinstance(value, dict):
        required = schema.get("required", [])
        missing = [key for key in required if key not in value]
        if missing:
            raise ValueError(f"{path} missing required key(s): {', '.join(missing)}")
        min_props = schema.get("minProperties")
        max_props = schema.get("maxProperties")
        if min_props is not None and len(value) < min_props:
            raise ValueError(f"{path} has fewer than minProperties")
        if max_props is not None and len(value) > max_props:
            raise ValueError(f"{path} has more than maxProperties")
        properties = schema.get("properties", {})
        for key, child in properties.items():
            if key in value:
                _validate_schema_value(child, value[key], path=f"{path}.{key}")
        extra_keys = [key for key in value if key not in properties]
        additional = schema.get("additionalProperties", True)
        if additional is False and extra_keys:
            raise ValueError(f"{path} has unexpected key(s): {', '.join(sorted(extra_keys))}")
        if isinstance(additional, dict):
            for key in extra_keys:
                _validate_schema_value(additional, value[key], path=f"{path}.{key}")

    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            raise ValueError(f"{path} has fewer than minItems")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise ValueError(f"{path} has more than maxItems")
        if schema.get("uniqueItems"):
            canonical = [_canonical_json(item) for item in value]
            if len(canonical) != len(set(canonical)):
                raise ValueError(f"{path} contains duplicate items")
        if "items" in schema:
            for index, item in enumerate(value):
                _validate_schema_value(schema["items"], item, path=f"{path}[{index}]")

    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            raise ValueError(f"{path} is shorter than minLength")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise ValueError(f"{path} is longer than maxLength")
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            raise ValueError(f"{path} does not match pattern")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        if "minimum" in schema and number < float(schema["minimum"]):
            raise ValueError(f"{path} is below minimum")
        if "maximum" in schema and number > float(schema["maximum"]):
            raise ValueError(f"{path} is above maximum")
        if "exclusiveMinimum" in schema and number <= float(schema["exclusiveMinimum"]):
            raise ValueError(f"{path} is not above exclusiveMinimum")
        if "exclusiveMaximum" in schema and number >= float(schema["exclusiveMaximum"]):
            raise ValueError(f"{path} is not below exclusiveMaximum")
        if "multipleOf" in schema:
            divisor = float(schema["multipleOf"])
            quotient = number / divisor
            if not math.isclose(quotient, round(quotient), rel_tol=1e-12, abs_tol=1e-12):
                raise ValueError(f"{path} is not a multipleOf value")


class ToolRegistry:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.store.connection.executescript(_TOOL_LEDGER_SCHEMA)
        effect_columns = {
            row["name"] for row in self.store.connection.execute("PRAGMA table_info(tool_effects)")
        }
        if "request_json" not in effect_columns:
            self.store.connection.execute("ALTER TABLE tool_effects ADD COLUMN request_json TEXT")
        self._specs: dict[str, ToolSpec] = {}
        self._handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {}

    def register(
        self,
        spec: ToolSpec,
        handler: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> None:
        if not spec.name or spec.name in self._specs:
            raise ValueError(f"duplicate or empty tool name: {spec.name!r}")
        try:
            _check_supported_schema(spec.input_schema)
        except ValueError as exc:
            raise ValueError(f"invalid input_schema for tool {spec.name}: {exc}") from exc
        self._specs[spec.name] = spec
        self._handlers[spec.name] = handler

    def specs(self, allowed_capabilities: set[str]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for name in sorted(self._specs):
            spec = self._specs[name]
            if spec.capability not in allowed_capabilities:
                continue
            out.append(
                {
                    "name": spec.name,
                    "description": spec.description,
                    "input_schema": spec.input_schema,
                    "capability": spec.capability,
                    "mutation": spec.mutation,
                }
            )
        return out

    def _validate_arguments(self, spec: ToolSpec, arguments: dict[str, Any]) -> None:
        try:
            _validate_schema_value(spec.input_schema, arguments, path="$arguments")
        except ValueError as exc:
            raise ToolError(f"arguments do not match input_schema: {exc}") from exc

    def execute(
        self,
        *,
        name: str,
        arguments: dict[str, Any],
        request_id: str,
        allowed_capabilities: set[str],
        now: float,
    ) -> ToolExecution:
        if name not in self._specs:
            raise ToolError(f"unknown tool: {name}")
        spec = self._specs[name]
        if spec.capability not in allowed_capabilities:
            raise CapabilityDenied(f"capability not granted: {spec.capability}")
        self._validate_arguments(spec, arguments)
        request_payload = _canonical_json({"tool": name, "arguments": arguments})
        request_digest = _sha256(request_payload)

        if not spec.mutation:
            output = self._handlers[name](arguments)
            result_json = _canonical_json(output)
            return ToolExecution(
                request_id=request_id,
                tool_name=name,
                output=output,
                replayed=False,
                result_sha256=_sha256(result_json),
            )

        row = self.store.connection.execute(
            "SELECT * FROM tool_effects WHERE request_id = ?", (request_id,)
        ).fetchone()
        if row is not None:
            if row["request_sha256"] != request_digest or row["tool_name"] != name:
                raise IdempotencyConflict("request_id was already used for different input")
            if row["state"] == "COMMITTED":
                output = json.loads(row["result_json"])
                return ToolExecution(
                    request_id=request_id,
                    tool_name=name,
                    output=output,
                    replayed=True,
                    result_sha256=str(row["result_sha256"]),
                )
            if row["state"] == "RECONCILED_NO_EFFECT":
                self.store.connection.execute(
                    """
                    UPDATE tool_effects
                    SET state='EXECUTING', result_json=NULL, result_sha256=NULL, updated_at=?
                    WHERE request_id=?
                    """,
                    (now, request_id),
                )
            else:
                raise AmbiguousEffect(
                    f"mutation request {request_id} is in unresolved state {row['state']}"
                )
        else:
            self.store.connection.execute(
                """
                INSERT INTO tool_effects (
                    request_id, tool_name, request_sha256, request_json, state,
                    result_json, result_sha256, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'EXECUTING', NULL, NULL, ?, ?)
                """,
                (request_id, name, request_digest, request_payload, now, now),
            )
        try:
            output = self._handlers[name](arguments)
        except BaseException:
            self.store.connection.execute(
                "UPDATE tool_effects SET state='ATTEMPTED_UNKNOWN', updated_at=? WHERE request_id=?",
                (now, request_id),
            )
            raise

        result_json = _canonical_json(output)
        result_digest = _sha256(result_json)
        self.store.connection.execute(
            """
            UPDATE tool_effects
            SET state='COMMITTED', result_json=?, result_sha256=?, updated_at=?
            WHERE request_id=?
            """,
            (result_json, result_digest, now, request_id),
        )
        return ToolExecution(
            request_id=request_id,
            tool_name=name,
            output=output,
            replayed=False,
            result_sha256=result_digest,
        )
    def effect_state(self, request_id: str) -> str | None:
        row = self.store.connection.execute(
            "SELECT state FROM tool_effects WHERE request_id=?", (request_id,)
        ).fetchone()
        return None if row is None else str(row["state"])

    def recover_reconciled(
        self,
        *,
        request_id: str,
        allowed_capabilities: set[str],
        now: float,
    ) -> ToolExecution:
        row = self.store.connection.execute(
            "SELECT * FROM tool_effects WHERE request_id=?", (request_id,)
        ).fetchone()
        if row is None:
            raise ToolError(f"unknown mutation request: {request_id}")
        if row["state"] == "COMMITTED":
            output = json.loads(row["result_json"])
            return ToolExecution(
                request_id=request_id,
                tool_name=str(row["tool_name"]),
                output=output,
                replayed=True,
                result_sha256=str(row["result_sha256"]),
            )
        if row["state"] != "RECONCILED_NO_EFFECT":
            raise AmbiguousEffect(
                f"mutation request {request_id} remains unresolved in state {row['state']}"
            )
        if not row["request_json"]:
            raise ToolError("mutation request predates recoverable request payload storage")
        payload = json.loads(row["request_json"] )
        return self.execute(
            name=str(payload["tool"]),
            arguments=dict(payload["arguments"]),
            request_id=request_id,
            allowed_capabilities=allowed_capabilities,
            now=now,
        )

    def reconcile(
        self,
        *,
        request_id: str,
        effect_occurred: bool,
        evidence_digest: str,
        now: float,
        result: dict[str, Any] | None = None,
    ) -> None:
        if len(evidence_digest) != 64 or any(
            ch not in "0123456789abcdefABCDEF" for ch in evidence_digest
        ):
            raise ValueError("evidence_digest must be a 64-character SHA-256 hex digest")
        row = self.store.connection.execute(
            "SELECT * FROM tool_effects WHERE request_id = ?", (request_id,)
        ).fetchone()
        if row is None:
            raise ToolError(f"unknown mutation request: {request_id}")
        if row["state"] not in {"EXECUTING", "ATTEMPTED_UNKNOWN"}:
            raise ToolError(
                f"mutation request {request_id} cannot be reconciled from state {row['state']}"
            )
        if effect_occurred and result is None:
            raise ValueError("result is required when reconciliation confirms the effect")
        if not effect_occurred and result is not None:
            raise ValueError("result must be omitted when reconciliation confirms no effect")

        result_json = _canonical_json(result) if result is not None else None
        result_digest = _sha256(result_json) if result_json is not None else None
        self.store.connection.execute("BEGIN IMMEDIATE")
        try:
            current = self.store.connection.execute(
                "SELECT state FROM tool_effects WHERE request_id = ?", (request_id,)
            ).fetchone()
            if current is None or current["state"] not in {"EXECUTING", "ATTEMPTED_UNKNOWN"}:
                raise ToolError("mutation state changed during reconciliation")
            self.store.connection.execute(
                """
                INSERT INTO tool_reconciliations (
                    request_id, effect_occurred, evidence_digest, result_json, reconciled_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (request_id, int(effect_occurred), evidence_digest.lower(), result_json, now),
            )
            if effect_occurred:
                self.store.connection.execute(
                    """
                    UPDATE tool_effects
                    SET state='COMMITTED', result_json=?, result_sha256=?, updated_at=?
                    WHERE request_id=?
                    """,
                    (result_json, result_digest, now, request_id),
                )
            else:
                self.store.connection.execute(
                    """
                    UPDATE tool_effects
                    SET state='RECONCILED_NO_EFFECT', result_json=NULL, result_sha256=NULL, updated_at=?
                    WHERE request_id=?
                    """,
                    (now, request_id),
                )
            self.store.connection.execute("COMMIT")
        except BaseException:
            self.store.connection.execute("ROLLBACK")
            raise

