from __future__ import annotations

from dataclasses import dataclass
import sys
import time

from .engine import Engine
from .observers import ObserverManager, ObserverTickResult
from .scheduler import Scheduler


@dataclass(frozen=True)
class CycleResult:
    emitted_events: int
    run_id: str | None
    observer_samples: int = 0
    observer_events: int = 0
    observer_errors: int = 0
    schedule_events: int = 0


class Daemon:
    def __init__(
        self,
        *,
        scheduler: Scheduler,
        engine: Engine,
        observers: ObserverManager | None = None,
    ) -> None:
        self.scheduler = scheduler
        self.engine = engine
        self.observers = observers

    def cycle(self, *, now: float) -> CycleResult:
        observed = (
            ObserverTickResult(sampled=0, emitted=0, errors=0)
            if self.observers is None
            else self.observers.tick(now=now)
        )
        scheduled = self.scheduler.tick(now=now)
        run_id = self.engine.run_once(now=now)
        return CycleResult(
            emitted_events=observed.emitted + scheduled,
            run_id=run_id,
            observer_samples=observed.sampled,
            observer_events=observed.emitted,
            observer_errors=observed.errors,
            schedule_events=scheduled,
        )

    def run_forever(self, *, poll_seconds: float = 1.0) -> None:
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be > 0")
        while True:
            try:
                self.cycle(now=time.time())
            except Exception as exc:
                print(
                    f"pre-active cycle failed: {type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )
            time.sleep(poll_seconds)
