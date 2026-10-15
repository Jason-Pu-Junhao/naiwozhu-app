# 奶我助 · Windows 兼容性体检（外壳）
#
# 用法（推荐这条，绕开执行策略的限制）：
#     powershell -ExecutionPolicy Bypass -File tools\verify_windows.ps1
#
# 它只做四件事，然后把测量交给 tools\verify_windows.py：
#   1. 找一个 Windows 上的 Python（没有就试 winget 装，winget 也没有就告诉你去哪下）
#   2. 建 .venv 并装依赖（Windows 的解释器在 .venv\Scripts\，不是在 .venv/bin/）
#   3. 跑测量脚本
#   4. 把报告路径打出来，方便你贴回来
#
# 参数：
#   -SkipDocs    只跑核心项，不装 matplotlib / python-docx（装得快一点）
#   -Open        跑完自动打开报告
#   -RecreateVenv  .venv 是从 Linux 拷过来的（只有 bin/ 没有 Scripts/）时重建它
#
# 这个文件必须存成 UTF-8 **带 BOM**：Windows PowerShell 5.1 在读不带 BOM 的
# 文件时会按本地 ANSI 代码页解释，中文注释和提示会变成乱码。

[CmdletBinding()]
param(
    [switch]$SkipDocs,
    [switch]$Open,
    [switch]$RecreateVenv
)

$ErrorActionPreference = 'Continue'
$repo = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location -LiteralPath $repo

