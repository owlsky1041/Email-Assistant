"""同步编排：IMAP -> 解析 -> 归档 -> 入库 -> 索引（§3.1 / §3.2）。

流程
----
1. 列出文件夹（保持服务端层级结构）；
2. 按文件夹做**增量**检索：``UID (last_uid+1):*``；
3. 先用 ``RFC822.SIZE`` + ``BODYSTRUCTURE`` 预检，超大附件不下载（§11.2）；
4. 解析 -> 落盘 Markdown + 附件 -> 写 SQLite；
5. 增量推进水位 ``folders.last_uid``，并记录 ``sync_log``；
6. 每 ``full_scan_interval_hours`` 小时做一次**全量 UID 比对**，
   处理网页端删除 / 移动的邮件（§11.2）；
7. 最后统一对新增邮件做切片与向量索引，附带增量嵌入复用。
"""

from __future__ import annotations

import email
import logging
import threading
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from email.message import Message
from pathlib import Path
from typing import Any

from .cancellation import CancellationToken, CancelledError, get_cancellation_token
from .cleaner import CleanPolicy
from .config import AppConfig
from .database import Database
from .imap_client import ImapClient, ImapError, MessageFetchPlan, PartInfo
from .indexer import IndexService, IndexStats
from .mail_parser import MailParser, decode_payload_text
from .markdown_exporter import ArchiveResult, MarkdownExporter
from .models import (
    AttachmentMeta,
    FolderState,
    MessageRecord,
    ParsedMessage,
    SyncResult,
    utcnow,
)
from .utils import sanitize_filename

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[str, dict[str, Any]], None]


