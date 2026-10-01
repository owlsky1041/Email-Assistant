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
