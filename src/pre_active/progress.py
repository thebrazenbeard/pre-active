from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any

from .store import Store




class ProgressLedger:
    """Durable task-progress evidence with explicit verification.

    Candidate progress is evidence, not trusted progress. Only an explicit
    verifier transition may produce VERIFIED state.
    """

    STATES = {"CANDIDATE", "VERIFIED", "REJECTED"}
    TERMINAL_STATES = {"VERIFIED", "REJECTED"}

    def __init__(self, store: Store) -> None:
        self.store = store
        self.connection = store.connection

    @staticmethod
    def _require_text(value: str, field: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError(f"{field} must be non-empty text")
        return normalized

    def add_candidate(
        self,
        *,
        run_id: str,
        step: int,
        summary: str,
        evidence: dict[str, Any],
        producer: str,
        now: float,
    ) -> str:
        if isinstance(step, bool) or not isinstance(step, int) or step < 0:
            raise ValueError("checkpoint step must be a non-negative integer")
        if not isinstance(evidence, dict):
            raise ValueError("checkpoint evidence must be a JSON object")
        normalized_summary = self._require_text(summary, "checkpoint summary")
        normalized_producer = self._require_text(producer, "checkpoint producer")

        checkpoint_id = str(uuid.uuid4())
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            run = self.connection.execute(
                "SELECT step_count FROM runs WHERE id=?",
                (run_id,),
            ).fetchone()
            if run is None:
                raise KeyError(run_id)
            current_step = int(run["step_count"])
            if step > current_step:
                raise ValueError(
                    f"checkpoint step {step} exceeds current run step {current_step}"
                )
            self.connection.execute(
                """
                INSERT INTO progress_checkpoints (
                    id, run_id, step, state, summary, evidence_json,
                    producer, verifier, decision_reason, created_at, decided_at
                ) VALUES (?, ?, ?, 'CANDIDATE', ?, ?, ?, NULL, NULL, ?, NULL)
                """,
                (
                    checkpoint_id,
                    run_id,
                    step,
                    normalized_summary,
                    json.dumps(evidence, sort_keys=True, separators=(",", ":")),
                    normalized_producer,
                    now,
                ),
            )
            self.store.append_journal(
                event_type="PROGRESS_CANDIDATE_RECORDED",
                subject_id=run_id,
                payload={
                    "checkpoint_id": checkpoint_id,
                    "step": step,
                    "producer": normalized_producer,
                },
                now=now,
            )
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise
        return checkpoint_id

    def decide(
        self,
        checkpoint_id: str,
        *,
        decision: str,
        verifier: str,
        reason: str,
        now: float,
    ) -> None:
        normalized_decision = decision.upper()
        if normalized_decision not in self.TERMINAL_STATES:
            raise ValueError("checkpoint decision must be VERIFIED or REJECTED")
        normalized_verifier = self._require_text(verifier, "checkpoint verifier")
        normalized_reason = self._require_text(reason, "checkpoint decision reason")

        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                """
                SELECT run_id, state, step, verifier, decision_reason
                FROM progress_checkpoints
                WHERE id=?
                """,
                (checkpoint_id,),
            ).fetchone()
            if row is None:
                raise KeyError(checkpoint_id)
            current_state = str(row["state"])
            if current_state != "CANDIDATE":
                if (
                    current_state == normalized_decision
                    and row["verifier"] == normalized_verifier
                    and row["decision_reason"] == normalized_reason
                ):
                    self.connection.execute("COMMIT")
                    return
                raise RuntimeError(
                    f"checkpoint decision is immutable after {current_state}"
                )
            cursor = self.connection.execute(
                """
                UPDATE progress_checkpoints
                SET state=?, verifier=?, decision_reason=?, decided_at=?
                WHERE id=? AND state='CANDIDATE'
                """,
                (
                    normalized_decision,
                    normalized_verifier,
                    normalized_reason,
                    now,
                    checkpoint_id,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("checkpoint decision lost CANDIDATE state")
            self.store.append_journal(
                event_type=f"PROGRESS_{normalized_decision}",
                subject_id=str(row["run_id"]),
                payload={
                    "checkpoint_id": checkpoint_id,
                    "step": int(row["step"]),
                    "verifier": normalized_verifier,
                    "reason": normalized_reason,
                },
                now=now,
            )
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise

    def get(self, checkpoint_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM progress_checkpoints WHERE id=?",
            (checkpoint_id,),
        ).fetchone()
        if row is None:
            raise KeyError(checkpoint_id)
        return self._row(row)

    def list(
        self,
        *,
        run_id: str,
        state: str | None = None,
    ) -> list[dict[str, Any]]:
        if state is not None:
            normalized_state = state.upper()
            if normalized_state not in self.STATES:
                raise ValueError(
                    "checkpoint state must be CANDIDATE, VERIFIED, or REJECTED"
                )
            rows = self.connection.execute(
                """
                SELECT * FROM progress_checkpoints
                WHERE run_id=? AND state=?
                ORDER BY created_at ASC, id ASC
                """,
                (run_id, normalized_state),
            ).fetchall()
        else:
            rows = self.connection.execute(
                """
                SELECT * FROM progress_checkpoints
                WHERE run_id=?
                ORDER BY created_at ASC, id ASC
                """,
                (run_id,),
            ).fetchall()
        return [self._row(row) for row in rows]

    def trusted(self, *, run_id: str) -> list[dict[str, Any]]:
        return self.list(run_id=run_id, state="VERIFIED")

    def latest_verified(self, *, run_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            """
            SELECT * FROM progress_checkpoints
            WHERE run_id=? AND state='VERIFIED'
            ORDER BY decided_at DESC, created_at DESC, id DESC
            LIMIT 1
            """,
            (run_id,),
        ).fetchone()
        return None if row is None else self._row(row)

    @staticmethod
    def _row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": str(row["id"]),
            "run_id": str(row["run_id"]),
            "step": int(row["step"]),
            "state": str(row["state"]),
            "summary": str(row["summary"]),
            "evidence": json.loads(row["evidence_json"]),
            "producer": str(row["producer"]),
            "verifier": row["verifier"],
            "decision_reason": row["decision_reason"],
            "created_at": float(row["created_at"]),
            "decided_at": (
                None if row["decided_at"] is None else float(row["decided_at"])
            ),
        }