class SyncService:
    """邮件同步总控。"""

    def __init__(
        self,
        config: AppConfig,
        database: Database,
        *,
        index_service: IndexService | None = None,
        index_service_factory: Callable[[], IndexService] | None = None,
        cancel_token: CancellationToken | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> None:
        self.config = config
        self.db = database
        # 允许惰性创建：加载嵌入模型代价高，而 status / --no-index 并不需要
        self._index_service = index_service
        self._index_service_factory = index_service_factory
        self.cancel = cancel_token or get_cancellation_token()
        self.on_progress = on_progress
        self.account = config.email.address
        self.exporter = MarkdownExporter(config)
        self.parser = MailParser(
            max_attachment_bytes=int(config.sync.max_attachment_size_mb * 1024 * 1024),
            download_attachments=config.sync.download_attachments,
            max_body_bytes=config.sync.max_body_index_size_kb * 1024,
            policy=CleanPolicy(
                strip_signature=config.clean.strip_signature,
                strip_quoted_history=config.clean.strip_quoted_history,
                strip_legal_disclaimer=config.clean.strip_legal_disclaimer,
                noise_tail_ratio=config.clean.noise_tail_ratio,
            ),
        )
        self._last_result: SyncResult | None = None
        self._running = False
        self._index_stats = IndexStats()

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------

    @property
    def index_service(self) -> IndexService | None:
        """按需实例化索引服务（可能触发嵌入模型加载）。"""
        if self._index_service is None and self._index_service_factory is not None:
            self._index_service = self._index_service_factory()
        return self._index_service

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def last_result(self) -> SyncResult | None:
        return self._last_result

    def status(self) -> dict[str, Any]:
        db_last = self.db.last_sync_summary()
        folders = self.db.list_folders(self.account)
        return {
            "account": _mask(self.account),
            "running": self._running,
            "total_messages": self.db.count_messages(),
            "chunks": self.db.count_chunks(),
            # 直接查库统计，避免为了一个数字而加载嵌入模型
            "pending_index": self.db.count_pending_index(),
            "folders": [
                {
                    "name": f.name,
                    "uidvalidity": f.uidvalidity,
                    "last_uid": f.last_uid,
                    "last_sync_at": f.last_sync_at.isoformat() if f.last_sync_at else None,
                    "last_full_scan_at": (
                        f.last_full_scan_at.isoformat() if f.last_full_scan_at else None
                    ),
                    "message_count": f.message_count,
                }
                for f in folders
            ],
            "last_sync": db_last,
            "last_result": self._last_result.to_dict() if self._last_result else None,
            "last_index": self._index_stats.to_dict(),
        }

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    def sync_all(
        self,
        *,
        folders: Sequence[str] | None = None,
        full: bool = False,
        index: bool = True,
    ) -> SyncResult:
        """同步全部（或指定）文件夹。"""
        if self._running:
            logger.warning("已有同步任务在执行，忽略本次请求")
            return SyncResult(error_summary="同步已在进行中")
        self._running = True
        overall = SyncResult(folder="*", started_at=utcnow())
        workers = max(1, int(self.config.sync.fetch_workers or 1))
        self._emit("run_start", {"workers": workers})
        try:
            with ImapClient(self.config, self._auth_code(), cancel_token=self.cancel) as client:
                targets = self._resolve_folders(client, folders)
                if not targets:
                    overall.error_summary = "没有可同步的文件夹"
                    return overall

                total_limit = self.config.sync.max_messages_per_run
                consumed = 0

                for index_no, folder in enumerate(targets, start=1):
                    self.cancel.raise_if_cancelled()

                    # max_messages_per_run 是**全局**预算，跨文件夹共享
                    budget = 0
                    if total_limit:
                        budget = total_limit - consumed
                        if budget <= 0:
                            logger.info(
                                "已达到单次同步全局上限 %d 封，剩余 %d 个文件夹留待下次",
                                total_limit,
                                len(targets) - index_no + 1,
                            )
                            break

                    self._emit(
                        "folder_start",
                        {"folder": folder, "index": index_no, "total": len(targets)},
                    )
                    result = self.sync_folder(
                        client,
                        folder,
                        full=full,
                        index=index,
                        force_reconcile=full,
                        max_messages=budget,
                    )
                    consumed += result.archived + result.failed
                    overall.merge(result)
                    self._emit("folder_done", result.to_dict())

            if index and self.index_service is not None:
                self._emit("index_start", {"pending": self.index_service.count_pending()})
                stats = self.index_service.index_pending()
                self._index_stats = stats
                self._emit("index_done", stats.to_dict())

            return overall
        except CancelledError:
            logger.info("同步被取消，正在安全收尾")
            overall.error_summary = "同步被取消"
            return overall
        except ImapError as exc:
            logger.error("同步失败：%s", exc)
            overall.error_summary = str(exc)
            return overall
        except Exception as exc:  # noqa: BLE001
            logger.exception("同步出现未预期错误")
            overall.error_summary = f"{type(exc).__name__}: {exc}"
            return overall
        finally:
            overall.finished_at = utcnow()
            self._last_result = overall
            self._running = False
            self._emit("run_done", overall.to_dict())
            self.db.optimize()

    def sync_folder(
        self,
        client: ImapClient,
        folder: str,
        *,
        full: bool = False,
        index: bool = True,
        force_reconcile: bool = False,
        max_messages: int = 0,
    ) -> SyncResult:
        """同步单个文件夹。

        :param index: 是否在本文件夹同步后立即建索引
        :param force_reconcile: 强制做全量 UID 比对（``--full`` 时启用）
        :param max_messages: 本轮允许处理的封数上限，0 表示用配置值
        """
        result = SyncResult(folder=folder, started_at=utcnow())
        log_id = self.db.start_sync_log(self.account, folder)
        try:
            status = client.select_folder(folder)
            uidvalidity = status.get("UIDVALIDITY", 0)
            state = self.db.get_folder(self.account, folder) or FolderState(
                account=self.account, name=folder
            )

            # UIDVALIDITY 变化 = 服务端重建了 UID 空间，必须全量重来
            if state.uidvalidity and uidvalidity and state.uidvalidity != uidvalidity:
                logger.warning(
                    "文件夹 %s 的 UIDVALIDITY 由 %s 变为 %s，将执行全量同步",
                    folder,
                    state.uidvalidity,
                    uidvalidity,
                )
                state.last_uid = 0
                state.uidvalidity = uidvalidity
                self.db.reset_folder_uidvalidity(self.account, folder, uidvalidity)

            self.db.upsert_folder(
                FolderState(
                    account=self.account,
                    name=folder,
                    delimiter="/",
                    uidvalidity=uidvalidity,
                    last_uid=state.last_uid,
                    message_count=status.get("MESSAGES", 0),
                )
            )

            # ---- 检索目标 UID ----
            if full or state.last_uid <= 0:
                remote_uids = client.search_uids("ALL", folder=folder)
            else:
                remote_uids = client.search_uids_since(state.last_uid, folder=folder)

            new_uids = [
                uid
                for uid in remote_uids
                if not self.db.message_exists(self.account, folder, uidvalidity, uid)
            ]
            result.fetched = len(new_uids)
            logger.info(
                "文件夹 %s：远端匹配 %d 封，待下载 %d 封",
                folder,
                len(remote_uids),
                len(new_uids),
            )
            self._emit(
                "fetch_plan",
                {"folder": folder, "remote": len(remote_uids), "to_fetch": len(new_uids)},
            )

            # ---- 分批下载 ----
            # max_messages 为本轮**全局**预算（跨文件夹共享），0 表示不限。
            limit = max_messages if max_messages else self.config.sync.max_messages_per_run
            processed = 0
            #: 本轮真正处理过的最高 UID —— 截断时水位只能推进到这里，
            #: 否则未下载的邮件会被永久跳过（§7 断点续传）
            highest_processed = 0
            truncated = False

            workers = max(1, int(self.config.sync.fetch_workers or 1))
            pool: _ClientPool | None = None
            if workers > 1:
                # 每个工作线程需要**独立的 IMAP 连接**（imaplib 非线程安全），
                # 因此另开一个连接池，而不是把主连接共享出去。
                pool = _ClientPool(self.config, self._auth_code(), self.cancel)

            try:
                for batch in _batched(new_uids, max(1, self.config.sync.fetch_batch_size)):
                    self.cancel.raise_if_cancelled()
                    if limit and processed >= limit:
                        truncated = True
                        break

                    # 预算截断时只取批次的前缀：这样"已处理集合"始终是前缀，
                    # 水位可以直接取其中最大 UID，不会跳过未下载的邮件。
                    allowed = batch
                    if limit:
                        remaining = limit - processed
                        if remaining <= 0:
                            truncated = True
                            break
                        if len(batch) > remaining:
                            allowed = batch[:remaining]
                            truncated = True

                    sizes = client.fetch_sizes(allowed, folder=folder)

                    if pool is not None and len(allowed) > 1:
                        self._process_parallel(
                            pool,
                            folder=folder,
                            uidvalidity=uidvalidity,
                            uids=allowed,
                            sizes=sizes,
                            result=result,
                            workers=min(workers, len(allowed)),
                        )
                    else:
                        for uid in allowed:
                            self.cancel.raise_if_cancelled()
                            self._process_one(
                                client,
                                folder=folder,
                                uidvalidity=uidvalidity,
                                uid=uid,
                                size=sizes.get(uid, 0),
                                result=result,
                            )

                    processed += len(allowed)
                    if allowed:
                        highest_processed = max(
                            highest_processed, max(_to_int(u) for u in allowed)
                        )

                    # 每批结束推进水位，保证中断后可从断点续传（§7 断点续传）
                    if highest_processed:
                        self.db.update_folder_state(
                            self.account,
                            folder,
                            last_uid=highest_processed,
                            last_sync_at=utcnow(),
                        )
                    if truncated:
                        break
            finally:
                if pool is not None:
                    pool.close_all()

            if truncated:
                result.skipped += max(0, len(new_uids) - processed)
                logger.info(
                    "文件夹 %s 达到单次同步上限，已处理 %d 封，水位停在 UID %d，剩余留待下次",
                    folder,
                    processed,
                    highest_processed,
                )

            # ---- 水位推进 ----
            # 只有在没有截断时才允许跳到远端最高 UID
            if remote_uids and not truncated:
                highest = max(_to_int(u) for u in remote_uids)
                self.db.update_folder_state(
                    self.account,
                    folder,
                    uidvalidity=uidvalidity,
                    last_uid=highest,
                    last_sync_at=utcnow(),
                )

            # ---- 全量比对（§11.2） ----
            # 关键：比对必须基于**整个文件夹**的 UID 列表。
            # 增量同步时 remote_uids 只是水位之后的新邮件，
            # 拿它去比对会把窗口之外的邮件全部误判为"已删除"。
            reconcile_due = self.config.sync.reconcile_deletions and (
                force_reconcile or self._should_reconcile(state)
            )
            if reconcile_due and not truncated:
                if full or state.last_uid <= 0:
                    complete_uids = remote_uids  # 本次检索本身就是全量
                else:
                    complete_uids = client.search_uids("ALL", folder=folder)
                result.deleted = self._reconcile(client, folder, complete_uids, result)
                self.db.update_folder_state(
                    self.account, folder, last_full_scan_at=utcnow()
                )
                logger.info(
                    "文件夹 %s 全量比对完成（比对 %d 封远端邮件），标记删除 %d 封",
                    folder,
                    len(complete_uids),
                    result.deleted,
                )

            # ---- 同步后立即索引本文件夹新邮件 ----
            if index and self.index_service is not None and result.archived:
                stats = self.index_service.index_pending(limit=result.archived)
                self._index_stats.merge(stats)

        except CancelledError:
            logger.info("文件夹 %s 同步被取消", folder)
            result.error_summary = "同步被取消"
            raise
        except ImapError as exc:
            logger.error("文件夹 %s 同步失败：%s", folder, exc)
            result.error_summary = str(exc)
        except Exception as exc:  # noqa: BLE001
            logger.exception("文件夹 %s 同步出现未预期错误", folder)
            result.error_summary = f"{type(exc).__name__}: {exc}"
        finally:
            result.finished_at = utcnow()
            self.db.finish_sync_log(log_id, result)
        return result

    # ------------------------------------------------------------------
    # 单封处理
    # ------------------------------------------------------------------

    def _process_parallel(
        self,
        pool: "_ClientPool",
        *,
        folder: str,
        uidvalidity: int,
        uids: Sequence[str],
        sizes: dict[str, int],
        result: SyncResult,
        workers: int,
    ) -> None:
        """并发下载一批邮件。

        每个线程用自己的 IMAP 连接（imaplib 非线程安全）；
        数据库写入由 ``Database`` 内部的写锁串行化，WAL 模式下安全；
        文件写入互不重叠，可安全并发。
        """
        counter_lock = threading.Lock()

        def work(uid: str) -> None:
            try:
                worker_client = pool.get()
            except Exception as exc:  # noqa: BLE001 - 建连失败不应拖垮整批
                with counter_lock:
                    result.failed += 1
                    summary = f"uid={uid}: 建立连接失败 {type(exc).__name__}: {exc}"
                    result.error_summary = (
                        f"{result.error_summary}; {summary}" if result.error_summary else summary
                    )
                logger.error("并发下载建立连接失败：%s", exc)
                return
            self._process_one(
                worker_client,
                folder=folder,
                uidvalidity=uidvalidity,
                uid=uid,
                size=sizes.get(uid, 0),
                result=result,
            )
            with counter_lock:
                self._emit(
                    "message_progress",
                    {"folder": folder, "uid": uid, "archived": result.archived},
                )

        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="imap-fetch") as pool_exec:
            futures = [pool_exec.submit(work, uid) for uid in uids]
            for future in as_completed(futures):
                self.cancel.raise_if_cancelled()
                exc = future.exception()
                if exc is not None and not isinstance(exc, CancelledError):
                    logger.error("并发下载任务异常：%s", exc)

    def _process_one(
        self,
        client: ImapClient,
        *,
        folder: str,
        uidvalidity: int,
        uid: str,
        size: int,
        result: SyncResult,
    ) -> None:
        try:
            plan = client.plan_fetch(uid, folder=folder, size=size)
            if plan.full_download:
                parsed = self._parse_full(client, folder, uid, uidvalidity, size)
            else:
                logger.info(
                    "邮件 uid=%s 含超大分部（总 %d 字节），仅拉取正文",
                    uid,
                    size,
                )
                parsed = self._parse_partial(client, folder, uid, uidvalidity, size, plan)

            if parsed is None:
                result.failed += 1
                return

            # 跨文件夹按 Message-ID 去重（§3.2）
            duplicate_of = self._find_duplicate(parsed, folder)

            archive = self.exporter.export(parsed, account=self.account)
            record = self._to_record(parsed, archive, duplicate_of=duplicate_of)
            pk = self.db.insert_message(record, archive.attachments)

            if duplicate_of:
                logger.debug(
                    "邮件 %s 与 #%s 内容重复，跳过索引", parsed.message_id, duplicate_of
                )

            result.archived += 1
            if parsed.message_id:
                result.new_message_ids.append(parsed.message_id)
            self._emit(
                "message_archived",
                {
                    "folder": folder,
                    "uid": uid,
                    "subject": parsed.subject[:80],
                    "pk": pk,
                    "attachments": len(archive.attachments),
                },
            )
        except CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 单封失败不能中断整个文件夹
            result.failed += 1
            summary = f"uid={uid}: {type(exc).__name__}: {exc}"
            logger.error("邮件归档失败（%s）", summary, exc_info=True)
            result.error_summary = (
                f"{result.error_summary}; {summary}" if result.error_summary else summary
            )
            self._emit("message_failed", {"folder": folder, "uid": uid, "error": summary})

    def _parse_full(
        self, client: ImapClient, folder: str, uid: str, uidvalidity: int, size: int
    ) -> ParsedMessage | None:
        message = client.fetch_one(uid, folder=folder)
        if message is None:
            logger.warning("未能拉取邮件 uid=%s", uid)
            return None
        obj: Message = message.obj
        return self.parser.parse(
            obj,
            folder=folder,
            uid=uid,
            uidvalidity=uidvalidity,
            size_bytes=size or getattr(message, "size", 0) or 0,
        )

    def _parse_partial(
        self,
        client: ImapClient,
        folder: str,
        uid: str,
        uidvalidity: int,
        size: int,
        plan: MessageFetchPlan,
    ) -> ParsedMessage | None:
        """超大邮件：只取头部 + 文本分部，附件仅记录元数据。"""
        header_message = None
        for message in client.iter_messages([uid], folder=folder, headers_only=True):
            header_message = message
            break
        if header_message is None:
            logger.warning("未能拉取邮件头部 uid=%s", uid)
            return None

        parsed = self.parser.parse(
            header_message.obj,
            folder=folder,
            uid=uid,
            uidvalidity=uidvalidity,
            size_bytes=size,
        )

        # 从 BODYSTRUCTURE 合成附件元数据（不下载）
        attachments: list[AttachmentMeta] = []
        for part in plan.parts:
            if part.is_text and not part.filename:
                continue
            attachments.append(
                AttachmentMeta(
                    filename=sanitize_filename(
                        part.filename or f"part_{part.section}",
                        max_bytes=120,
                        fallback=f"part_{part.section}",
                    ),
                    content_type=part.content_type,
                    size_bytes=part.size,
                    is_inline=part.disposition == "inline",
                    downloaded=False,
                    skip_reason=(
                        f"邮件整体超过 {self.config.sync.max_attachment_size_mb}MB 阈值，"
                        "附件未下载（仅记录元数据）"
                    ),
                    part_index=_to_int(part.section.split(".")[-1], 0),
                )
            )
        if attachments:
            parsed.attachments = attachments

        # 拉取文本分部补回正文
        sections = client.text_sections(plan.parts)
        if sections:
            plain_parts: list[str] = []
            html_parts: list[str] = []
            for raw in client.fetch_text_parts(uid, sections, folder=folder):
                try:
                    part = email.message_from_bytes(raw)
                except Exception:  # noqa: BLE001
                    continue
                if part.is_multipart():
                    for sub in part.walk():
                        if sub.is_multipart():
                            continue
                        text = decode_payload_text(sub)
                        if not text.strip():
                            continue
                        if sub.get_content_type() == "text/html":
                            html_parts.append(text)
                        else:
                            plain_parts.append(text)
                else:
                    text = decode_payload_text(part)
                    if not text.strip():
                        continue
                    if part.get_content_type() == "text/html":
                        html_parts.append(text)
                    else:
                        plain_parts.append(text)

            if plain_parts or html_parts:
                from .cleaner import CleanPolicy, compose_body

                parsed.text_plain = "\n\n".join(plain_parts)
                parsed.text_html = "\n\n".join(html_parts)
                markdown, plain = compose_body(parsed.text_plain, parsed.text_html, parsed.cid_map)
                if markdown.strip():
                    parsed.body_markdown = markdown
                    parsed.body_text = plain
            else:
                parsed.body_text = (
                    f"(邮件体积 {size / (1024 * 1024):.1f}MB 超过阈值，正文未能提取，"
                    "请到企业邮箱网页版查看原文)"
                )
                parsed.body_markdown = f"*{parsed.body_text}*"
        return parsed

    def _find_duplicate(self, parsed: ParsedMessage, folder: str) -> int | None:
        if not self.config.sync.dedupe_by_message_id or not parsed.message_id:
            return None
        existing = self.db.find_by_message_id(self.account, parsed.message_id)
        if existing is None or existing.pk is None:
            return None
        if existing.folder == folder:
            return None
        if existing.local_markdown_path and Path(existing.local_markdown_path).exists():
            return existing.pk
        return None

    def _to_record(
        self, parsed: ParsedMessage, archive: ArchiveResult, *, duplicate_of: int | None
    ) -> MessageRecord:
        return MessageRecord(
            account=self.account,
            message_id=parsed.message_id,
            uid=parsed.uid,
            uidvalidity=parsed.uidvalidity or 0,
            folder=parsed.folder,
            subject=parsed.subject,
            sender=parsed.sender,
            sender_name=parsed.sender_name,
            recipients=parsed.recipients,
            cc=parsed.cc,
            date_utc=parsed.date_utc,
            date_raw=parsed.date_raw,
            local_markdown_path=str(archive.markdown_path),
            body_text=parsed.body_text,
            has_attachments=parsed.has_attachments,
            size_bytes=parsed.size_bytes,
            content_hash=MessageRecord.content_fingerprint(
                parsed.subject, parsed.body_text
            ),
            duplicate_of=duplicate_of,
        )

    # ------------------------------------------------------------------
    # 全量比对
    # ------------------------------------------------------------------

    def _should_reconcile(self, state: FolderState) -> bool:
        if not self.config.sync.reconcile_deletions:
            return False
        interval = self.config.sync.full_scan_interval_hours
        if interval <= 0:
            return False
        if state.last_full_scan_at is None:
            return True
        return utcnow() - state.last_full_scan_at > timedelta(hours=interval)

    def _reconcile(
        self, client: ImapClient, folder: str, remote_uids: Sequence[str], result: SyncResult
    ) -> int:
        """把本地记录与服务端全量 UID 列表比对，标记已删除的邮件。"""
        keep = {str(u) for u in remote_uids}
        deleted = self.db.soft_delete_missing(self.account, folder, keep)
        if deleted and self.index_service is not None:
            for row in self.db.query(
                "SELECT id FROM messages WHERE account = ? AND folder = ? "
                "AND deleted_at IS NOT NULL",
                (self.account, folder),
            ):
                self.index_service.remove_message(int(row["id"]))
        return deleted

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    def _resolve_folders(
        self, client: ImapClient, requested: Sequence[str] | None
    ) -> list[str]:
        available = [f for f in client.list_folders() if f.selectable]
        names = [f.name for f in available]

        if requested:
            wanted = list(requested)
        elif self.config.sync.folders:
            wanted = list(self.config.sync.folders)
        else:
            wanted = names

        excluded = {e.lower() for e in self.config.sync.exclude_folders}
        selected = [
            name
            for name in wanted
            if name in names and name.lower() not in excluded
        ]
        missing = [name for name in wanted if name not in names]
        if missing:
            logger.warning("以下文件夹在服务端不存在，已跳过：%s", ", ".join(missing))
        return selected

    def fetch_folders(self) -> list[dict[str, Any]]:
        """列出服务端文件夹（``doctor`` / 配置向导用）。"""
        with ImapClient(self.config, self._auth_code(), cancel_token=self.cancel) as client:
            return [
                {
                    "name": f.name,
                    "delimiter": f.delimiter,
                    "selectable": f.selectable,
                    "flags": list(f.flags),
                }
                for f in client.list_folders()
            ]

    def test_connection(self) -> dict[str, Any]:
        """连通性自检。"""
        with ImapClient(self.config, self._auth_code(), cancel_token=self.cancel) as client:
            capabilities = client.capabilities()
            folders = client.list_folders()
            return {
                "ok": True,
                "server": self.config.email.imap_server,
                "account": _mask(self.account),
                "capabilities": capabilities,
                "folder_count": len(folders),
            }

    def _auth_code(self) -> str:
        from .config import resolve_auth_code

        code = resolve_auth_code(self.config)
        if not code:
            raise ImapError(
                "未配置授权码。请执行 `python main.py auth set`，"
                f"或设置环境变量 {self.config.email.auth_code_env}。"
            )
        return code

    def _emit(self, event: str, payload: dict[str, Any]) -> None:
        if self.on_progress is None:
            return
        try:
            self.on_progress(event, payload)
        except Exception:  # noqa: BLE001 - 回调异常不得影响同步
            logger.debug("进度回调失败", exc_info=True)


