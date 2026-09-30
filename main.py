#!/usr/bin/env python3
"""腾讯企业邮箱邮件管理助手 —— 程序入口。

用法::

    python main.py init                # 生成配置并进入配置向导
    python main.py auth set            # 安全写入授权码
    python main.py doctor              # 环境自检
    python main.py sync                # 手动同步
    python main.py serve               # 启动知识库 API（127.0.0.1:8990）
    python main.py tray                # 托盘常驻 + 定时同步

直接运行 ``python main.py`` 且不带子命令时，等价于 ``tray``
（若无法使用图形界面则自动降级为守护模式）。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 允许从任意工作目录直接执行 main.py
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def main() -> int:
    from src.cli import main as cli_main

    argv = sys.argv[1:]
    if not argv:
        argv = ["tray"]
    return cli_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
