"""切片测试（§3.4）：尺寸、重叠、元数据、边界情况。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.chunker import TextChunker, iter_units
from src.utils import estimate_tokens


def make_chunker(**kwargs) -> TextChunker:
    params = {"chunk_size": 100, "chunk_overlap": 25, "min_chunk_tokens": 5}
    params.update(kwargs)
    return TextChunker(**params)


class TestIterUnits:
    def test_splits_paragraphs(self) -> None:
        units = list(iter_units("第一段。\n\n第二段。"))
        assert units == ["第一段。", "第二段。"]

    def test_splits_long_paragraph_into_sentences(self) -> None:
        paragraph = "这是一个句子。" * 60  # 420 字，超过 200 字上限
        units = list(iter_units(paragraph))
        assert len(units) > 1
        assert all(len(u) <= 260 for u in units)

    def test_never_returns_empty_units(self) -> None:
        assert all(u.strip() for u in iter_units("甲\n\n\n\n乙"))


class TestTextChunker:
    def test_empty_input(self) -> None:
        chunker = make_chunker()
        assert chunker.split_text("") == []
        assert chunker.split_text("   \n\n  ") == []

    def test_short_text_single_chunk(self) -> None:
        chunker = make_chunker()
        assert len(chunker.split_text("很短的一段话。")) == 1

    def test_respects_chunk_size(self) -> None:
        chunker = make_chunker()
        text = "\n\n".join(f"第{i}段内容，包含一些用于测试的中文文字。" for i in range(40))
        chunks = chunker.split_text(text)
        assert len(chunks) > 1
        for chunk in chunks:
            # 允许单个语义单元超出上限，但不该大幅超标
            assert estimate_tokens(chunk) <= chunker.chunk_size * 2.5

    def test_overlap_between_adjacent_chunks(self) -> None:
        chunker = make_chunker()
        text = "\n\n".join(f"这是第{i}段的内容，用于测试切片与重叠逻辑。" for i in range(20))
        chunks = chunker.split_text(text)
        assert len(chunks) >= 3
        for previous, current in zip(chunks, chunks[1:]):
            prev_sentences = {s for s in previous.split("\n\n") if s}
            curr_sentences = {s for s in current.split("\n\n") if s}
            assert prev_sentences & curr_sentences, "相邻切片之间应存在重叠内容"

    def test_no_overlap_when_disabled(self) -> None:
        chunker = make_chunker(chunk_size=40, chunk_overlap=0, min_chunk_tokens=1)
        text = "\n\n".join(f"第{i}段内容文字。" for i in range(20))
        chunks = chunker.split_text(text)
        for previous, current in zip(chunks, chunks[1:]):
            assert not ({*previous.split("\n\n")} & {*current.split("\n\n")})

    def test_no_duplicate_chunks(self) -> None:
        chunker = make_chunker()
        text = "\n\n".join(f"唯一段落编号 {i}。" for i in range(30))
        chunks = chunker.split_text(text)
        normalized = ["".join(c.split()) for c in chunks]
        assert len(normalized) == len(set(normalized))

    def test_oversized_single_unit_emitted_alone(self) -> None:
        chunker = TextChunker(chunk_size=50, chunk_overlap=10, min_chunk_tokens=1)
        huge = "甲" * 500  # 无标点超长串
        chunks = chunker.split_text(huge)
        assert chunks
        assert "".join(chunks).replace("\n\n", "") == huge

    def test_rejects_invalid_overlap(self) -> None:
        with pytest.raises(ValueError):
            TextChunker(chunk_size=100, chunk_overlap=100)
        with pytest.raises(ValueError):
            TextChunker(chunk_size=100, chunk_overlap=150)

    def test_terminates_on_repetitive_text(self) -> None:
        """高重复度文本不能让重叠逻辑陷入死循环或无限膨胀。"""
        text = "重复内容。" * 500
        chunks = TextChunker(chunk_size=60, chunk_overlap=30).split_text(text)
        assert 0 < len(chunks) < 500

    def test_metadata_binding(self) -> None:
        """§3.4：每个切片必须携带完整元数据。"""
        chunker = TextChunker(chunk_size=60, chunk_overlap=10, min_chunk_tokens=1)
        text = "\n\n".join(f"段落 {i} 的正文内容。" for i in range(10))
        date = datetime(2024, 3, 1, 10, 0, tzinfo=timezone.utc)
        chunks = chunker.split(
            text,
            message_pk=42,
            message_id="<m@corp.com>",
            subject="季度报告",
            sender="alice@corp.com",
            date=date,
            folder="INBOX/财务",
            local_markdown_path="/archive/INBOX/report.md",
        )
        assert len(chunks) > 1
        for index, chunk in enumerate(chunks):
            assert chunk.chunk_index == index
            assert chunk.message_pk == 42
            assert chunk.message_id == "<m@corp.com>"
            assert chunk.subject == "季度报告"
            assert chunk.sender == "alice@corp.com"
            assert chunk.date_utc == date
            assert chunk.folder == "INBOX/财务"
            assert chunk.local_markdown_path == "/archive/INBOX/report.md"
            assert chunk.token_count > 0
            assert chunk.content_hash

    def test_content_hash_differs_by_index(self) -> None:
        chunker = TextChunker(chunk_size=30, chunk_overlap=0, min_chunk_tokens=1)
        chunks = chunker.split("相同文本" * 40, message_id="<m@x>")
        hashes = {c.content_hash for c in chunks}
        assert len(hashes) == len(chunks)

    def test_content_hash_stable_across_runs(self) -> None:
        chunker = TextChunker(chunk_size=60, chunk_overlap=10, min_chunk_tokens=1)
        text = "\n\n".join(f"内容 {i}" for i in range(20))
        first = chunker.split(text, message_id="<m@x>")
        second = chunker.split(text, message_id="<m@x>")
        assert [c.content_hash for c in first] == [c.content_hash for c in second]

    def test_chunk_size_within_recommended_range(self) -> None:
        """§3.4 建议 300-500 token。用推荐参数验证实际产出落在合理区间。"""
        chunker = TextChunker(chunk_size=400, chunk_overlap=80)
        paragraphs = [
            f"第{i}段：本段用于验证切片尺寸是否落在建议区间内，包含足够的中文文字。"
            for i in range(60)
        ]
        chunks = chunker.split_text("\n\n".join(paragraphs))
        tokens = [estimate_tokens(c) for c in chunks]
        assert all(t <= 500 for t in tokens), f"超出上限：{max(tokens)}"
        # 大多数切片应达到下限附近（末尾碎片除外）
        assert sum(1 for t in tokens if t >= 300) >= len(tokens) - 2
