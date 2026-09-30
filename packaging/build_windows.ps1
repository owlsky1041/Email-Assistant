# 构建 Windows 发行包（必须在 Windows 上执行）
#
#   powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
#
# 产物：
#   dist\email-assistant\                                  可执行目录（绿色版）
#   dist\email-assistant-<版本>-windows-x64.zip
#   dist\EmailAssistant-<版本>-windows-setup.exe           安装程序（需 Inno Setup 6）
#
# 说明：PyInstaller 不支持交叉编译，Windows 产物必须在 Windows 上构建，
#       或使用 CI（.github\workflows\build.yml 的 windows-latest 任务）。

param(
    [switch]$SkipInstaller
)

$ErrorActionPreference = "Stop"

# PowerShell 7.4+ 默认把**原生命令的非零退出码**也视为终止性错误
# （$PSNativeCommandUseErrorActionPreference 默认 $true）。
# 本项目里 `doctor` 在首次运行（尚未配置邮箱）时会**正常返回 1**，
# 于是脚本会在冒烟测试处莫名其妙地中断——实测这就是 Windows 打包失败的原因。
# 因此显式关闭，改为逐处检查 $LASTEXITCODE。
if (Get-Variable PSNativeCommandUseErrorActionPreference -ErrorAction SilentlyContinue) {
    $PSNativeCommandUseErrorActionPreference = $false
}

$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $ProjectRoot

$Version = "0.0.0"
$versionMatch = Select-String -Path "src\__init__.py" -Pattern '__version__ = "([^"]+)"'
if ($versionMatch -and $versionMatch.Matches.Count -gt 0) {
    $Version = $versionMatch.Matches[0].Groups[1].Value
} else {
    Write-Host "! 未能从 src\__init__.py 解析版本号，回退为 $Version" -ForegroundColor Yellow
}

$Python = if ($env:PYTHON) { $env:PYTHON } else { "python" }

Write-Host "==> 构建 Windows 发行包 v$Version" -ForegroundColor Cyan

# ---- 1. 依赖检查 ----------------------------------------------------------
& $Python -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "!! 未安装 PyInstaller。请执行：pip install pyinstaller" -ForegroundColor Red
    exit 1
}

foreach ($pkg in @("onnxruntime", "tokenizers", "chromadb", "pystray", "PIL")) {
    & $Python -c "import $pkg" 2>$null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "    OK  $pkg" -ForegroundColor Green
    } else {
        Write-Host "    !   $pkg 未安装 —— 打包产物将缺少对应能力" -ForegroundColor Yellow
    }
}

# ---- 2. 清理并构建 --------------------------------------------------------
if (Test-Path build) { Remove-Item -Recurse -Force build }
if (Test-Path dist)  { Remove-Item -Recurse -Force dist }

& $Python -m PyInstaller packaging\email-assistant.spec --noconfirm --log-level WARN
if ($LASTEXITCODE -ne 0) {
    Write-Host "!! 构建失败" -ForegroundColor Red
    exit 1
}

Write-Host "==> 冒烟测试（隔离环境，验证产物自包含）"
$SmokeHome = Join-Path $env:TEMP ("ea-smoke-" + [guid]::NewGuid().ToString("N").Substring(0, 8))
New-Item -ItemType Directory -Force -Path $SmokeHome | Out-Null
$Exe = "dist\email-assistant\email-assistant.exe"

# 用独立 HOME 初始化，避免污染构建机上的既有配置
$env:EMAIL_ASSISTANT_HOME = $SmokeHome
& $Exe init --non-interactive | Out-Null
& $Exe --version
$DoctorJson = Join-Path $SmokeHome "doctor.json"
# doctor 在未配置邮箱时返回 1，这是预期行为，因此不检查此处退出码
& $Exe doctor --json > $DoctorJson

# 解析 JSON 检查关键能力（退出码非 0 即中止）
$Checker = @'
import json, sys
checks = json.load(open(sys.argv[1], encoding="utf-8"))["checks"]
by_name = {c["name"]: c for c in checks}
required = [
    "SQLite 可用", "FTS5 全文检索", "数据库完整性",
    "归档目录可写", "依赖 imap_tools", "依赖 fastapi",
    "依赖 cryptography", "依赖 bs4", "依赖 markdownify",
]
failed = [n for n in required if not by_name.get(n, {}).get("ok")]
if failed:
    print("    X 关键检查未通过：" + ", ".join(failed))
    sys.exit(1)
print("    OK 关键检查全部通过")
for n in ("可选依赖 onnxruntime", "可选依赖 chromadb", "可选依赖 pystray"):
    print("      " + ("OK " if by_name.get(n, {}).get("ok") else "-- ") + n)
