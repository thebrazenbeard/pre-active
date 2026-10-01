from __future__ import annotations

from dataclasses import dataclass
import sys
import time

from .engine import Engine
from .scheduler import Scheduler


@dataclass(frozen=True)
class CycleResult:
    emitted_events: int
    run_id: str | None


class Daemon:
    def __init__(self, *, scheduler: Scheduler, engine: Engine) -> None:
        self.scheduler = scheduler
        self.engine = engine

    def cycle(self, *, now: float) -> CycleResult:
        emitted = self.scheduler.tick(now=now)
        run_id = self.engine.run_once(now=now)
        return CycleResult(emitted_events=emitted, run_id=run_id)

    def run_forever(self, *, poll_seconds: float = 1.0) -> None:
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be > 0")
        while True:
            try:
                self.cycle(now=time.time())
            except Exception as exc:
                print(f"pre-active cycle failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            time.sleep(poll_seconds)
