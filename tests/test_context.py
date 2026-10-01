from pathlib import Path

from pre_active.context import ContextAssembler
from pre_active.store import Store


def test_context_is_bounded_and_keeps_task_before_old_memory(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    store.add_memory(kind="semantic", content="old " * 100, salience=0.1, now=1.0)
    store.add_memory(kind="semantic", content="critical constraint", salience=1.0, now=2.0)

    assembler = ContextAssembler(store, max_chars=240)
    messages = assembler.build(
        system_prompt="You are Pre-Active.",
        task="Do the exact current task safely.",
        query="critical",
    )

    joined = "\n".join(message["content"] for message in messages)
    assert "Do the exact current task safely." in joined
    assert "critical constraint" in joined
    assert len(joined) <= 240
    assert "old old old old old old old old old old" not in joined
