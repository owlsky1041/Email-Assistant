"""主窗口的**纯逻辑层**：状态面板格式化、检索、邮件详情组装。

与 :mod:`src.gui.settings_model` 一样，这一层刻意不依赖 ``tkinter``，
因此可以在无显示器环境里完整测试。窗口只负责把这里的结果画出来。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: 正文预览上限：界面上是个 Text 控件，塞几十万字符会卡住
MAX_BODY_CHARS = 200_000


def format_duration(seconds: float | None) -> str:
    """把秒数格式化成 ``mm:ss`` / ``h:mm:ss``。"""
    if seconds is None or seconds < 0:
        return "—"
    total = int(round(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def format_size(num_bytes: int | None) -> str:
    if not num_bytes:
        return "0 B"
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


PHASE_LABELS: dict[str, str] = {
    "idle": "空闲",
    "connecting": "正在连接",
    "scanning": "扫描文件夹",
    "fetching": "正在同步邮件",
    "indexing": "正在建立索引",
    "done": "已完成",
    "partial": "部分完成（个别文件夹失败）",
    "error": "出错",
}


@dataclass
class DashboardView:
    """状态面板要显示的全部内容。"""

    #: 语料规模
    messages: int = 0
    chunks: int = 0
    vectors: int = 0
    pending_index: int = 0

    running: bool = False
    phase: str = "idle"
    phase_label: str = "空闲"
    current_folder: str = ""
    folders_progress: str = ""       # 例 "INBOX（2/5）"
    percent: float = 0.0
    processed: int = 0
    total: int = 0
    #: 当前/最近一封邮件（就是"正在同步的邮件状态"）
    current_message: str = ""
    rate_text: str = ""
    eta_text: str = ""
    elapsed_text: str = ""
    workers: int = 1
    last_error: str = ""

    #: 结果/跳过/失败，一行摘要
    counters_text: str = ""

    @classmethod
    def from_snapshot(
        cls,
        snapshot: dict[str, Any],
        *,
        messages: int = 0,
        chunks: int = 0,
        vectors: int = 0,
        pending_index: int = 0,
    ) -> "DashboardView":
        phase = str(snapshot.get("phase") or "idle")
        running = bool(snapshot.get("running"))

        folder = str(snapshot.get("current_folder") or "")
        folders_total = int(snapshot.get("folders_total") or 0)
        folders_done = int(snapshot.get("folders_done") or 0)
        if folder and folders_total:
            folders_progress = f"{folder}（{folders_done + 1}/{folders_total}）"
        elif folder:
            folders_progress = folder
        else:
            folders_progress = ""

        total = int(snapshot.get("folder_messages_total") or 0)
        done = int(snapshot.get("folder_messages_done") or 0)

        view = cls(
            messages=messages,
            chunks=chunks,
            vectors=vectors,
            pending_index=pending_index,
            running=running,
            phase=phase,
            phase_label=PHASE_LABELS.get(phase, phase),
            current_folder=folder,
            folders_progress=folders_progress,
            percent=float(snapshot.get("percent") or 0.0),
            processed=done,
            total=total,
            workers=int(snapshot.get("workers") or 1),
            last_error=str(snapshot.get("last_error") or ""),
        )
        view.current_message = _latest_message_line(snapshot)
        view.counters_text = (
            f"归档 {snapshot.get('archived', 0)} / 跳过 {snapshot.get('skipped', 0)} "
            f"/ 失败 {snapshot.get('failed', 0)} / 删除 {snapshot.get('deleted', 0)}"
        )
        rate = float(snapshot.get("rate_per_second") or 0.0)
        view.rate_text = f"{rate:.1f} 封/秒" if rate else "—"
        view.eta_text = format_duration(snapshot.get("eta_seconds"))
        view.elapsed_text = format_duration(snapshot.get("elapsed_seconds"))
        return view


def _latest_message_line(snapshot: dict[str, Any]) -> str:
    """从事件流里取最近一条"邮件级"状态。

    SyncService 只在归档/失败时上报，没有"开始拉取某封"的事件，
    所以这里展示的是**最近处理完的一封**，而不是"正在下载的那一封" ——
    界面文案按这个事实写，不假装。
    """
    for event in reversed(snapshot.get("events") or []):
        level = event.get("level")
        if level in ("item", "error"):
            text = str(event.get("message") or "")
            if text:
                return text
    return ""


# ----------------------------------------------------------------------
# 检索
# ----------------------------------------------------------------------


@dataclass
class SearchRow:
    """结果表格里的一行。"""

    message_id: str
    subject: str
    sender: str
    date: str
    folder: str
    score: float
    snippet: str
    source: str

    def to_tree_values(self) -> tuple[str, str, str, str]:
        return (
            self.subject or "(无主题)",
            self.sender or "",
            (self.date or "")[:19].replace("T", " "),
            f"{self.score:.4f}",
        )


def run_search(
    context: Any,
    query: str,
    *,
    limit: int = 30,
    snippet_length: int = 200,
) -> list[SearchRow]:
    """执行检索并转成表格行。空查询直接返回空列表。"""
    text = (query or "").strip()
    if not text:
        return []
    hits = context.search.search(text, limit=limit, snippet_length=snippet_length)
    return [
        SearchRow(
            message_id=hit.message_id,
            subject=hit.subject,
            sender=hit.sender,
            date=hit.date,
            folder=hit.folder,
            score=hit.score,
            snippet=hit.snippet,
            source=hit.source,
        )
        for hit in hits
    ]


# ----------------------------------------------------------------------
# 邮件详情
# ----------------------------------------------------------------------


@dataclass
class AttachmentRow:
    attachment_id: int
    filename: str
    size_bytes: int
    content_type: str
    local_path: str
    exists: bool

    def to_tree_values(self) -> tuple[str, str, str]:
        return (self.filename, format_size(self.size_bytes), "✓" if self.exists else "缺失")


@dataclass
class MessageDetail:
    found: bool = False
    error: str = ""
    pk: int | None = None
    message_id: str = ""
    subject: str = ""
    sender: str = ""
    recipients: str = ""
    cc: str = ""
    date: str = ""
    folder: str = ""
    markdown_path: str = ""
    body: str = ""
    body_truncated: bool = False
    attachments: list[AttachmentRow] = field(default_factory=list)
    has_attachments: bool = False

    def header_lines(self) -> list[tuple[str, str]]:
        rows = [
            ("主题", self.subject or "(无主题)"),
            ("发件人", self.sender or ""),
            ("收件人", self.recipients or ""),
            ("抄送", self.cc or ""),
            ("时间", (self.date or "").replace("T", " ")[:19]),
            ("文件夹", self.folder or ""),
        ]
        return [(k, v) for k, v in rows if v]


def load_detail(context: Any, message_id: str) -> MessageDetail:
    """按 Message-ID 取正文与附件。

    正文优先用数据库里的 ``body_text``（检索用的就是它，和结果片段一致）；
    数据库里没有时再回落到磁盘上的 ``.md``。
    """
    detail = MessageDetail(message_id=message_id)
    if not message_id:
        detail.error = "缺少 Message-ID"
        return detail

    record = context.db.find_message_by_message_id(message_id)
    if record is None:
        record = context.db.find_message_by_message_id(message_id.strip("<>"))
    if record is None:
        detail.error = f"归档中没有这封邮件：{message_id}"
        return detail

    detail.found = True
    detail.pk = record.pk
    detail.subject = record.subject
    detail.sender = f"{record.sender_name} <{record.sender}>".strip() if record.sender_name else record.sender
    detail.recipients = record.recipients
    detail.cc = record.cc
    detail.date = record.date_raw or (record.date_utc.isoformat() if record.date_utc else "")
    detail.folder = record.folder
    detail.markdown_path = record.local_markdown_path
    detail.has_attachments = record.has_attachments

    body = record.body_text or ""
    if not body.strip() and record.local_markdown_path:
        body = _read_markdown_body(Path(record.local_markdown_path))
    if len(body) > MAX_BODY_CHARS:
        body = body[:MAX_BODY_CHARS]
        detail.body_truncated = True
    detail.body = body

    if record.pk is not None:
        for row in context.db.list_attachments(record.pk):
            path = row["local_path"] or ""
            detail.attachments.append(
                AttachmentRow(
                    attachment_id=int(row["id"]),
                    filename=row["filename"] or "(未命名)",
                    size_bytes=int(row["size_bytes"] or 0),
                    content_type=row["content_type"] or "",
                    local_path=path,
                    exists=bool(path) and Path(path).is_file(),
                )
            )
    return detail


def _read_markdown_body(path: Path) -> str:
    """从归档的 .md 里剥掉 YAML frontmatter，取正文。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.debug("读取归档正文失败 %s：%s", path, exc)
        return ""
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            return parts[2].strip()
    return text.strip()


def corpus_stats(context: Any) -> dict[str, int]:
    """状态面板顶部的语料规模。"""
    stats = {"messages": 0, "chunks": 0, "vectors": 0, "pending_index": 0}
    try:
        stats["messages"] = context.db.count_messages()
    except Exception:  # noqa: BLE001 - 统计失败不该拦住界面
        logger.debug("统计邮件数失败", exc_info=True)
    try:
        health = context.indexer.health()
        stats["chunks"] = int(health.get("chunks") or 0)
        stats["vectors"] = int(health.get("vectors") or 0)
    except Exception:  # noqa: BLE001
        logger.debug("统计索引规模失败", exc_info=True)
    try:
        stats["pending_index"] = context.indexer.count_pending()
    except Exception:  # noqa: BLE001
        logger.debug("统计待索引失败", exc_info=True)
    return stats
