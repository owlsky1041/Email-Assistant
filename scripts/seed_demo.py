#!/usr/bin/env python3
"""生成演示数据（``python main.py demo`` 的便捷别名）。

真正的实现在 ``src/demo_data.py``，这样**打包后的应用**也能用
（安装包不包含 scripts 目录）。

用法::

    python scripts/seed_demo.py                 # 12 封
    python scripts/seed_demo.py --count 50
    python scripts/seed_demo.py --reset         # 先清空
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config import load_config  # noqa: E402
from src.context import AppContext  # noqa: E402
from src.demo_data import generate_demo_data  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="生成演示邮件数据")
    parser.add_argument("--count", type=int, default=12, help="生成数量")
    parser.add_argument("--config", help="配置文件路径")
    parser.add_argument("--reset", action="store_true", help="先清空已有数据")
    args = parser.parse_args()

    config = load_config(args.config)
    context = AppContext(config)
    try:
        result = generate_demo_data(context, count=args.count, reset=args.reset)
        print(f"已归档 {result['created']} 封演示邮件 → {result['archive']}")
        stats = result["index"]
        print(
            f"索引完成：{stats['messages']} 封 / {stats['chunks']} 切片 "
            f"（新嵌入 {stats['embedded']}，复用 {stats['reused']}，失败 {stats['failed']}）"
        )
        print()
        print("下一步：")
        print("  python main.py status")
        print('  python main.py search "报销发票怎么弄"')
        print("  python main.py serve")
        return 0
    finally:
        context.close()


if __name__ == "__main__":
    raise SystemExit(main())
