#!/usr/bin/env python3
"""真实桌面环境下的端到端验证（KDE Plasma / X11）。

Xvfb 里能验证"能不能构造"，但验证不了"图标是否真的出现、菜单是否真的渲染"——
那需要一个真正的系统托盘宿主。这个脚本在真实桌面会话里跑，逐项确认：

  1. 托盘图标能停靠（Xvfb 下会报 Failed to dock icon）
  2. 菜单能弹出，且中文标签渲染正常
  3. 设置界面能打开并渲染出各分组
  4. 目录选择框能弹出
  5. 气泡通知能发出
  6. xdg-open 能打开文件与目录

用法（在图形会话中）::

    DISPLAY=:0 python tools/verify_desktop.py --screenshot-dir /tmp/ea-shots
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    mark = "OK  " if ok else "FAIL"
    print(f"[{mark}] {name}" + (f" — {detail}" if detail else ""), flush=True)


def has_display() -> bool:
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


# ---------------------------------------------------------------------------
# 各项检查
# ---------------------------------------------------------------------------

def check_tray(shots: Path) -> None:
    """托盘图标必须能停靠。Xvfb（无托盘宿主）下会失败——这正是要区分的。"""
    try:
        import pystray

        from src.tray_app import build_icon_image, tray_title
    except Exception as exc:  # noqa: BLE001
        record("托盘：依赖可用", False, f"{type(exc).__name__}: {exc}")
        return

    record("托盘：依赖可用", True, "pystray + Pillow")

    docked = threading.Event()
    errors: list[str] = []

    def on_setup(icon):  # type: ignore[no-untyped-def]
        # pystray 在成功停靠后回调 setup
        docked.set()

    menu = pystray.Menu(
        pystray.MenuItem("立即同步", lambda *a: None, default=True),
        pystray.MenuItem("设置…", lambda *a: None),
        pystray.MenuItem("查看状态", lambda *a: None),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("退出", lambda *a: None),
    )
    try:
        icon = pystray.Icon(
            "email-assistant",
            icon=build_icon_image(64),
            title=tray_title(),
            menu=menu,
        )
    except Exception as exc:  # noqa: BLE001
        record("托盘：构造图标", False, f"{type(exc).__name__}: {exc}")
        return
    record("托盘：构造图标", True, f"标题 {tray_title()!r}")

    def run() -> None:
        try:
            icon.run(setup=on_setup)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{type(exc).__name__}: {exc}")

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    time.sleep(5)

    if errors:
        record("托盘：运行", False, errors[0])
    elif docked.is_set():
        record("托盘：成功停靠到系统托盘", True, "图标应已出现在面板上")
    else:
        record(
            "托盘：成功停靠到系统托盘",
            False,
            "未收到 setup 回调——通常是没有托盘宿主（Xvfb）或面板未运行",
        )

    if shots:
        shot = shots / "01-tray.png"
        if screenshot(shot):
            record("托盘：截图", True, str(shot))

    time.sleep(2)
    try:
        icon.stop()
    except Exception:  # noqa: BLE001
        pass


def check_settings_ui(shots: Path) -> None:
    """启动服务并确认设置页可访问、内容完整。"""
    import urllib.request

    from src.config import load_config
    from src.context import AppContext
    from src.kb_api import create_app
    import uvicorn

    config = load_config()
    context = AppContext(config, configure_logging=False)
    app = create_app(context)
    status = getattr(app.state, "settings_status", {})
    record("设置界面：已注册", bool(status.get("enabled")), status.get("reason", ""))

    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=config.api.port, log_level="warning")
    )
    threading.Thread(target=server.run, daemon=True).start()

    url = f"http://127.0.0.1:{config.api.port}/setup"
    html = ""
    for _ in range(40):
        time.sleep(0.25)
        try:
            with urllib.request.urlopen(url, timeout=3) as response:
                html = response.read().decode("utf-8")
            break
        except Exception:  # noqa: BLE001
            continue

    if not html:
        record("设置界面：可访问", False, url)
        server.should_exit = True
        context.close()
        return

    record("设置界面：可访问", True, f"{len(html)} 字节")
    groups = ["同步进度", "邮箱账户", "数据存放位置", "同步设置",
              "检索与语义搜索", "本地知识库服务", "通知"]
    missing = [g for g in groups if g not in html]
    record("设置界面：分组完整", not missing, f"缺少 {missing}" if missing else f"{len(groups)} 组")
    record("设置界面：无外部依赖", "http://cdn" not in html and "unpkg.com" not in html)

    # 用系统默认浏览器打开（真实桌面下应当弹出窗口）
    # 依次尝试常见打开方式；都没装时给出可操作的提示
    opened_with = ""
    for cmd in (["xdg-open", url], ["kde-open5", url], ["kde-open", url],
                ["gio", "open", url], ["firefox", url], ["konqueror", url],
                ["chromium", url]):
        try:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            opened_with = cmd[0]
            break
        except FileNotFoundError:
            continue
    record(
        "设置界面：可用系统浏览器打开",
        bool(opened_with),
        f"用 {opened_with} 打开 {url}" if opened_with
        else f"未找到可用的浏览器/xdg-open；请手工访问 {url}",
    )
    time.sleep(6)

    if shots:
        shot = shots / "02-settings.png"
        if screenshot(shot):
            record("设置界面：截图", True, str(shot))

    server.should_exit = True
    time.sleep(1)
    context.close()


def check_folder_picker(shots: Path) -> None:
    import tkinter
    from tkinter import filedialog

    from src.config import load_config
    from src.settings_service import SettingsService

    try:
        root = tkinter.Tk()
        root.withdraw()
    except Exception as exc:  # noqa: BLE001
        record("目录选择框：Tk 可用", False, f"{type(exc).__name__}: {exc}")
        return
    record("目录选择框：Tk 可用", True, f"Tk {tkinter.TkVersion}")

    result: dict[str, object] = {}

    def show() -> None:
        try:
            result["path"] = filedialog.askdirectory(title="选择数据存放目录")
        except Exception as exc:  # noqa: BLE001
            result["error"] = f"{type(exc).__name__}: {exc}"

    threading.Thread(target=show, daemon=True).start()
    time.sleep(3)

    # 必须按**窗口类名**判断：Xvfb 等没有 UTF-8 locale 的环境下，
    # 中文标题会转换失败（"failure in conversion from UTF8_STRING to ANSI_X3.4-1968"），
    # 按标题匹配会得到假阴性。
    found = _has_window_class("TkChooseDir")
    record("目录选择框：弹出", found, "按窗口类名 TkChooseDir 判定")
    if shots:
        shot = shots / "03-folder-picker.png"
        if screenshot(shot):
            record("目录选择框：截图", True, str(shot))

    # 关掉对话框：用 Esc 取消，而不是 windowkill ——
    # 后者会直接切断 X 连接，让被调方拿到 "X connection broken" 而非"已取消"。
    try:
        for wid in _search_windows_by_class("TkChooseDir"):
            subprocess.run(["xdotool", "key", "--window", wid, "Escape"], check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:  # noqa: BLE001
        pass
    try:
        root.destroy()
    except Exception:  # noqa: BLE001
        pass

    service = SettingsService(load_config())
    probe = service.pick_directory.__doc__ is not None
    record("目录选择框：服务层接口存在", probe)


def check_notification() -> None:
    """气泡通知。KDE 有通知守护进程，应当能收到。"""
    sent = False
    try:
        from src.tray_app import PLYER_AVAILABLE

        if PLYER_AVAILABLE:
            from plyer import notification

            notification.notify(
                title="邮件管理助手", message="这是一条测试通知", timeout=5
            )
            sent = True
    except Exception as exc:  # noqa: BLE001
        record("通知：plyer", False, f"{type(exc).__name__}: {exc}")
    if sent:
        record("通知：plyer 已发送", True, "屏幕上应出现气泡")

    try:
        subprocess.run(
            ["notify-send", "邮件管理助手", "notify-send 测试通知"],
            check=False, timeout=5,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        record("通知：notify-send", True, "KDE 下应弹出气泡")
    except FileNotFoundError:
        record("通知：notify-send", False, "未安装 libnotify-bin")


def check_open_path(tmp_path: Path) -> None:
    from src.tray_app import open_local_path

    target = tmp_path / "打开测试"
    target.mkdir(parents=True, exist_ok=True)
    (target / "示例.md").write_text("# 测试\n", encoding="utf-8")
    ok = open_local_path(target)
    record("打开本地目录：xdg-open", ok, str(target))
    ok_file = open_local_path(target / "示例.md")
    record("打开本地文件：xdg-open", ok_file, "应弹出默认文本编辑器或文件管理器")


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------

def screenshot(path: Path) -> bool:
    """整屏截图（优先 ImageMagick，其次 KDE 自带工具）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    commands = [
        ["import", "-window", "root", str(path)],
        ["spectacle", "-b", "-n", "-o", str(path)],
        ["scrot", str(path)],
        ["gnome-screenshot", "-f", str(path)],
    ]
    for cmd in commands:
        try:
            subprocess.run(cmd, check=True, timeout=25,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if path.is_file() and path.stat().st_size > 0:
                return True
        except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            continue
    return False


def _window_titles() -> list[str]:
    """列出当前所有窗口标题。

    不能用 ``--onlyvisible``：tkinter 的目录选择框是 override-redirect
    窗口，会被该选项过滤掉，导致"明明弹出来了却判定为未弹出"。
    """
    try:
        out = subprocess.run(
            ["xdotool", "search", "--name", "."],
            capture_output=True, text=True, timeout=10,
        ).stdout.split()
        titles = []
        for wid in out:
            name = subprocess.run(
                ["xdotool", "getwindowname", wid],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip()
            if name:
                titles.append(name)
        return titles
    except Exception:  # noqa: BLE001
        return []


def _has_window_class(class_name: str) -> bool:
    """按窗口类名判断窗口是否存在（比标题可靠）。"""
    try:
        out = subprocess.run(
            ["xwininfo", "-root", "-children"],
            capture_output=True, text=True, timeout=10,
        ).stdout
        return class_name in out
    except Exception:  # noqa: BLE001
        return False


def _search_windows_by_class(class_name: str) -> list[str]:
    try:
        return subprocess.run(
            ["xdotool", "search", "--class", class_name],
            capture_output=True, text=True, timeout=10,
        ).stdout.split()
    except Exception:  # noqa: BLE001
        return []


def main() -> int:
    parser = argparse.ArgumentParser(description="真实桌面环境下的端到端验证")
    parser.add_argument("--screenshot-dir", default="", help="截图输出目录")
    parser.add_argument("--skip-tray", action="store_true")
    args = parser.parse_args()

    shots = Path(args.screenshot_dir) if args.screenshot_dir else Path()

    print(f"DISPLAY={os.environ.get('DISPLAY')!r} "
          f"WAYLAND_DISPLAY={os.environ.get('WAYLAND_DISPLAY')!r}")
    if not has_display():
        print("!! 当前没有图形会话，本脚本需要在真实桌面里运行")
        return 2

    print("\n=== 会话信息 ===")
    for key in ("XDG_CURRENT_DESKTOP", "XDG_SESSION_TYPE", "DESKTOP_SESSION"):
        print(f"  {key}={os.environ.get(key)!r}")
    print(f"  窗口列表: {_window_titles()[:8]}")

    print("\n=== 逐项验证 ===")
    if not args.skip_tray:
        check_tray(shots)
    check_settings_ui(shots)
    check_folder_picker(shots)
    check_notification()
    check_open_path(Path("/tmp") / "ea-open-test")

    print("\n=== 汇总 ===")
    failed = [n for n, ok, _ in RESULTS if not ok]
    for name, ok, detail in RESULTS:
        print(f"  {'OK  ' if ok else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    print(f"\n通过 {len(RESULTS) - len(failed)}/{len(RESULTS)}")
    if failed:
        print("未通过：")
        for name in failed:
            print(f"  - {name}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
