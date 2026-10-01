from __future__ import annotations

from dataclasses import dataclass

from .store import Store


@dataclass
class ContextAssembler:
    store: Store
    max_chars: int = 12000
    memory_limit: int = 8

    def build(self, *, system_prompt: str, task: str, query: str = "") -> list[dict[str, str]]:
        messages: list[dict[str, str]] = [
            {"role": "system", "content": system_prompt.strip()},
            {"role": "user", "content": task.strip()},
        ]
        separator_cost = 1
        used = sum(len(item["content"]) for item in messages) + separator_cost * (len(messages) - 1)
        remaining = max(0, self.max_chars - used - separator_cost)
        if remaining <= 0:
            return self._trim_required(messages)

        selected: list[str] = []
        for memory in self.store.search_memories(query=query, limit=self.memory_limit):
            line = f"[{memory['kind']}] {memory['content'].strip()}"
            prefix = "Relevant durable memory:\n" if not selected else ""
            cost = len(prefix) + len(line) + (1 if selected else 0)
            if cost <= remaining:
                selected.append(line)
                remaining -= cost
        if selected:
            messages.append(
                {"role": "system", "content": "Relevant durable memory:\n" + "\n".join(selected)}
            )
        return self._trim_required(messages)

    def _trim_required(self, messages: list[dict[str, str]]) -> list[dict[str, str]]:
        total = sum(len(item["content"]) for item in messages) + max(0, len(messages) - 1)
        if total <= self.max_chars:
            return messages
        # System and current task are mandatory. Trim lower-priority content first.
        trimmed = [dict(item) for item in messages]
        while len(trimmed) > 2 and total > self.max_chars:
            dropped = trimmed.pop()
            total -= len(dropped["content"]) + 1
        if total > self.max_chars and len(trimmed) >= 2:
            allowance = max(0, self.max_chars - len(trimmed[0]["content"]) - 1)
            trimmed[1]["content"] = trimmed[1]["content"][:allowance]
        return trimmed
