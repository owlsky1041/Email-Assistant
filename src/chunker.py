"""正文切片（§3.4）。

策略：**语义单元贪心装箱**
1. 先按空行切成段落；
2. 超长段落再按句子切（中英文标点都识别）；
3. 句子仍超长时按字符硬切；
4. 依次装箱到 ``chunk_size`` token，相邻切片保留 ``chunk_overlap`` token 重叠。

相比「固定字符窗口」，这种方式不会把一句话劈成两半，检索片段可读性更好。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from datetime import datetime

from .models import Chunk
from .utils import estimate_tokens

logger = logging.getLogger(__name__)

# 段落分隔：一个及以上空行
_PARAGRAPH_SPLIT_RE = re.compile(r"\n\s*\n+")
# 句子边界：中文句末标点 / 英文句末标点+空白 / 换行
_SENTENCE_SPLIT_RE = re.compile(
    r"(?<=[。！？；!?;])\s*|(?<=[.!?])\s+|\n+"
)
# 没有任何标点的超长串（如 base64 残留）
_HARD_BREAK_CHARS = 200


def _split_paragraphs(text: str) -> list[str]:
    return [p.strip() for p in _PARAGRAPH_SPLIT_RE.split(text) if p.strip()]


def _split_sentences(paragraph: str) -> list[str]:
    parts = [s.strip() for s in _SENTENCE_SPLIT_RE.split(paragraph) if s and s.strip()]
    return parts or [paragraph]


def _hard_split(text: str, max_chars: int = _HARD_BREAK_CHARS) -> list[str]:
    """对完全没有标点的超长文本按字符窗口硬切。"""
    if len(text) <= max_chars:
        return [text]
    out: list[str] = []
    for i in range(0, len(text), max_chars):
        piece = text[i : i + max_chars].strip()
        if piece:
            out.append(piece)
    return out


def iter_units(text: str, *, max_unit_chars: int = _HARD_BREAK_CHARS) -> Iterator[str]:
    """把正文拆成原子语义单元（保证不会二次拆分）。"""
    for paragraph in _split_paragraphs(text):
        if len(paragraph) <= max_unit_chars:
            yield paragraph
            continue
        for sentence in _split_sentences(paragraph):
            if len(sentence) <= max_unit_chars:
                yield sentence
            else:
                yield from _hard_split(sentence, max_unit_chars)


class TextChunker:
    """把邮件正文切成带重叠的片段。"""

    def __init__(
        self,
        chunk_size: int = 400,
        chunk_overlap: int = 80,
        *,
        min_chunk_tokens: int = 16,
        max_chunk_multiplier: float = 1.6,
    ) -> None:
        if chunk_overlap >= chunk_size:
            raise ValueError("chunk_overlap 必须小于 chunk_size")
        self.chunk_size = max(50, int(chunk_size))
        self.chunk_overlap = max(0, int(chunk_overlap))
        self.min_chunk_tokens = max(1, int(min_chunk_tokens))
        # 单个语义单元允许超出 chunk_size 的上限倍数（避免超长表格被切碎）
        self.max_unit_tokens = int(self.chunk_size * max_chunk_multiplier)

    # ------------------------------------------------------------------

    def split_text(self, text: str) -> list[str]:
        """返回切好的文本片段列表。"""
        if not text or not text.strip():
            return []

        units: list[tuple[str, int]] = [(u, estimate_tokens(u)) for u in iter_units(text)]
        if not units:
            return []

        chunks: list[str] = []
        # current 保存 (单元文本, token 数)，避免反复重新估算
        current: list[tuple[str, int]] = []
        current_tokens = 0

        def overlap_tail() -> tuple[list[tuple[str, int]], int]:
            """从当前尾部回取重叠预算内的语义单元，作为下一块的前缀。"""
            if self.chunk_overlap <= 0:
                return [], 0
            tail: list[tuple[str, int]] = []
            total = 0
            for unit, tokens in reversed(current):
                if tail and total + tokens > self.chunk_overlap:
                    break
                tail.insert(0, (unit, tokens))
                total += tokens
            # 整块都在重叠预算内时至少丢弃第一个单元，否则会原地打转
            if len(tail) >= len(current) and len(tail) > 1:
                total -= tail[0][1]
                tail = tail[1:]
            return tail, total

        def flush() -> None:
            nonlocal current, current_tokens
            if not current:
                return
            joined = "\n\n".join(unit for unit, _ in current)
            # 只有与原子上一个切片不同才提交：重叠逻辑可能产生完全相同的块
            if not chunks or joined.strip() != chunks[-1].strip():
                chunks.append(joined)
            current, current_tokens = overlap_tail()

        for unit, tokens in units:
            # 单个单元远大于 chunk_size：单独成块，不与其他内容混合。
            # 注意这里必须无条件提交——硬切产生的片段内容可能完全相同，
            # 但它们对应正文中不同的位置，去重会丢数据。
            if tokens > self.max_unit_tokens and not current:
                chunks.append(unit)
                continue

            if current_tokens + tokens > self.chunk_size and current:
                flush()

            current.append((unit, tokens))
            current_tokens += tokens

        if current:
            tail = "\n\n".join(unit for unit, _ in current)
            if not chunks or tail.strip() != chunks[-1].strip():
                if estimate_tokens(tail) >= self.min_chunk_tokens or not chunks:
                    chunks.append(tail)

        # 过滤空白，并把过短碎片并入上一块（避免噪音切片）
        result: list[str] = []
        for chunk in chunks:
            normalized = chunk.strip()
            if not normalized:
                continue
            if (
                result
                and estimate_tokens(normalized) < self.min_chunk_tokens
                and normalized != result[-1].strip()
            ):
                result[-1] = f"{result[-1]}\n\n{normalized}"
                continue
            result.append(normalized)

        return result

    def split(
        self,
        text: str,
        *,
        message_pk: int | None = None,
        message_id: str = "",
        subject: str = "",
        sender: str = "",
        date: datetime | None = None,
        folder: str = "",
        local_markdown_path: str = "",
    ) -> list[Chunk]:
        """切片并附加完整元数据（§3.4）。"""
        pieces = self.split_text(text)
        chunks: list[Chunk] = []
        for index, piece in enumerate(pieces):
            chunk = Chunk(
                chunk_index=index,
                text=piece,
                token_count=estimate_tokens(piece),
                message_pk=message_pk,
                message_id=message_id,
                subject=subject,
                sender=sender,
                date_utc=date,
                folder=folder,
                local_markdown_path=local_markdown_path,
            )
            chunk.content_hash = chunk.compute_hash()
            chunks.append(chunk)
        return chunks
