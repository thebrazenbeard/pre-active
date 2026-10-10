from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


WINDOWS = Path(__file__).resolve().parents[1] / "ops" / "windows"
HARNESS = Path(__file__).parent / "windows" / "task_script_harness.ps1"
POWERSHELL = shutil.which("powershell.exe") if os.name == "nt" else None
pytestmark = pytest.mark.skipif(
    POWERSHELL is None, reason="Windows PowerShell is required for mocked task-script execution"
)


def run_script(tmp_path: Path, mode: str, **options: object) -> list[dict]:
    case = {
        "mode": mode,
        "script": str(WINDOWS / ("register-tasks.ps1" if mode == "register" else "watchdog.ps1")),
        "root": None,
        "environment_root": None,
        "register_bundled_qwen": False,
        "tasks": {},
        **options,
    }
    input_path = tmp_path / "task-script-case.json"
    input_path.write_text(json.dumps(case), encoding="utf-8")
    result = subprocess.run(
        [str(POWERSHELL), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-File", str(HARNESS), "-InputPath", str(input_path)],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


def test_default_registration_preserves_existing_optional_endpoint(tmp_path: Path) -> None:
    effects = run_script(
        tmp_path, "register", root=str(tmp_path / "runtime root with spaces"),
        tasks={"PreActive Qwen Endpoint": "Running"},
    )
    assert not [effect for effect in effects if effect["kind"] in {"stop", "unregister"}]
    assert [effect["task"] for effect in effects if effect["kind"] == "register"] == [
        "PreActive Daemon", "PreActive Watchdog",
    ]
    assert [effect["task"] for effect in effects if effect["kind"] == "start"] == [
        "PreActive Daemon", "PreActive Watchdog",
    ]


def test_registration_passes_custom_root_to_watchdog(tmp_path: Path) -> None:
    root = str(tmp_path / "runtime root with spaces")
    effects = run_script(tmp_path, "register", root=root)
    watchdog = next(effect for effect in effects
                    if effect["kind"] == "register" and effect["task"] == "PreActive Watchdog")
    assert watchdog["action"]["Argument"] == (
        f'-NoProfile -ExecutionPolicy Bypass -File "{root}\\runtime\\watchdog.ps1" -Root "{root}"'
    )


def test_explicit_bundled_endpoint_registration_binds_requested_root(tmp_path: Path) -> None:
    root = str(tmp_path / "runtime root with spaces")
    effects = run_script(tmp_path, "register", root=root, register_bundled_qwen=True)
    endpoint = next(effect for effect in effects
                    if effect["kind"] == "register" and effect["task"] == "PreActive Qwen Endpoint")
    assert endpoint["action"] == {
        "Execute": f"{root}\\python\\python.exe",
        "Argument": f'"{root}\\runtime\\qwen_http.py"',
        "WorkingDirectory": f"{root}\\runtime",
    }
    assert [effect["task"] for effect in effects if effect["kind"] == "start"] == [
        "PreActive Qwen Endpoint", "PreActive Daemon", "PreActive Watchdog",
    ]


@pytest.mark.parametrize("root_source", ["parameter", "environment", "default"])
def test_watchdog_uses_bound_root_and_creates_log_directory(tmp_path: Path, root_source: str) -> None:
    root = str(tmp_path / "runtime root with spaces")
    options: dict[str, object] = {"tasks": {"PreActive Daemon": "Ready"}}
    if root_source == "parameter":
        options.update(root=root, environment_root=str(tmp_path / "other root"))
    elif root_source == "environment":
        options["environment_root"] = root
    else:
        root = r"C:\ProgramData\PreActive"
    effects = run_script(tmp_path, "watchdog", **options)
    assert {effect["path"] for effect in effects if effect["kind"] == "mkdir"} == {f"{root}\\logs"}
    log_writes = [effect for effect in effects if effect["kind"] == "append"]
    assert len(log_writes) == 1
    assert next(i for i, effect in enumerate(effects) if effect["kind"] == "mkdir") < next(
        i for i, effect in enumerate(effects) if effect["kind"] == "append"
    )
    assert log_writes[0]["path"] == f"{root}\\logs\\watchdog.log"
    assert "RESTART task=PreActive Daemon prior_state=Ready" in log_writes[0]["value"]
    assert [effect["task"] for effect in effects if effect["kind"] == "start"] == ["PreActive Daemon"]


def test_watchdog_does_not_restart_running_tasks(tmp_path: Path) -> None:
    effects = run_script(
        tmp_path, "watchdog", root=str(tmp_path / "runtime root with spaces"),
        tasks={"PreActive Daemon": "Running", "PreActive Qwen Endpoint": "Running"},
    )
    assert not [effect for effect in effects if effect["kind"] in {"start", "append"}]