# 控制台尽量按 UTF-8 显示，免得中文提示自己先乱码。
# 注意：这**不会**影响测量结果——子进程的输出被重定向到文件时，Python 用的是
# 系统 ANSI 代码页（GetACP），不是控制台代码页，所以该暴露的问题还是会暴露。
try {
    chcp 65001 | Out-Null
    [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
    $OutputEncoding = New-Object System.Text.UTF8Encoding($false)
} catch { }
$env:PYTHONUTF8 = '1'   # 只作用于这个外壳和测量脚本本身；测量脚本会给子进程清掉

function Say($t) { Write-Host $t }
function Fail($t) { Write-Host $t -ForegroundColor Red }

$bar = "=" * 68
Say $bar
Say "  奶我助 · Windows 兼容性体检"
Say "  仓库：$repo"
Say $bar

# ---------------------------------------------------------------- 0. 环境自检
if ([System.Environment]::OSVersion.Platform -ne [System.PlatformID]::Win32NT) {
    Fail "  这看起来不是 Windows（Platform = $([System.Environment]::OSVersion.Platform)）。"
    Fail "  这个脚本要在 Windows 上跑。"
    exit 2
}

# 从 Linux 整个拷过来的 .venv 是坏的：Windows 需要 .venv\Scripts\python.exe
$winVenvPy = Join-Path $repo '.venv\Scripts\python.exe'
$nixVenvPy = Join-Path $repo '.venv\bin\python'
if ((Test-Path -LiteralPath $nixVenvPy) -and -not (Test-Path -LiteralPath $winVenvPy)) {
    $venvDir = Join-Path $repo '.venv'
    if ($RecreateVenv) {
        Say "  .venv 是 Linux 上建的（只有 bin\），按 -RecreateVenv 重建。"
        Say "  要删掉的是：$venvDir"
        Remove-Item -LiteralPath $venvDir -Recurse -Force
    } else {
        Fail "  .venv 是从 Linux 拷过来的（只有 bin\python，没有 Scripts\python.exe）。"
        Say  "  Windows 上建的解释器不一样，这个 venv 用不了。两种做法："
        Say  "    1) 重跑一次，加上 -RecreateVenv 让我删掉重建"
        Say  "    2) 自己删：Remove-Item -Recurse -Force .venv"
        exit 2
    }
}

# ---------------------------------------------------------------- 1. 找 Python
function Resolve-BasePython {
    if (Test-Path -LiteralPath $winVenvPy) { return $winVenvPy }
    $cands = @()
    if (Get-Command py -ErrorAction SilentlyContinue) { $cands += ,@('py', '-3') }
    if (Get-Command python -ErrorAction SilentlyContinue) { $cands += ,@('python') }
    if (Get-Command python3 -ErrorAction SilentlyContinue) { $cands += ,@('python3') }
    foreach ($c in $cands) {
        try {
            $exe = $c[0]
            $pre = @()
            if ($c.Count -gt 1) { $pre = $c[1..($c.Count - 1)] }
            $v = & $exe @pre -c "import sys; print(sys.version.split()[0])" 2>$null
            if ($LASTEXITCODE -eq 0 -and $v) {
                $ver = ("$v").Trim()
                return @{ exe = $exe; pre = $pre; ver = $ver }
            }
        } catch { }
    }
    return $null
}

Say ""
Say "[1/4] 找 Python"
$py = Resolve-BasePython
if (-not $py) {
    Say "  没找到 Python。试一下用 winget 装一个（需要联网）…"
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        & winget install --id Python.Python.3.12 -e --silent `
            --accept-package-agreements --accept-source-agreements
        $env:Path = [System.Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
                    [System.Environment]::GetEnvironmentVariable('Path', 'User')
        $py = Resolve-BasePython
    } else {
        Fail "  没有 winget，也没有 Python。请先装 Python 3.10 以上："
        Say  "    https://www.python.org/downloads/windows/  （安装时勾上 Add python.exe to PATH）"
        exit 2
    }
}
if (-not $py) {
    Fail "  装了但没找到。关掉这个窗口重新开一个，再跑一次。"
    exit 2
}
if ($py -is [hashtable]) {
    $pyExe = $py.exe
    $pyPre = $py.pre
    Say "  找到 $pyExe $($pyPre -join ' ')（Python $($py.ver)）"
} else {
    $pyExe = $py
    $pyPre = @()
    Say "  用现成的 venv：$pyExe"
}

# ---------------------------------------------------------------- 2. 建 venv 装依赖
Say ""
Say "[2/4] 准备 .venv 和依赖"
$venvPy = $winVenvPy
if (-not (Test-Path -LiteralPath $venvPy)) {
    Say "  建 .venv …"
    & $pyExe @pyPre -m venv .venv
    if (-not (Test-Path -LiteralPath $venvPy)) {
        Fail "  建 venv 失败。如果报错提到权限，换个普通目录再试（别放在 C:\Program Files 下）。"
        exit 2
    }
} else {
    Say "  .venv 已存在，直接用"
}
Say "  装运行期依赖（Flask、requests）…"
& $venvPy -m pip install --disable-pip-version-check -q -r app\requirements.txt
if ($LASTEXITCODE -ne 0) {
    Fail "  装依赖失败（上面有 pip 的报错）。如果是网络/代理问题，先解决再跑。"
    exit 2
}

if (-not $SkipDocs) {
    Say "  装文档工具链（matplotlib、python-docx、python-pptx）…"
    & $venvPy -m pip install --disable-pip-version-check -q `
        matplotlib python-docx python-pptx
    if ($LASTEXITCODE -ne 0) {
        Say "  这一步没成功，不影响核心项；字体与 Word 那两步会显示成「跳过」。"
    }
} else {
    Say "  按 -SkipDocs 跳过文档工具链（字体与 Word 那两步会显示成「跳过」）"
}

# ---------------------------------------------------------------- 3. 跑测量
Say ""
Say "[3/4] 跑测量（会起一次本地服务、跑四套测试，大约一两分钟）"
Say ""
$driver = Join-Path $repo 'tools\verify_windows.py'

# 先删掉上一次的报告：否则脚本在找 Python / 装依赖那几步就退出时，
# 你会把上一份（可能是别的系统上跑的）报告当成这次的结果发回来
$stale = Join-Path $repo 'out\win-verify\verify-windows.log'
if (Test-Path -LiteralPath $stale) {
    Say "  清掉上一次的报告：$stale"
    Remove-Item -LiteralPath $stale -Force
}

$rest = @()
if ($SkipDocs) { $rest += '--skip-docs' }
& $venvPy $driver @rest
$rc = $LASTEXITCODE

# ---------------------------------------------------------------- 4. 报告
Say ""
Say "[4/4] 报告"
$log = Join-Path $repo 'out\win-verify\verify-windows.log'
$json = Join-Path $repo 'out\win-verify\verify-windows.json'
if (Test-Path -LiteralPath $log) {
    Say "  把这两份发回来就行："
    Say "    $log"
    Say "    $json"
    Say "  （原始日志在 out\win-verify\raw\，一般不用发）"
    if ($Open) { Invoke-Item -LiteralPath $log }
} else {
    Fail "  没找到报告文件，说明测量脚本没跑起来。看上面的报错。"
    exit 2
}

switch ($rc) {
    0 {
        Say ""
        Write-Host "  结论：没有失败项。" -ForegroundColor Green
    }
    1 {
        Say ""
        Write-Host "  结论：有失败项，把报告发回来我照着修。" -ForegroundColor Yellow
    }
    default {
        Say ""
        Write-Host "  测量脚本异常退出（$rc）。" -ForegroundColor Red
    }
}
exit $rc
