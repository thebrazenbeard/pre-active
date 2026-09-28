from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
import uuid
from typing import Any


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
    attempts INTEGER NOT NULL DEFAULT 0,
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
CREATE TABLE IF NOT EXISTS journal (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    subject_id TEXT,
    payload_json TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS journal_subject_idx ON journal(subject_id, seq);
"""


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


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.executescript(_SCHEMA)
        run_columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(runs)")}
        if "blocked_request_id" not in run_columns:
            self.connection.execute("ALTER TABLE runs ADD COLUMN blocked_request_id TEXT")
        if "source_event_id" not in run_columns:
            self.connection.execute("ALTER TABLE runs ADD COLUMN source_event_id TEXT")
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
    ) -> str:
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
                    created_at, updated_at
                ) VALUES (?, ?, 'RUNNING', ?, 0, ?, ?, ?)
                """,
                (run_id, task, json.dumps(sorted(capabilities)), source_event_id, now, now),
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
    ) -> str:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            run_id = self.create_run(
                task=task,
                capabilities=capabilities,
                now=now,
                source_event_id=source_event_id,
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
        }

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
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise
        return next_step

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

    def ack_event(self, event_id: str, *, worker_id: str, now: float) -> None:
        cursor = self.connection.execute(
            """
            UPDATE events SET status='DONE', lease_owner=NULL, lease_until=NULL, updated_at=?
            WHERE id=? AND status='CLAIMED' AND lease_owner=?
            """,
            (now, event_id, worker_id),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("event acknowledgement lost lease ownership")
        self.append_journal(
            event_type="EVENT_ACKED",
            subject_id=event_id,
            payload={"worker_id": worker_id},
            now=now,
        )

    def fail_event(self, event_id: str, *, worker_id: str, now: float, retry_at: float | None = None) -> None:
        cursor = self.connection.execute(
            """
            UPDATE events SET status='PENDING', lease_owner=NULL, lease_until=NULL, available_at=?, updated_at=?
            WHERE id=? AND status='CLAIMED' AND lease_owner=?
            """,
            (now if retry_at is None else retry_at, now, event_id, worker_id),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("event failure update lost lease ownership")
        self.append_journal(
            event_type="EVENT_RETRY_SCHEDULED",
            subject_id=event_id,
            payload={"worker_id": worker_id, "retry_at": now if retry_at is None else retry_at},
            now=now,
        )

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
            }
            for row in rows
        ]

    def pending_event_count(self) -> int:
        row = self.connection.execute(
            "SELECT COUNT(*) AS n FROM events WHERE status IN ('PENDING','CLAIMED')"
        ).fetchone()
        return int(row["n"])

    def claim_event(
        self, *, worker_id: str, now: float, lease_seconds: float
    ) -> Event | None:
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
            self.connection.execute(
                """
                UPDATE events
                SET status='CLAIMED', lease_owner=?, lease_until=?, attempts=?, updated_at=?
                WHERE id=?
                """,
                (worker_id, lease_until, attempts, now, row["id"]),
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
        )
