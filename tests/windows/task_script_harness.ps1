param([Parameter(Mandatory = $true)][string]$InputPath)

$ErrorActionPreference = "Stop"
$case = Get-Content -LiteralPath $InputPath -Raw | ConvertFrom-Json
$script:effects = [System.Collections.Generic.List[object]]::new()

function Record-Effect([string]$Kind, [hashtable]$Fields) {
    $Fields.kind = $Kind
    $script:effects.Add([pscustomobject]$Fields)
}

# These are the host-effect boundaries. No real task service, process control,
# or runtime-file writes are reached by either production script.
function New-ScheduledTaskPrincipal {
    param($UserId, $LogonType, $RunLevel)
    return [pscustomobject]@{ UserId = $UserId; LogonType = $LogonType; RunLevel = $RunLevel }
}
function New-ScheduledTaskTrigger {
    param([switch]$AtStartup, [switch]$Once, $At, $RepetitionInterval, $RepetitionDuration)
    return [pscustomobject]@{ AtStartup = [bool]$AtStartup; Once = [bool]$Once }
}
function New-ScheduledTaskSettingsSet {
    param($ExecutionTimeLimit, $RestartCount, $RestartInterval, [switch]$StartWhenAvailable,
        [switch]$AllowStartIfOnBatteries, [switch]$DontStopIfGoingOnBatteries, $MultipleInstances)
    return [pscustomobject]@{ MultipleInstances = $MultipleInstances }
}
function New-ScheduledTaskAction {
    param($Execute, $Argument, $WorkingDirectory)
    return [pscustomobject]@{ Execute = $Execute; Argument = $Argument; WorkingDirectory = $WorkingDirectory }
}
function Register-ScheduledTask {
    param($TaskName, $Action, $Trigger, $Principal, $Settings, [switch]$Force)
    Record-Effect "register" @{ task = $TaskName; action = $Action }
}
function Get-ScheduledTask {
    [CmdletBinding()]
    param($TaskName)
    Record-Effect "get" @{ task = $TaskName }
    $property = $case.tasks.PSObject.Properties[$TaskName]
    if ($null -ne $property) {
        return [pscustomobject]@{ TaskName = $TaskName; State = $property.Value }
    }
    if ($PSBoundParameters["ErrorAction"] -eq "Stop") { throw "Task missing: $TaskName" }
}
function Start-ScheduledTask {
    param($TaskName)
    Record-Effect "start" @{ task = $TaskName }
}
function Stop-ScheduledTask {
    [CmdletBinding()]
    param($TaskName)
    Record-Effect "stop" @{ task = $TaskName }
}
function Unregister-ScheduledTask {
    param($TaskName, [switch]$Confirm)
    Record-Effect "unregister" @{ task = $TaskName }
}
function Test-Path { param($Path) return $true }
function New-Item {
    param($ItemType, [switch]$Force, [string[]]$Path)
    foreach ($itemPath in $Path) { Record-Effect "mkdir" @{ path = $itemPath } }
}
function Set-Content {
    param($Path, $Value, $Encoding)
    Record-Effect "write" @{ path = $Path; value = $Value }
}
function Add-Content {
    param([Parameter(Position = 0)]$Path, [Parameter(Position = 1)]$Value)
    Record-Effect "append" @{ path = $Path; value = $Value }
}
function Start-Process { throw "Unexpected process launch in task-script test" }
function Stop-Process { throw "Unexpected process stop in task-script test" }
function Invoke-FakePinnedPython {
    param($Python)
    Record-Effect "python_import" @{ path = $Python }
    $global:LASTEXITCODE = 0
}

$env:PRE_ACTIVE_ROOT = $case.environment_root
$source = [System.IO.File]::ReadAllText($case.script)
if ($case.mode -eq "register") {
    # Intercept only the native interpreter preflight. The actual task scripts
    # execute unchanged at all scheduler boundaries, which are mocked above.
    $tokens = $null
    $parseErrors = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseInput($source, [ref]$tokens, [ref]$parseErrors)
    if ($parseErrors.Count -ne 0) { throw "Cannot parse production task script" }
    $nativeCalls = @($ast.FindAll({ param($node)
        $node -is [System.Management.Automation.Language.CommandAst] -and
        $node.InvocationOperator -eq [System.Management.Automation.Language.TokenKind]::Ampersand
    }, $true))
    if ($nativeCalls.Count -ne 1 -or $nativeCalls[0].CommandElements[0].Extent.Text -ne '$python') {
        throw "Unexpected native process boundary in registration script"
    }
    $extent = $nativeCalls[0].Extent
    $source = $source.Substring(0, $extent.StartOffset) + 'Invoke-FakePinnedPython -Python $python' + $source.Substring($extent.EndOffset)
    $parameters = @{ Root = $case.root }
    if ($case.register_bundled_qwen) { $parameters.RegisterBundledQwenHost = $true }
} else {
    $parameters = @{}
    if ($case.root) { $parameters.Root = $case.root }
}
& ([scriptblock]::Create($source)) @parameters
ConvertTo-Json -InputObject @($script:effects.ToArray()) -Depth 8 -Compress
