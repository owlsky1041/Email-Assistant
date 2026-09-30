# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（三平台通用）。

用法::

    pyinstaller packaging/email-assistant.spec --noconfirm

产物::

    dist/email-assistant/
        email-assistant          # 主程序（控制台版，用于 sync / doctor 等 CLI）
        email-assistant-tray     # 托盘版（Windows/macOS 下不弹控制台窗口）
        _internal/               # 依赖与原生库
    dist/邮件管理助手.app/        # 仅 macOS

设计决策
--------
1. **onedir 而非 onefile**：onefile 每次启动都要把几百 MB 解压到临时目录，
   对常驻托盘程序是灾难；onedir 也便于用户替换配置、追加 ONNX 模型。
2. **显式排除导出工具链**（torch / transformers / optimum）：
   它们只在导出 ONNX 模型时需要，运行时完全用不到，却有 900MB+。
3. **不打包 ONNX 模型**：95MB 的模型放外部 ``data/models/``，
   便于用户更换模型，也让安装包保持精简。
4. ``UPX`` 关闭：压缩后常被杀毒软件误报，邮件工具不值得冒这个险。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

# spec 位于 packaging/ 下，其父目录即项目根
PROJECT_ROOT = Path(SPECPATH).resolve().parent  # noqa: F821

# ---------------------------------------------------------------------------
# 数据与二进制收集
# ---------------------------------------------------------------------------

datas: list[tuple[str, str]] = [
    (str(PROJECT_ROOT / "README.md"), "."),
]

binaries: list[tuple[str, str]] = []


def _collect(package: str, *, submodules: bool = True) -> None:
    """收集包的代码、数据文件与动态库。

    chromadb / onnxruntime / tokenizers 都有 PyInstaller 静态分析
    看不到的数据文件或原生库，必须显式收集，否则运行时报
    ``FileNotFoundError`` 或 ``ImportError``。
    """
    try:
        pkg_datas, pkg_binaries, pkg_hidden = collect_all(package)
    except Exception as exc:  # noqa: BLE001 - 未安装时静默跳过
        print(f"[spec] 跳过 {package}：{exc}")
        return
    datas.extend(pkg_datas)
    binaries.extend(pkg_binaries)
    hiddenimports.extend(pkg_hidden)
    if submodules:
        hiddenimports.extend(collect_submodules(package))
    print(f"[spec] 已收集 {package}")


# ---------------------------------------------------------------------------
# 隐藏导入：动态导入的模块 PyInstaller 分析不到
# ---------------------------------------------------------------------------

hiddenimports: list[str] = [
    # --- uvicorn：循环/协议/生命周期实现全部是字符串动态导入 ---
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.http.httptools_impl",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
    # --- APScheduler：触发器与执行器按字符串解析 ---
    "apscheduler.triggers.interval",
    "apscheduler.triggers.cron",
    "apscheduler.triggers.date",
    "apscheduler.executors.pool",
    "apscheduler.jobstores.memory",
    # --- 标准库中被间接使用的解析器 ---
    "email.mime.text",
    "email.mime.multipart",
    "email.mime.base",
    "email.mime.image",
    "email.encoders",
    "html.parser",
    "sqlite3",
    # --- 本项目 ---
    "src",
]

# ---------------------------------------------------------------------------
# 排除项
# ---------------------------------------------------------------------------

excludes: list[str] = [
    # 导出工具链：只在导出 ONNX 模型时需要，体积 900MB+
    "torch",
    "torchvision",
    "torchaudio",
    "transformers",
    "optimum",
    "optimum_onnx",
    "sentence_transformers",
    "accelerate",
    "datasets",
    # 开发与测试
    "pytest",
    "pytest_asyncio",
    "_pytest",
    "IPython",
    "jupyter",
    "notebook",
    "matplotlib",
    "scipy",
    "pandas",
    "PIL.ImageQt",
    # 未使用的 GUI 框架
    "tkinter",
    "PyQt5",
    "PyQt6",
    "PySide2",
    "PySide6",
    "wx",
    # 明确用不到的重型科学计算栈
    # 注意：opentelemetry / grpc / kubernetes / boto3 **不能排除**——
    # chromadb/__init__.py 会导入 chromadb.auth.token_authn，
    # 而它依赖 opentelemetry，排除后整个 chromadb 都 import 不了。
    "duckdb",
    "pyarrow",
    "scipy",
    "pandas",
]

if sys.platform == "win32":
    excludes.extend(["fcntl", "pwd", "grp", "termios"])
else:
    excludes.extend(["win32com", "win32api", "win32con", "pythoncom", "pywintypes"])

# ---------------------------------------------------------------------------
# 收集可选但重要的原生库
# ---------------------------------------------------------------------------

for _pkg in ("onnxruntime", "tokenizers", "chromadb", "pydantic_core"):
    _collect(_pkg)

# Pillow / pystray / plyer 用于托盘。
# 用 find_spec 判断"是否安装"，而不是 __import__ 判断"能否导入"：
# 无显示环境下 `import pystray` 会抛 Xlib.error.DisplayNameError，
# 用它做判据会导致 CI/容器里构建出来的包**根本没有托盘**。
for _pkg in ("pystray", "PIL", "plyer"):
    if importlib.util.find_spec(_pkg) is None:
        print(f"[spec] 跳过 {_pkg}：未安装")
        continue
    _collect(_pkg, submodules=False)


# ---------------------------------------------------------------------------
# 构建
# ---------------------------------------------------------------------------

a = Analysis(  # noqa: F821
    [str(PROJECT_ROOT / "main.py")],
    pathex=[str(PROJECT_ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure, a.zipped_data)  # noqa: F821

# --- 主程序：保留控制台，CLI 子命令（sync / doctor / search）需要 ---
exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="email-assistant",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

# --- 托盘程序：Windows 下不弹控制台窗口，双击即常驻 ---
exe_tray = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="email-assistant-tray",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(  # noqa: F821
    exe,
    exe_tray,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="email-assistant",
)

# --- macOS：额外产出 .app 应用包 ---
if sys.platform == "darwin":
    app = BUNDLE(  # noqa: F821
        coll,
        name="邮件管理助手.app",
        icon=None,  # 有图标时填 str(PROJECT_ROOT / "packaging" / "icon.icns")
        bundle_identifier="com.emailassistant.desktop",
        info_plist={
            "CFBundleName": "邮件管理助手",
            "CFBundleDisplayName": "邮件管理助手",
            "CFBundleShortVersionString": "0.1.0",
            "CFBundleVersion": "0.1.0",
            "LSMinimumSystemVersion": "11.0",
            "NSHighResolutionCapable": True,
            # 托盘常驻程序：不在 Dock 中显示
            "LSUIElement": True,
            "NSHumanReadableCopyright": "腾讯企业邮箱邮件管理助手",
            # 说明为什么要访问网络（macOS 首次联网会提示）
            "NSAppleEventsUsageDescription": "用于打开归档的邮件文件与知识库文档页面。",
        },
    )
