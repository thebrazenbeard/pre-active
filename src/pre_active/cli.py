from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time
from typing import Sequence

from .context import ContextAssembler
from .contracts import RUN_CONTRACT_VERSION
from .daemon import Daemon
from .engine import Engine
from .observers import ObserverManager
from .model_targets import (
    ModelTarget,
    TargetResolvingModelAdapter,
    probe_model_target,
    resolve_model_target,
)
from .providers.openai_compatible import OpenAICompatibleAdapter
from .progress import ProgressLedger
from .scheduler import Scheduler
from .store import Store
from .tools import ToolRegistry
from .volition_bridge import VolitionBridge


def _open_store(path: str) -> Store:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    return Store(target)


def _submit(store: Store, task: str, capabilities: set[str], now: float) -> str:
    return store.create_run_with_initial_step(
        task=task, capabilities=capabilities, now=now
    )


def _runtime(args: argparse.Namespace, store: Store) -> Daemon:
    if args.base_url or args.model:
        try:
            target = resolve_model_target(
                store=store,
                target_name=None,
                base_url=args.base_url,
                model=args.model,
                api_key_env=args.api_key_env,
                env=os.environ,
            )
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        api_key = os.getenv(target.api_key_env) if target.api_key_env else None
        model = OpenAICompatibleAdapter(
            base_url=target.base_url,
            model=target.model,
            api_key=api_key,
            timeout_seconds=args.timeout,
        )
        store.append_journal(
            event_type="MODEL_TARGET_BOUND",
            subject_id=target.name,
            payload={
                "provider": target.provider,
                "base_url": target.base_url,
                "model": target.model,
            },
            now=time.time(),
        )
    else:
        dynamic_model = TargetResolvingModelAdapter(
            store=store,
            target_name=args.target,
            api_key_env=args.api_key_env,
            env=os.environ,
            timeout_seconds=args.timeout,
        )
        try:
            dynamic_model.current_target()
        except (ValueError, KeyError) as exc:
            raise SystemExit(str(exc)) from exc
        model = dynamic_model

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
        max_autonomous_turns_per_run=args.max_autonomous_turns_per_run,
        max_consecutive_endogenous_turns=args.max_consecutive_endogenous_turns,
    )
    return Daemon(
        scheduler=Scheduler(store),
        observers=ObserverManager(store),
        engine=engine,
    )


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
    schedule.add_argument(
        "--autonomous",
        action="store_true",
        help="emit autonomous model turns instead of ordinary task requests",
    )
    schedule.add_argument(
        "--reason",
        help="why this temporal condition warrants an autonomous model turn",
    )
    schedule.add_argument(
        "--volition",
        action="store_true",
        help="emit typed Volition signals instead of direct scheduled work",
    )
    schedule.add_argument("--volition-config-json", default="{}")

    autonomous = sub.add_parser(
        "autonomous-turn",
        help="grant a model turn without requiring a human prompt",
    )
    autonomous.add_argument("task")
    autonomous.add_argument(
        "--source",
        choices=["EXTERNAL", "TEMPORAL", "OPEN_LOOP", "ENDOGENOUS"],
        default="EXTERNAL",
    )
    autonomous.add_argument("--reason", required=True)
    autonomous.add_argument("--after", type=float, default=0.0, help="delay in seconds")
    autonomous.add_argument("--capability", action="append", default=[])

    volition_signal = sub.add_parser(
        "volition-signal",
        help="enqueue one explicit typed Volition motive signal",
    )
    volition_signal.add_argument("target")
    volition_signal.add_argument("--kind", required=True)
    volition_signal.add_argument("--magnitude", type=float, required=True)
    volition_signal.add_argument("--confidence", type=float, default=1.0)
    volition_signal.add_argument("--source", required=True)
    volition_signal.add_argument("--provenance", default="current_observation")
    volition_signal.add_argument("--expected-information-gain", type=float, default=0.0)
    volition_signal.add_argument("--learning-progress", type=float, default=0.0)
    volition_signal.add_argument("--controllability", type=float, default=1.0)
    volition_signal.add_argument("--predicted-deficit-reduction", type=float, default=1.0)
    volition_signal.add_argument("--current-reappraisal", action="store_true")
    volition_signal.add_argument("--dedup-key")

    observer = sub.add_parser(
        "observer",
        help="configure read-only observation sources that can grant autonomous turns",
    )
    observer_sub = observer.add_subparsers(dest="observer_command", required=True)

    observer_add_file = observer_sub.add_parser(
        "add-file",
        help="watch one exact local file for snapshot changes",
    )
    observer_add_file.add_argument("name")
    observer_add_file.add_argument("path")
    observer_add_file.add_argument("task")
    observer_add_file.add_argument("--every", type=float, required=True)
    observer_add_file.add_argument("--priority", type=int, default=-10)
    observer_add_file.add_argument("--emit-initial", action="store_true")
    observer_add_file.add_argument("--capability", action="append", default=[])
    observer_add_file.add_argument("--initiative-policy", default="on_change")
    observer_add_file.add_argument("--initiative-config-json", default="{}")
    observer_add_file.add_argument(
        "--dispatch-route",
        default="autonomous_turn",
        choices=["autonomous_turn", "volition_signal"],
    )
    observer_add_file.add_argument("--dispatch-config-json", default="{}")

    observer_sub.add_parser("list", help="list configured observers")

    observer_show = observer_sub.add_parser("show", help="show one observer")
    observer_show.add_argument("name")

    observer_enable = observer_sub.add_parser("enable", help="enable one observer")
    observer_enable.add_argument("name")

    observer_disable = observer_sub.add_parser("disable", help="disable one observer")
    observer_disable.add_argument("name")

    observer_remove = observer_sub.add_parser("remove", help="remove one observer")
    observer_remove.add_argument("name")

    observer_probe = observer_sub.add_parser(
        "probe",
        help="sample one observer without mutating its baseline or emitting a turn",
    )
    observer_probe.add_argument("name")

    target = sub.add_parser("target", help="configure the local model target")
    target_sub = target.add_subparsers(dest="target_command", required=True)

    target_set = target_sub.add_parser("set", help="create or update a local model target")
    target_set.add_argument("name")
    target_set.add_argument("--provider", default="openai-compatible")
    target_set.add_argument("--base-url", required=True)
    target_set.add_argument("--model", required=True)
    target_set.add_argument("--api-key-env")
    target_set.add_argument("--activate", action="store_true")

    target_sub.add_parser("list", help="list configured local model targets")

    target_show = target_sub.add_parser("show", help="show one target or the active target")
    target_show.add_argument("name", nargs="?")

    target_activate = target_sub.add_parser("activate", help="make a target the daemon default")
    target_activate.add_argument("name")

    target_remove = target_sub.add_parser("remove", help="remove an inactive target")
    target_remove.add_argument("name")

    target_probe = target_sub.add_parser("probe", help="probe one target or the active target")
    target_probe.add_argument("name", nargs="?")
    target_probe.add_argument("--timeout", type=float, default=5.0)

    checkpoint = sub.add_parser(
        "checkpoint",
        help="record and verify durable task-progress evidence",
    )
    checkpoint_sub = checkpoint.add_subparsers(
        dest="checkpoint_command",
        required=True,
    )

    checkpoint_add = checkpoint_sub.add_parser(
        "add",
        help="record candidate progress evidence for a run",
    )
    checkpoint_add.add_argument("run_id")
    checkpoint_add.add_argument("summary")
    checkpoint_add.add_argument("--step", type=int)
    checkpoint_add.add_argument("--producer", default="operator")
    checkpoint_add.add_argument("--evidence-json", default="{}")

    checkpoint_list = checkpoint_sub.add_parser(
        "list",
        help="list progress checkpoints for a run",
    )
    checkpoint_list.add_argument("run_id")
    checkpoint_list.add_argument(
        "--state",
        dest="checkpoint_state",
        choices=["CANDIDATE", "VERIFIED", "REJECTED"],
    )
    checkpoint_list.add_argument(
        "--trusted",
        action="store_true",
        help="show only VERIFIED progress",
    )

    checkpoint_verify = checkpoint_sub.add_parser(
        "verify",
        help="promote one candidate checkpoint to VERIFIED",
    )
    checkpoint_verify.add_argument("checkpoint_id")
    checkpoint_verify.add_argument("--verifier", required=True)
    checkpoint_verify.add_argument("--reason", required=True)

    checkpoint_reject = checkpoint_sub.add_parser(
        "reject",
        help="mark one candidate checkpoint REJECTED",
    )
    checkpoint_reject.add_argument("checkpoint_id")
    checkpoint_reject.add_argument("--verifier", required=True)
    checkpoint_reject.add_argument("--reason", required=True)

    contract = sub.add_parser(
        "contract",
        help="inspect durable run/runtime contract affinity",
    )
    contract_sub = contract.add_subparsers(
        dest="contract_command",
        required=True,
    )
    contract_show = contract_sub.add_parser(
        "show",
        help="show one run's contract compatibility",
    )
    contract_show.add_argument("run_id")

    memory = sub.add_parser("remember", help="add durable context memory")
    memory.add_argument("content")
    memory.add_argument("--kind", default="semantic")
    memory.add_argument("--salience", type=float, default=0.5)

    sub.add_parser("status", help="show durable queue/run status")

    dead = sub.add_parser("dead", help="list dead-lettered events")
    dead.add_argument("--limit", type=int, default=100)

    redrive = sub.add_parser("redrive", help="redrive one exact dead-lettered event")
    redrive.add_argument("event_id")

    pause = sub.add_parser("pause", help="request a durable run pause")
    pause.add_argument("run_id")
    pause.add_argument("--reason")

    resume = sub.add_parser("resume", help="resume one paused run")
    resume.add_argument("run_id")
    resume.add_argument("--reason")

    cancel = sub.add_parser("cancel", help="request durable run cancellation")
    cancel.add_argument("run_id")
    cancel.add_argument("--reason")

    for name in ("run-once", "daemon"):
        run = sub.add_parser(name, help="execute the continuous runtime")
        run.add_argument("--target", help="named persisted local model target")
        run.add_argument("--base-url", help="one-run local endpoint override")
        run.add_argument("--model", help="one-run local model-id override")
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
        run.add_argument("--max-autonomous-turns-per-run", type=int, default=16)
        run.add_argument("--max-consecutive-endogenous-turns", type=int, default=2)
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
            if args.volition:
                if args.autonomous:
                    raise SystemExit(
                        "--volition cannot be combined with --autonomous"
                    )
                if args.capability:
                    raise SystemExit(
                        "--volition schedules cannot have capabilities"
                    )
                if args.reason is not None:
                    raise SystemExit("--reason is invalid with --volition")
                try:
                    volition_config = json.loads(args.volition_config_json)
                except json.JSONDecodeError as exc:
                    raise SystemExit(
                        "--volition-config-json must be valid JSON"
                    ) from exc
                if not isinstance(volition_config, dict):
                    raise SystemExit(
                        "--volition-config-json must decode to a JSON object"
                    )
                schedule_id = Scheduler(store).add_volition_interval(
                    config=volition_config,
                    every_seconds=args.every,
                    first_at=first_at,
                    now=now,
                )
            else:
                kind = "task.requested"
                payload = {
                    "task": args.task,
                    "capabilities": sorted(set(args.capability)),
                }
                if args.autonomous:
                    if not args.reason or not args.reason.strip():
                        raise SystemExit("--autonomous schedules require --reason")
                    kind = "autonomous.turn"
                    payload.update(
                        {
                            "source": "TEMPORAL",
                            "reason": args.reason.strip(),
                        }
                    )
                schedule_id = Scheduler(store).add_interval(
                    kind=kind,
                    payload=payload,
                    every_seconds=args.every,
                    first_at=first_at,
                    now=now,
                )
            print(
                json.dumps(
                    {"schedule_id": schedule_id, "first_at": first_at},
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "autonomous-turn":
            if args.after < 0:
                raise SystemExit("--after must be >= 0")
            event_id = store.request_autonomous_turn(
                task=args.task,
                capabilities=set(args.capability),
                source=args.source,
                reason=args.reason,
                now=now,
                available_at=now + args.after,
                dedup_key=None,
            )
            print(
                json.dumps(
                    {
                        "event_id": event_id,
                        "kind": "autonomous.turn",
                        "source": args.source,
                        "available_at": now + args.after,
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "volition-signal":
            payload = {
                "target": args.target,
                "kind": args.kind,
                "magnitude": args.magnitude,
                "confidence": args.confidence,
                "source": args.source,
                "provenance": args.provenance,
                "expected_information_gain": args.expected_information_gain,
                "learning_progress": args.learning_progress,
                "controllability": args.controllability,
                "predicted_deficit_reduction": args.predicted_deficit_reduction,
                "current_reappraisal": args.current_reappraisal,
                "effect_authority": False,
            }
            try:
                event_id = VolitionBridge(store).enqueue_signal(
                    payload=payload,
                    now=now,
                    dedup_key=args.dedup_key,
                )
            except (ValueError, RuntimeError) as exc:
                raise SystemExit(str(exc)) from exc
            print(
                json.dumps(
                    {
                        "event_id": event_id,
                        "kind": "volition.signal",
                        "target": args.target,
                        "drive_kind": args.kind,
                        "provenance": args.provenance,
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "observer":
            observers = ObserverManager(store)
            if args.observer_command == "add-file":
                try:
                    initiative_config = json.loads(args.initiative_config_json)
                except json.JSONDecodeError as exc:
                    raise SystemExit(
                        "--initiative-config-json must be valid JSON"
                    ) from exc
                if not isinstance(initiative_config, dict):
                    raise SystemExit(
                        "--initiative-config-json must decode to a JSON object"
                    )
                try:
                    dispatch_config = json.loads(args.dispatch_config_json)
                except json.JSONDecodeError as exc:
                    raise SystemExit(
                        "--dispatch-config-json must be valid JSON"
                    ) from exc
                if not isinstance(dispatch_config, dict):
                    raise SystemExit(
                        "--dispatch-config-json must decode to a JSON object"
                    )
                observer_id = observers.add_file(
                    name=args.name,
                    path=args.path,
                    task=args.task,
                    capabilities=set(args.capability),
                    every_seconds=args.every,
                    priority=args.priority,
                    emit_initial=args.emit_initial,
                    now=now,
                    initiative_policy=args.initiative_policy,
                    initiative_config=initiative_config,
                    dispatch_route=args.dispatch_route,
                    dispatch_config=dispatch_config,
                )
                print(
                    json.dumps(
                        observers.get(args.name) | {"id": observer_id},
                        sort_keys=True,
                    )
                )
                return 0
            if args.observer_command == "list":
                print(json.dumps({"observers": observers.list()}, sort_keys=True))
                return 0
            if args.observer_command == "show":
                print(json.dumps(observers.get(args.name), sort_keys=True))
                return 0
            if args.observer_command in {"enable", "disable"}:
                observers.set_enabled(
                    args.name,
                    enabled=args.observer_command == "enable",
                    now=now,
                )
                print(
                    json.dumps(
                        observers.get(
                            args.name,
                            tolerate_dispatch_error=True,
                        ),
                        sort_keys=True,
                    )
                )
                return 0
            if args.observer_command == "remove":
                observers.remove(args.name, now=now)
                print(json.dumps({"name": args.name, "removed": True}, sort_keys=True))
                return 0
            if args.observer_command == "probe":
                observation = observers.probe(args.name)
                print(
                    json.dumps(
                        {
                            "name": args.name,
                            "digest": observation.digest,
                            "summary": observation.summary,
                            "evidence": observation.evidence,
                        },
                        sort_keys=True,
                    )
                )
                return 0
            raise AssertionError(args.observer_command)
        if args.command == "contract":
            if args.contract_command == "show":
                run = store.get_run(args.run_id)
                print(
                    json.dumps(
                        {
                            "run_id": args.run_id,
                            "status": run["status"],
                            "run_contract_version": run["contract_version"],
                            "runtime_contract_version": RUN_CONTRACT_VERSION,
                            "compatible": (
                                run["contract_version"] == RUN_CONTRACT_VERSION
                            ),
                            "blocked_event_id": run["contract_event_id"],
                            "resume_status": run["contract_resume_status"],
                        },
                        sort_keys=True,
                    )
                )
                return 0
            raise AssertionError(args.contract_command)
        if args.command == "checkpoint":
            ledger = ProgressLedger(store)
            if args.checkpoint_command == "add":
                try:
                    evidence = json.loads(args.evidence_json)
                except json.JSONDecodeError as exc:
                    raise SystemExit("--evidence-json must be valid JSON") from exc
                if not isinstance(evidence, dict):
                    raise SystemExit("--evidence-json must decode to a JSON object")
                run = store.get_run(args.run_id)
                step = run["step_count"] if args.step is None else args.step
                checkpoint_id = ledger.add_candidate(
                    run_id=args.run_id,
                    step=step,
                    summary=args.summary,
                    evidence=evidence,
                    producer=args.producer,
                    now=now,
                )
                print(
                    json.dumps(
                        ledger.get(checkpoint_id),
                        sort_keys=True,
                    )
                )
                return 0
            if args.checkpoint_command == "list":
                if args.trusted and args.checkpoint_state is not None:
                    raise SystemExit("--trusted cannot be combined with --state")
                checkpoints = (
                    ledger.trusted(run_id=args.run_id)
                    if args.trusted
                    else ledger.list(
                        run_id=args.run_id,
                        state=args.checkpoint_state,
                    )
                )
                print(
                    json.dumps(
                        {"checkpoints": checkpoints},
                        sort_keys=True,
                    )
                )
                return 0
            if args.checkpoint_command in {"verify", "reject"}:
                decision = (
                    "VERIFIED"
                    if args.checkpoint_command == "verify"
                    else "REJECTED"
                )
                ledger.decide(
                    args.checkpoint_id,
                    decision=decision,
                    verifier=args.verifier,
                    reason=args.reason,
                    now=now,
                )
                print(
                    json.dumps(
                        ledger.get(args.checkpoint_id),
                        sort_keys=True,
                    )
                )
                return 0
            raise AssertionError(args.checkpoint_command)
        if args.command == "target":
            if args.target_command == "set":
                target = ModelTarget(
                    name=args.name,
                    provider=args.provider,
                    base_url=args.base_url,
                    model=args.model,
                    api_key_env=args.api_key_env,
                )
                store.upsert_model_target(
                    name=target.name,
                    provider=target.provider,
                    base_url=target.base_url,
                    model=target.model,
                    api_key_env=target.api_key_env,
                    activate=args.activate,
                    now=now,
                )
                record = store.get_model_target(target.name)
                assert record is not None
                print(json.dumps(record, sort_keys=True))
                return 0
            if args.target_command == "list":
                print(json.dumps({"targets": store.list_model_targets()}, sort_keys=True))
                return 0
            if args.target_command == "show":
                record = (
                    store.get_model_target(args.name)
                    if args.name
                    else store.get_active_model_target()
                )
                if record is None:
                    raise SystemExit("model target not found")
                print(
                    json.dumps(
                        {
                            "target": record,
                            "active": store.get_active_model_target(),
                        },
                        sort_keys=True,
                    )
                )
                return 0
            if args.target_command == "activate":
                store.activate_model_target(args.name, now=now)
                record = store.get_model_target(args.name)
                assert record is not None
                print(json.dumps(record, sort_keys=True))
                return 0
            if args.target_command == "remove":
                store.remove_model_target(args.name, now=now)
                print(json.dumps({"name": args.name, "removed": True}, sort_keys=True))
                return 0
            if args.target_command == "probe":
                record = (
                    store.get_model_target(args.name)
                    if args.name
                    else store.get_active_model_target()
                )
                if record is None:
                    raise SystemExit("model target not found")
                target = ModelTarget.from_record(record)
                result = probe_model_target(
                    target,
                    env=os.environ,
                    timeout_seconds=args.timeout,
                )
                print(json.dumps(result, sort_keys=True))
                return 0 if result["ready"] else 2
            raise AssertionError(args.target_command)
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
            snapshot = store.operational_snapshot(now=now)
            snapshot["model_target"] = store.get_active_model_target()
            snapshot["runtime_contract_version"] = RUN_CONTRACT_VERSION
            snapshot["observers"] = ObserverManager(store).snapshot(now=now)
            print(json.dumps(snapshot, sort_keys=True))
            return 0
        if args.command == "dead":
            print(
                json.dumps(
                    {"events": store.list_dead_events(limit=args.limit)},
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "redrive":
            store.redrive_event(args.event_id, now=now)
            print(
                json.dumps(
                    {"event_id": args.event_id, "status": "PENDING"},
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "pause":
            store.request_run_control(
                args.run_id,
                action="PAUSE",
                reason=args.reason,
                now=now,
            )
            run = store.get_run(args.run_id)
            print(
                json.dumps(
                    {
                        "run_id": args.run_id,
                        "status": run["status"],
                        "control_action": run["control_action"],
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "resume":
            store.resume_paused_run(
                args.run_id,
                reason=args.reason,
                now=now,
            )
            print(
                json.dumps(
                    {"run_id": args.run_id, "status": "RUNNING"},
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "cancel":
            store.request_run_control(
                args.run_id,
                action="CANCEL",
                reason=args.reason,
                now=now,
            )
            run = store.get_run(args.run_id)
            print(
                json.dumps(
                    {
                        "run_id": args.run_id,
                        "status": run["status"],
                        "control_action": run["control_action"],
                    },
                    sort_keys=True,
                )
            )
            return 0
        runtime = _runtime(args, store)
        if args.command == "run-once":
            result = runtime.cycle(now=now)
            print(
                json.dumps(
                    {
                        "emitted_events": result.emitted_events,
                        "observer_samples": result.observer_samples,
                        "observer_events": result.observer_events,
                        "observer_errors": result.observer_errors,
                        "schedule_events": result.schedule_events,
                        "run_id": result.run_id,
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "daemon":
            runtime.run_forever(poll_seconds=args.poll_seconds)
            return 0
        raise AssertionError(args.command)
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
