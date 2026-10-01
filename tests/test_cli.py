import json
from pathlib import Path

from pre_active.cli import main
from pre_active.store import Store


def test_cli_submit_creates_durable_run_and_event(tmp_path: Path, capsys) -> None:
    state = tmp_path / "state.db"
    code = main([
        "--state", str(state),
        "submit", "Do the thing",
        "--capability", "files.read",
    ])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    store = Store(state)
    run = store.get_run(payload["run_id"])
    assert run["task"] == "Do the thing"
    assert run["capabilities"] == {"files.read"}
    assert store.pending_event_count() == 1
