from pathlib import Path
import tempfile

from pre_active.context import ContextAssembler
from pre_active.engine import Engine, ModelResponse
from pre_active.store import Store
from pre_active.tools import ToolRegistry


class EchoModel:
    def respond(self, *, messages, tools):
        return ModelResponse(final_text="completed by the example model")


with tempfile.TemporaryDirectory() as temp:
    store = Store(Path(temp) / "state.db")
    try:
        engine = Engine(
            store=store,
            model=EchoModel(),
            tools=ToolRegistry(store),
            context=ContextAssembler(store),
            system_prompt="Execute the task.",
            worker_id="example",
        )
        run_id = engine.submit_task("Demonstrate durable execution", set(), now=1.0)
        engine.run_once(now=2.0)
        print(store.get_run(run_id))
    finally:
        store.close()
