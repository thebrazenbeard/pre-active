from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
import uuid
from typing import Any, Callable, Iterator

from .contracts import RUN_CONTRACT_VERSION, validate_contract_version


_SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    priority INTEGER NOT NULL,
    dedup_key TEXT UNIQUE,
    status TEXT NOT NULL,
    available_at REAL NOT NULL,
    lease_owner TEXT,
    lease_until REAL,
    lease_token TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    dead_lettered_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS events_claim_idx
ON events(status, available_at, priority DESC, created_at ASC);
CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    content TEXT NOT NULL,
    salience REAL NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS memories_rank_idx
ON memories(salience DESC, created_at DESC);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    task TEXT NOT NULL,
    status TEXT NOT NULL,
    capabilities_json TEXT NOT NULL,
    step_count INTEGER NOT NULL DEFAULT 0,
    final_text TEXT,
    last_error TEXT,
    blocked_request_id TEXT,
    source_event_id TEXT UNIQUE,
    failed_event_id TEXT,
    control_action TEXT,
    control_reason TEXT,
    control_requested_at REAL,
    paused_event_id TEXT,
    paused_at REAL,
    cancelled_at REAL,
    autonomous_turn_count INTEGER NOT NULL DEFAULT 0,
    contract_version INTEGER NOT NULL DEFAULT 1,
    contract_event_id TEXT,
    contract_resume_status TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS run_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    message_key TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS run_messages_idx ON run_messages(run_id, id);
