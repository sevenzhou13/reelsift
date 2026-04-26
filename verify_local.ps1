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

$env:PYTHONPATH = ($projectRoot + ";" + $vendorPackages)

$script = @'
from fastapi.testclient import TestClient
import server

client = TestClient(server.app)
for path in ["/", "/upload"]:
    response = client.get(path)
    print(path, response.status_code)
    if response.status_code != 200:
        raise SystemExit(1)
print("verify ok")
'@

$tempScript = Join-Path $projectRoot ".verify_local_temp.py"
Set-Content -Path $tempScript -Value $script -Encoding UTF8
try {
    & $runtimePython $tempScript
}
finally {
    if (Test-Path $tempScript) {
        Remove-Item -LiteralPath $tempScript -Force
    }
}
