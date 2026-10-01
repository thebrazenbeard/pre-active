from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Protocol

from .context import ContextAssembler
from .store import Store
from .tools import AmbiguousEffect, ToolError, ToolRegistry


@dataclass(frozen=True)
class ToolCall:
    request_id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ModelResponse:
    final_text: str | None = None
    tool_call: ToolCall | None = None

    def __post_init__(self) -> None:
        if (self.final_text is None) == (self.tool_call is None):
            raise ValueError("model response must contain exactly one of final_text or tool_call")


class ModelAdapter(Protocol):
    def respond(
        self, *, messages: list[dict[str, str]], tools: list[dict[str, Any]]
    ) -> ModelResponse: ...


def _decision_from_response(response: ModelResponse) -> dict[str, Any]:
    if response.tool_call is not None:
        return {
            "kind": "tool_call",
            "request_id": response.tool_call.request_id,
            "name": response.tool_call.name,
            "arguments": response.tool_call.arguments,
        }
    assert response.final_text is not None
    return {"kind": "final_text", "final_text": response.final_text}


def _response_from_decision(decision: dict[str, Any]) -> ModelResponse:
    kind = decision.get("kind")
    if kind == "tool_call":
        arguments = decision.get("arguments")
        if not isinstance(arguments, dict):
            raise RuntimeError("stored tool-call arguments must be an object")
        return ModelResponse(
            tool_call=ToolCall(
                request_id=str(decision["request_id"]),
                name=str(decision["name"]),
                arguments=arguments,
            )
        )
    if kind == "final_text":
        final_text = decision.get("final_text")
        if not isinstance(final_text, str):
            raise RuntimeError("stored final_text decision must be text")
        return ModelResponse(final_text=final_text)
    raise RuntimeError("stored run step decision has unknown kind")


