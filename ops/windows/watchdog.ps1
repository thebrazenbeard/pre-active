param(
    [string]$Root = $(if ($env:PRE_ACTIVE_ROOT) { $env:PRE_ACTIVE_ROOT } else { "C:\ProgramData\PreActive" })
)

$ErrorActionPreference = "Stop"

$logDirectory = Join-Path $Root "logs"
New-Item -ItemType Directory -Force -Path $logDirectory | Out-Null
$log = Join-Path $logDirectory "watchdog.log"

$names = @("PreActive Daemon")
if (Get-ScheduledTask -TaskName "PreActive Qwen Endpoint" -ErrorAction SilentlyContinue) {
    $names += "PreActive Qwen Endpoint"
}

foreach ($name in $names) {
    try {
        $task = Get-ScheduledTask -TaskName $name -ErrorAction Stop
        if ($task.State -ne "Running") {
            Add-Content $log ((Get-Date -Format o) + " RESTART task=" + $name + " prior_state=" + $task.State)
            Start-ScheduledTask -TaskName $name
        }
    } catch {
        Add-Content $log ((Get-Date -Format o) + " ERROR task=" + $name + " message=" + $_.Exception.Message)
    }
}
