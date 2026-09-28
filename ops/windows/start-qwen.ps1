$ErrorActionPreference = "Stop"

$root = if ($env:PRO_RUN_ROOT) { $env:PRO_RUN_ROOT } else { "C:\ProgramData\ProRun" }
$python = Join-Path $root "python\python.exe"
$script = Join-Path $root "runtime\qwen_http.py"
$modelPathFile = Join-Path $root "runtime\model-path.txt"
$supervisorLog = Join-Path $root "logs\qwen-supervisor.log"
$nvidiaSmi = "C:\Windows\System32\nvidia-smi.exe"
$minimumFreeMiB = if ($env:PRO_RUN_MIN_FREE_VRAM_MIB) { [int]$env:PRO_RUN_MIN_FREE_VRAM_MIB } else { 3400 }
$port = if ($env:PRO_RUN_MODEL_PORT) { [int]$env:PRO_RUN_MODEL_PORT } else { 18081 }

if (-not (Test-Path $python)) { throw "Pinned Python missing: $python" }
if (-not (Test-Path $script)) { throw "Qwen server script missing: $script" }
if (-not (Test-Path $nvidiaSmi)) { throw "nvidia-smi missing: $nvidiaSmi" }

if (-not $env:PRO_RUN_MODEL_PATH) {
    if (-not (Test-Path $modelPathFile)) { throw "Model path binding missing: $modelPathFile" }
    $env:PRO_RUN_MODEL_PATH = (Get-Content $modelPathFile -Raw).Trim()
}
if (-not (Test-Path $env:PRO_RUN_MODEL_PATH)) { throw "Bound model path does not exist: $($env:PRO_RUN_MODEL_PATH)" }

$env:PYTHONPATH = Join-Path $root "model-env\Lib\site-packages"

while ($true) {
    $listener = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    if ($listener) {
        Add-Content $supervisorLog ((Get-Date -Format o) + " PORT_ALREADY_LISTENING port=" + $port + " pid=" + $listener.OwningProcess)
        Start-Sleep -Seconds 15
        continue
    }

    $freeRaw = & $nvidiaSmi --query-gpu=memory.free --format=csv,noheader,nounits 2>$null | Select-Object -First 1
    $freeMiB = 0
    [void][int]::TryParse(($freeRaw -replace '[^0-9]',''), [ref]$freeMiB)
    if ($freeMiB -lt $minimumFreeMiB) {
        Add-Content $supervisorLog ((Get-Date -Format o) + " WAIT_GPU free_mib=" + $freeMiB + " required_mib=" + $minimumFreeMiB)
        Start-Sleep -Seconds 15
        continue
    }

    Add-Content $supervisorLog ((Get-Date -Format o) + " START qwen_http.py free_mib=" + $freeMiB)
    $stdout = Join-Path $root "logs\qwen-http.out.log"
    $stderr = Join-Path $root "logs\qwen-http.err.log"
    $p = Start-Process -FilePath $python -ArgumentList @($script) -Wait -PassThru -RedirectStandardOutput $stdout -RedirectStandardError $stderr
    Add-Content $supervisorLog ((Get-Date -Format o) + " EXIT code=" + $p.ExitCode)
    Start-Sleep -Seconds 5
}
