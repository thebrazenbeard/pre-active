$ErrorActionPreference = "Stop"

$root = if ($env:PRE_ACTIVE_ROOT) { $env:PRE_ACTIVE_ROOT } else { "C:\ProgramData\PreActive" }
$python = Join-Path $root "python\python.exe"
$supervisorLog = Join-Path $root "logs\pre-active-supervisor.log"
$modelPort = if ($env:PRE_ACTIVE_MODEL_PORT) { [int]$env:PRE_ACTIVE_MODEL_PORT } else { 18081 }
$modelId = if ($env:PRE_ACTIVE_MODEL_ID) { $env:PRE_ACTIVE_MODEL_ID } else { "qwen3.5-4b-local" }

if (-not (Test-Path $python)) { throw "Pinned Python missing: $python" }

$env:PYTHONPATH = Join-Path $root "source\src"
$env:PRE_ACTIVE_BASE_URL = "http://127.0.0.1:$modelPort/v1"
$env:PRE_ACTIVE_MODEL = $modelId

while ($true) {
    $deadline = (Get-Date).AddMinutes(3)
    $ready = $false
    while ((Get-Date) -lt $deadline) {
        try {
            $response = Invoke-RestMethod "$($env:PRE_ACTIVE_BASE_URL)/models" -TimeoutSec 2
            if ($response.data.id -contains $modelId) {
                $ready = $true
                break
            }
        } catch {
        }
        Start-Sleep -Seconds 2
    }

    if (-not $ready) {
        Add-Content $supervisorLog ((Get-Date -Format o) + " MODEL_NOT_READY; retrying supervisor cycle")
        Start-Sleep -Seconds 10
        continue
    }

    Add-Content $supervisorLog ((Get-Date -Format o) + " START pre-active daemon")
    $args = @(
        "-m", "pre_active",
        "--state", (Join-Path $root "state\state.db"),
        "daemon", "--poll-seconds", "1", "--timeout", "180"
    )
    $stdout = Join-Path $root "logs\pre-active-daemon.out.log"
    $stderr = Join-Path $root "logs\pre-active-daemon.err.log"
    $p = Start-Process -FilePath $python -ArgumentList $args -Wait -PassThru -RedirectStandardOutput $stdout -RedirectStandardError $stderr
    Add-Content $supervisorLog ((Get-Date -Format o) + " EXIT code=" + $p.ExitCode)
    Start-Sleep -Seconds 5
}
