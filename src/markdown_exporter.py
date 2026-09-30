"""邮件本地归档：Markdown 正文 + 附件（§3.2）。

目录结构
--------
::

    data/mail_archive/<account>/<文件夹层级>/20240101_093000_主题_12345.md
    data/mail_archive/<account>/<文件夹层级>/attachments/报表.xlsx

要点
----
* 文件名按 UTF-8 **字节数** 截断，避免中文标题撞 NTFS 255 字节上限；
* 附件先写 ``.tmp`` 再原子重命名，并校验大小与 SHA-256（§11.2）；
* 附件按内容哈希跨邮件去重，相同文件只存一份；
* 内联图片（``cid:``）落盘后回填真实相对路径，重新生成 Markdown（§11.3）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .cleaner import compose_body
from .config import AppConfig
from .models import AttachmentMeta, ParsedMessage
from .utils import (
    ChunkedFileWriter,
    atomic_write_text,
    dedupe_path,
    format_timestamp,
    sanitize_filename,
    sanitize_relative_path,
    sha256_bytes,
)

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ArchiveResult:
    markdown_path: Path
    attachments: list[AttachmentMeta] = field(default_factory=list)
    attachments_saved: int = 0
    attachments_reused: int = 0
    attachments_skipped: int = 0
    total_bytes: int = 0


class MarkdownExporter:
    """把 :class:`ParsedMessage` 落盘为 Markdown + 附件。"""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.root = config.archive_path
        self.attachment_root = config.attachment_path
        self.sibling_layout = config.storage.attachment_layout == "sibling"
        self.per_account = config.storage.per_account_subdir
        self.file_mode = config.storage.file_mode

    # ------------------------------------------------------------------
    # 路径计算
    # ------------------------------------------------------------------

    def account_root(self, account: str) -> Path:
        if not self.per_account:
            return self.root
        return self.root / sanitize_filename(account, max_bytes=80, fallback="default")

    def folder_dir(self, folder: str, account: str = "") -> Path:
        """把 IMAP 文件夹名映射为本地目录（保持原始层级结构）。"""
        parts = [p for p in str(folder or "INBOX").split("/") if p]
        relative = sanitize_relative_path(parts)
        return self.account_root(account) / relative

    def attachment_dir_for(self, folder: str, account: str = "") -> Path:
        if self.sibling_layout:
            return self.folder_dir(folder, account) / "attachments"
        return (
            self.attachment_root
            / sanitize_filename(account or "default", max_bytes=80, fallback="default")
            / sanitize_relative_path([p for p in str(folder or "INBOX").split("/") if p])
        )

    def build_filename(self, message: ParsedMessage, *, include_uid: bool = True) -> str:
        stamp = format_timestamp(message.date)
        subject = sanitize_filename(message.subject, max_bytes=100, fallback="无主题")
        suffix = f"_{sanitize_filename(message.uid, max_bytes=20, fallback='0')}" if include_uid else ""
        return f"{stamp}_{subject}{suffix}.md"

    def markdown_path_for(
        self, message: ParsedMessage, *, account: str = "", include_uid: bool = True
    ) -> Path:
        return self.folder_dir(message.folder, account) / self.build_filename(
            message, include_uid=include_uid
        )

    # ------------------------------------------------------------------
    # 导出
    # ------------------------------------------------------------------

    def export(
        self,
        message: ParsedMessage,
        *,
        account: str = "",
        overwrite: bool = False,
    ) -> ArchiveResult:
        """落盘一封邮件，返回归档结果。"""
        target_dir = self.folder_dir(message.folder, account)
        target_dir.mkdir(parents=True, exist_ok=True)

        # 1) 先写附件，拿到最终相对路径
        saved, cid_map = self._save_attachments(message, account=account)

        # 2) 用真实附件路径重新生成 Markdown（内联图片引用需要回填）
        markdown, plain = self._build_body(message, cid_map)

        # 3) 组装 frontmatter 并原子写入
        md_path = self.markdown_path_for(message, account=account)
        if md_path.exists() and not overwrite:
            md_path = dedupe_path(md_path)

        frontmatter = self._build_frontmatter(message, md_path, saved, account=account)
        document = self._compose_document(frontmatter, markdown)

        atomic_write_text(md_path, document, mode=self.file_mode)

        result = ArchiveResult(markdown_path=md_path, attachments=saved)
        result.attachments_saved = sum(1 for a in saved if a.downloaded)
        result.attachments_reused = sum(
            1 for a in saved if a.downloaded and a.skip_reason == "reused"
        )
        result.attachments_skipped = sum(1 for a in saved if not a.downloaded)
        result.total_bytes = len(document.encode("utf-8")) + sum(
            a.size_bytes for a in saved if a.downloaded
        )
        return result

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------

    def _build_body(
        self, message: ParsedMessage, cid_map: dict[str, str]
    ) -> tuple[str, str]:
        if not cid_map:
            return message.body_markdown, message.body_text
        # 只有存在内联图片时才需要重新转换
        markdown, plain = compose_body(message.text_plain, message.text_html, cid_map)
        if not markdown.strip():
            return message.body_markdown, message.body_text
        return markdown, plain

    def _save_attachments(
        self, message: ParsedMessage, *, account: str
    ) -> tuple[list[AttachmentMeta], dict[str, str]]:
        """保存附件，返回 ``(附件元数据, cid -> 相对路径)``。"""
        out: list[AttachmentMeta] = []
        cid_map: dict[str, str] = {}
        if not message.attachments:
            return out, cid_map

        target_dir = self.attachment_dir_for(message.folder, account)
        base_dir = self.folder_dir(message.folder, account)

        for meta in message.attachments:
            payload = message.attachment_payloads.get(meta.part_index)

            if payload is None:
                # 未下载（超大 / 配置跳过 / 空内容）
                out.append(meta)
                continue

            digest = sha256_bytes(payload)
            meta.sha256 = digest

            target = target_dir / sanitize_filename(
                meta.filename, max_bytes=120, fallback=f"attachment_{meta.part_index}"
            )
            target = dedupe_path(target)

            try:
                with ChunkedFileWriter(
                    target, expected_size=len(payload), mode=self.file_mode
                ) as writer:
                    # 分块写入，避免一次性构造大缓冲区（§11.2）
                    view = memoryview(payload)
                    step = writer.chunk_size
                    for offset in range(0, len(view), step):
                        writer.write(bytes(view[offset : offset + step]))

                meta.local_path = str(target)
                meta.downloaded = True

                # 计算相对 Markdown 文件的引用路径
                try:
                    relative = target.relative_to(base_dir).as_posix()
                except ValueError:
                    relative = target.as_posix()
                if meta.content_id:
                    cid_map[meta.content_id] = relative
            except OSError as exc:
                logger.error("附件写入失败 %s：%s", target.name, exc)
                meta.downloaded = False
                meta.skip_reason = f"写入失败：{exc}"

            out.append(meta)

        return out, cid_map

    def _build_frontmatter(
        self,
        message: ParsedMessage,
        md_path: Path,
        attachments: list[AttachmentMeta],
        *,
        account: str,
    ) -> dict[str, Any]:
        attachment_names = [a.filename for a in attachments]
        data: dict[str, Any] = {
            "message_id": message.message_id,
            "subject": message.subject,
            "from": f"{message.sender_name} <{message.sender}>".strip()
            if message.sender_name
            else message.sender,
            "to": message.recipients,
            "cc": message.cc,
            "date": message.date.isoformat() if message.date else "",
            "folder": message.folder,
            "uid": message.uid,
            "attachments": attachment_names,
            "local_path": md_path.as_posix(),
        }
        # 扩展字段（便于增量索引与排障）
        data.update(
            {
                "account": account,
                "uidvalidity": message.uidvalidity or 0,
                "in_reply_to": message.in_reply_to,
                "references": message.references,
                "size_bytes": message.size_bytes,
                "has_attachments": message.has_attachments,
                "attachment_count": len(attachment_names),
                "archived_at": _now_iso(),
            }
        )
        return data

    @staticmethod
    def _compose_document(frontmatter: dict[str, Any], markdown: str) -> str:
        yaml_block = yaml.safe_dump(
            frontmatter,
            allow_unicode=True,
            default_flow_style=False,
            sort_keys=False,
            width=1000,
        ).strip()
        body = markdown.strip() or "*(此邮件无正文内容)*"
        return f"---\n{yaml_block}\n---\n\n# {frontmatter.get('subject') or '(无主题)'}\n\n{body}\n"

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------

    @staticmethod
    def read_markdown(path: str | Path) -> tuple[dict[str, Any], str]:
        """读取归档文件，返回 ``(frontmatter, 正文)``。"""
        p = Path(path)
        text = p.read_text(encoding="utf-8", errors="replace")
        if not text.startswith("---"):
            return {}, text
        parts = text.split("---", 2)
        if len(parts) < 3:
            return {}, text
        try:
            meta = yaml.safe_load(parts[1]) or {}
        except yaml.YAMLError:
            meta = {}
        body = parts[2].lstrip("\n")
        if body.startswith("# "):
            body = body.split("\n", 1)[1].lstrip("\n") if "\n" in body else ""
        return (meta if isinstance(meta, dict) else {}), body


def _now_iso() -> str:
    from .models import utcnow

    return utcnow().isoformat(timespec="seconds")
