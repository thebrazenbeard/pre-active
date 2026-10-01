from __future__ import annotations

import sqlite3
import threading
import time


class LeaseLost(RuntimeError):
    """Raised when the active event claim can no longer be proven."""


class LeaseHeartbeat:
    """Renew one exact fenced event claim while blocking work is in progress."""

    def __init__(
        self,
        *,
        state_path: str,
        event_id: str,
        worker_id: str,
        lease_token: str,
        lease_seconds: float,
        heartbeat_seconds: float | None = None,
        max_extension_seconds: float = 900.0,
    ) -> None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be > 0")
        if heartbeat_seconds is not None and heartbeat_seconds <= 0:
            raise ValueError("heartbeat_seconds must be > 0")
        if max_extension_seconds <= 0:
            raise ValueError("max_extension_seconds must be > 0")
        interval = lease_seconds / 3.0 if heartbeat_seconds is None else heartbeat_seconds
        if interval >= lease_seconds:
            raise ValueError("heartbeat_seconds must be less than lease_seconds")

        self.state_path = state_path
        self.event_id = event_id
        self.worker_id = worker_id
        self.lease_token = lease_token
        self.lease_seconds = float(lease_seconds)
        self.heartbeat_seconds = float(interval)
        self.max_extension_seconds = float(max_extension_seconds)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started_monotonic: float | None = None
        self._lost_reason: str | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("lease heartbeat already started")
        self._started_monotonic = time.monotonic()
        connection = sqlite3.connect(self.state_path, timeout=5.0, isolation_level=None)
        try:
            self._renew_once(connection)
        finally:
            connection.close()
        self.assert_owned()
        self._thread = threading.Thread(
            target=self._run,
            name=f"pre-active-lease-{self.event_id}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=max(1.0, self.heartbeat_seconds * 2.0))

    def assert_owned(self) -> None:
        with self._lock:
            reason = self._lost_reason
        if reason is not None:
            raise LeaseLost(reason)

    def _lose(self, reason: str) -> None:
        with self._lock:
            if self._lost_reason is None:
                self._lost_reason = reason

    def _renew_once(self, connection: sqlite3.Connection) -> None:
        assert self._started_monotonic is not None
        if time.monotonic() - self._started_monotonic >= self.max_extension_seconds:
            self._lose("maximum lease extension elapsed")
            return
        now = time.time()
        lease_until = now + self.lease_seconds
        try:
            cursor = connection.execute(
                """
                UPDATE events
                SET lease_until=?, updated_at=?
                WHERE id=? AND status='CLAIMED' AND lease_owner=? AND lease_token=?
                  AND lease_until IS NOT NULL AND lease_until > ?
                """,
                (
                    lease_until,
                    now,
                    self.event_id,
                    self.worker_id,
                    self.lease_token,
                    now,
                ),
            )
        except sqlite3.Error as exc:
            self._lose(f"lease heartbeat storage failure: {type(exc).__name__}: {exc}")
            return
        if cursor.rowcount != 1:
            self._lose("event lease heartbeat lost lease ownership")

    def _run(self) -> None:
        connection = sqlite3.connect(self.state_path, timeout=5.0, isolation_level=None)
        try:
            while not self._stop.wait(self.heartbeat_seconds):
                self._renew_once(connection)
                with self._lock:
                    if self._lost_reason is not None:
                        return
        finally:
            connection.close()

    def __enter__(self) -> "LeaseHeartbeat":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()
