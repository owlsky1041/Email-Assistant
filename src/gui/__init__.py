"""桌面 GUI（原生窗口）。

设计约束
--------
* 只用标准库 ``tkinter``，不引入任何新依赖 —— 打包器已经在打 tkinter，
  Windows / Linux 都能直接用，也不依赖 EdgeWebView2 之类的运行时。
* tkinter **必须跑在主线程**：本模块的窗口要么由 CLI 命令在主线程里直接
  运行，要么由调用方 fork 出独立子进程（见 ``settings_window.open_window_process``）。
  绝不能在 FastAPI / 托盘的后台线程里创建 Tk 窗口。
* 业务逻辑全部放在 :mod:`src.gui.settings_model`，与控件无关，可无头测试。
"""


def window_available() -> bool:
    """当前环境有没有图形界面。

    这是 GUI 的通用能力，两个窗口（主窗口 / 设置窗口）都要用，
    因此放在包入口，避免互相 import 造成循环依赖。
    只依赖 sys/os，不 import tkinter —— 无头环境的测试会导入本包。
    """
    import os
    import sys

    if sys.platform in ("win32", "darwin"):
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


#: 无控制台的孪生可执行文件（PyInstaller 分开打的两个 EXE）
WINDOWED_EXE_STEM = "email-assistant-tray"


def is_windowed_build() -> bool:
    """当前冻结产物是不是「无控制台」的 GUI 版本。

    源码运行恒为 ``False``（没有控制台问题需要处理）。
    """
    import sys
    from pathlib import Path

    from ..config import is_frozen

    if not is_frozen():
        return False
    return WINDOWED_EXE_STEM in Path(sys.executable).stem.lower()


def windowed_command(args: list[str]) -> list[str] | None:
    """把命令转交给同目录下「无控制台」的孪生程序。

    打包产物有两个 EXE：``email-assistant.exe``（控制台版，供命令行）
    和 ``email-assistant-tray.exe``（无控制台，供图形界面）。
    用户双击前者时会在桌面上弹一个黑终端 —— 图形界面应当由后者承担。

    :return: 可执行的命令；找不到孪生程序时返回 ``None``。
    """
    import sys
    from pathlib import Path

    from ..config import is_frozen

    if not is_frozen():
        return None
    exe = Path(sys.executable)
    suffix = exe.suffix or ".exe"
    twin = exe.with_name(f"{WINDOWED_EXE_STEM}{suffix}")
    if not twin.is_file():
        return None
    return [str(twin), *args]


def spawn_detached(command: list[str]) -> bool:
    """启动一个与当前进程解耦的子进程（不弹控制台、不随父进程退出）。"""
    import subprocess
    import sys

    kwargs: dict = {"close_fds": True}
    if sys.platform == "win32":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        flags |= getattr(subprocess, "DETACHED_PROCESS", 0)
        kwargs["creationflags"] = flags
        kwargs["stdin"] = subprocess.DEVNULL
        kwargs["stdout"] = subprocess.DEVNULL
        kwargs["stderr"] = subprocess.DEVNULL
    try:
        subprocess.Popen(command, **kwargs)
        return True
    except OSError:
        return False