CREATE TABLE IF NOT EXISTS run_step_decisions (
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    step INTEGER NOT NULL,
    decision_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (run_id, step)
);
CREATE TABLE IF NOT EXISTS progress_checkpoints (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    step INTEGER NOT NULL CHECK (step >= 0),
    state TEXT NOT NULL CHECK (state IN ('CANDIDATE', 'VERIFIED', 'REJECTED')),
    summary TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    producer TEXT NOT NULL,
    verifier TEXT,
    decision_reason TEXT,
    created_at REAL NOT NULL,
    decided_at REAL
);
CREATE INDEX IF NOT EXISTS progress_checkpoints_run_idx
ON progress_checkpoints(run_id, created_at, id);
CREATE INDEX IF NOT EXISTS progress_checkpoints_state_idx
ON progress_checkpoints(state, created_at, id);
CREATE TABLE IF NOT EXISTS schedules (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    every_seconds REAL,
    next_at REAL NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS model_targets (
    name TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    base_url TEXT NOT NULL,
    model TEXT NOT NULL,
    api_key_env TEXT,
    provenance_json TEXT,
    is_active INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS model_targets_one_active_idx
ON model_targets(is_active) WHERE is_active=1;
CREATE TABLE IF NOT EXISTS volition_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    snapshot_json TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS volition_signal_receipts (
    source_event_id TEXT PRIMARY KEY,
    state_revision INTEGER NOT NULL CHECK (state_revision >= 1),
    cognition_event_id TEXT,
    goal_id TEXT,
    target TEXT NOT NULL,
    urgency REAL,
    provenance TEXT NOT NULL,
    signal_source TEXT NOT NULL,
    choice_id TEXT,
    choice_class TEXT,
    choice_source TEXT,
    created_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS volition_receipt_cognition_idx
ON volition_signal_receipts(cognition_event_id)
WHERE cognition_event_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS journal (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    subject_id TEXT,
    payload_json TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS journal_subject_idx ON journal(subject_id, seq);
"""


class EventLeaseLost(RuntimeError):
    """Raised when a fenced event claim is no longer active."""


@dataclass(frozen=True)
class Event:
    id: str
    kind: str
    payload: dict[str, Any]
    priority: int
    status: str
    attempts: int
    lease_owner: str | None
    lease_until: float | None
    lease_token: str | None


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.executescript(_SCHEMA)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            event_columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(events)")}
            if "lease_token" not in event_columns:
                self.connection.execute("ALTER TABLE events ADD COLUMN lease_token TEXT")
            if "last_error" not in event_columns:
                self.connection.execute("ALTER TABLE events ADD COLUMN last_error TEXT")
            if "dead_lettered_at" not in event_columns:
                self.connection.execute("ALTER TABLE events ADD COLUMN dead_lettered_at REAL")
            run_columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(runs)")}
            if "blocked_request_id" not in run_columns:
                self.connection.execute("ALTER TABLE runs ADD COLUMN blocked_request_id TEXT")
            if "source_event_id" not in run_columns:
                self.connection.execute("ALTER TABLE runs ADD COLUMN source_event_id TEXT")
            if "failed_event_id" not in run_columns:
                self.connection.execute("ALTER TABLE runs ADD COLUMN failed_event_id TEXT")
            for column, ddl in (
                ("control_action", "TEXT"),
                ("control_reason", "TEXT"),
                ("control_requested_at", "REAL"),
                ("paused_event_id", "TEXT"),
                ("paused_at", "REAL"),
                ("cancelled_at", "REAL"),
                ("autonomous_turn_count", "INTEGER NOT NULL DEFAULT 0"),
                ("contract_version", "INTEGER NOT NULL DEFAULT 1"),
                ("contract_event_id", "TEXT"),
                ("contract_resume_status", "TEXT"),
            ):
                if column not in run_columns:
                    self.connection.execute(f"ALTER TABLE runs ADD COLUMN {column} {ddl}")
            self.connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS runs_source_event_idx ON runs(source_event_id) "
                "WHERE source_event_id IS NOT NULL"
            )
            message_columns = {
                row["name"] for row in self.connection.execute("PRAGMA table_info(run_messages)")
            }
            if "message_key" not in message_columns:
                self.connection.execute("ALTER TABLE run_messages ADD COLUMN message_key TEXT")
            self.connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS run_messages_key_idx "
                "ON run_messages(run_id, message_key) WHERE message_key IS NOT NULL"
            )
            model_target_columns = {
                row["name"]
                for row in self.connection.execute("PRAGMA table_info(model_targets)")
            }
            if "provenance_json" not in model_target_columns:
                self.connection.execute(
                    "ALTER TABLE model_targets ADD COLUMN provenance_json TEXT"
                )
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise

    def close(self) -> None:
        self.connection.close()

    def append_journal(
        self,
        *,
        event_type: str,
        subject_id: str | None,
        payload: dict[str, Any],
        now: float,
    ) -> int:
        cursor = self.connection.execute(
            "INSERT INTO journal (event_type, subject_id, payload_json, created_at) VALUES (?, ?, ?, ?)",
            (
                event_type,
                subject_id,
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
                now,
            ),
        )
        return int(cursor.lastrowid)

    def list_journal(
        self, *, subject_id: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        if subject_id is None:
            rows = self.connection.execute(
                "SELECT * FROM journal ORDER BY seq ASC LIMIT ?", (limit,)
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM journal WHERE subject_id=? ORDER BY seq ASC LIMIT ?",
                (subject_id, limit),
            ).fetchall()
        return [
            {
                "seq": int(row["seq"]),
                "event_type": str(row["event_type"]),
                "subject_id": row["subject_id"],
                "payload": json.loads(row["payload_json"]),
                "created_at": float(row["created_at"]),
            }
            for row in rows
        ]

    def enqueue_event(
        self,
        *,
        kind: str,
        payload: dict[str, Any],
        priority: int = 0,
        dedup_key: str | None = None,
        now: float,
        available_at: float | None = None,
    ) -> str:
        payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        effective_available_at = now if available_at is None else available_at

        def existing_for_dedup() -> sqlite3.Row | None:
            if not dedup_key:
                return None
            return self.connection.execute(
                """
                SELECT id, kind, payload_json, priority, available_at
                FROM events WHERE dedup_key=?
                """,
                (dedup_key,),
            ).fetchone()

        def reuse_or_reject(row: sqlite3.Row) -> str:
            same_request = (
                str(row["kind"]) == kind
                and str(row["payload_json"]) == payload_json
                and int(row["priority"]) == int(priority)
            )
            if not same_request:
                raise RuntimeError(
                    f"dedup_key is already bound to a different event request: {dedup_key}"
                )
            return str(row["id"])

        existing = existing_for_dedup()
        if existing is not None:
            return reuse_or_reject(existing)

        event_id = str(uuid.uuid4())
        try:
            self.connection.execute(
                """
                INSERT INTO events (
                    id, kind, payload_json, priority, dedup_key, status,
                    available_at, lease_owner, lease_until, attempts, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'PENDING', ?, NULL, NULL, 0, ?, ?)
                """,
                (
                    event_id,
                    kind,
                    payload_json,
                    int(priority),
                    dedup_key,
                    effective_available_at,
                    now,
                    now,
                ),
            )
        except sqlite3.IntegrityError:
            raced = existing_for_dedup()
            if raced is None:
                raise
            return reuse_or_reject(raced)
        self.append_journal(
            event_type="EVENT_ENQUEUED",
            subject_id=event_id,
            payload={"kind": kind, "priority": int(priority), "dedup_key": dedup_key},
            now=now,
        )
        return event_id


    def create_run(
        self,
        *,
        task: str,
        capabilities: set[str],
        now: float,
        source_event_id: str | None = None,
        contract_version: int = RUN_CONTRACT_VERSION,
    ) -> str:
        contract_version = validate_contract_version(contract_version)
        if source_event_id is not None:
            existing = self.connection.execute(
                "SELECT id, task, capabilities_json FROM runs WHERE source_event_id=?",
                (source_event_id,),
            ).fetchone()
            if existing is not None:
                if str(existing["task"]) != task or set(json.loads(existing["capabilities_json"])) != capabilities:
                    raise RuntimeError("source event is already bound to a different run request")
                return str(existing["id"])
        run_id = str(uuid.uuid4())
        try:
            self.connection.execute(
                """
                INSERT INTO runs (
                    id, task, status, capabilities_json, step_count, source_event_id,
                    contract_version, created_at, updated_at
                ) VALUES (?, ?, 'RUNNING', ?, 0, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    task,
                    json.dumps(sorted(capabilities)),
                    source_event_id,
                    contract_version,
                    now,
                    now,
                ),
            )
        except sqlite3.IntegrityError:
            if source_event_id is None:
                raise
            existing = self.connection.execute(
                "SELECT id, task, capabilities_json FROM runs WHERE source_event_id=?",
                (source_event_id,),
            ).fetchone()
            if existing is None:
                raise
            if str(existing["task"]) != task or set(json.loads(existing["capabilities_json"])) != capabilities:
                raise RuntimeError("source event is already bound to a different run request")
            return str(existing["id"])
        return run_id

    def create_run_with_initial_step(
        self,
        *,
        task: str,
        capabilities: set[str],
        now: float,
        source_event_id: str | None = None,
        priority: int = 0,
        contract_version: int = RUN_CONTRACT_VERSION,
    ) -> str:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            run_id = self.create_run(
                task=task,
                capabilities=capabilities,
                now=now,
                source_event_id=source_event_id,
                contract_version=contract_version,
            )
            self.enqueue_event(
                kind="run.step",
                payload={"run_id": run_id, "step": 0},
                priority=priority,
                dedup_key=f"run-step:{run_id}:0",
                now=now,
            )
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise
        return run_id

    def get_run(self, run_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        return {
            "id": str(row["id"]),
            "task": str(row["task"]),
            "status": str(row["status"]),
            "capabilities": set(json.loads(row["capabilities_json"])),
            "step_count": int(row["step_count"]),
            "final_text": row["final_text"],
            "last_error": row["last_error"],
            "blocked_request_id": row["blocked_request_id"],
            "failed_event_id": row["failed_event_id"],
            "control_action": row["control_action"],
            "control_reason": row["control_reason"],
            "control_requested_at": row["control_requested_at"],
            "paused_event_id": row["paused_event_id"],
            "paused_at": row["paused_at"],
            "cancelled_at": row["cancelled_at"],
            "autonomous_turn_count": int(row["autonomous_turn_count"]),
            "contract_version": int(row["contract_version"]),
            "contract_event_id": row["contract_event_id"],
            "contract_resume_status": row["contract_resume_status"],
        }

    def request_run_control(
        self,
        run_id: str,
        *,
        action: str,
        reason: str | None,
        now: float,
    ) -> None:
        normalized = action.upper()
        if normalized not in {"PAUSE", "CANCEL"}:
            raise ValueError("run control action must be PAUSE or CANCEL")
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                """
                SELECT status, step_count, control_action, control_reason,
                       paused_event_id, contract_event_id, contract_resume_status
                FROM runs WHERE id=?
                """,
                (run_id,),
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            status = str(row["status"])
            if status in {"COMPLETED", "FAILED", "CANCELLED"}:
                raise RuntimeError(f"terminal run cannot accept control request: {status}")
            if status == "BLOCKED_CONTRACT":
                if normalized == "PAUSE":
                    raise RuntimeError("run is already blocked on contract compatibility")
                event_id = row["contract_event_id"]
                if not isinstance(event_id, str) or not event_id:
                    raise RuntimeError("contract-blocked run is missing its paused event")
                self.append_journal(
                    event_type="RUN_CONTROL_REQUESTED",
                    subject_id=run_id,
                    payload={"action": normalized, "reason": reason},
                    now=now,
                )
                event_cursor = self.connection.execute(
                    """
                    UPDATE events
                    SET status='CANCELLED', updated_at=?
                    WHERE id=? AND status='PAUSED'
                    """,
                    (now, event_id),
                )
                if event_cursor.rowcount != 1:
                    raise RuntimeError(
                        "contract-blocked cancellation lost paused event"
                    )
                run_cursor = self.connection.execute(
                    """
                    UPDATE runs
                    SET status='CANCELLED', control_action=NULL,
                        control_reason=?, control_requested_at=?,
                        contract_event_id=NULL, contract_resume_status=NULL,
                        cancelled_at=?, updated_at=?
                    WHERE id=? AND status='BLOCKED_CONTRACT'
                    """,
                    (reason, now, now, now, run_id),
                )
                if run_cursor.rowcount != 1:
                    raise RuntimeError(
                        "contract-blocked cancellation lost blocked run state"
                    )
                self.append_journal(
                    event_type="EVENT_CANCELLED",
                    subject_id=event_id,
                    payload={"run_id": run_id, "from_contract_block": True},
                    now=now,
                )
                self.append_journal(
                    event_type="RUN_CANCELLED",
                    subject_id=run_id,
                    payload={
                        "event_id": event_id,
                        "reason": reason,
                        "from_contract_block": True,
                    },
                    now=now,
                )
                self.connection.execute("COMMIT")
                return
            current = row["control_action"]
            if current == "CANCEL" and normalized == "PAUSE":
                raise RuntimeError("cannot replace CANCEL with PAUSE")
            if current == normalized and row["control_reason"] == reason:
                self.connection.execute("COMMIT")
                return

            if status == "WAITING":
                step = int(row["step_count"])
                event = self.connection.execute(
                    """
                    SELECT id, status FROM events
                    WHERE dedup_key=?
                    """,
                    (f"run-step:{run_id}:{step}",),
                ).fetchone()
                if event is None or str(event["status"]) != "PENDING":
                    raise RuntimeError(
                        "waiting run lost its pending autonomous turn event"
                    )
                event_id = str(event["id"])
                self.append_journal(
                    event_type="RUN_CONTROL_REQUESTED",
                    subject_id=run_id,
                    payload={"action": normalized, "reason": reason},
                    now=now,
                )
                if normalized == "PAUSE":
                    event_cursor = self.connection.execute(
                        """
                        UPDATE events
                        SET status='PAUSED', updated_at=?
                        WHERE id=? AND status='PENDING'
                        """,
                        (now, event_id),
                    )
                    if event_cursor.rowcount != 1:
                        raise RuntimeError(
                            "waiting run pause lost pending autonomous event"
                        )
                    run_cursor = self.connection.execute(
                        """
                        UPDATE runs
                        SET status='PAUSED', control_action=NULL,
                            control_reason=?, control_requested_at=?,
                            paused_event_id=?, paused_at=?, updated_at=?
                        WHERE id=? AND status='WAITING' AND step_count=?
                        """,
                        (reason, now, event_id, now, now, run_id, step),
                    )
                    if run_cursor.rowcount != 1:
                        raise RuntimeError("waiting run pause lost WAITING state")
                    self.append_journal(
                        event_type="EVENT_PAUSED",
                        subject_id=event_id,
                        payload={"run_id": run_id, "from_waiting": True},
                        now=now,
                    )
                    self.append_journal(
                        event_type="RUN_PAUSED",
                        subject_id=run_id,
                        payload={
                            "event_id": event_id,
                            "reason": reason,
                            "from_waiting": True,
                        },
                        now=now,
                    )
                else:
                    event_cursor = self.connection.execute(
                        """
                        UPDATE events
                        SET status='CANCELLED', updated_at=?
                        WHERE id=? AND status='PENDING'
                        """,
                        (now, event_id),
                    )
                    if event_cursor.rowcount != 1:
                        raise RuntimeError(
                            "waiting run cancellation lost pending autonomous event"
                        )
                    run_cursor = self.connection.execute(
                        """
                        UPDATE runs
                        SET status='CANCELLED', control_action=NULL,
                            control_reason=?, control_requested_at=?,
                            paused_event_id=NULL, cancelled_at=?, updated_at=?
                        WHERE id=? AND status='WAITING' AND step_count=?
                        """,
                        (reason, now, now, now, run_id, step),
                    )
                    if run_cursor.rowcount != 1:
                        raise RuntimeError(
                            "waiting run cancellation lost WAITING state"
                        )
                    self.append_journal(
                        event_type="EVENT_CANCELLED",
                        subject_id=event_id,
                        payload={"run_id": run_id, "from_waiting": True},
                        now=now,
                    )
                    self.append_journal(
                        event_type="RUN_CANCELLED",
                        subject_id=run_id,
                        payload={
                            "event_id": event_id,
                            "reason": reason,
                            "from_waiting": True,
                        },
                        now=now,
                    )
                self.connection.execute("COMMIT")
                return

            if status == "PAUSED":
                if normalized == "PAUSE":
                    self.connection.execute(
                        """
                        UPDATE runs
                        SET control_reason=?, control_requested_at=?, updated_at=?
                        WHERE id=? AND status='PAUSED'
                        """,
                        (reason, now, now, run_id),
                    )
                    self.append_journal(
                        event_type="RUN_CONTROL_REQUESTED",
                        subject_id=run_id,
                        payload={"action": normalized, "reason": reason},
                        now=now,
                    )
                    self.connection.execute("COMMIT")
                    return

                paused_event_id = row["paused_event_id"]
                self.append_journal(
                    event_type="RUN_CONTROL_REQUESTED",
                    subject_id=run_id,
                    payload={"action": normalized, "reason": reason},
                    now=now,
                )
                if paused_event_id is not None:
                    event_cursor = self.connection.execute(
                        """
                        UPDATE events
                        SET status='CANCELLED', lease_owner=NULL, lease_until=NULL,
                            lease_token=NULL, updated_at=?
                        WHERE id=? AND status='PAUSED'
                        """,
                        (now, paused_event_id),
                    )
                    if event_cursor.rowcount != 1:
                        raise RuntimeError("paused run cancellation lost paused event")
                    self.append_journal(
                        event_type="EVENT_CANCELLED",
                        subject_id=str(paused_event_id),
                        payload={"run_id": run_id, "from_paused": True},
                        now=now,
                    )
                run_cursor = self.connection.execute(
                    """
                    UPDATE runs
                    SET status='CANCELLED', control_action=NULL, control_reason=?,
                        control_requested_at=?, paused_event_id=NULL, cancelled_at=?,
                        updated_at=?
                    WHERE id=? AND status='PAUSED'
                    """,
                    (reason, now, now, now, run_id),
                )
                if run_cursor.rowcount != 1:
                    raise RuntimeError("paused run cancellation lost PAUSED state")
                self.append_journal(
                    event_type="RUN_CANCELLED",
                    subject_id=run_id,
                    payload={
                        "reason": reason,
                        "from_paused": True,
                        "event_id": paused_event_id,
                    },
                    now=now,
                )
                self.connection.execute("COMMIT")
                return

            self.connection.execute(
                """
                UPDATE runs
                SET control_action=?, control_reason=?, control_requested_at=?, updated_at=?
                WHERE id=?
                """,
                (normalized, reason, now, now, run_id),
            )
            self.append_journal(
                event_type="RUN_CONTROL_REQUESTED",
                subject_id=run_id,
                payload={"action": normalized, "reason": reason},
                now=now,
            )
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise

    def block_run_for_contract_mismatch(
        self,
        *,
        run_id: str,
        event_id: str,
        worker_id: str,
        lease_token: str,
        runtime_contract_version: int,
        now: float,
    ) -> None:
        runtime_contract_version = validate_contract_version(
            runtime_contract_version
        )
        self._require_active_claim(
            event_id,
            worker_id=worker_id,
            lease_token=lease_token,
            now=now,
        )
        row = self.connection.execute(
            """
            SELECT status, step_count, contract_version
            FROM runs WHERE id=?
            """,
            (run_id,),
        ).fetchone()
        if row is None:
            raise KeyError(run_id)
        prior_status = str(row["status"])
        if prior_status not in {"RUNNING", "WAITING"}:
            raise RuntimeError(
                "contract mismatch can only block RUNNING or WAITING runs"
            )
        run_contract_version = int(row["contract_version"])
        if run_contract_version == runtime_contract_version:
            raise RuntimeError("run contract already matches runtime contract")

        event_cursor = self.connection.execute(
            """
            UPDATE events
            SET status='PAUSED', lease_owner=NULL, lease_until=NULL,
                lease_token=NULL, updated_at=?
            WHERE id=? AND status='CLAIMED'
              AND lease_owner=? AND lease_token=?
              AND lease_until IS NOT NULL AND lease_until > ?
            """,
            (now, event_id, worker_id, lease_token, now),
        )
        if event_cursor.rowcount != 1:
            raise EventLeaseLost(
                "contract mismatch pause lost event lease ownership"
            )
        message = (
            f"run contract version {run_contract_version} is incompatible "
            f"with runtime contract version {runtime_contract_version}"
        )
        run_cursor = self.connection.execute(
            """
            UPDATE runs
            SET status='BLOCKED_CONTRACT', contract_event_id=?,
                contract_resume_status=?, control_action=NULL,
                last_error=?, updated_at=?
            WHERE id=? AND status=? AND contract_version=?
            """,
            (
                event_id,
                prior_status,
                message,
                now,
                run_id,
                prior_status,
                run_contract_version,
            ),
        )
        if run_cursor.rowcount != 1:
            raise RuntimeError("contract mismatch lost exact run state")
        self.append_journal(
            event_type="EVENT_PAUSED",
            subject_id=event_id,
            payload={
                "run_id": run_id,
                "reason": "contract_mismatch",
            },
            now=now,
        )
        self.append_journal(
            event_type="RUN_CONTRACT_BLOCKED",
            subject_id=run_id,
            payload={
                "event_id": event_id,
                "run_contract_version": run_contract_version,
                "runtime_contract_version": runtime_contract_version,
                "resume_status": prior_status,
            },
            now=now,
        )

    def apply_claimed_run_control(
        self,
        *,
        run_id: str,
        event_id: str,
        worker_id: str,
        lease_token: str,
        now: float,
        advance_step: bool = False,
    ) -> str | None:
        row = self.connection.execute(
            """
            SELECT status, step_count, control_action, control_reason
            FROM runs WHERE id=?
            """,
            (run_id,),
        ).fetchone()
        if row is None:
            raise KeyError(run_id)
        if str(row["status"]) != "RUNNING":
            return None
        action = row["control_action"]
        if action not in {"PAUSE", "CANCEL"}:
            return None
        reason = row["control_reason"]

        self._require_active_claim(
            event_id,
            worker_id=worker_id,
            lease_token=lease_token,
            now=now,
        )

        if action == "PAUSE":
            if advance_step:
                cursor = self.connection.execute(
                    """
                    UPDATE runs
                    SET status='PAUSED', step_count=step_count+1,
                        control_action=NULL, paused_event_id=NULL, paused_at=?,
                        last_error=NULL, updated_at=?
                    WHERE id=? AND status='RUNNING'
                    """,
                    (now, now, run_id),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("run pause lost RUNNING state")
                self.ack_event(
                    event_id,
                    worker_id=worker_id,
                    lease_token=lease_token,
                    now=now,
                )
            else:
                event_cursor = self.connection.execute(
                    """
                    UPDATE events
                    SET status='PAUSED', lease_owner=NULL, lease_until=NULL,
                        lease_token=NULL, updated_at=?
                    WHERE id=? AND status='CLAIMED' AND lease_owner=? AND lease_token=?
                      AND lease_until IS NOT NULL AND lease_until > ?
                    """,
                    (now, event_id, worker_id, lease_token, now),
                )
                if event_cursor.rowcount != 1:
                    raise EventLeaseLost("event pause lost lease ownership")
                cursor = self.connection.execute(
                    """
                    UPDATE runs
                    SET status='PAUSED', control_action=NULL, paused_event_id=?,
                        paused_at=?, updated_at=?
                    WHERE id=? AND status='RUNNING'
                    """,
                    (event_id, now, now, run_id),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("run pause lost RUNNING state")
                self.append_journal(
                    event_type="EVENT_PAUSED",
                    subject_id=event_id,
                    payload={"run_id": run_id},
                    now=now,
                )
            self.append_journal(
                event_type="RUN_PAUSED",
                subject_id=run_id,
                payload={
                    "event_id": event_id,
                    "reason": reason,
                    "advanced_step": bool(advance_step),
                },
                now=now,
            )
            return "PAUSE"

        sets = [
            "status='CANCELLED'",
            "control_action=NULL",
            "paused_event_id=NULL",
            "cancelled_at=?",
            "updated_at=?",
        ]
        params: list[Any] = [now, now]
        if advance_step:
            sets.append("step_count=step_count+1")
            sets.append("last_error=NULL")
        params.append(run_id)
        run_cursor = self.connection.execute(
            f"UPDATE runs SET {', '.join(sets)} WHERE id=? AND status='RUNNING'",
            params,
        )
        if run_cursor.rowcount != 1:
            raise RuntimeError("run cancellation lost RUNNING state")
        event_cursor = self.connection.execute(
            """
            UPDATE events
            SET status='CANCELLED', lease_owner=NULL, lease_until=NULL,
                lease_token=NULL, updated_at=?
            WHERE id=? AND status='CLAIMED' AND lease_owner=? AND lease_token=?
              AND lease_until IS NOT NULL AND lease_until > ?
            """,
            (now, event_id, worker_id, lease_token, now),
        )
        if event_cursor.rowcount != 1:
            raise EventLeaseLost("event cancellation lost lease ownership")
        self.append_journal(
            event_type="EVENT_CANCELLED",
            subject_id=event_id,
            payload={"run_id": run_id},
            now=now,
        )
        self.append_journal(
            event_type="RUN_CANCELLED",
            subject_id=run_id,
            payload={
                "event_id": event_id,
                "reason": reason,
                "advanced_step": bool(advance_step),
            },
            now=now,
        )
        return "CANCEL"

    def resume_paused_run(
        self,
        run_id: str,
        *,
        reason: str | None,
        now: float,
        priority: int = 0,
    ) -> None:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                """
                SELECT status, step_count, paused_event_id, control_action
                FROM runs WHERE id=?
                """,
                (run_id,),
            ).fetchone()
            if row is None:
                raise KeyError(run_id)

            status = str(row["status"])
            control_action = row["control_action"]
            if status == "RUNNING":
                if control_action == "CANCEL":
                    raise RuntimeError("cannot resume while CANCEL is pending")
                if control_action != "PAUSE":
                    raise RuntimeError("run is not PAUSED and has no pending PAUSE")
                cursor = self.connection.execute(
                    """
                    UPDATE runs
                    SET control_action=NULL, control_reason=NULL,
                        control_requested_at=NULL, updated_at=?
                    WHERE id=? AND status='RUNNING' AND control_action='PAUSE'
                    """,
                    (now, run_id),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("pending PAUSE withdrawal lost run control state")
                self.append_journal(
                    event_type="RUN_RESUMED",
                    subject_id=run_id,
                    payload={
                        "reason": reason,
                        "pending_pause_withdrawn": True,
                        "step": int(row["step_count"]),
                    },
                    now=now,
                )
                self.connection.execute("COMMIT")
                return
            if status != "PAUSED":
                raise RuntimeError("run is not PAUSED")

            paused_event_id = row["paused_event_id"]
            step = int(row["step_count"])
            if paused_event_id is not None:
                event = self.connection.execute(
                    "SELECT kind, payload_json, status FROM events WHERE id=?",
                    (paused_event_id,),
                ).fetchone()
                if event is None or str(event["status"]) != "PAUSED":
                    raise RuntimeError("paused run lost its paused event")
                payload = json.loads(event["payload_json"])
                if (
                    str(event["kind"]) != "run.step"
                    or payload.get("run_id") != run_id
                    or payload.get("step") != step
                ):
                    raise RuntimeError("paused event does not match run generation")
                event_cursor = self.connection.execute(
                    """
                    UPDATE events
                    SET status='PENDING', available_at=?, updated_at=?
                    WHERE id=? AND status='PAUSED'
                    """,
                    (now, now, paused_event_id),
                )
                if event_cursor.rowcount != 1:
                    raise RuntimeError("paused event resume lost PAUSED state")
            else:
                dedup_key = f"run-step:{run_id}:{step}"
                existing = self.connection.execute(
                    """
                    SELECT id, kind, payload_json, status
                    FROM events WHERE dedup_key=?
                    """,
                    (dedup_key,),
                ).fetchone()
                if existing is None:
                    self.enqueue_event(
                        kind="run.step",
                        payload={"run_id": run_id, "step": step},
                        priority=priority,
                        dedup_key=dedup_key,
                        now=now,
                    )
                else:
                    payload = json.loads(existing["payload_json"])
                    if (
                        str(existing["kind"]) != "run.step"
                        or payload.get("run_id") != run_id
                        or payload.get("step") != step
                    ):
                        raise RuntimeError("existing run.step identity does not match paused run")
                    if str(existing["status"]) != "DONE":
                        raise RuntimeError(
                            "paused run successor already exists in nonterminal state"
                        )
                    event_cursor = self.connection.execute(
                        """
                        UPDATE events
                        SET status='PENDING', available_at=?, lease_owner=NULL,
                            lease_until=NULL, lease_token=NULL, updated_at=?
                        WHERE id=? AND status='DONE'
                        """,
                        (now, now, existing["id"]),
                    )
                    if event_cursor.rowcount != 1:
                        raise RuntimeError("paused run could not requeue prior completed step")
                    self.append_journal(
                        event_type="EVENT_RESUMED",
                        subject_id=str(existing["id"]),
                        payload={"run_id": run_id, "step": step},
                        now=now,
                    )

            cursor = self.connection.execute(
                """
                UPDATE runs
                SET status='RUNNING', paused_event_id=NULL, updated_at=?
                WHERE id=? AND status='PAUSED'
                """,
                (now, run_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("run resume lost PAUSED state")
            self.append_journal(
                event_type="RUN_RESUMED",
                subject_id=run_id,
                payload={"reason": reason, "step": step},
                now=now,
            )
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise

    def apply_blocked_run_control_after_reconciliation(
        self,
        run_id: str,
        *,
        now: float,
        effect_completed: bool,
    ) -> str | None:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                """
                SELECT status, control_action, control_reason, step_count
                FROM runs WHERE id=?
                """,
                (run_id,),
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            if str(row["status"]) != "BLOCKED_EFFECT":
                raise RuntimeError("run is not BLOCKED_EFFECT")
            action = row["control_action"]
            if action not in {"PAUSE", "CANCEL"}:
                self.connection.execute("COMMIT")
                return None
            reason = row["control_reason"]
            step_sql = ", step_count=step_count+1" if effect_completed else ""

            if action == "PAUSE":
                cursor = self.connection.execute(
                    f"""
                    UPDATE runs
                    SET status='PAUSED', blocked_request_id=NULL, control_action=NULL,
                        paused_event_id=NULL, paused_at=?, last_error=NULL, updated_at=?
                        {step_sql}
                    WHERE id=? AND status='BLOCKED_EFFECT' AND control_action='PAUSE'
                    """,
                    (now, now, run_id),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("blocked run pause lost control state")
                self.append_journal(
                    event_type="RUN_PAUSED",
                    subject_id=run_id,
                    payload={
                        "reason": reason,
                        "after_reconciliation": True,
                        "effect_completed": bool(effect_completed),
                    },
                    now=now,
                )
            else:
                cursor = self.connection.execute(
                    f"""
                    UPDATE runs
                    SET status='CANCELLED', blocked_request_id=NULL, control_action=NULL,
                        paused_event_id=NULL, cancelled_at=?, last_error=NULL, updated_at=?
                        {step_sql}
                    WHERE id=? AND status='BLOCKED_EFFECT' AND control_action='CANCEL'
                    """,
                    (now, now, run_id),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("blocked run cancellation lost control state")
                self.append_journal(
                    event_type="RUN_CANCELLED",
                    subject_id=run_id,
                    payload={
                        "reason": reason,
                        "after_reconciliation": True,
                        "effect_completed": bool(effect_completed),
                    },
                    now=now,
                )
            self.connection.execute("COMMIT")
            return str(action)
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise

    def _require_active_claim(
        self,
        event_id: str,
        *,
        worker_id: str,
        lease_token: str,
        now: float,
    ) -> float:
        row = self.connection.execute(
            """
            SELECT lease_until FROM events
            WHERE id=? AND status='CLAIMED' AND lease_owner=? AND lease_token=?
              AND lease_until IS NOT NULL AND lease_until > ?
            """,
            (event_id, worker_id, lease_token, now),
        ).fetchone()
        if row is None:
            raise EventLeaseLost("event progress commit lost lease ownership")
        return float(row["lease_until"])

    def assert_active_claim(
        self,
        event_id: str,
        *,
        worker_id: str,
        lease_token: str,
        now: float,
    ) -> None:
        self._require_active_claim(
            event_id,
            worker_id=worker_id,
            lease_token=lease_token,
            now=now,
        )

    @contextmanager
    def active_claim_transaction(
        self,
        event_id: str,
        *,
        worker_id: str,
        lease_token: str,
        now: float | Callable[[], float],
        validate: Callable[[], None] | None = None,
    ) -> Iterator[float]:
        if self.connection.in_transaction:
            raise RuntimeError("active claim transaction cannot be nested")

        def current_time() -> float:
            return float(now() if callable(now) else now)

        self.connection.execute("BEGIN IMMEDIATE")
        try:
            started_at = current_time()
            lease_deadline = self._require_active_claim(
                event_id,
                worker_id=worker_id,
                lease_token=lease_token,
                now=started_at,
            )
            yield started_at
            commit_at = current_time()
            if commit_at >= lease_deadline:
                raise EventLeaseLost(
                    "event progress commit crossed its admitted lease deadline"
                )
            if validate is not None:
                validate()
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise

    def get_volition_state(self) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT revision, snapshot_json, updated_at FROM volition_state WHERE id=1"
        ).fetchone()
        if row is None:
            return None
        snapshot = json.loads(str(row["snapshot_json"]))
        if not isinstance(snapshot, dict):
            raise RuntimeError("stored volition snapshot must be an object")
        return {
            "revision": int(row["revision"]),
            "snapshot": snapshot,
            "updated_at": float(row["updated_at"]),
        }

    def save_volition_state(
        self,
        snapshot: dict[str, Any],
        *,
        expected_revision: int,
        now: float,
    ) -> int:
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
            raise ValueError("expected_revision must be an integer")
        if expected_revision < 0:
            raise ValueError("expected_revision must be >= 0")
        if not isinstance(snapshot, dict):
            raise ValueError("volition snapshot must be an object")
        payload = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
        owns_transaction = not self.connection.in_transaction
        if owns_transaction:
            self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                "SELECT revision FROM volition_state WHERE id=1"
            ).fetchone()
            if row is None:
                if expected_revision != 0:
                    raise RuntimeError("volition state revision changed")
                revision = 1
                self.connection.execute(
                    """
                    INSERT INTO volition_state (id, snapshot_json, revision, updated_at)
                    VALUES (1, ?, ?, ?)
                    """,
                    (payload, revision, now),
                )
            else:
                current_revision = int(row["revision"])
                if current_revision != expected_revision:
                    raise RuntimeError("volition state revision changed")
                revision = current_revision + 1
                cursor = self.connection.execute(
                    """
                    UPDATE volition_state
                    SET snapshot_json=?, revision=?, updated_at=?
                    WHERE id=1 AND revision=?
                    """,
                    (payload, revision, now, current_revision),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("volition state revision changed")
            if owns_transaction:
                self.connection.execute("COMMIT")
            return revision
        except BaseException:
            if owns_transaction and self.connection.in_transaction:
                self.connection.execute("ROLLBACK")
            raise

    def get_volition_signal_receipt(
        self, source_event_id: str
    ) -> dict[str, Any] | None:
        row = self.connection.execute(
            """
            SELECT source_event_id, state_revision, cognition_event_id, goal_id,
                   target, urgency, provenance, signal_source, choice_id,
                   choice_class, choice_source, created_at
            FROM volition_signal_receipts
            WHERE source_event_id=?
            """,
            (source_event_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "source_event_id": str(row["source_event_id"]),
            "state_revision": int(row["state_revision"]),
            "cognition_event_id": row["cognition_event_id"],
            "goal_id": row["goal_id"],
            "target": str(row["target"]),
            "urgency": None if row["urgency"] is None else float(row["urgency"]),
            "provenance": str(row["provenance"]),
            "signal_source": str(row["signal_source"]),
            "choice_id": row["choice_id"],
            "choice_class": row["choice_class"],
            "choice_source": row["choice_source"],
            "created_at": float(row["created_at"]),
        }

    def record_volition_signal_receipt(
        self,
        *,
        source_event_id: str,
        state_revision: int,
        cognition_event_id: str | None,
        goal_id: str | None,
        target: str,
        urgency: float | None,
        provenance: str,
        signal_source: str,
        choice_id: str | None,
        choice_class: str | None,
        choice_source: str | None,
        now: float,
    ) -> None:
        if not source_event_id:
            raise ValueError("source_event_id is required")
        if state_revision < 1:
            raise ValueError("state_revision must be >= 1")
        owns_transaction = not self.connection.in_transaction
        if owns_transaction:
            self.connection.execute("BEGIN IMMEDIATE")
        try:
            existing = self.get_volition_signal_receipt(source_event_id)
            if existing is not None:
                raise RuntimeError("volition signal receipt already exists")
            self.connection.execute(
                """
                INSERT INTO volition_signal_receipts (
                    source_event_id, state_revision, cognition_event_id, goal_id,
                    target, urgency, provenance, signal_source, choice_id,
                    choice_class, choice_source, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_event_id,
                    int(state_revision),
                    cognition_event_id,
                    goal_id,
                    target,
                    urgency,
                    provenance,
                    signal_source,
                    choice_id,
                    choice_class,
                    choice_source,
                    now,
                ),
            )
            if owns_transaction:
                self.connection.execute("COMMIT")
        except sqlite3.IntegrityError as exc:
            if owns_transaction and self.connection.in_transaction:
                self.connection.execute("ROLLBACK")
            raise RuntimeError("volition signal receipt already exists") from exc
        except BaseException:
            if owns_transaction and self.connection.in_transaction:
                self.connection.execute("ROLLBACK")
            raise

    def request_autonomous_turn(
        self,
        *,
        task: str,
        capabilities: set[str],
        source: str,
        reason: str,
        now: float,
        available_at: float | None = None,
        dedup_key: str | None = None,
        priority: int = 0,
    ) -> str:
        normalized_source = source.upper().strip()
        if normalized_source not in {"EXTERNAL", "TEMPORAL", "OPEN_LOOP", "ENDOGENOUS"}:
            raise ValueError("autonomous turn source must be EXTERNAL, TEMPORAL, OPEN_LOOP, or ENDOGENOUS")
        if not task.strip():
            raise ValueError("autonomous turn task is required")
        if not reason.strip():
            raise ValueError("autonomous turn reason is required")
        return self.enqueue_event(
            kind="autonomous.turn",
            payload={
                "task": task.strip(),
                "capabilities": sorted(capabilities),
                "source": normalized_source,
                "reason": reason.strip(),
            },
            priority=priority,
            dedup_key=dedup_key,
            now=now,
            available_at=available_at,
        )

    def defer_run_for_autonomous_turn(
        self,
        *,
        run_id: str,
        expected_step: int,
        reason: str,
        delay_seconds: float,
        priority: int,
        now: float,
        endogenous_depth: int = 1,
    ) -> int:
        if not reason.strip():
            raise ValueError("autonomous turn reason is required")
        if delay_seconds < 0:
            raise ValueError("delay_seconds must be >= 0")
        if endogenous_depth < 1:
            raise ValueError("endogenous_depth must be >= 1")
        next_step = int(expected_step) + 1
        owns_transaction = not self.connection.in_transaction
        if owns_transaction:
            self.connection.execute("BEGIN IMMEDIATE")
        try:
            cursor = self.connection.execute(
                """
                UPDATE runs
                SET status='WAITING', step_count=step_count+1,
                    autonomous_turn_count=autonomous_turn_count+1,
                    last_error=NULL, updated_at=?
                WHERE id=? AND status='RUNNING' AND step_count=?
                """,
                (now, run_id, int(expected_step)),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(
                    "autonomous turn deferral lost expected run generation"
                )
            self.enqueue_event(
                kind="run.step",
                payload={
                    "run_id": run_id,
                    "step": next_step,
                    "autonomous": True,
                    "source": "ENDOGENOUS",
                    "reason": reason.strip(),
                    "endogenous_depth": int(endogenous_depth),
                },
                priority=priority,
                dedup_key=f"run-step:{run_id}:{next_step}",
                now=now,
                available_at=now + float(delay_seconds),
            )
            self.append_journal(
                event_type="AUTONOMOUS_TURN_REQUESTED",
                subject_id=run_id,
                payload={
                    "source": "ENDOGENOUS",
                    "reason": reason.strip(),
                    "next_step": next_step,
                    "not_before": now + float(delay_seconds),
                    "endogenous_depth": int(endogenous_depth),
                },
                now=now,
            )
            if owns_transaction:
                self.connection.execute("COMMIT")
        except BaseException:
            if owns_transaction:
                self.connection.execute("ROLLBACK")
            raise
        return next_step

    def activate_waiting_run(
        self, *, run_id: str, expected_step: int, reason: str, now: float
    ) -> None:
        cursor = self.connection.execute(
            """
            UPDATE runs SET status='RUNNING', updated_at=?
            WHERE id=? AND status='WAITING' AND step_count=?
            """,
            (now, run_id, int(expected_step)),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("autonomous turn activation lost expected waiting generation")
        self.append_journal(
            event_type="AUTONOMOUS_TURN_GRANTED",
            subject_id=run_id,
            payload={"source": "ENDOGENOUS", "reason": reason, "step": int(expected_step)},
            now=now,
        )

    def update_run(
        self, run_id: str, *, now: float, status: str | None = None,
        final_text: str | None = None, last_error: str | None = None,
        clear_last_error: bool = False,
        increment_step: bool = False,
    ) -> None:
        if clear_last_error and last_error is not None:
            raise ValueError("last_error and clear_last_error are mutually exclusive")
        sets = ["updated_at=?"]
        values: list[Any] = [now]
        if status is not None:
            sets.append("status=?")
            values.append(status)
        if final_text is not None:
            sets.append("final_text=?")
            values.append(final_text)
        if clear_last_error:
            sets.append("last_error=NULL")
        elif last_error is not None:
            sets.append("last_error=?")
            values.append(last_error)
        if increment_step:
            sets.append("step_count=step_count+1")
        values.append(run_id)
        self.connection.execute(f"UPDATE runs SET {', '.join(sets)} WHERE id=?", values)

    def advance_run_step(
        self,
        *,
        run_id: str,
        expected_step: int,
        priority: int,
        now: float,
    ) -> int:
        next_step = int(expected_step) + 1
        owns_transaction = not self.connection.in_transaction
        if owns_transaction:
            self.connection.execute("BEGIN IMMEDIATE")
        try:
            cursor = self.connection.execute(
                """
                UPDATE runs
                SET step_count=step_count+1, last_error=NULL, updated_at=?
                WHERE id=? AND status='RUNNING' AND step_count=?
                """,
                (now, run_id, int(expected_step)),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("run step advance lost expected generation")
            self.enqueue_event(
                kind="run.step",
                payload={"run_id": run_id, "step": next_step},
                priority=priority,
                dedup_key=f"run-step:{run_id}:{next_step}",
                now=now,
            )
            if owns_transaction:
                self.connection.execute("COMMIT")
        except BaseException:
            if owns_transaction:
                self.connection.execute("ROLLBACK")
            raise
        return next_step

    def complete_run_step(
        self,
        *,
        run_id: str,
        expected_step: int,
        final_text: str,
        now: float,
    ) -> None:
        cursor = self.connection.execute(
            """
            UPDATE runs
            SET status='COMPLETED', final_text=?, last_error=NULL,
                step_count=step_count+1, updated_at=?
            WHERE id=? AND status='RUNNING' AND step_count=?
            """,
            (final_text, now, run_id, int(expected_step)),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("run completion lost expected generation")

    def block_run_on_effect(
        self, *, run_id: str, request_id: str, error: str, now: float
    ) -> None:
        cursor = self.connection.execute(
            """
            UPDATE runs
            SET status='BLOCKED_EFFECT', blocked_request_id=?, last_error=?, updated_at=?
            WHERE id=? AND status='RUNNING'
            """,
            (request_id, error, now, run_id),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("run could not enter BLOCKED_EFFECT")
        self.append_journal(
            event_type="RUN_BLOCKED_EFFECT",
            subject_id=run_id,
            payload={"request_id": request_id, "error": error},
            now=now,
        )

    def resume_run_after_effect(
        self, *, run_id: str, now: float, priority: int = 0
    ) -> int:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                "SELECT step_count, status FROM runs WHERE id=?", (run_id,)
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            if row["status"] != "BLOCKED_EFFECT":
                raise RuntimeError("run is not blocked on an effect")
            expected_step = int(row["step_count"])
            next_step = expected_step + 1
            cursor = self.connection.execute(
                """
                UPDATE runs
                SET status='RUNNING', blocked_request_id=NULL, last_error=NULL,
                    step_count=step_count+1, updated_at=?
                WHERE id=? AND status='BLOCKED_EFFECT' AND step_count=?
                """,
                (now, run_id, expected_step),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("effect recovery lost expected run generation")
            self.append_journal(
                event_type="RUN_EFFECT_RECOVERED",
                subject_id=run_id,
                payload={"next_step": next_step},
                now=now,
            )
            self.enqueue_event(
                kind="run.step",
                payload={"run_id": run_id, "step": next_step},
                priority=priority,
                dedup_key=f"run-step:{run_id}:{next_step}",
                now=now,
            )
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise
        return next_step

    def record_run_step_decision(
        self, *, run_id: str, step: int, decision: dict[str, Any], now: float
    ) -> None:
        payload = json.dumps(decision, sort_keys=True, separators=(",", ":"))
        try:
            self.connection.execute(
                """
                INSERT INTO run_step_decisions (run_id, step, decision_json, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (run_id, int(step), payload, now),
            )
        except sqlite3.IntegrityError:
            row = self.connection.execute(
                "SELECT decision_json FROM run_step_decisions WHERE run_id=? AND step=?",
                (run_id, int(step)),
            ).fetchone()
            if row is None or str(row["decision_json"]) != payload:
                raise RuntimeError("run step decision changed after durable admission")

    def get_run_step_decision(self, *, run_id: str, step: int) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT decision_json FROM run_step_decisions WHERE run_id=? AND step=?",
            (run_id, int(step)),
        ).fetchone()
        if row is None:
            return None
        value = json.loads(row["decision_json"])
        if not isinstance(value, dict):
            raise RuntimeError("stored run step decision must be a JSON object")
        return value

    def record_run_message(
        self,
        *,
        run_id: str,
        role: str,
        content: str,
        now: float,
        message_key: str | None = None,
    ) -> None:
        try:
            self.connection.execute(
                """
                INSERT INTO run_messages (run_id, role, content, message_key, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (run_id, role, content, message_key, now),
            )
        except sqlite3.IntegrityError:
            if message_key is None:
                raise
            row = self.connection.execute(
                "SELECT role, content FROM run_messages WHERE run_id=? AND message_key=?",
                (run_id, message_key),
            ).fetchone()
            if row is None:
                raise
            if str(row["role"]) != role or str(row["content"]) != content:
                raise RuntimeError("run message key changed after durable admission")

    def list_run_messages(self, run_id: str, *, limit: int = 32) -> list[dict[str, str]]:
        rows = self.connection.execute(
            "SELECT role, content FROM run_messages WHERE run_id=? ORDER BY id DESC LIMIT ?",
            (run_id, limit),
        ).fetchall()
        return [{"role": str(r["role"]), "content": str(r["content"])} for r in reversed(rows)]

    def upsert_model_target(
        self,
        *,
        name: str,
        provider: str,
        base_url: str,
        model: str,
        api_key_env: str | None,
        provenance: dict[str, Any] | None = None,
        activate: bool,
        now: float,
    ) -> None:
        normalized_name = name.strip()
        if not normalized_name:
            raise ValueError("model target name is required")
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            existing = self.connection.execute(
                "SELECT created_at, is_active FROM model_targets WHERE name=?",
                (normalized_name,),
            ).fetchone()
            created_at = now if existing is None else float(existing["created_at"])
            active_value = (
                1
                if activate
                else (int(existing["is_active"]) if existing is not None else 0)
            )
            if activate:
                self.connection.execute(
                    "UPDATE model_targets SET is_active=0, updated_at=? WHERE is_active=1",
                    (now,),
                )
            self.connection.execute(
                """
                INSERT INTO model_targets
                    (name, provider, base_url, model, api_key_env, provenance_json,
                     is_active, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    provider=excluded.provider,
                    base_url=excluded.base_url,
                    model=excluded.model,
                    api_key_env=excluded.api_key_env,
                    provenance_json=excluded.provenance_json,
                    is_active=excluded.is_active,
                    updated_at=excluded.updated_at
                """,
                (
                    normalized_name,
                    provider,
                    base_url,
                    model,
                    api_key_env,
                    (
                        None
                        if provenance is None
                        else json.dumps(
                            provenance,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                    ),
                    active_value,
                    created_at,
                    now,
                ),
            )
            self.append_journal(
                event_type="MODEL_TARGET_CONFIGURED",
                subject_id=normalized_name,
                payload={
                    "provider": provider,
                    "base_url": base_url,
                    "model": model,
                    "api_key_env": api_key_env,
                    "provenance": provenance,
                    "active": bool(active_value),
                },
                now=now,
            )
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise

    def list_model_targets(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT name, provider, base_url, model, api_key_env, provenance_json,
                   is_active, created_at, updated_at
            FROM model_targets
            ORDER BY name ASC
            """
        ).fetchall()
        return [
            {
                "name": str(row["name"]),
                "provider": str(row["provider"]),
                "base_url": str(row["base_url"]),
                "model": str(row["model"]),
                "api_key_env": row["api_key_env"],
                "provenance": (
                    None
                    if row["provenance_json"] is None
                    else json.loads(str(row["provenance_json"]))
                ),
                "active": bool(row["is_active"]),
                "created_at": float(row["created_at"]),
                "updated_at": float(row["updated_at"]),
            }
            for row in rows
        ]

    def get_model_target(self, name: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            """
            SELECT name, provider, base_url, model, api_key_env, provenance_json,
                   is_active, created_at, updated_at
            FROM model_targets WHERE name=?
            """,
            (name,),
        ).fetchone()
        if row is None:
            return None
        return {
            "name": str(row["name"]),
            "provider": str(row["provider"]),
            "base_url": str(row["base_url"]),
            "model": str(row["model"]),
            "api_key_env": row["api_key_env"],
            "provenance": (
                None
                if row["provenance_json"] is None
                else json.loads(str(row["provenance_json"]))
            ),
            "active": bool(row["is_active"]),
            "created_at": float(row["created_at"]),
            "updated_at": float(row["updated_at"]),
        }

    def get_active_model_target(self) -> dict[str, Any] | None:
        row = self.connection.execute(
            """
            SELECT name, provider, base_url, model, api_key_env, provenance_json,
                   is_active, created_at, updated_at
            FROM model_targets WHERE is_active=1
            """
        ).fetchone()
        if row is None:
            return None
        return {
            "name": str(row["name"]),
            "provider": str(row["provider"]),
            "base_url": str(row["base_url"]),
            "model": str(row["model"]),
            "api_key_env": row["api_key_env"],
            "provenance": (
                None
                if row["provenance_json"] is None
                else json.loads(str(row["provenance_json"]))
            ),
            "active": True,
            "created_at": float(row["created_at"]),
            "updated_at": float(row["updated_at"]),
        }

    def activate_model_target(self, name: str, *, now: float) -> None:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                "SELECT name FROM model_targets WHERE name=?",
                (name,),
            ).fetchone()
            if row is None:
                raise KeyError(name)
            self.connection.execute(
                "UPDATE model_targets SET is_active=0, updated_at=? WHERE is_active=1",
                (now,),
            )
            cursor = self.connection.execute(
                """
                UPDATE model_targets
                SET is_active=1, updated_at=?
                WHERE name=?
                """,
                (now, name),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("model target activation lost target")
            self.append_journal(
                event_type="MODEL_TARGET_ACTIVATED",
                subject_id=name,
                payload={},
                now=now,
            )
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise

    def remove_model_target(self, name: str, *, now: float) -> None:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                "SELECT is_active FROM model_targets WHERE name=?",
                (name,),
            ).fetchone()
            if row is None:
                raise KeyError(name)
            if bool(row["is_active"]):
                raise RuntimeError("cannot remove the active model target")
            self.connection.execute(
                "DELETE FROM model_targets WHERE name=?",
                (name,),
            )
            self.append_journal(
                event_type="MODEL_TARGET_REMOVED",
                subject_id=name,
                payload={},
                now=now,
            )
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise

    def add_memory(
        self, *, kind: str, content: str, salience: float = 0.5, now: float
    ) -> str:
        memory_id = str(uuid.uuid4())
        self.connection.execute(
            "INSERT INTO memories (id, kind, content, salience, created_at) VALUES (?, ?, ?, ?, ?)",
            (memory_id, kind, content, float(salience), now),
        )
        return memory_id

    def search_memories(self, *, query: str = "", limit: int = 8) -> list[dict[str, Any]]:
        terms = [term.lower() for term in query.split() if term.strip()]
        rows = self.connection.execute(
            "SELECT id, kind, content, salience, created_at FROM memories ORDER BY salience DESC, created_at DESC LIMIT ?",
            (max(limit * 4, limit),),
        ).fetchall()
        scored: list[tuple[int, float, float, sqlite3.Row]] = []
        for row in rows:
            text = str(row["content"]).lower()
            matches = sum(1 for term in terms if term in text)
            scored.append((matches, float(row["salience"]), float(row["created_at"]), row))
        scored.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
        return [
            {
                "id": str(row["id"]),
                "kind": str(row["kind"]),
                "content": str(row["content"]),
                "salience": float(row["salience"]),
                "created_at": float(row["created_at"]),
            }
            for _, _, _, row in scored[:limit]
        ]

    def renew_event_lease(
        self,
        event_id: str,
        *,
        worker_id: str,
        lease_token: str,
        now: float,
        lease_seconds: float,
    ) -> float:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be > 0")
        lease_until = now + lease_seconds
        cursor = self.connection.execute(
            """
            UPDATE events
            SET lease_until=?, updated_at=?
            WHERE id=? AND status='CLAIMED' AND lease_owner=? AND lease_token=?
              AND lease_until IS NOT NULL AND lease_until > ?
            """,
            (lease_until, now, event_id, worker_id, lease_token, now),
        )
        if cursor.rowcount != 1:
            raise EventLeaseLost("event lease renewal lost lease ownership")
        return lease_until

    def ack_event(
        self,
        event_id: str,
        *,
        worker_id: str,
        lease_token: str,
        now: float,
    ) -> None:
        cursor = self.connection.execute(
            """
            UPDATE events
            SET status='DONE', lease_owner=NULL, lease_until=NULL, lease_token=NULL, updated_at=?
            WHERE id=? AND status='CLAIMED' AND lease_owner=? AND lease_token=?
              AND lease_until IS NOT NULL AND lease_until > ?
            """,
            (now, event_id, worker_id, lease_token, now),
        )
        if cursor.rowcount != 1:
            raise EventLeaseLost("event acknowledgement lost lease ownership")
        self.append_journal(
            event_type="EVENT_ACKED",
            subject_id=event_id,
            payload={"worker_id": worker_id},
            now=now,
        )

    def fail_event(
        self,
        event_id: str,
        *,
        worker_id: str,
        lease_token: str,
        now: float,
        retry_at: float | None = None,
        max_attempts: int | None = None,
        error: str | None = None,
        failed_run_id: str | None = None,
    ) -> bool:
        if max_attempts is not None and max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                """
                SELECT attempts FROM events
                WHERE id=? AND status='CLAIMED' AND lease_owner=? AND lease_token=?
                  AND lease_until IS NOT NULL AND lease_until > ?
                """,
                (event_id, worker_id, lease_token, now),
            ).fetchone()
            if row is None:
                raise EventLeaseLost("event failure update lost lease ownership")
            dead_lettered = (
                max_attempts is not None and int(row["attempts"]) >= max_attempts
            )
            if dead_lettered:
                cursor = self.connection.execute(
                    """
                    UPDATE events
                    SET status='DEAD', lease_owner=NULL, lease_until=NULL, lease_token=NULL,
                        last_error=?, dead_lettered_at=?, updated_at=?
                    WHERE id=? AND status='CLAIMED' AND lease_owner=? AND lease_token=?
                    """,
                    (error, now, now, event_id, worker_id, lease_token),
                )
                if cursor.rowcount != 1:
                    raise EventLeaseLost("event dead-letter update lost lease ownership")
                self.append_journal(
                    event_type="EVENT_DEAD_LETTERED",
                    subject_id=event_id,
                    payload={
                        "worker_id": worker_id,
                        "attempts": int(row["attempts"]),
                        "max_attempts": max_attempts,
                        "error": error,
                    },
                    now=now,
                )
                if failed_run_id is not None:
                    run_cursor = self.connection.execute(
                        """
                        UPDATE runs
                        SET status='FAILED', last_error=?, failed_event_id=?, updated_at=?
                        WHERE id=? AND status='RUNNING'
                        """,
                        (error, event_id, now, failed_run_id),
                    )
                    if run_cursor.rowcount != 1:
                        raise RuntimeError(
                            "dead-letter run transition requires one RUNNING run"
                        )
            else:
                effective_retry_at = now if retry_at is None else retry_at
                cursor = self.connection.execute(
                    """
                    UPDATE events
                    SET status='PENDING', lease_owner=NULL, lease_until=NULL, lease_token=NULL,
                        available_at=?, last_error=?, dead_lettered_at=NULL, updated_at=?
                    WHERE id=? AND status='CLAIMED' AND lease_owner=? AND lease_token=?
                    """,
                    (
                        effective_retry_at,
                        error,
                        now,
                        event_id,
                        worker_id,
                        lease_token,
                    ),
                )
                if cursor.rowcount != 1:
                    raise EventLeaseLost("event failure update lost lease ownership")
                self.append_journal(
                    event_type="EVENT_RETRY_SCHEDULED",
                    subject_id=event_id,
                    payload={
                        "worker_id": worker_id,
                        "retry_at": effective_retry_at,
                        "error": error,
                    },
                    now=now,
                )
                if failed_run_id is not None:
                    run_cursor = self.connection.execute(
                        """
                        UPDATE runs
                        SET last_error=?, updated_at=?
                        WHERE id=? AND status='RUNNING'
                        """,
                        (error, now, failed_run_id),
                    )
                    if run_cursor.rowcount != 1:
                        raise RuntimeError(
                            "retry run error update requires one RUNNING run"
                        )
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise
        return dead_lettered

    def list_dead_events(self, *, limit: int = 100) -> list[dict[str, Any]]:
        if limit < 1:
            raise ValueError("limit must be >= 1")
        rows = self.connection.execute(
            """
            SELECT * FROM events
            WHERE status='DEAD'
            ORDER BY dead_lettered_at ASC, created_at ASC, id ASC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
        return [
            {
                "id": str(row["id"]),
                "kind": str(row["kind"]),
                "payload": json.loads(row["payload_json"]),
                "dedup_key": row["dedup_key"],
                "attempts": int(row["attempts"]),
                "last_error": row["last_error"],
                "dead_lettered_at": row["dead_lettered_at"],
            }
            for row in rows
        ]

    def redrive_event(self, event_id: str, *, now: float) -> None:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                "SELECT * FROM events WHERE id=? AND status='DEAD'",
                (event_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError("event is not dead-lettered")

            prior_attempts = int(row["attempts"])
            prior_error = row["last_error"]
            prior_dead_lettered_at = row["dead_lettered_at"]
            payload = json.loads(row["payload_json"])

            if str(row["kind"]) == "run.step":
                run_id = payload.get("run_id")
                step = payload.get("step")
                if not isinstance(run_id, str) or not isinstance(step, int):
                    raise RuntimeError("dead run.step payload is malformed")
                run = self.connection.execute(
                    """
                    SELECT status, step_count, failed_event_id
                    FROM runs WHERE id=?
                    """,
                    (run_id,),
                ).fetchone()
                if (
                    run is None
                    or str(run["status"]) != "FAILED"
                    or run["failed_event_id"] != event_id
                    or int(run["step_count"]) != step
                ):
                    raise RuntimeError(
                        "dead run.step is not bound to its exact failed run generation"
                    )
                run_cursor = self.connection.execute(
                    """
                    UPDATE runs
                    SET status='RUNNING', last_error=NULL, failed_event_id=NULL, updated_at=?
                    WHERE id=? AND status='FAILED' AND failed_event_id=? AND step_count=?
                    """,
                    (now, run_id, event_id, step),
                )
                if run_cursor.rowcount != 1:
                    raise RuntimeError("run redrive lost exact failed-run binding")
                self.append_journal(
                    event_type="RUN_REDRIVEN",
                    subject_id=run_id,
                    payload={"event_id": event_id, "step": step},
                    now=now,
                )

            cursor = self.connection.execute(
                """
                UPDATE events
                SET status='PENDING', available_at=?, lease_owner=NULL, lease_until=NULL,
                    lease_token=NULL, attempts=0, last_error=NULL,
                    dead_lettered_at=NULL, updated_at=?
                WHERE id=? AND status='DEAD'
                """,
                (now, now, event_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("event redrive lost dead-letter state")
            self.append_journal(
                event_type="EVENT_REDRIVEN",
                subject_id=event_id,
                payload={
                    "prior_attempts": prior_attempts,
                    "prior_error": prior_error,
                    "prior_dead_lettered_at": prior_dead_lettered_at,
                },
                now=now,
            )
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise

    def list_events(self, *, kind: str | None = None) -> list[dict[str, Any]]:
        if kind is None:
            rows = self.connection.execute(
                "SELECT * FROM events ORDER BY created_at ASC, id ASC"
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM events WHERE kind=? ORDER BY created_at ASC, id ASC", (kind,)
            ).fetchall()
        return [
            {
                "id": str(row["id"]),
                "kind": str(row["kind"]),
                "payload": json.loads(row["payload_json"]),
                "dedup_key": row["dedup_key"],
                "status": str(row["status"]),
                "attempts": int(row["attempts"]),
                "last_error": row["last_error"],
                "dead_lettered_at": row["dead_lettered_at"],
            }
            for row in rows
        ]

    def operational_snapshot(self, *, now: float) -> dict[str, Any]:
        event_statuses = ("PENDING", "CLAIMED", "DONE", "DEAD", "PAUSED", "CANCELLED")
        run_statuses = (
            "RUNNING",
            "COMPLETED",
            "FAILED",
            "BLOCKED_EFFECT",
            "PAUSED",
            "CANCELLED",
        )

        owns_transaction = not self.connection.in_transaction
        if owns_transaction:
            self.connection.execute("BEGIN")
        try:
            event_counts = {status: 0 for status in event_statuses}
            for row in self.connection.execute(
                "SELECT status, COUNT(*) AS n FROM events GROUP BY status"
            ).fetchall():
                event_counts[str(row["status"])] = int(row["n"])

            run_counts = {status: 0 for status in run_statuses}
            for row in self.connection.execute(
                "SELECT status, COUNT(*) AS n FROM runs GROUP BY status"
            ).fetchall():
                run_counts[str(row["status"])] = int(row["n"])

            def scalar(query: str, params: tuple[Any, ...] = ()) -> int:
                row = self.connection.execute(query, params).fetchone()
                return 0 if row is None else int(row["n"])

            def oldest_age(query: str, params: tuple[Any, ...]) -> float | None:
                row = self.connection.execute(query, params).fetchone()
                if row is None or row["ts"] is None:
                    return None
                return max(0.0, float(now) - float(row["ts"]))

            ready_pending = scalar(
                """
                SELECT COUNT(*) AS n FROM events
                WHERE status='PENDING' AND available_at <= ?
                """,
                (now,),
            )
            delayed_pending = scalar(
                """
                SELECT COUNT(*) AS n FROM events
                WHERE status='PENDING' AND available_at > ?
                """,
                (now,),
            )
            retry_pending = scalar(
                """
                SELECT COUNT(*) AS n FROM events
                WHERE status='PENDING' AND attempts > 0
                """
            )
            active_claims = scalar(
                """
                SELECT COUNT(*) AS n FROM events
                WHERE status='CLAIMED' AND lease_until IS NOT NULL AND lease_until > ?
                """,
                (now,),
            )
            expired_claims = scalar(
                """
                SELECT COUNT(*) AS n FROM events
                WHERE status='CLAIMED' AND lease_until IS NOT NULL AND lease_until <= ?
                """,
                (now,),
            )

            schedules = self.connection.execute(
                """
                SELECT
                    SUM(CASE WHEN enabled=1 THEN 1 ELSE 0 END) AS enabled_count,
                    SUM(CASE WHEN enabled=1 AND next_at <= ? THEN 1 ELSE 0 END) AS due_count
                FROM schedules
                """,
                (now,),
            ).fetchone()

            snapshot = {
                "observed_at": float(now),
                "pending_events": event_counts.get("PENDING", 0)
                + event_counts.get("CLAIMED", 0),
                "dead_events": event_counts.get("DEAD", 0),
                "events": {
                    "by_status": event_counts,
                    "ready_pending": ready_pending,
                    "delayed_pending": delayed_pending,
                    "retry_pending": retry_pending,
                    "active_claims": active_claims,
                    "expired_claims": expired_claims,
                    "oldest_ready_age_seconds": oldest_age(
                        """
                        SELECT MIN(created_at) AS ts FROM events
                        WHERE status='PENDING' AND available_at <= ?
                        """,
                        (now,),
                    ),
                    "oldest_expired_lease_age_seconds": oldest_age(
                        """
                        SELECT MIN(lease_until) AS ts FROM events
                        WHERE status='CLAIMED'
                          AND lease_until IS NOT NULL AND lease_until <= ?
                        """,
                        (now,),
                    ),
                    "oldest_dead_age_seconds": oldest_age(
                        """
                        SELECT MIN(dead_lettered_at) AS ts FROM events
                        WHERE status='DEAD' AND dead_lettered_at IS NOT NULL
                        """,
                        (),
                    ),
                },
                "runs": run_counts,
                "schedules": {
                    "enabled": (
                        0
                        if schedules is None or schedules["enabled_count"] is None
                        else int(schedules["enabled_count"])
                    ),
                    "due": (
                        0
                        if schedules is None or schedules["due_count"] is None
                        else int(schedules["due_count"])
                    ),
                },
            }
            if owns_transaction:
                self.connection.execute("COMMIT")
            return snapshot
        except BaseException:
            if owns_transaction:
                self.connection.execute("ROLLBACK")
            raise

    def pending_event_count(self) -> int:
        row = self.connection.execute(
            "SELECT COUNT(*) AS n FROM events WHERE status IN ('PENDING','CLAIMED')"
        ).fetchone()
        return int(row["n"])

    def dead_event_count(self) -> int:
        row = self.connection.execute(
            "SELECT COUNT(*) AS n FROM events WHERE status='DEAD'"
        ).fetchone()
        return int(row["n"])

    def claim_event(
        self, *, worker_id: str, now: float, lease_seconds: float
    ) -> Event | None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be > 0")
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                """
                SELECT * FROM events
                WHERE available_at <= ?
                  AND (
                    status = 'PENDING'
                    OR (status = 'CLAIMED' AND lease_until IS NOT NULL AND lease_until <= ?)
                  )
                ORDER BY priority DESC, created_at ASC
                LIMIT 1
                """,
                (now, now),
            ).fetchone()
            if row is None:
                self.connection.execute("COMMIT")
                return None
            attempts = int(row["attempts"]) + 1
            lease_until = now + lease_seconds
            lease_token = str(uuid.uuid4())
            self.connection.execute(
                """
                UPDATE events
                SET status='CLAIMED', lease_owner=?, lease_until=?, lease_token=?,
                    attempts=?, updated_at=?
                WHERE id=?
                """,
                (worker_id, lease_until, lease_token, attempts, now, row["id"]),
            )
            self.append_journal(
                event_type="EVENT_CLAIMED",
                subject_id=str(row["id"]),
                payload={"worker_id": worker_id, "attempt": attempts, "lease_until": lease_until},
                now=now,
            )
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise
        return Event(
            id=str(row["id"]),
            kind=str(row["kind"]),
            payload=json.loads(row["payload_json"]),
            priority=int(row["priority"]),
            status="CLAIMED",
            attempts=attempts,
            lease_owner=worker_id,
            lease_until=lease_until,
            lease_token=lease_token,
        )
