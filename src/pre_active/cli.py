from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time
from typing import Sequence

from .context import ContextAssembler
from .daemon import Daemon
from .engine import Engine
from .providers.openai_compatible import OpenAICompatibleAdapter
from .scheduler import Scheduler
from .store import Store
from .tools import ToolRegistry


def _open_store(path: str) -> Store:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    return Store(target)


def _submit(store: Store, task: str, capabilities: set[str], now: float) -> str:
    return store.create_run_with_initial_step(
        task=task, capabilities=capabilities, now=now
    )


def _runtime(args: argparse.Namespace, store: Store) -> Daemon:
    base_url = args.base_url or os.getenv("PRE_ACTIVE_BASE_URL")
    model_name = args.model or os.getenv("PRE_ACTIVE_MODEL")
    if not base_url or not model_name:
        raise SystemExit("run-once/daemon require --base-url and --model (or PRE_ACTIVE_BASE_URL/PRE_ACTIVE_MODEL)")
    api_key = os.getenv(args.api_key_env) if args.api_key_env else None
    model = OpenAICompatibleAdapter(
        base_url=base_url,
        model=model_name,
        api_key=api_key,
        timeout_seconds=args.timeout,
    )
    tools = ToolRegistry(store)
    engine = Engine(
        store=store,
        model=model,
        tools=tools,
        context=ContextAssembler(store, max_chars=args.context_chars),
        system_prompt=args.system_prompt,
        worker_id=args.worker_id,
        lease_seconds=args.lease_seconds,
        lease_heartbeat_seconds=args.lease_heartbeat_seconds,
        max_lease_extension_seconds=args.max_lease_extension_seconds,
        max_event_attempts=args.max_event_attempts,
        max_steps=args.max_steps,
    )
    return Daemon(scheduler=Scheduler(store), engine=engine)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pre-active")
    parser.add_argument("--state", default=".pre-active/state.db", help="SQLite state path")
    sub = parser.add_subparsers(dest="command", required=True)

    submit = sub.add_parser("submit", help="submit a durable task")
    submit.add_argument("task")
    submit.add_argument("--capability", action="append", default=[])

    schedule = sub.add_parser("schedule", help="schedule a recurring task")
    schedule.add_argument("task")
    schedule.add_argument("--every", type=float, required=True, help="interval in seconds")
    schedule.add_argument("--first-at", type=float)
    schedule.add_argument("--capability", action="append", default=[])

    memory = sub.add_parser("remember", help="add durable context memory")
    memory.add_argument("content")
    memory.add_argument("--kind", default="semantic")
    memory.add_argument("--salience", type=float, default=0.5)

    sub.add_parser("status", help="show durable queue/run status")

    for name in ("run-once", "daemon"):
        run = sub.add_parser(name, help="execute the continuous runtime")
        run.add_argument("--base-url")
        run.add_argument("--model")
        run.add_argument("--api-key-env", default="PRE_ACTIVE_API_KEY")
        run.add_argument("--timeout", type=float, default=120.0)
        run.add_argument("--context-chars", type=int, default=12000)
        run.add_argument("--system-prompt", default="Execute the task using only admitted tools and capabilities.")
        run.add_argument("--worker-id", default=f"pre-active-{os.getpid()}")
        run.add_argument("--lease-seconds", type=float, default=30.0)
        run.add_argument("--lease-heartbeat-seconds", type=float)
        run.add_argument("--max-lease-extension-seconds", type=float, default=900.0)
        run.add_argument("--max-event-attempts", type=int, default=16)
        run.add_argument("--max-steps", type=int, default=24)
        if name == "daemon":
            run.add_argument("--poll-seconds", type=float, default=1.0)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    now = time.time()
    store = _open_store(args.state)
    try:
        if args.command == "submit":
            run_id = _submit(store, args.task, set(args.capability), now)
            print(json.dumps({"run_id": run_id}, sort_keys=True))
            return 0
        if args.command == "schedule":
            first_at = args.first_at if args.first_at is not None else now + args.every
            schedule_id = Scheduler(store).add_interval(
                kind="task.requested",
                payload={"task": args.task, "capabilities": sorted(set(args.capability))},
                every_seconds=args.every,
                first_at=first_at,
                now=now,
            )
            print(json.dumps({"schedule_id": schedule_id, "first_at": first_at}, sort_keys=True))
            return 0
        if args.command == "remember":
            memory_id = store.add_memory(
                kind=args.kind,
                content=args.content,
                salience=args.salience,
                now=now,
            )
            print(json.dumps({"memory_id": memory_id}, sort_keys=True))
            return 0
        if args.command == "status":
            rows = store.connection.execute(
                "SELECT status, COUNT(*) AS n FROM runs GROUP BY status ORDER BY status"
            ).fetchall()
            print(
                json.dumps(
                    {
                        "pending_events": store.pending_event_count(),
                        "dead_events": store.dead_event_count(),
                        "runs": {str(row["status"]): int(row["n"]) for row in rows},
                    },
                    sort_keys=True,
                )
            )
            return 0
        runtime = _runtime(args, store)
        if args.command == "run-once":
            result = runtime.cycle(now=now)
            print(json.dumps({"emitted_events": result.emitted_events, "run_id": result.run_id}, sort_keys=True))
            return 0
        if args.command == "daemon":
            runtime.run_forever(poll_seconds=args.poll_seconds)
            return 0
        raise AssertionError(args.command)
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
