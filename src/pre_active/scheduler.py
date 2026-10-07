from __future__ import annotations

import json
import math
import uuid
from decimal import Decimal, ROUND_FLOOR, localcontext
from typing import Any

from .store import Store
from .volition_bridge import VolitionBridge


_MIN_VOLITION_INTERVAL_SECONDS = 0.000001


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

    def add_volition_interval(
        self,
        *,
        config: dict[str, Any],
        every_seconds: float,
        first_at: float,
        now: float,
    ) -> str:
        interval_value = float(every_seconds)
        first_at_value = float(first_at)
        now_value = float(now)
        if not math.isfinite(interval_value):
            raise ValueError("every_seconds must be finite")
        if interval_value <= 0:
            raise ValueError("every_seconds must be > 0")
        if interval_value < _MIN_VOLITION_INTERVAL_SECONDS:
            raise ValueError("every_seconds must be at least 0.000001")
        if not math.isfinite(first_at_value):
            raise ValueError("first_at must be finite")
        if not math.isfinite(now_value):
            raise ValueError("now must be finite")
        if (
            first_at_value + interval_value <= first_at_value
            or now_value + interval_value <= now_value
        ):
            raise ValueError(
                "every_seconds is too small for schedule timestamp resolution"
            )
        schedule_id = str(uuid.uuid4())
        payload = VolitionBridge.validate_static_signal_config(
            config,
            source=f"schedule:{schedule_id}",
        )
        self.store.connection.execute(
            """
            INSERT INTO schedules (
                id, kind, payload_json, every_seconds, next_at,
                enabled, created_at, updated_at
            )
            VALUES (?, 'volition.signal', ?, ?, ?, 1, ?, ?)
            """,
            (
                schedule_id,
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
                interval_value,
                first_at_value,
                now_value,
                now_value,
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
            elif str(row["kind"]) == "volition.signal":
                if emitted >= max_occurrences:
                    break
                interval_value = float(interval)
                with localcontext() as context:
                    context.prec = 50
                    next_decimal = Decimal(str(next_at))
                    now_decimal = Decimal(str(now))
                    interval_decimal = Decimal(str(interval_value))
                    skipped = int(
                        ((now_decimal - next_decimal) / interval_decimal)
                        .to_integral_value(rounding=ROUND_FLOOR)
                    )
                    occurrence_decimal = (
                        next_decimal + (interval_decimal * skipped)
                    )
                    next_decimal = occurrence_decimal + interval_decimal
                occurrence = float(occurrence_decimal)
                next_at = float(next_decimal)
                if next_at <= occurrence:
                    raise ValueError(
                        "volition schedule interval cannot advance at "
                        "current timestamp resolution"
                    )
                occurrences = [occurrence]
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
