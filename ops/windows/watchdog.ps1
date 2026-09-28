$ErrorActionPreference = "Stop"

$root = if ($env:PRO_RUN_ROOT) { $env:PRO_RUN_ROOT } else { "C:\ProgramData\ProRun" }
$log = Join-Path $root "logs\watchdog.log"

foreach ($name in @("ProRun Qwen Endpoint", "ProRun Daemon")) {
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
