from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WINDOWS = ROOT / "ops" / "windows"


def test_resident_daemon_uses_persisted_model_target_not_hardcoded_qwen() -> None:
    text = (WINDOWS / "start-pre-active.ps1").read_text(encoding="utf-8")
    assert "target show" in text
    assert "PRE_ACTIVE_BASE_URL" not in text
    assert "PRE_ACTIVE_MODEL =" not in text
    assert "qwen3.5-4b-local" not in text


def test_windows_registration_makes_bundled_qwen_host_optional() -> None:
    text = (WINDOWS / "register-tasks.ps1").read_text(encoding="utf-8")
    assert "[switch]$RegisterBundledQwenHost" in text
    assert 'Register-PreActiveLongTask -Name "PreActive Daemon"' in text
    assert "if ($RegisterBundledQwenHost)" in text


def test_watchdog_requires_daemon_but_only_watches_qwen_when_registered() -> None:
    text = (WINDOWS / "watchdog.ps1").read_text(encoding="utf-8")
    assert '$names = @("PreActive Daemon")' in text
    assert 'Get-ScheduledTask -TaskName "PreActive Qwen Endpoint"' in text
    assert '$names += "PreActive Qwen Endpoint"' in text
