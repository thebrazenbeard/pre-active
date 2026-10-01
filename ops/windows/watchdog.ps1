$ErrorActionPreference = "Stop"

$root = if ($env:PRE_ACTIVE_ROOT) { $env:PRE_ACTIVE_ROOT } else { "C:\ProgramData\PreActive" }
$log = Join-Path $root "logs\watchdog.log"

foreach ($name in @("Pre-Active Qwen Endpoint", "Pre-Active Daemon")) {
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