'@
# PowerShell 没有 bash 的 <<EOF heredoc，改为把脚本文本管道给 `python -`
$Checker | & $Python - $DoctorJson
if ($LASTEXITCODE -ne 0) {
    Write-Host "!! 冒烟测试失败，中止打包" -ForegroundColor Red
    exit 1
}
Remove-Item -Recurse -Force $SmokeHome -ErrorAction SilentlyContinue
Remove-Item Env:\EMAIL_ASSISTANT_HOME -ErrorAction SilentlyContinue

# ---- 2.5 清理包内运行时残留 -----------------------------------------------
# 发行包绝不能带构建机的配置 / 数据库 / 日志
Write-Host "==> 清理包内运行时残留"
foreach ($d in @("config", "data", "logs")) {
    $p = "dist\email-assistant\$d"
    if (Test-Path $p) { Remove-Item -Recurse -Force $p }
}
Get-ChildItem -Path "dist\email-assistant" -Filter "*.tmp" -Recurse -ErrorAction SilentlyContinue |
    Remove-Item -Force -ErrorAction SilentlyContinue

# ---- 3. 快速上手说明 ------------------------------------------------------
$Readme = @"
腾讯企业邮箱邮件管理助手 —— Windows 版
=======================================

【图形界面】双击 email-assistant-tray.exe，程序会常驻系统托盘。

【命令行】在本目录打开 PowerShell 或 CMD：

  1. 初始化配置（生成 config\config.yaml）
       email-assistant.exe init

  2. 设置授权码（输入不回显）
       email-assistant.exe auth set
     也可改用环境变量：
       set EMAIL_ASSISTANT_AUTH_CODE=你的授权码

  3. 自检
       email-assistant.exe doctor --check-imap

  4. 同步 / 检索 / 服务
       email-assistant.exe sync
       email-assistant.exe search "报销发票"
       email-assistant.exe serve      # http://127.0.0.1:8990/docs

想先看看效果（不连邮箱）：
       email-assistant.exe demo

启用语义检索（需要 ONNX 模型）：
       详见 README.md「启用真正的语义检索」

数据位置：本目录下的 config\ data\ logs\
可用 EMAIL_ASSISTANT_HOME 环境变量整体重定位。
"@
$Readme | Out-File -FilePath "dist\email-assistant\快速上手.txt" -Encoding UTF8

# ---- 4. 打包 zip（绿色版）-------------------------------------------------
$Zip = "dist\email-assistant-$Version-windows-x64.zip"
Write-Host "==> 打包 $Zip"
# 保留顶层 email-assistant\ 目录，与 Linux/macOS 的 tar 布局一致。
# 优先用 Windows 10+ 自带的 bsdtar：Compress-Archive 在 300MB+ /
# 上万个文件时极慢且可能内存溢出（会撞上 CI 超时）。
$UseTar = $false
try {
    & tar.exe --version > $null 2>&1
    $UseTar = ($LASTEXITCODE -eq 0)
} catch { $UseTar = $false }

if ($UseTar) {
    Remove-Item -Force $Zip -ErrorAction SilentlyContinue
    Push-Location dist
    & tar.exe -a -c -f "email-assistant-$Version-windows-x64.zip" "email-assistant"
    $tarRc = $LASTEXITCODE
    Pop-Location
    if ($tarRc -ne 0) {
        Write-Host "    ! tar 打包失败，回退到 Compress-Archive" -ForegroundColor Yellow
        Compress-Archive -Path "dist\email-assistant" -DestinationPath $Zip -Force
    }
} else {
    Compress-Archive -Path "dist\email-assistant" -DestinationPath $Zip -Force
}
$ZipSize = [math]::Round((Get-Item $Zip).Length / 1MB, 1)
Write-Host "    -> $Zip ($ZipSize MB)"

# ---- 5. Inno Setup 安装程序（可选）----------------------------------------
if ($SkipInstaller) {
    Write-Host "    (跳过安装程序：-SkipInstaller)"
} else {
    $Iscc = $null
    foreach ($p in @(
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
    )) {
        if (Test-Path $p) { $Iscc = $p; break }
    }

    if ($Iscc) {
        Write-Host "==> 构建安装程序"
        & $Iscc "/DMyAppVersion=$Version" "packaging\installer.iss"
        if ($LASTEXITCODE -eq 0) {
            Write-Host "    -> dist\EmailAssistant-$Version-windows-setup.exe" -ForegroundColor Green
        } else {
            Write-Host "    ! 安装程序构建失败（不影响 zip）" -ForegroundColor Yellow
        }
    } else {
        Write-Host "    (跳过安装程序：未安装 Inno Setup 6)" -ForegroundColor Yellow
        Write-Host "     下载：https://jrsoftware.org/isdl.php"
        Write-Host "     或使用 winget：winget install JRSoftware.InnoSetup"
    }
}

Write-Host ""
Write-Host "==> 完成。产物在 dist\ 下。" -ForegroundColor Cyan
