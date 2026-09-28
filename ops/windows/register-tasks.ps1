param(
    [string]$Root = "C:\ProgramData\ProRun"
)

$ErrorActionPreference = "Stop"

$required = @(
    (Join-Path $Root "python\python.exe"),
    (Join-Path $Root "source\src"),
    (Join-Path $Root "model-env\Lib\site-packages"),
    (Join-Path $Root "runtime\qwen_http.py"),
    (Join-Path $Root "runtime\start-qwen.ps1"),
    (Join-Path $Root "runtime\start-prorun.ps1"),
    (Join-Path $Root "runtime\watchdog.ps1"),
    (Join-Path $Root "runtime\model-path.txt")
)
foreach ($path in $required) {
    if (-not (Test-Path $path)) {
        throw "Required runtime path missing: $path"
    }
}

New-Item -ItemType Directory -Force -Path (Join-Path $Root "state"), (Join-Path $Root "logs") | Out-Null

$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$boot = New-ScheduledTaskTrigger -AtStartup
$longSettings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 10 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew

function Register-ProRunLongTask {
    param(
        [string]$Name,
        [string]$Script
    )
    $arguments = '-NoProfile -ExecutionPolicy Bypass -File "' + $Script + '"'
    $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $arguments
    Register-ScheduledTask -TaskName $Name -Action $action -Trigger $boot -Principal $principal -Settings $longSettings -Force | Out-Null
}

Register-ProRunLongTask -Name "ProRun Qwen Endpoint" -Script (Join-Path $Root "runtime\start-qwen.ps1")
Register-ProRunLongTask -Name "ProRun Daemon" -Script (Join-Path $Root "runtime\start-prorun.ps1")

$watchScript = Join-Path $Root "runtime\watchdog.ps1"
$watchArguments = '-NoProfile -ExecutionPolicy Bypass -File "' + $watchScript + '"'
$watchAction = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $watchArguments
$watchTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1) -RepetitionDuration (New-TimeSpan -Days 3650)
$watchSettings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 1) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName "ProRun Watchdog" -Action $watchAction -Trigger $watchTrigger -Principal $principal -Settings $watchSettings -Force | Out-Null

Start-ScheduledTask -TaskName "ProRun Qwen Endpoint"
Start-ScheduledTask -TaskName "ProRun Daemon"
Start-ScheduledTask -TaskName "ProRun Watchdog"
