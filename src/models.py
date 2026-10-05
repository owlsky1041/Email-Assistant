"""领域数据模型（纯数据结构，不依赖数据库与网络）。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

# ---------------------------------------------------------------------------
# 邮件
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class AttachmentMeta:
    """附件元数据。

    ``payload`` 只在真正需要落盘时携带，且必定已通过 ``max_attachment_size_mb``
    检查，避免把巨大附件读进内存（§11.2）。
    """

    filename: str
    content_type: str = "application/octet-stream"
    size_bytes: int = 0
    content_id: str | None = None
    is_inline: bool = False
    sha256: str | None = None
    local_path: str | None = None
    downloaded: bool = False
    skip_reason: str | None = None
    part_index: int = 0

    def to_row(self) -> dict[str, Any]:
        return {
            "filename": self.filename,
            "content_type": self.content_type,
            "size_bytes": self.size_bytes,
            "content_id": self.content_id,
            "is_inline": int(self.is_inline),
            "sha256": self.sha256,
            "local_path": self.local_path,
            "downloaded": int(self.downloaded),
            "skip_reason": self.skip_reason,
            "part_index": self.part_index,
        }


@dataclass(slots=True)
class ParsedMessage:
    """一封已解析但尚未归档的邮件。"""

    uid: str
    folder: str
    uidvalidity: int | None = None
    message_id: str = ""
    subject: str = ""
    sender: str = ""
    sender_name: str = ""
    recipients: str = ""
    cc: str = ""
    reply_to: str = ""
    date: datetime | None = None
    date_raw: str = ""
    text_plain: str = ""
    text_html: str = ""
    body_text: str = ""  # 清洗后的纯文本（用于 FTS / 切片）
    body_markdown: str = ""  # 清洗后的 Markdown（用于落盘）
    attachments: list[AttachmentMeta] = field(default_factory=list)
    #: ``{part_index: 原始字节}``，仅包含**未超过阈值**的附件。
    #: 超阈值附件只在 ``attachments`` 中留下元数据（§3.2 超大附件保护）。
    attachment_payloads: dict[int, bytes] = field(default_factory=dict)
    #: ``{content-id: 本地相对路径}``，用于把 HTML 中的 ``cid:`` 换成真实图片
    cid_map: dict[str, str] = field(default_factory=dict)
    size_bytes: int = 0
    in_reply_to: str = ""
    references: str = ""
    raw_headers: dict[str, str] = field(default_factory=dict)

    @property
    def has_attachments(self) -> bool:
        return any(not a.is_inline for a in self.attachments)

    @property
    def date_utc(self) -> datetime | None:
        if self.date is None:
            return None
        if self.date.tzinfo is None:
            return self.date.replace(tzinfo=timezone.utc)
        return self.date.astimezone(timezone.utc)

    def dedup_key(self) -> str:
        """去重键：优先 Message-ID，退化到 folder+uidvalidity+uid。"""
        if self.message_id:
            return f"mid:{self.message_id}"
        return f"uid:{self.folder}:{self.uidvalidity}:{self.uid}"


# ---------------------------------------------------------------------------
# 数据库行
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class MessageRecord:
    """``messages`` 表的一行。"""

    pk: int | None = None
    account: str = ""
    message_id: str = ""
    uid: str = ""
    uidvalidity: int | None = None
    folder: str = ""
    subject: str = ""
    sender: str = ""
    sender_name: str = ""
    recipients: str = ""
    cc: str = ""
    date_utc: datetime | None = None
    date_raw: str = ""
    local_markdown_path: str = ""
    body_text: str = ""
    has_attachments: bool = False
    size_bytes: int = 0
    content_hash: str = ""
    #: 同一封邮件在其它文件夹已归档时，指向已有记录的 ``messages.id``
    duplicate_of: int | None = None
    synced_at: datetime | None = None
    updated_at: datetime | None = None
    deleted_at: datetime | None = None

    @staticmethod
    def content_fingerprint(subject: str, body_text: str) -> str:
        h = hashlib.sha256()
        h.update((subject or "").encode("utf-8", "replace"))
        h.update(b"\x00")
        h.update((body_text or "").encode("utf-8", "replace"))
        return h.hexdigest()


@dataclass(slots=True)
class FolderState:
    """``folders`` 表的一行：每个文件夹的增量同步水位。"""

    id: int | None = None
    account: str = ""
    name: str = ""
    delimiter: str = "/"
    uidvalidity: int | None = None
    last_uid: int = 0
    last_sync_at: datetime | None = None
    last_full_scan_at: datetime | None = None
    message_count: int = 0
    selectable: bool = True


@dataclass(slots=True)
class Chunk:
    """一段知识库切片。"""

    chunk_index: int
    text: str
    token_count: int
    message_pk: int | None = None
    message_id: str = ""
    subject: str = ""
    sender: str = ""
    date_utc: datetime | None = None
    folder: str = ""
    local_markdown_path: str = ""
    content_hash: str = ""
    id: int | None = None

    def compute_hash(self) -> str:
        h = hashlib.sha256()
        h.update(f"{self.message_id}|{self.chunk_index}|".encode("utf-8", "replace"))
        h.update(self.text.encode("utf-8", "replace"))
        return h.hexdigest()


@dataclass(slots=True)
class SearchHit:
    """统一的检索结果结构（§3.7）。"""

    message_pk: int | None
    message_id: str
    subject: str
    sender: str
    date: str
    folder: str
    snippet: str
    local_markdown_path: str
    score: float
    chunk_id: int | None = None
    chunk_index: int | None = None
    source: Literal["keyword", "vector", "hybrid"] = "hybrid"
    keyword_rank: int | None = None
    vector_rank: int | None = None
    keyword_score: float | None = None
    vector_score: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "subject": self.subject,
            "sender": self.sender,
            "date": self.date,
            "folder": self.folder,
            "snippet": self.snippet,
            "local_markdown_path": self.local_markdown_path,
            "score": round(self.score, 6),
            "chunk_id": self.chunk_id,
            "chunk_index": self.chunk_index,
            "source": self.source,
            "keyword_rank": self.keyword_rank,
            "vector_rank": self.vector_rank,
            "keyword_score": (
                round(self.keyword_score, 6) if self.keyword_score is not None else None
            ),
            "vector_score": (
                round(self.vector_score, 6) if self.vector_score is not None else None
            ),
        }


@dataclass(slots=True)
class SyncResult:
    """一次同步运行的结果汇总。"""

    folder: str = ""
    fetched: int = 0
    archived: int = 0
    skipped: int = 0
    failed: int = 0
    deleted: int = 0
    #: 附件命中内容去重、用硬链接复用而非重复写盘的个数
    attachments_reused: int = 0
    #: 同步失败的文件夹名。与整轮失败（error_summary）区分开：
    #: 个别文件夹出问题不该让整轮显示成"失败"。
    failed_folders: list[str] = field(default_factory=list)
    new_message_ids: list[str] = field(default_factory=list)
    error_summary: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None

    def merge(self, other: "SyncResult") -> None:
        self.fetched += other.fetched
        self.archived += other.archived
        self.skipped += other.skipped
        self.failed += other.failed
        self.deleted += other.deleted
        self.attachments_reused += other.attachments_reused
        self.failed_folders.extend(other.failed_folders)
        self.new_message_ids.extend(other.new_message_ids)
        if other.error_summary:
            self.error_summary = (
                f"{self.error_summary}; {other.error_summary}"
                if self.error_summary
                else other.error_summary
            )

    @property
    def status(self) -> str:
        # 只有个别文件夹失败、本轮其余部分正常走完 → partial，不是 failed。
        # 否则"49 个文件夹里 1 个容器文件夹服务端不认"会被显示成整轮失败。
        if self.failed_folders and self.failed == 0:
            return "partial"
        if self.failed == 0 and not self.error_summary:
            return "success"
        if self.archived or self.skipped:
            return "partial"
        return "failed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "folder": self.folder,
            "status": self.status,
            "fetched": self.fetched,
            "archived": self.archived,
            "skipped": self.skipped,
            "failed": self.failed,
            "deleted": self.deleted,
            "attachments_reused": self.attachments_reused,
            "failed_folders": list(self.failed_folders),
            "new_messages": len(self.new_message_ids),
            "error_summary": self.error_summary,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
