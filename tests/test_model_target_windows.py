from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WINDOWS = ROOT / "ops" / "windows"


def test_foreground_daemon_launcher_uses_persisted_target_not_hardcoded_qwen() -> None:
    text = (WINDOWS / "start-pre-active.ps1").read_text(encoding="utf-8")
    assert "-m pre_active" in text
    assert "daemon --poll-seconds 1 --timeout 180" in text
    assert "PRE_ACTIVE_BASE_URL" not in text
    assert "PRE_ACTIVE_MODEL =" not in text
    assert "qwen3.5-4b-local" not in text


def test_windows_registration_makes_bundled_qwen_host_optional() -> None:
    text = (WINDOWS / "register-tasks.ps1").read_text(encoding="utf-8")
    assert "[switch]$RegisterBundledQwenHost" in text
    assert "if ($RegisterBundledQwenHost)" in text


def test_scheduled_tasks_own_long_lived_python_processes_directly() -> None:
    text = (WINDOWS / "register-tasks.ps1").read_text(encoding="utf-8")
    assert '$daemonAction = New-ScheduledTaskAction -Execute $python' in text
    assert '$qwenAction = New-ScheduledTaskAction -Execute $python' in text
    assert 'runtime\\start-pre-active.ps1' not in text
    assert 'runtime\\start-qwen.ps1' not in text
    assert "-m pre_active" in text
    assert "qwen_http.py" in text


def test_registration_binds_exact_source_into_pinned_python() -> None:
    text = (WINDOWS / "register-tasks.ps1").read_text(encoding="utf-8")
    assert "pre_active_source.pth" in text
    assert 'Set-Content -Path $sourceBinding -Value (Join-Path $source "src")' in text
    assert "import pathlib, pre_active" in text
    assert "-m pip install" not in text
    assert "Failed to bind Pre-Active source into pinned Python" in text


def test_qwen_models_endpoint_advertises_model_provenance() -> None:
    text = (WINDOWS / "qwen_http.py").read_text(encoding="utf-8")
    assert "model_provenance" in text
    assert '"provenance"' in text
    assert "effect_authority" not in text


def test_qwen_host_owns_gpu_wait_and_model_path_resolution() -> None:
    text = (WINDOWS / "qwen_http.py").read_text(encoding="utf-8")
    assert "MODEL_PATH_FILE" in text
    assert "model-path.txt" in text
    assert "MODEL_SITE_PACKAGES" in text
    assert "WAIT_GPU" in text
    assert "MIN_FREE_VRAM_MIB" in text
    assert "PORT_ALREADY_LISTENING" in text


def test_watchdog_requires_daemon_but_only_watches_qwen_when_registered() -> None:
    text = (WINDOWS / "watchdog.ps1").read_text(encoding="utf-8")
    assert '$names = @("PreActive Daemon")' in text
    assert 'Get-ScheduledTask -TaskName "PreActive Qwen Endpoint"' in text
    assert '$names += "PreActive Qwen Endpoint"' in text
