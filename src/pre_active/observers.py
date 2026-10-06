from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sqlite3
import uuid
from typing import Any, Mapping, Protocol

from .initiative import build_initiative_policy
from .store import Store
from .volition_bridge import VolitionBridge


_OBSERVER_SCHEMA = """
CREATE TABLE IF NOT EXISTS observers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    config_json TEXT NOT NULL,
    task TEXT NOT NULL,
    capabilities_json TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT -10,
    interval_seconds REAL NOT NULL,
    next_at REAL NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    emit_initial INTEGER NOT NULL DEFAULT 0,
    last_digest TEXT,
    last_summary TEXT,
    last_evidence_json TEXT,
    last_observed_at REAL,
    last_changed_at REAL,
    last_error TEXT,
    consecutive_errors INTEGER NOT NULL DEFAULT 0,
    change_count INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS observers_due_idx
ON observers(enabled, next_at, name);
"""

_INITIATIVE_SCHEMA = """
CREATE TABLE IF NOT EXISTS observer_initiative (
    observer_id TEXT PRIMARY KEY REFERENCES observers(id) ON DELETE CASCADE,
    policy_kind TEXT NOT NULL,
    config_json TEXT NOT NULL,
    state_json TEXT NOT NULL,
    last_decision_json TEXT,
    updated_at REAL NOT NULL
);
"""

_DISPATCH_SCHEMA = """
CREATE TABLE IF NOT EXISTS observer_dispatch (
    observer_id TEXT PRIMARY KEY REFERENCES observers(id) ON DELETE CASCADE,
    route_kind TEXT NOT NULL,
    config_json TEXT NOT NULL,
    updated_at REAL NOT NULL
);
"""


@dataclass(frozen=True)
class Observation:
    digest: str
    summary: str
    evidence: dict[str, Any]


@dataclass(frozen=True)
class ObserverTickResult:
    sampled: int
    emitted: int
    errors: int


class ObserverAdapter(Protocol):
    def sample(self, config: Mapping[str, Any]) -> Observation: ...


