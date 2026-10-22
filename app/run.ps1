# 奶我助 · Windows 上的启动脚本
#
#   .\app\run.ps1                 起网页版（默认 5173 端口，只监听本机）
#   .\app\run.ps1 -Port 5199      换端口
#   .\app\run.ps1 -NoBrowser      不自动开浏览器
#   .\app\run.ps1 -Tests          跑五套测试（391 项）
#
#   # 让手机 / 树莓派访问（先设好访问口令，否则服务拒绝启动）
#   .\app\run.ps1 -BindHost 0.0.0.0 -AccessKey '你的口令'
#   # 两台机器同步（详见仓库根目录 SYNC.md）
#   .\app\run.ps1 -SyncPeer http://192.168.1.23:5173 -SyncInterval 300
#
# 对应 app/run.sh 与 app/run_tests.sh —— 那两个是 POSIX 脚本，里面写的是
# .venv/bin/python；Windows 上解释器在 .venv\Scripts\，所以另立这一份。
#
# 这个文件必须存成 UTF-8 **带 BOM**：Windows PowerShell 5.1 读不带 BOM 的文件时
# 会按本地 ANSI 代码页解释，中文提示会变成乱码。
#
# 参数名用 -BindHost，不用 -Host：$Host 是 PowerShell 的自动变量（只读），
# 拿它当参数名会直接报错。

[CmdletBinding()]
param(
    [int]$Port = 5173,
    [switch]$NoBrowser,
    [switch]$Tests,
    [string]$BindHost = '127.0.0.1',
    [string]$SyncPeer = '',
    [int]$SyncInterval = -1,
    [string]$AccessKey = ''
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
    $suites = @('test_query.py', 'test_ddl.py', 'test_store.py',
                'test_server.py', 'test_sync.py')
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
    Write-Host "五套测试全部通过。" -ForegroundColor Green
    exit 0
}

$pyArgs = @('app\server.py', '--port', "$Port", '--host', $BindHost)
if ($NoBrowser) { $pyArgs += '--no-browser' }
if ($SyncPeer) { $pyArgs += @('--sync-peer', $SyncPeer) }
if ($SyncInterval -ge 0) { $pyArgs += @('--sync-interval', "$SyncInterval") }
if ($AccessKey) { $pyArgs += @('--access-key', $AccessKey) }
& $py @pyArgs
exit $LASTEXITCODE
