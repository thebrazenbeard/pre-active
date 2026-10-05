param(
    [string]$Root = "C:\ProgramData\PreActive"
)

$ErrorActionPreference = "Stop"
$python = Join-Path $Root "python\python.exe"
$script = Join-Path $Root "runtime\qwen_http.py"

if (-not (Test-Path $python)) { throw "Pinned Python missing: $python" }
if (-not (Test-Path $script)) { throw "Qwen server script missing: $script" }

& $python $script
exit $LASTEXITCODE