class FileSnapshotObserver:
    """Observe one exact local file without injecting its contents into model context."""

    def sample(self, config: Mapping[str, Any]) -> Observation:
        raw_path = config.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ValueError("file observer requires a non-empty path")
        path = Path(raw_path).expanduser()
        if not path.exists():
            evidence: dict[str, Any] = {
                "path": str(path),
                "exists": False,
            }
            summary = f"file is absent: {path}"
        elif not path.is_file():
            raise ValueError(f"file observer path is not a regular file: {path}")
        else:
            hasher = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    hasher.update(chunk)
            stat = path.stat()
            evidence = {
                "path": str(path),
                "exists": True,
                "size": int(stat.st_size),
                "mtime_ns": int(stat.st_mtime_ns),
                "sha256": hasher.hexdigest(),
            }
            summary = (
                f"file snapshot changed: {path} "
                f"(size={stat.st_size}, sha256={hasher.hexdigest()[:12]})"
            )
        canonical = json.dumps(
            evidence,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        digest = hashlib.sha256(canonical).hexdigest()
        return Observation(digest=digest, summary=summary, evidence=evidence)


class ObserverManager:
    def __init__(
        self,
        store: Store,
        *,
        adapters: Mapping[str, ObserverAdapter] | None = None,
    ) -> None:
        self.store = store
        self.adapters: dict[str, ObserverAdapter] = {
            "file": FileSnapshotObserver(),
        }
        if adapters:
            self.adapters.update(adapters)
        dispatch_table_existed = (
            self.store.connection.execute(
                """
                SELECT 1
                FROM sqlite_master
                WHERE type='table' AND name='observer_dispatch'
                """
            ).fetchone()
            is not None
        )
        self.store.connection.executescript(_OBSERVER_SCHEMA)
        self.store.connection.executescript(_INITIATIVE_SCHEMA)
        self.store.connection.execute(
            """
            INSERT OR IGNORE INTO observer_initiative (
                observer_id, policy_kind, config_json, state_json,
                last_decision_json, updated_at
            )
            SELECT id, 'on_change', '{}', '{}', NULL, updated_at
            FROM observers
            """
        )
        if not dispatch_table_existed:
            self.store.connection.execute("BEGIN IMMEDIATE")
            try:
                self.store.connection.execute(_DISPATCH_SCHEMA)
                self.store.connection.execute(
                    """
                    INSERT OR IGNORE INTO observer_dispatch (
                        observer_id, route_kind, config_json, updated_at
                    )
                    SELECT id, 'autonomous_turn', '{}', updated_at
                    FROM observers
                    """
                )
                self.store.connection.execute("COMMIT")
            except BaseException:
                self.store.connection.execute("ROLLBACK")
                raise

    @staticmethod
    def _validate_dispatch(
        *,
        route_kind: str,
        config: dict[str, Any],
        capabilities: set[str],
    ) -> None:
        if route_kind == "autonomous_turn":
            if config:
                raise ValueError("autonomous_turn dispatch config must be empty")
            return
        if route_kind != "volition_signal":
            raise ValueError(f"unknown observer dispatch route: {route_kind}")
        if capabilities:
            raise ValueError("volition_signal observers cannot have capabilities")
        for reserved_key in ("source", "effect_authority", "capabilities", "priority"):
            if reserved_key in config:
                raise ValueError(
                    f"dispatch config cannot contain {reserved_key}"
                )
        VolitionBridge.validate_static_signal_config(
            config,
            source="observer:validation",
            label="dispatch config",
        )

    def add(
        self,
        *,
        name: str,
        kind: str,
        config: dict[str, Any],
        task: str,
        capabilities: set[str],
        every_seconds: float,
        now: float,
        priority: int = -10,
        emit_initial: bool = False,
        first_at: float | None = None,
        initiative_policy: str = "on_change",
        initiative_config: dict[str, Any] | None = None,
        dispatch_route: str = "autonomous_turn",
        dispatch_config: dict[str, Any] | None = None,
    ) -> str:
        normalized_name = name.strip()
        normalized_kind = kind.strip()
        if not normalized_name:
            raise ValueError("observer name is required")
        if normalized_kind not in self.adapters:
            raise ValueError(f"unknown observer kind: {normalized_kind}")
        if not task.strip():
            raise ValueError("observer task is required")
        if every_seconds <= 0:
            raise ValueError("observer interval must be > 0")
        normalized_initiative_policy = initiative_policy.strip().lower()
        initiative_config_value = dict(initiative_config or {})
        build_initiative_policy(
            normalized_initiative_policy,
            initiative_config_value,
        )
        normalized_dispatch_route = dispatch_route.strip().lower()
        if dispatch_config is not None and not isinstance(dispatch_config, dict):
            raise ValueError("observer dispatch config must be an object")
        dispatch_config_value = dict(dispatch_config or {})
        self._validate_dispatch(
            route_kind=normalized_dispatch_route,
            config=dispatch_config_value,
            capabilities=capabilities,
        )

        observer_id = str(uuid.uuid4())
        next_at = now if first_at is None else float(first_at)
        self.store.connection.execute("BEGIN IMMEDIATE")
        try:
            self.store.connection.execute(
                """
                INSERT INTO observers (
                    id, name, kind, config_json, task, capabilities_json,
                    priority, interval_seconds, next_at, enabled, emit_initial,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                """,
                (
                    observer_id,
                    normalized_name,
                    normalized_kind,
                    json.dumps(config, sort_keys=True, separators=(",", ":")),
                    task.strip(),
                    json.dumps(sorted(capabilities)),
                    int(priority),
                    float(every_seconds),
                    next_at,
                    1 if emit_initial else 0,
                    now,
                    now,
                ),
            )
            self.store.connection.execute(
                """
                INSERT INTO observer_initiative (
                    observer_id, policy_kind, config_json, state_json,
                    last_decision_json, updated_at
                ) VALUES (?, ?, ?, '{}', NULL, ?)
                """,
                (
                    observer_id,
                    normalized_initiative_policy,
                    json.dumps(
                        initiative_config_value,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    now,
                ),
            )
            self.store.connection.execute(
                """
                INSERT INTO observer_dispatch (
                    observer_id, route_kind, config_json, updated_at
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    observer_id,
                    normalized_dispatch_route,
                    json.dumps(
                        dispatch_config_value,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    now,
                ),
            )
            self.store.append_journal(
                event_type="OBSERVER_CONFIGURED",
                subject_id=observer_id,
                payload={
                    "name": normalized_name,
                    "kind": normalized_kind,
                    "priority": int(priority),
                    "every_seconds": float(every_seconds),
                    "emit_initial": bool(emit_initial),
                    "initiative_policy": normalized_initiative_policy,
                    "dispatch_route": normalized_dispatch_route,
                },
                now=now,
            )
            self.store.connection.execute("COMMIT")
        except BaseException:
            self.store.connection.execute("ROLLBACK")
            raise
        return observer_id

    def add_file(
        self,
        *,
        name: str,
        path: str,
        task: str,
        capabilities: set[str],
        every_seconds: float,
        now: float,
        priority: int = -10,
        emit_initial: bool = False,
        first_at: float | None = None,
        initiative_policy: str = "on_change",
        initiative_config: dict[str, Any] | None = None,
        dispatch_route: str = "autonomous_turn",
        dispatch_config: dict[str, Any] | None = None,
    ) -> str:
        return self.add(
            name=name,
            kind="file",
            config={"path": path},
            task=task,
            capabilities=capabilities,
            every_seconds=every_seconds,
            now=now,
            priority=priority,
            emit_initial=emit_initial,
            first_at=first_at,
            initiative_policy=initiative_policy,
            initiative_config=initiative_config,
            dispatch_route=dispatch_route,
            dispatch_config=dispatch_config,
        )

    def _initiative_record(self, observer_id: str) -> dict[str, Any]:
        row = self.store.connection.execute(
            "SELECT * FROM observer_initiative WHERE observer_id=?",
            (observer_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError(
                f"observer initiative state is missing: {observer_id}"
            )
        return {
            "policy_kind": str(row["policy_kind"]),
            "config": json.loads(row["config_json"]),
            "state": json.loads(row["state_json"]),
            "last_decision": (
                None
                if row["last_decision_json"] is None
                else json.loads(row["last_decision_json"])
            ),
            "updated_at": float(row["updated_at"]),
        }

    def _dispatch_record(
        self,
        observer_id: str,
        *,
        tolerate_corrupt: bool = False,
    ) -> dict[str, Any]:
        row = self.store.connection.execute(
            "SELECT * FROM observer_dispatch WHERE observer_id=?",
            (observer_id,),
        ).fetchone()
        if row is None:
            message = f"observer dispatch state is missing: {observer_id}"
            if tolerate_corrupt:
                return {
                    "route_kind": None,
                    "config": None,
                    "updated_at": None,
                    "error": f"RuntimeError: {message}",
                }
            raise RuntimeError(message)
        try:
            config = json.loads(row["config_json"])
        except json.JSONDecodeError as exc:
            if not tolerate_corrupt:
                raise
            return {
                "route_kind": str(row["route_kind"]),
                "config": None,
                "updated_at": float(row["updated_at"]),
                "error": f"{type(exc).__name__}: {exc}",
            }
        if not isinstance(config, dict):
            if not tolerate_corrupt:
                raise RuntimeError("observer dispatch config must be an object")
            return {
                "route_kind": str(row["route_kind"]),
                "config": None,
                "updated_at": float(row["updated_at"]),
                "error": "RuntimeError: observer dispatch config must be an object",
            }
        return {
            "route_kind": str(row["route_kind"]),
            "config": config,
            "updated_at": float(row["updated_at"]),
        }

    def _row_to_record(
        self,
        row: sqlite3.Row,
        *,
        include_initiative: bool = True,
        include_dispatch: bool = True,
        tolerate_dispatch_error: bool = False,
    ) -> dict[str, Any]:
        record = {
            "id": str(row["id"]),
            "name": str(row["name"]),
            "kind": str(row["kind"]),
            "config": json.loads(row["config_json"]),
            "task": str(row["task"]),
            "capabilities": list(json.loads(row["capabilities_json"])),
            "priority": int(row["priority"]),
            "interval_seconds": float(row["interval_seconds"]),
            "next_at": float(row["next_at"]),
            "enabled": bool(row["enabled"]),
            "emit_initial": bool(row["emit_initial"]),
            "last_digest": row["last_digest"],
            "last_summary": row["last_summary"],
            "last_evidence": (
                None
                if row["last_evidence_json"] is None
                else json.loads(row["last_evidence_json"])
            ),
            "last_observed_at": row["last_observed_at"],
            "last_changed_at": row["last_changed_at"],
            "last_error": row["last_error"],
            "consecutive_errors": int(row["consecutive_errors"]),
            "change_count": int(row["change_count"]),
            "created_at": float(row["created_at"]),
            "updated_at": float(row["updated_at"]),
        }
        if include_initiative:
            record["initiative"] = self._initiative_record(str(row["id"]))
        if include_dispatch:
            record["dispatch"] = self._dispatch_record(
                str(row["id"]),
                tolerate_corrupt=tolerate_dispatch_error,
            )
        return record

    def list(self) -> list[dict[str, Any]]:
        rows = self.store.connection.execute(
            "SELECT * FROM observers ORDER BY name ASC"
        ).fetchall()
        return [
            self._row_to_record(row, tolerate_dispatch_error=True)
            for row in rows
        ]

    def get(
        self,
        name: str,
        *,
        tolerate_dispatch_error: bool = False,
    ) -> dict[str, Any]:
        row = self.store.connection.execute(
            "SELECT * FROM observers WHERE name=?",
            (name,),
        ).fetchone()
        if row is None:
            raise KeyError(name)
        return self._row_to_record(
            row,
            tolerate_dispatch_error=tolerate_dispatch_error,
        )

    def set_enabled(self, name: str, *, enabled: bool, now: float) -> None:
        row = self.store.connection.execute(
            "SELECT id FROM observers WHERE name=?",
            (name,),
        ).fetchone()
        if row is None:
            raise KeyError(name)
        cursor = self.store.connection.execute(
            "UPDATE observers SET enabled=?, updated_at=? WHERE name=?",
            (1 if enabled else 0, now, name),
        )
        if cursor.rowcount != 1:
            raise KeyError(name)
        self.store.append_journal(
            event_type="OBSERVER_ENABLED" if enabled else "OBSERVER_DISABLED",
            subject_id=str(row["id"]),
            payload={"name": name},
            now=now,
        )

    def remove(self, name: str, *, now: float) -> None:
        row = self.store.connection.execute(
            "SELECT id FROM observers WHERE name=?",
            (name,),
        ).fetchone()
        if row is None:
            raise KeyError(name)
        cursor = self.store.connection.execute(
            "DELETE FROM observers WHERE name=?",
            (name,),
        )
        if cursor.rowcount != 1:
            raise KeyError(name)
        self.store.append_journal(
            event_type="OBSERVER_REMOVED",
            subject_id=str(row["id"]),
            payload={"name": name},
            now=now,
        )

    def probe(self, name: str) -> Observation:
        record = self.get(name)
        adapter = self.adapters.get(record["kind"])
        if adapter is None:
            raise RuntimeError(f"observer adapter is unavailable: {record['kind']}")
        return adapter.sample(record["config"])

    def _claim_due(self, observer_id: str, *, now: float) -> dict[str, Any] | None:
        self.store.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.store.connection.execute(
                "SELECT * FROM observers WHERE id=?",
                (observer_id,),
            ).fetchone()
            if (
                row is None
                or not bool(row["enabled"])
                or float(row["next_at"]) > now
            ):
                self.store.connection.execute("COMMIT")
                return None
            next_at = now + float(row["interval_seconds"])
            cursor = self.store.connection.execute(
                """
                UPDATE observers
                SET next_at=?, updated_at=?
                WHERE id=? AND enabled=1 AND next_at <= ?
                """,
                (next_at, now, observer_id, now),
            )
            if cursor.rowcount != 1:
                self.store.connection.execute("COMMIT")
                return None
            self.store.connection.execute("COMMIT")
            return self._row_to_record(
                row,
                include_initiative=False,
                include_dispatch=False,
            )
        except BaseException:
            self.store.connection.execute("ROLLBACK")
            raise

    def _record_error(
        self,
        record: dict[str, Any],
        *,
        error: str,
        now: float,
    ) -> None:
        self.store.connection.execute(
            """
            UPDATE observers
            SET last_error=?, consecutive_errors=consecutive_errors+1,
                last_observed_at=?, updated_at=?
            WHERE id=?
            """,
            (error, now, now, record["id"]),
        )
        self.store.append_journal(
            event_type="OBSERVER_SAMPLE_FAILED",
            subject_id=record["id"],
            payload={"name": record["name"], "error": error},
            now=now,
        )

    def _record_observation(
        self,
        record: dict[str, Any],
        observation: Observation,
        *,
        now: float,
    ) -> bool:
        self.store.connection.execute("BEGIN IMMEDIATE")
        try:
            current = self.store.connection.execute(
                "SELECT * FROM observers WHERE id=?",
                (record["id"],),
            ).fetchone()
            if current is None:
                self.store.connection.execute("COMMIT")
                return False

            initiative_row = self.store.connection.execute(
                "SELECT * FROM observer_initiative WHERE observer_id=?",
                (record["id"],),
            ).fetchone()
            if initiative_row is None:
                raise RuntimeError(
                    f"observer initiative state is missing: {record['id']}"
                )
            dispatch_row = self.store.connection.execute(
                "SELECT * FROM observer_dispatch WHERE observer_id=?",
                (record["id"],),
            ).fetchone()
            if dispatch_row is None:
                raise RuntimeError(
                    f"observer dispatch state is missing: {record['id']}"
                )
            dispatch_config = json.loads(dispatch_row["config_json"])
            if not isinstance(dispatch_config, dict):
                raise RuntimeError("observer dispatch config must be an object")
            dispatch_route = str(dispatch_row["route_kind"])
            capabilities = set(json.loads(current["capabilities_json"]))
            self._validate_dispatch(
                route_kind=dispatch_route,
                config=dispatch_config,
                capabilities=capabilities,
            )

            previous_digest = current["last_digest"]
            is_initial = previous_digest is None
            changed = is_initial or str(previous_digest) != observation.digest
            candidate_change = changed and (
                not is_initial or bool(current["emit_initial"])
            )
            change_count = int(current["change_count"])
            decision = None
            emit = False

            if candidate_change:
                change_count += 1
                policy = build_initiative_policy(
                    str(initiative_row["policy_kind"]),
                    json.loads(initiative_row["config_json"]),
                )
                decision = policy.decide(
                    now=now,
                    state=json.loads(initiative_row["state_json"]),
                )
                emit = decision.emit

            self.store.connection.execute(
                """
                UPDATE observers
                SET last_digest=?, last_summary=?, last_evidence_json=?,
                    last_observed_at=?,
                    last_changed_at=CASE WHEN ? THEN ? ELSE last_changed_at END,
                    last_error=NULL, consecutive_errors=0,
                    change_count=?, updated_at=?
                WHERE id=?
                """,
                (
                    observation.digest,
                    observation.summary,
                    json.dumps(
                        observation.evidence,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    now,
                    1 if candidate_change else 0,
                    now,
                    change_count,
                    now,
                    record["id"],
                ),
            )

            if decision is not None:
                decision_json = {
                    "emit": bool(decision.emit),
                    "reason": decision.reason,
                    "metrics": decision.metrics,
                }
                self.store.connection.execute(
                    """
                    UPDATE observer_initiative
                    SET state_json=?, last_decision_json=?, updated_at=?
                    WHERE observer_id=?
                    """,
                    (
                        json.dumps(
                            decision.state,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        json.dumps(
                            decision_json,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        now,
                        record["id"],
                    ),
                )

            if emit:
                assert decision is not None
                if dispatch_route == "autonomous_turn":
                    event_kind = "autonomous.turn"
                    event_id = self.store.request_autonomous_turn(
                        task=(
                            str(current["task"])
                            + "\n\nObservation evidence (read-only):\n"
                            + json.dumps(
                                observation.evidence,
                                sort_keys=True,
                                separators=(",", ":"),
                            )
                        ),
                        capabilities=capabilities,
                        source="EXTERNAL",
                        reason=(
                            f"Observer {current['name']} detected change: "
                            f"{observation.summary}"
                        ),
                        now=now,
                        dedup_key=(
                            f"observer:{record['id']}:change:{change_count}:"
                            f"{observation.digest}"
                        ),
                        priority=int(current["priority"]),
                    )
                else:
                    assert dispatch_route == "volition_signal"
                    event_kind = "volition.signal"
                    observation_context = {
                        "observer_id": str(record["id"]),
                        "observer_name": str(current["name"]),
                        "observer_kind": str(current["kind"]),
                        "digest": observation.digest,
                        "summary": observation.summary,
                        "change_count": change_count,
                        "evidence": observation.evidence,
                        "initiative": {
                            "policy_kind": str(initiative_row["policy_kind"]),
                            "reason": decision.reason,
                            "metrics": decision.metrics,
                        },
                    }
                    event_id = VolitionBridge(self.store).enqueue_signal(
                        payload={
                            **dispatch_config,
                            "source": f"observer:{record['id']}",
                            "effect_authority": False,
                            "observation_context": observation_context,
                        },
                        now=now,
                        dedup_key=(
                            f"observer:{record['id']}:volition:{change_count}:"
                            f"{observation.digest}"
                        ),
                    )
                self.store.append_journal(
                    event_type="OBSERVER_CHANGE_DETECTED",
                    subject_id=record["id"],
                    payload={
                        "name": str(current["name"]),
                        "digest": observation.digest,
                        "change_count": change_count,
                        "dispatch_route": dispatch_route,
                        "event_kind": event_kind,
                        "event_id": event_id,
                        "initiative_policy": str(
                            initiative_row["policy_kind"]
                        ),
                        "initiative_reason": decision.reason,
                        "initiative_metrics": decision.metrics,
                    },
                    now=now,
                )
            elif decision is not None:
                self.store.append_journal(
                    event_type="OBSERVER_CHANGE_SUPPRESSED",
                    subject_id=record["id"],
                    payload={
                        "name": str(current["name"]),
                        "digest": observation.digest,
                        "change_count": change_count,
                        "initiative_policy": str(
                            initiative_row["policy_kind"]
                        ),
                        "initiative_reason": decision.reason,
                        "initiative_metrics": decision.metrics,
                    },
                    now=now,
                )
            elif is_initial:
                self.store.append_journal(
                    event_type="OBSERVER_BASELINED",
                    subject_id=record["id"],
                    payload={
                        "name": str(current["name"]),
                        "digest": observation.digest,
                    },
                    now=now,
                )
            self.store.connection.execute("COMMIT")
            return emit
        except BaseException:
            self.store.connection.execute("ROLLBACK")
            raise

    def tick(self, *, now: float) -> ObserverTickResult:
        due_ids = [
            str(row["id"])
            for row in self.store.connection.execute(
                """
                SELECT id FROM observers
                WHERE enabled=1 AND next_at <= ?
                ORDER BY next_at ASC, name ASC
                """,
                (now,),
            ).fetchall()
        ]
        sampled = 0
        emitted = 0
        errors = 0
        for observer_id in due_ids:
            record = self._claim_due(observer_id, now=now)
            if record is None:
                continue
            sampled += 1
            adapter = self.adapters.get(record["kind"])
            if adapter is None:
                errors += 1
                self._record_error(
                    record,
                    error=f"observer adapter is unavailable: {record['kind']}",
                    now=now,
                )
                continue
            try:
                observation = adapter.sample(record["config"])
            except Exception as exc:
                errors += 1
                self._record_error(
                    record,
                    error=f"{type(exc).__name__}: {exc}",
                    now=now,
                )
                continue
            try:
                if self._record_observation(record, observation, now=now):
                    emitted += 1
            except Exception as exc:
                errors += 1
                self._record_error(
                    record,
                    error=f"{type(exc).__name__}: {exc}",
                    now=now,
                )
        return ObserverTickResult(sampled=sampled, emitted=emitted, errors=errors)

    def snapshot(self, *, now: float) -> dict[str, Any]:
        row = self.store.connection.execute(
            """
            SELECT
                SUM(CASE WHEN enabled=1 THEN 1 ELSE 0 END) AS enabled_count,
                SUM(CASE WHEN enabled=1 AND next_at <= ? THEN 1 ELSE 0 END) AS due_count,
                SUM(CASE WHEN last_error IS NOT NULL THEN 1 ELSE 0 END) AS error_count
            FROM observers
            """,
            (now,),
        ).fetchone()
        return {
            "enabled": 0 if row is None or row["enabled_count"] is None else int(row["enabled_count"]),
            "due": 0 if row is None or row["due_count"] is None else int(row["due_count"]),
            "with_errors": 0 if row is None or row["error_count"] is None else int(row["error_count"]),
        }
