param(
    [string]$Root = "C:\ProgramData\PreActive",
    [switch]$RegisterBundledQwenHost
)

$ErrorActionPreference = "Stop"

$python = Join-Path $Root "python\python.exe"
$source = Join-Path $Root "source"
$state = Join-Path $Root "state\state.db"
$watchScript = Join-Path $Root "runtime\watchdog.ps1"

$required = @(
    $python,
    (Join-Path $source "src"),
    $watchScript
)
if ($RegisterBundledQwenHost) {
    $required += @(
        (Join-Path $Root "model-env\Lib\site-packages"),
        (Join-Path $Root "runtime\qwen_http.py"),
        (Join-Path $Root "runtime\model-path.txt")
    )
}
foreach ($path in $required) {
    if (-not (Test-Path $path)) {
        throw "Required runtime path missing: $path"
    }
}

New-Item -ItemType Directory -Force -Path (Join-Path $Root "state"), (Join-Path $Root "logs") | Out-Null

& $python -m pip install --disable-pip-version-check --no-deps --no-build-isolation -e $source
if ($LASTEXITCODE -ne 0) {
    throw "Failed to bind Pre-Active source into pinned Python"
}

$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$boot = New-ScheduledTaskTrigger -AtStartup
$longSettings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 10 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew

$daemonArgs = '-m pre_active --state "' + $state + '" daemon --poll-seconds 1 --timeout 180'
$daemonAction = New-ScheduledTaskAction -Execute $python -Argument $daemonArgs -WorkingDirectory $source
Register-ScheduledTask -TaskName "PreActive Daemon" -Action $daemonAction -Trigger $boot -Principal $principal -Settings $longSettings -Force | Out-Null

if ($RegisterBundledQwenHost) {
    $qwenScript = Join-Path $Root "runtime\qwen_http.py"
    $qwenArgs = '"' + $qwenScript + '"'
    $qwenAction = New-ScheduledTaskAction -Execute $python -Argument $qwenArgs -WorkingDirectory (Join-Path $Root "runtime")
    Register-ScheduledTask -TaskName "PreActive Qwen Endpoint" -Action $qwenAction -Trigger $boot -Principal $principal -Settings $longSettings -Force | Out-Null
} else {
    $existingQwen = Get-ScheduledTask -TaskName "PreActive Qwen Endpoint" -ErrorAction SilentlyContinue
    if ($existingQwen) {
        Stop-ScheduledTask -TaskName "PreActive Qwen Endpoint" -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName "PreActive Qwen Endpoint" -Confirm:$false
    }
}

$watchArguments = '-NoProfile -ExecutionPolicy Bypass -File "' + $watchScript + '"'
$watchAction = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $watchArguments
$watchTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1) -RepetitionDuration (New-TimeSpan -Days 3650)
$watchSettings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 1) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName "PreActive Watchdog" -Action $watchAction -Trigger $watchTrigger -Principal $principal -Settings $watchSettings -Force | Out-Null

if ($RegisterBundledQwenHost) {
    Start-ScheduledTask -TaskName "PreActive Qwen Endpoint"
}
Start-ScheduledTask -TaskName "PreActive Daemon"
Start-ScheduledTask -TaskName "PreActive Watchdog"
