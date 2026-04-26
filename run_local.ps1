$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$runtimePython = "C:\Users\17628\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
$vendorPackages = Join-Path $projectRoot ".vendor"

if (-not (Test-Path $runtimePython)) {
    throw "找不到桌面运行时 Python：$runtimePython"
}

if (-not (Test-Path $vendorPackages)) {
    throw "找不到项目依赖目录：$vendorPackages"
}

$pythonPathParts = @($projectRoot, $vendorPackages)
$env:PYTHONPATH = ($pythonPathParts -join ";")

& $runtimePython -m uvicorn server:app --reload --host 127.0.0.1 --port 8000
