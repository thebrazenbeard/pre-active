from __future__ import annotations

import json
import uuid
from typing import Any

from .store import Store


class Scheduler:
    def __init__(self, store: Store) -> None:
        self.store = store

    def add_interval(
        self,
        *,
        kind: str,
        payload: dict[str, Any],
        every_seconds: float,
        first_at: float,
        now: float,
    ) -> str:
        if every_seconds <= 0:
            raise ValueError("every_seconds must be > 0")
        schedule_id = str(uuid.uuid4())
        self.store.connection.execute(
            """
            INSERT INTO schedules (id, kind, payload_json, every_seconds, next_at, enabled, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (
                schedule_id,
                kind,
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
                every_seconds,
                first_at,
                now,
                now,
            ),
        )
        return schedule_id

    def tick(self, *, now: float, max_occurrences: int = 100) -> int:
        emitted = 0
        rows = self.store.connection.execute(
            "SELECT * FROM schedules WHERE enabled=1 AND next_at <= ? ORDER BY next_at ASC",
            (now,),
        ).fetchall()
        for row in rows:
            next_at = float(row["next_at"])
            interval = row["every_seconds"]
            if interval is None:
                occurrences = [next_at]
            else:
                occurrences = []
                while next_at <= now and emitted + len(occurrences) < max_occurrences:
                    occurrences.append(next_at)
                    next_at += float(interval)
            for occurrence in occurrences:
                dedup = f"schedule:{row['id']}:{occurrence:.6f}"
                self.store.enqueue_event(
                    kind=str(row["kind"]),
                    payload=json.loads(row["payload_json"]),
                    priority=0,
                    dedup_key=dedup,
                    now=now,
                    available_at=occurrence,
                )
                emitted += 1
            if interval is None:
                self.store.connection.execute(
                    "UPDATE schedules SET enabled=0, updated_at=? WHERE id=?",
                    (now, row["id"]),
                )
            else:
                self.store.connection.execute(
                    "UPDATE schedules SET next_at=?, updated_at=? WHERE id=?",
                    (next_at, now, row["id"]),
                )
            if emitted >= max_occurrences:
                break
        return emitted
