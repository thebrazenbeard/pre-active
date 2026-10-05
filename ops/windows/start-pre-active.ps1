param(
    [string]$Root = "C:\ProgramData\PreActive"
)

$ErrorActionPreference = "Stop"
$python = Join-Path $Root "python\python.exe"
$state = Join-Path $Root "state\state.db"

if (-not (Test-Path $python)) { throw "Pinned Python missing: $python" }

& $python -m pre_active --state $state daemon --poll-seconds 1 --timeout 180
exit $LASTEXITCODE