class Engine:
    def __init__(
        self,
        *,
        store: Store,
        model: ModelAdapter,
        tools: ToolRegistry,
        context: ContextAssembler,
        system_prompt: str,
        worker_id: str,
        lease_seconds: float = 30.0,
        max_steps: int = 24,
    ) -> None:
        self.store = store
        self.model = model
        self.tools = tools
        self.context = context
        self.system_prompt = system_prompt
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.max_steps = max_steps

    def submit_task(
        self,
        task: str,
        capabilities: set[str],
        *,
        now: float,
        source_event_id: str | None = None,
    ) -> str:
        return self.store.create_run_with_initial_step(
            task=task,
            capabilities=capabilities,
            now=now,
            source_event_id=source_event_id,
            priority=0,
        )

    def _messages_for_run(self, run: dict[str, Any]) -> list[dict[str, str]]:
        transcript = self.store.list_run_messages(run["id"])
        task = run["task"]
        if transcript:
            rendered = "\n".join(f"{m['role']}: {m['content']}" for m in transcript)
            task = f"{task}\n\nDurable run transcript:\n{rendered}"
        return self.context.build(
            system_prompt=self.system_prompt,
            task=task,
            query=run["task"],
        )

    def resume_blocked_effect(self, run_id: str, *, now: float) -> None:
        run = self.store.get_run(run_id)
        if run["status"] != "BLOCKED_EFFECT" or not run["blocked_request_id"]:
            raise RuntimeError("run is not blocked on an unresolved effect")
        request_id = str(run["blocked_request_id"] )
        result = self.tools.recover_reconciled(
            request_id=request_id,
            allowed_capabilities=run["capabilities"],
            now=now,
        )
        self.store.record_run_message(
            run_id=run_id,
            role="tool",
            content=f"reconciled {result.tool_name} => {json.dumps(result.output, sort_keys=True)}",
            now=now,
            message_key=f"effect:{request_id}:reconciled-result",
        )
        self.store.resume_run_after_effect(run_id=run_id, now=now, priority=0)

    def run_once(self, *, now: float) -> str | None:
        event = self.store.claim_event(
            worker_id=self.worker_id,
            now=now,
            lease_seconds=self.lease_seconds,
        )
        if event is None:
            return None
        if event.kind == "task.requested":
            task = event.payload.get("task")
            capabilities = event.payload.get("capabilities", [])
            if not isinstance(task, str) or not task.strip():
                self.store.append_journal(
                    event_type="EVENT_REJECTED",
                    subject_id=event.id,
                    payload={"reason": "task.requested task must be a non-empty string"},
                    now=now,
                )
                self.store.ack_event(event.id, worker_id=self.worker_id, now=now)
                return None
            if not isinstance(capabilities, list) or not all(
                isinstance(item, str) for item in capabilities
            ):
                self.store.append_journal(
                    event_type="EVENT_REJECTED",
                    subject_id=event.id,
                    payload={"reason": "task.requested capabilities must be a list of strings"},
                    now=now,
                )
                self.store.ack_event(event.id, worker_id=self.worker_id, now=now)
                return None
            run_id = self.submit_task(
                task.strip(),
                set(capabilities),
                now=now,
                source_event_id=event.id,
            )
            self.store.ack_event(event.id, worker_id=self.worker_id, now=now)
            return run_id
        if event.kind != "run.step":
            self.store.ack_event(event.id, worker_id=self.worker_id, now=now)
            return None

        run_id = str(event.payload["run_id"])
        run = self.store.get_run(run_id)
        event_step = event.payload.get("step")
        if not isinstance(event_step, int) or event_step != run["step_count"]:
            self.store.ack_event(event.id, worker_id=self.worker_id, now=now)
            return run_id
        if run["status"] != "RUNNING":
            self.store.ack_event(event.id, worker_id=self.worker_id, now=now)
            return run_id
        if run["step_count"] >= self.max_steps:
            self.store.update_run(
                run_id,
                now=now,
                status="FAILED",
                last_error="max_steps exceeded",
            )
            self.store.ack_event(event.id, worker_id=self.worker_id, now=now)
            return run_id

        try:
            stored_decision = self.store.get_run_step_decision(
                run_id=run_id, step=run["step_count"]
            )
            if stored_decision is None:
                response = self.model.respond(
                    messages=self._messages_for_run(run),
                    tools=self.tools.specs(run["capabilities"]),
                )
                self.store.record_run_step_decision(
                    run_id=run_id,
                    step=run["step_count"],
                    decision=_decision_from_response(response),
                    now=now,
                )
            else:
                response = _response_from_decision(stored_decision)
            if response.tool_call is not None:
                call = response.tool_call
                self.store.record_run_message(
                    run_id=run_id,
                    role="assistant",
                    content=(
                        f"tool_call id={call.request_id} name={call.name} "
                        f"arguments={json.dumps(call.arguments, sort_keys=True)}"
                    ),
                    now=now,
                    message_key=f"step:{run['step_count']}:assistant-decision",
                )
                try:
                    result = self.tools.execute(
                        name=call.name,
                        arguments=call.arguments,
                        request_id=call.request_id,
                        allowed_capabilities=run["capabilities"],
                        now=now,
                    )
                except BaseException as exc:
                    effect_state = self.tools.effect_state(call.request_id)
                    if effect_state in {"EXECUTING", "ATTEMPTED_UNKNOWN"}:
                        self.store.block_run_on_effect(
                            run_id=run_id,
                            request_id=call.request_id,
                            error=f"{type(exc).__name__}: {exc}",
                            now=now,
                        )
                        self.store.ack_event(
                            event.id, worker_id=self.worker_id, now=now
                        )
                        return run_id
                    if isinstance(exc, ToolError):
                        self.store.update_run(
                            run_id,
                            now=now,
                            status="FAILED",
                            last_error=f"{type(exc).__name__}: {exc}",
                        )
                        self.store.ack_event(
                            event.id, worker_id=self.worker_id, now=now
                        )
                        return run_id
                    raise
                self.store.record_run_message(
                    run_id=run_id,
                    role="tool",
                    content=f"{call.name} => {json.dumps(result.output, sort_keys=True)}",
                    now=now,
                    message_key=f"step:{run['step_count']}:tool-result",
                )
                self.store.advance_run_step(
                    run_id=run_id,
                    expected_step=run["step_count"],
                    priority=event.priority,
                    now=now,
                )
            else:
                assert response.final_text is not None
                self.store.record_run_message(
                    run_id=run_id,
                    role="assistant",
                    content=response.final_text,
                    now=now,
                    message_key=f"step:{run['step_count']}:assistant-final",
                )
                self.store.update_run(
                    run_id,
                    now=now,
                    status="COMPLETED",
                    final_text=response.final_text,
                    clear_last_error=True,
                    increment_step=True,
                )
            self.store.ack_event(event.id, worker_id=self.worker_id, now=now)
            return run_id
        except BaseException as exc:
            self.store.update_run(run_id, now=now, last_error=f"{type(exc).__name__}: {exc}")
            self.store.fail_event(
                event.id,
                worker_id=self.worker_id,
                now=now,
                retry_at=now + min(60.0, 2.0 ** min(event.attempts, 6)),
            )
            raise
