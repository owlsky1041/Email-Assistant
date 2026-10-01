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
