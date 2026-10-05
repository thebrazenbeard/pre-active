$ErrorActionPreference = "Stop"

$root = if ($env:PRE_ACTIVE_ROOT) { $env:PRE_ACTIVE_ROOT } else { "C:\ProgramData\PreActive" }
$python = Join-Path $root "python\python.exe"
$state = Join-Path $root "state\state.db"
$supervisorLog = Join-Path $root "logs\pre-active-supervisor.log"

if (-not (Test-Path $python)) { throw "Pinned Python missing: $python" }

$env:PYTHONPATH = Join-Path $root "source\src"

while ($true) {
    & $python -m pre_active --state $state target show *> $null
    if ($LASTEXITCODE -ne 0) {
        Add-Content $supervisorLog ((Get-Date -Format o) + " MODEL_TARGET_NOT_CONFIGURED; retrying supervisor cycle")
        Start-Sleep -Seconds 10
        continue
    }

    Add-Content $supervisorLog ((Get-Date -Format o) + " START pre_active daemon")
    $args = @(
        "-m", "pre_active",
        "--state", $state,
        "daemon", "--poll-seconds", "1", "--timeout", "180"
    )
    $stdout = Join-Path $root "logs\pre-active-daemon.out.log"
    $stderr = Join-Path $root "logs\pre-active-daemon.err.log"
    $p = Start-Process -FilePath $python -ArgumentList $args -Wait -PassThru -RedirectStandardOutput $stdout -RedirectStandardError $stderr
    Add-Content $supervisorLog ((Get-Date -Format o) + " EXIT code=" + $p.ExitCode)
    Start-Sleep -Seconds 5
}
