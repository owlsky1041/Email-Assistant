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
import os
import shutil
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

from .blob_store import BlobStore
from .cleaner import compose_body
from .config import AppConfig
from .models import AttachmentMeta, ParsedMessage
from .utils import (
    ChunkedFileWriter,
    claim_path,
    link_or_copy_exclusive,
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

    def __init__(
        self,
        config: AppConfig,
        *,
        blob_lookup: "Callable[[str], Path | None] | None" = None,
        blob_store: "BlobStore | None" = None,
    ) -> None:
        self.config = config
        self.root = config.archive_path
        self.attachment_root = config.attachment_path
        self.sibling_layout = config.storage.attachment_layout == "sibling"
        self.per_account = config.storage.per_account_subdir
        self.file_mode = config.storage.file_mode
        #: ``sha256 -> 已落盘文件路径``。同一个文件（内联签名图、被反复转发的
        #: 技术附件）会在几十封邮件里重复出现，没必要把内容重复写几十遍。
        self._blob_lookup = blob_lookup
        #: 内容寻址仓库。有它时附件先写成 blob，再硬链接到人工目录，
        #: 这样同一份内容有唯一的权威位置，可校验、可重建。
        self.blob_store = blob_store
        #: 本次运行内已写出的内容索引。**不能只依赖数据库**：并发下载时
        #: 多个工作线程同时归档，A 的附件记录要等 A 整封入库后才可见，
        #: 并行的 B 查库必然查不到，去重就形同虚设。
        self._written: dict[str, Path] = {}
        self._written_lock = threading.Lock()
        #: 每个内容哈希一把锁，把"同一份内容的查重+落盘"串行化。
        self._locks: dict[str, threading.Lock] = {}

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

            wanted = target_dir / sanitize_filename(
                meta.filename, max_bytes=120, fallback=f"attachment_{meta.part_index}"
            )

            # 先落到内容寻址仓库：它才是权威副本，人工目录里的那份是硬链接。
            # 这样即使人工目录被误删，内容也还在，能一条命令重建。
            blob_path: Path | None = None
            if self.blob_store is not None:
                try:
                    blob_path = self.blob_store.put_bytes(payload, digest=digest)
                except (OSError, ValueError) as exc:
                    logger.warning("写入 blob 失败，改为直接落盘：%s", exc)
                    blob_path = None

            # 同一份内容**串行处理**。并发下载时两个工作线程几乎同时到达，
            # 都在对方写盘之前查重、双双落空，去重就白做了 —— 尤其是那些
            # 每封邮件都带一份的内联签名图。按内容哈希加锁只序列化真正
            # 重复的部分，不同附件之间仍然并行。
            with self._digest_lock(digest):
                reused_path = self._try_reuse(digest, wanted)
                if reused_path is not None:
                    meta.local_path = str(reused_path)
                    meta.downloaded = True
                    meta.skip_reason = "reused"
                    if meta.content_id:
                        cid_map[meta.content_id] = self._relative_to_base(
                            reused_path, base_dir
                        )
                    out.append(meta)
                    continue

                # 原子占位：并发两个线程拿到同名附件时，只有一个能占住原名，
                # 另一个自动退到 _1。仅靠 exists() 判断会让它们互相覆盖。
                try:
                    target = claim_path(wanted, mode=self.file_mode)
                except OSError as exc:
                    logger.error("附件占位失败 %s：%s", wanted.name, exc)
                    meta.downloaded = False
                    meta.skip_reason = f"占位失败：{exc}"
                    out.append(meta)
                    continue
                relative = self._relative_to_base(target, base_dir)

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
                    self._remember(digest, target)
                    if meta.content_id:
                        cid_map[meta.content_id] = relative
                except OSError as exc:
                    logger.error("附件写入失败 %s：%s", target.name, exc)
                    meta.downloaded = False
                    meta.skip_reason = f"写入失败：{exc}"

            out.append(meta)

        return out, cid_map

    def _digest_lock(self, digest: str) -> threading.Lock:
        """拿到某个内容哈希专属的锁。"""
        with self._written_lock:
            lock = self._locks.get(digest)
            if lock is None:
                lock = threading.Lock()
                self._locks[digest] = lock
            return lock

    @staticmethod
    def _relative_to_base(target: Path, base_dir: Path) -> str:
        """Markdown 里引用的相对路径。"""
        try:
            return target.relative_to(base_dir).as_posix()
        except ValueError:
            return target.as_posix()

    def _try_reuse(self, digest: str, wanted: Path) -> Path | None:
        """同内容已在别处落盘时，用**硬链接**代替重新写一遍。

        为什么值得做：附件占了归档体积的 99%，而且分布极不均衡 ——
        实测 53 封里前 5 个附件就占了 47%。内联签名图更是每封邮件都带一份，
        实测样本中已有 7% 的文件是重复内容。

        硬链接在 NTFS / ext4 上都无需管理员权限；跨卷等情况会失败，
        此时退化为复制。两条路径都不会回退成"重新下载"。

        :return: 实际落盘的路径；无法复用时返回 ``None``（调用方照常写盘）。
        """
        source = self._find_source(digest)
        if source is None:
            return None

        wanted.parent.mkdir(parents=True, exist_ok=True)
        placed = link_or_copy_exclusive(source, wanted)
        if placed is None:
            logger.debug("附件复用失败，改为重新写入：%s", wanted.name)
            return None
        logger.debug("附件复用：%s -> %s", placed.name, source.name)
        return placed

    def _find_source(self, digest: str) -> Path | None:
        """按优先级找一份可复用的同内容文件。

        顺序：blob 仓库（权威且稳定）→ 本次运行内写出的（并发下唯一可靠的
        来源）→ 查库（跨运行复用）。
        """
        if self.blob_store is not None and self.blob_store.has(digest):
            return self.blob_store.path_for(digest)

        with self._written_lock:
            known = self._written.get(digest)
        if known is not None and known.is_file():
            return known

        if self._blob_lookup is None:
            return None
        try:
            found = self._blob_lookup(digest)
        except Exception:  # noqa: BLE001 - 查库失败不该影响归档
            logger.debug("查询附件去重来源失败", exc_info=True)
            return None
        if found is None:
            return None
        path = Path(found)
        return path if path.is_file() else None

    def _remember(self, digest: str, path: Path) -> None:
        """登记刚写出的内容，供同一轮并发归档中的其它邮件复用。"""
        if not digest:
            return
        with self._written_lock:
            self._written.setdefault(digest, path)

    @property
    def reused_count(self) -> int:
        with self._written_lock:
            return len(self._written)

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