# ---------------------------------------------------------------------------

class _ClientPool:
    """按线程分配独立 IMAP 连接。

    ``imaplib`` / ``imap-tools`` 的连接对象**不能跨线程共享**：
    同一条连接上并发发命令会导致响应错位，表现为"取到别人的邮件"或
    解析异常。因此并发下载必须做到"每线程一条连接"。

    连接按需创建（只有真正干活的线程才建连），退出时统一关闭。
    """

    def __init__(self, config: AppConfig, auth_code: str, cancel: CancellationToken) -> None:
        self._config = config
        self._auth_code = auth_code
        self._cancel = cancel
        self._local = threading.local()
        self._all: list[ImapClient] = []
        self._lock = threading.Lock()

    def get(self) -> ImapClient:
        client = getattr(self._local, "client", None)
        if client is None:
            client = ImapClient(self._config, self._auth_code, cancel_token=self._cancel)
            client.connect()
            self._local.client = client
            with self._lock:
                self._all.append(client)
            logger.debug("为线程 %s 建立独立 IMAP 连接", threading.current_thread().name)
        return client

    def close_all(self) -> None:
        with self._lock:
            clients, self._all = list(self._all), []
        for client in clients:
            try:
                client.disconnect()
            except Exception:  # noqa: BLE001
                logger.debug("关闭并发连接失败", exc_info=True)


def _batched(items: Sequence[Any], size: int) -> Iterator[list[Any]]:
    for start in range(0, len(items), size):
        yield list(items[start : start + size])


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _mask(address: str) -> str:
    if "@" not in address:
        return "***"
    local, _, domain = address.partition("@")
    return f"{local[:2]}***@{domain}" if len(local) > 2 else f"{local[:1]}***@{domain}"
