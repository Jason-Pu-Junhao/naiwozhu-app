# 奶我助 · Windows 上的启动脚本
#
#   .\app\run.ps1                 起网页版（默认 5173 端口，自动开浏览器）
#   .\app\run.ps1 -Port 5199      换端口
#   .\app\run.ps1 -NoBrowser      不自动开浏览器
#   .\app\run.ps1 -Tests          跑四套测试（222 项）
#
# 对应 app/run.sh 与 app/run_tests.sh —— 那两个是 POSIX 脚本，里面写的是
# .venv/bin/python；Windows 上解释器在 .venv\Scripts\，所以另立这一份。
#
# 这个文件必须存成 UTF-8 **带 BOM**：Windows PowerShell 5.1 读不带 BOM 的文件时
# 会按本地 ANSI 代码页解释，中文提示会变成乱码。

[CmdletBinding()]
param(
    [int]$Port = 5173,
    [switch]$NoBrowser,
    [switch]$Tests
)

$repo = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location -LiteralPath $repo

$py = Join-Path $repo '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $py)) {
    Write-Host "没有找到 .venv（Windows 上解释器在 .venv\Scripts\python.exe）。先建一个：" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "    py -3 -m venv .venv"
    Write-Host "    .venv\Scripts\python.exe -m pip install -r app\requirements.txt"
    Write-Host ""
    Write-Host "然后重新跑这个脚本。"
    exit 1
}

if ($Tests) {
    # 编码不用管：app/console.py 在输出被重定向时会自己切到 UTF-8
    $suites = @('test_query.py', 'test_ddl.py', 'test_store.py', 'test_server.py')
    $bad = 0
    foreach ($s in $suites) {
        Write-Host "=========== $s ==========="
        & $py (Join-Path $repo "app\test\$s")
        if ($LASTEXITCODE -ne 0) { $bad++ }
    }
    Write-Host ""
    if ($bad -gt 0) {
        Write-Host "有 $bad 套没通过。" -ForegroundColor Yellow
        exit 1
    }
    Write-Host "四套测试全部通过。" -ForegroundColor Green
    exit 0
}

$pyArgs = @('app\server.py', '--port', "$Port")
if ($NoBrowser) { $pyArgs += '--no-browser' }
& $py @pyArgs
exit $LASTEXITCODE
