#!/usr/bin/env python3
"""标定向量相似度阈值。

为什么需要标定
--------------
纯向量检索永远会返回"最近的邻居"，即使它们毫不相关。
而不同嵌入模型的余弦分布差异很大——用一个全局魔数会在切换模型时
悄悄改变召回行为。

这个脚本用你自己的邮件语料，测量「相关查询」与「无关查询」的相似度分布，
给出建议的 ``search.min_vector_score``。

用法::

    python scripts/calibrate_threshold.py                       # 用内置样例
    python scripts/calibrate_threshold.py --queries my_queries.txt

``queries.txt`` 每行一个查询，用 ``TAB`` 分隔类别与文本::

    relevant<TAB>出差费用怎么报
    irrelevant<TAB>今天天气怎么样
"""

from __future__ import annotations

import argparse
import statistics
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config import load_config  # noqa: E402
from src.context import AppContext  # noqa: E402

DEMO_QUERIES = [
    # (类别, 查询) —— 相关查询应能命中你的邮件，无关查询不应该
    ("relevant", "出差费用怎么报"),
    ("relevant", "机器不够用了想加几台"),
    ("relevant", "公司被黑客攻击的风险"),
    ("relevant", "我今年表现怎么样"),
    ("relevant", "结算流程需要改"),
    ("irrelevant", "今天天气怎么样"),
    ("irrelevant", "如何做红烧肉"),
    ("irrelevant", "推荐几部电影"),
    ("irrelevant", "明天股市会涨吗"),
    ("irrelevant", "北京有哪些景点"),
    ("irrelevant", "怎么养猫"),
    ("irrelevant", "世界杯决赛比分"),
]


def load_queries(path: str | None) -> list[tuple[str, str]]:
    if not path:
        print("使用内置样例查询（建议改用你自己的邮件语料以获得准确结论）\n")
        return DEMO_QUERIES

    queries: list[tuple[str, str]] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        kind, _, text = line.partition("\t")
        kind = kind.strip().lower()
        text = text.strip()
        if kind in ("relevant", "irrelevant") and text:
            queries.append((kind, text))
    return queries


def main() -> int:
    parser = argparse.ArgumentParser(description="标定向量相似度阈值")
    parser.add_argument("--queries", help="查询文件（TAB 分隔 类别+文本）")
    parser.add_argument("--top-k", type=int, default=5, help="每次查询取前 K 个候选")
    args = parser.parse_args()

    config = load_config()
    context = AppContext(config)
    try:
        if context.db.count_chunks() == 0:
            print("错误：知识库为空。请先执行 `python main.py sync` 或 "
                  "`python scripts/seed_demo.py`。")
            return 1

        print(f"嵌入后端 : {context.embedder.name}（{context.embedder.dimension} 维）")
        print(f"向量库   : {context.vector_store.backend}")
        print(f"切片总数 : {context.db.count_chunks()}")
        print(f"当前阈值 : {config.search.min_vector_score or '自适应'}"
              f"（后端推荐 {context.embedder.recommended_min_score}）")
        print()

        queries = load_queries(args.queries)
        relevant_scores: list[float] = []
        irrelevant_scores: list[float] = []
        top1_relevant: list[float] = []
        top1_irrelevant: list[float] = []

        print(f"{'查询':<26}{'类别':<12}各候选余弦分数")
        print("-" * 92)
        for kind, text in queries:
            vector = context.embedder.embed_query(text)
            hits = context.vector_store.query(vector, top_k=args.top_k)
            scores = sorted((h.score for h in hits), reverse=True)
            if not scores:
                print(f"{text:<26}{kind:<12}(无候选)")
                continue
            (relevant_scores if kind == "relevant" else irrelevant_scores).extend(scores)
            (top1_relevant if kind == "relevant" else top1_irrelevant).append(scores[0])
            label = "相关" if kind == "relevant" else "无关"
            print(f"{text:<26}{label:<12}" + " ".join(f"{s:.3f}" for s in scores))

        print("-" * 92)
        if not top1_relevant or not top1_irrelevant:
            print("需要同时提供 relevant 与 irrelevant 查询才能给出建议。")
            return 1

        print(f"\n相关查询 top1：min={min(top1_relevant):.3f} "
              f"中位={statistics.median(top1_relevant):.3f} max={max(top1_relevant):.3f}")
        print(f"无关查询 top1：min={min(top1_irrelevant):.3f} "
              f"中位={statistics.median(top1_irrelevant):.3f} max={max(top1_irrelevant):.3f}")

        gap_low, gap_high = max(top1_irrelevant), min(top1_relevant)
        print()
        if gap_low < gap_high:
            suggested = (gap_low + gap_high) / 2
            print(f"✓ 两类查询存在分离区间 [{gap_low:.3f}, {gap_high:.3f}]")
            print(f"  建议 min_vector_score = {suggested:.2f}")
        else:
            suggested = statistics.median(top1_irrelevant + top1_relevant)
            print(f"✗ 两类查询的 top1 分布**存在重叠**"
                  f"（无关最高 {gap_low:.3f} > 相关最低 {gap_high:.3f}）")
            print("  单一绝对阈值无法完美区分。可选做法：")
            print(f"    1) 保留召回：min_vector_score = {max(0.0, min(top1_relevant) - 0.02):.2f}"
                  "（宁可多返回，由调用方按 vector_score 判断）")
            print(f"    2) 优先降噪：min_vector_score = {max(top1_irrelevant) + 0.02:.2f}"
                  "（会损失部分召回）")
            print("    3) 用 vector_score_margin 裁掉每次查询的长尾")

        print("\n把结论写入 config/config.yaml 的 search.min_vector_score 即可。")
        return 0
    finally:
        context.close()


if __name__ == "__main__":
    raise SystemExit(main())
