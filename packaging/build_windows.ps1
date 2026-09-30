# 构建 Windows 发行包（必须在 Windows 上执行）
#
#   powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
#
# 产物：
#   dist\email-assistant\                                  可执行目录（绿色版）
#   dist\email-assistant-<版本>-windows-x64.zip
#   dist\邮件管理助手-<版本>-setup.exe                    安装程序（需 Inno Setup 6）
#
# 说明：PyInstaller 不支持交叉编译，Windows 产物必须在 Windows 上构建，
#       或使用 CI（.github\workflows\build.yml 的 windows-latest 任务）。

param(
    [switch]$SkipInstaller
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $ProjectRoot

$Version = (Select-String -Path "src\__init__.py" -Pattern '__version__ = "([^"]+)"').Matches[0].Groups[1].Value
if (-not $Version) { $Version = "0.0.0" }

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

Write-Host "==> 冒烟测试"
& "dist\email-assistant\email-assistant.exe" --version
& "dist\email-assistant\email-assistant.exe" doctor | Out-Null

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
# 保留顶层 email-assistant\ 目录，与 Linux/macOS 的 tar 布局一致
Compress-Archive -Path "dist\email-assistant" -DestinationPath $Zip -Force
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
            Write-Host "    -> dist\邮件管理助手-$Version-setup.exe" -ForegroundColor Green
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
