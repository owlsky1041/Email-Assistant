"""IMAP 客户端（基于 imap-tools，§11.2 严禁直接使用原生 imaplib 处理中文邮件）。

关键设计
--------
* **BODYSTRUCTURE 预检**：先取邮件结构，识别各 MIME 分部的大小，
  超过阈值的分部直接不下载，避免把巨大附件读进内存。
* **分页拉取**：按 ``sync.fetch_batch_size`` 分批，每批之间检查取消令牌。
* **断线重连**：捕获 ``imaplib`` 连接异常，自动重连并重试当前批次。
* **全量 UID 比对**：不只依赖最大 UID，支持定期拉取全量 UID 处理网页端删除。
"""

from __future__ import annotations

import imaplib
import logging
import re
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from imap_tools import MailBox, MailMessage
from imap_tools.errors import MailboxFolderSelectError

from .cancellation import CancellationToken, get_cancellation_token
from .config import AppConfig
from .logging_setup import register_runtime_secret

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------

class ImapError(RuntimeError):
    """IMAP 操作失败。"""


class ImapAuthError(ImapError):
    """认证失败（授权码错误或未开启 IMAP 服务）。"""


class ImapConnectionError(ImapError):
    """连接失败或中断。"""


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class FolderInfoLite:
    name: str
    delimiter: str = "/"
    selectable: bool = True
    flags: tuple[str, ...] = ()

    @property
    def is_drafts(self) -> bool:
        return "\\Drafts" in self.flags

    @property
    def is_junk(self) -> bool:
        return "\\Junk" in self.flags


@dataclass(slots=True)
class Envelope:
    """廉价的邮件概要（仅头部，用于去重判断）。"""

    uid: str
    message_id: str = ""
    subject: str = ""
    sender: str = ""
    date: datetime | None = None
    size: int = 0


@dataclass(slots=True)
class PartInfo:
    """BODYSTRUCTURE 中的一个 MIME 分部。"""

    section: str
    content_type: str = "application/octet-stream"
    encoding: str = "7BIT"
    size: int = 0
    lines: int = 0
    is_multipart: bool = False
    filename: str = ""
    disposition: str = ""

    @property
    def is_text(self) -> bool:
        return self.content_type.startswith("text/")


@dataclass(slots=True)
class MessageFetchPlan:
    """一封邮件的下载计划。"""

    uid: str
    total_size: int = 0
    parts: list[PartInfo] = field(default_factory=list)
    oversize_parts: list[PartInfo] = field(default_factory=list)
    #: True 表示可以整封安全下载（总大小在阈值内）
    full_download: bool = True


# ---------------------------------------------------------------------------
# BODYSTRUCTURE 解析
# ---------------------------------------------------------------------------

def _tokenize_bodystructure(raw: bytes) -> list[Any]:
    """把 BODYSTRUCTURE 的 S-表达式解析成嵌套列表。"""
    tokens: list[Any] = []
    stack: list[list[Any]] = []
    i = 0
    n = len(raw)
    current: list[Any] = tokens
    while i < n:
        ch = raw[i : i + 1]
        if ch == b"(":
            new: list[Any] = []
            current.append(new)
            stack.append(current)
            current = new
        elif ch == b")":
            if stack:
                current = stack.pop()
        elif ch == b'"':
            j = i + 1
            buf = bytearray()
            while j < n:
                c = raw[j : j + 1]
                if c == b"\\" and j + 1 < n:
                    buf += raw[j + 1 : j + 2]
                    j += 2
                    continue
                if c == b'"':
                    break
                buf += c
                j += 1
            current.append(bytes(buf))
            i = j
        elif ch in b" \r\n\t":
            pass
        else:
            j = i
            while j < n and raw[j : j + 1] not in b" ()\r\n\t":
                j += 1
            current.append(raw[i:j])
            i = j - 1
        i += 1
    return tokens


def _as_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bytes):
        try:
            return int(value)
        except ValueError:
            return default
    if isinstance(value, int):
        return value
    return default


def _as_bytes(value: Any) -> bytes:
    return value if isinstance(value, bytes) else b""


def _param_lookup(params: Any, key: str) -> str:
    """从 BODYSTRUCTURE 的参数列表 ``("charset" "utf-8" "name" "a.pdf")`` 里取值。"""
    if not isinstance(params, list):
        return ""
    lowered = key.lower()
    for i in range(0, len(params) - 1, 2):
        name = _as_bytes(params[i]).decode("ascii", "replace").lower()
        if name == lowered:
            raw = _as_bytes(params[i + 1])
            if raw:
                from .mail_parser import decode_mime_words

                return decode_mime_words(raw)
    return ""


def parse_bodystructure(raw: bytes) -> list[PartInfo]:
    """从 BODYSTRUCTURE 响应里提取所有叶子分部的信息。"""
    try:
        tree = _tokenize_bodystructure(raw)
    except Exception:  # noqa: BLE001
        logger.debug("BODYSTRUCTURE 解析失败", exc_info=True)
        return []

    # 定位最外层结构：形如 [b'12', [ ... ]]
    root: Any = None
    for node in tree:
        if isinstance(node, list) and node and isinstance(node[0], list):
            root = node
            break
    if root is None:
        for node in tree:
            if isinstance(node, list):
                root = node
                break
    if root is None:
        return []

    parts: list[PartInfo] = []

    def walk(node: list[Any], prefix: str) -> None:
        if not node:
            return
        if isinstance(node[0], list):
            # multipart：依次递归子分部
            idx = 0
            for child in node:
                if not isinstance(child, list):
                    break
                idx += 1
                section = f"{prefix}.{idx}" if prefix else str(idx)
                walk(child, section)
            return

        ctype = _as_bytes(node[0]).decode("ascii", "replace").lower()
        subtype = _as_bytes(node[1]).decode("ascii", "replace").lower() if len(node) > 1 else ""
        encoding = "7BIT"
        size = 0
        lines = 0
        if len(node) > 5:
            encoding = _as_bytes(node[5]).decode("ascii", "replace").upper()
        if len(node) > 6:
            size = _as_int(node[6])
        if len(node) > 7:
            lines = _as_int(node[7])

        # 文件名：优先取 Content-Type 的 name 参数，其次取 disposition 的 filename
        filename = _param_lookup(node[2] if len(node) > 2 else None, "name")
        disposition = ""
        for candidate in node[8:11]:
            if isinstance(candidate, list) and candidate and isinstance(candidate[0], bytes):
                disp = _as_bytes(candidate[0]).decode("ascii", "replace").lower()
                if disp in ("attachment", "inline"):
                    disposition = disp
                    filename = filename or _param_lookup(
                        candidate[1] if len(candidate) > 1 else None, "filename"
                    )
                    break

        full_type = f"{ctype}/{subtype}" if subtype else ctype
        section = prefix or "1"
        parts.append(
            PartInfo(
                section=section,
                content_type=full_type,
                encoding=encoding,
                size=size,
                lines=lines,
                filename=filename,
                disposition=disposition,
            )
        )
        # message/rfc822 内嵌结构
        if ctype == "message" and len(node) > 8 and isinstance(node[8], list):
            walk(node[8], f"{section}.1")

    walk(root, "")
    return parts


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------

class ImapClient:
    """带重连与取消支持的 IMAP 封装。"""

    def __init__(
        self,
        config: AppConfig,
        auth_code: str,
        *,
        cancel_token: CancellationToken | None = None,
    ) -> None:
        self.config = config
        self.auth_code = auth_code
        self.account = config.email.address
        self.cancel = cancel_token or get_cancellation_token()
        self._mailbox: MailBox | None = None
        self._current_folder: str | None = None
        self.max_attachment_bytes = int(config.sync.max_attachment_size_mb * 1024 * 1024)

    # ---- 连接生命周期 ------------------------------------------------

    def connect(self) -> "ImapClient":
        if self._mailbox is not None:
            return self
        cfg = self.config.email
        if not cfg.address:
            raise ImapAuthError("未配置邮箱地址（email.address）")
        if not self.auth_code:
            raise ImapAuthError(
                "未获取到授权码。请设置环境变量 "
                f"{cfg.auth_code_env}，或使用 `python main.py auth set` 写入本地密钥库。"
            )

        # 注册脱敏，确保授权码绝不落入日志
        register_runtime_secret(self.auth_code)

        last_error: Exception | None = None
        for attempt in range(1, cfg.max_retries + 1):
            self.cancel.raise_if_cancelled()
            try:
                logger.info(
                    "正在连接 IMAP %s:%s（账号 %s）",
                    cfg.imap_server,
                    cfg.imap_port,
                    _mask(cfg.address),
                )
                mailbox = MailBox(
                    host=cfg.imap_server, port=cfg.imap_port, timeout=cfg.connect_timeout
                )
                mailbox.login(cfg.address, self.auth_code, initial_folder=None)
                try:
                    mailbox.client.socket().settimeout(cfg.read_timeout)  # type: ignore[union-attr]
                except Exception:  # noqa: BLE001 - 部分后端不支持
                    logger.debug("设置读超时失败", exc_info=True)
                self._mailbox = mailbox
                self._current_folder = None
                logger.info("IMAP 连接成功")
                return self
            except imaplib.IMAP4.error as exc:
                message = str(exc)
                last_error = exc
                if "AUTHENTICATIONFAILED" in message.upper() or "LOGIN" in message.upper():
                    raise ImapAuthError(
                        "认证失败：请检查邮箱地址与授权码是否正确，"
                        "并确认腾讯企业邮箱已开启 IMAP/SMTP 服务。"
                    ) from exc
                logger.warning("IMAP 连接失败（第 %d 次）：%s", attempt, message)
            except (OSError, imaplib.IMAP4.abort) as exc:
                last_error = exc
                logger.warning("IMAP 网络错误（第 %d 次）：%s", attempt, exc)
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                logger.warning("IMAP 未知错误（第 %d 次）：%s", attempt, exc)

            if attempt < cfg.max_retries:
                delay = cfg.retry_backoff_seconds * attempt
                logger.info("%.1f 秒后重试…", delay)
                if self.cancel.wait(delay):
                    raise ImapError("连接过程被取消")

        raise ImapConnectionError(f"无法连接 IMAP 服务器：{last_error}")

    def disconnect(self) -> None:
        if self._mailbox is None:
            return
        try:
            self._mailbox.logout()
        except Exception:  # noqa: BLE001 - 退出失败无需上报
            logger.debug("IMAP 登出异常", exc_info=True)
        finally:
            self._mailbox = None
            self._current_folder = None

    def __enter__(self) -> "ImapClient":
        return self.connect()

    def __exit__(self, exc_type, exc, tb) -> bool:  # type: ignore[no-untyped-def]
        self.disconnect()
        return False

    @property
    def mailbox(self) -> MailBox:
        if self._mailbox is None:
            raise ImapConnectionError("IMAP 尚未连接")
        return self._mailbox

    @property
    def raw(self) -> imaplib.IMAP4:
        """底层已认证的 ``imaplib`` 连接。

        **注意**：``imap-tools.MailBox`` 并不继承 ``IMAP4``，它把连接放在
        实例属性 ``client`` 上（因此 ``hasattr(MailBox, "client")`` 是 False，
        只有实例才有）。直接写 ``mailbox.uid(...)`` / ``mailbox.noop()``
        会抛 AttributeError —— 曾经因为异常被吞掉，导致大小预检与
        分部分拉取在真实服务器上**从未生效**，所有邮件正文都成了占位符。
        """
        return self.mailbox.client  # type: ignore[return-value]

    def _reconnect(self) -> None:
        logger.warning("尝试重新建立 IMAP 连接…")
        self.disconnect()
        self.connect()

    # ---- 文件夹 ------------------------------------------------------

    def list_folders(self) -> list[FolderInfoLite]:
        self.cancel.raise_if_cancelled()
        try:
            infos = self.mailbox.folder.list("", "*")
        except Exception as exc:  # noqa: BLE001
            raise ImapError(f"列出邮箱文件夹失败：{exc}") from exc

        result: list[FolderInfoLite] = []
        for info in infos:
            flags = tuple(info.flags or ())
            result.append(
                FolderInfoLite(
                    name=info.name,
                    delimiter=info.delim or "/",
                    selectable="\\Noselect" not in flags,
                    flags=flags,
                )
            )
        logger.info("发现 %d 个文件夹", len(result))
        return result

    def select_folder(self, folder: str) -> dict[str, int]:
        """选中文件夹，返回 ``{UIDVALIDITY, UIDNEXT, MESSAGES}``。"""
        self.cancel.raise_if_cancelled()
        try:
            self.mailbox.folder.set(folder, readonly=True)
            self._current_folder = folder
        except MailboxFolderSelectError as exc:
            raise ImapError(f"无法选中文件夹 {folder}：{exc}") from exc
        except Exception as exc:  # noqa: BLE001
            raise ImapError(f"选中文件夹 {folder} 失败：{exc}") from exc

        try:
            status = self.mailbox.folder.status(folder)
        except Exception as exc:  # noqa: BLE001
            logger.debug("读取文件夹状态失败：%s", exc)
            status = {}
        return {
            "UIDVALIDITY": int(status.get("UIDVALIDITY", 0)),
            "UIDNEXT": int(status.get("UIDNEXT", 0)),
            "MESSAGES": int(status.get("MESSAGES", 0)),
        }

    # ---- 检索 --------------------------------------------------------

    def search_uids(self, criteria: str = "ALL", *, folder: str | None = None) -> list[str]:
        """返回满足条件的 UID 列表（字符串形式，便于比较）。"""
        if folder is not None and folder != self._current_folder:
            self.select_folder(folder)
        self.cancel.raise_if_cancelled()
        try:
            uids = self.mailbox.uids(criteria)
        except imaplib.IMAP4.abort:
            self._reconnect()
            if folder:
                self.select_folder(folder)
            uids = self.mailbox.uids(criteria)
        except Exception as exc:  # noqa: BLE001
            raise ImapError(f"搜索 UID 失败（{criteria}）：{exc}") from exc
        return [str(u) for u in uids]

    def search_uids_since(self, last_uid: int, *, folder: str) -> list[str]:
        """增量检索：UID ``last_uid+1`` 之后的新邮件。

        IMAP 的 ``UID n:*`` 至少返回最后一封邮件，即使其 UID < n，
        因此这里必须按数值再过滤一次。
        """
        if last_uid <= 0:
            return self.search_uids("ALL", folder=folder)
        uids = self.search_uids(f"UID {last_uid + 1}:*", folder=folder)
        return [u for u in uids if _to_int(u) > last_uid]

    # ---- 邮件大小预检 ------------------------------------------------

    def fetch_sizes(self, uids: Sequence[str], *, folder: str) -> dict[str, int]:
        """批量取 ``RFC822.SIZE``（比下载整封邮件廉价得多）。"""
        if not uids:
            return {}
        if folder != self._current_folder:
            self.select_folder(folder)

        sizes: dict[str, int] = {}
        for batch in _chunks(list(uids), 200):
            self.cancel.raise_if_cancelled()
            try:
                typ, data = self.mailbox.client.uid("FETCH", ",".join(batch), "(RFC822.SIZE)")
            except (AttributeError, TypeError, NotImplementedError):
                # 这类异常说明代码调错了 API，属于缺陷而非网络抖动，
                # 必须直接暴露。此前 `mailbox.uid(...)` 写错属性却在这里被
                # 静默吞掉，导致大小预检在真实服务器上从未生效。
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("获取邮件大小失败，将按整封下载：%s", exc)
                continue
            if typ != "OK" or not data:
                continue
            for item in data:
                if not isinstance(item, tuple):
                    continue
                head = item[0]
                if not isinstance(head, bytes):
                    continue
                uid_match = re.search(rb"UID\s+(\d+)", head)
                size_match = re.search(rb"RFC822\.SIZE\s+(\d+)", head)
                if uid_match and size_match:
                    sizes[uid_match.group(1).decode()] = int(size_match.group(1))
        return sizes

    def fetch_bodystructure(self, uid: str, *, folder: str) -> list[PartInfo]:
        """获取单封邮件的 BODYSTRUCTURE 分部列表。"""
        if folder != self._current_folder:
            self.select_folder(folder)
        try:
            typ, data = self.mailbox.client.uid("FETCH", uid, "(BODYSTRUCTURE)")
        except (AttributeError, TypeError, NotImplementedError):
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("获取 BODYSTRUCTURE 失败（uid=%s）：%s", uid, exc)
            return []
        if typ != "OK" or not data:
            return []
        raw = b""
        for item in data:
            if isinstance(item, tuple) and isinstance(item[0], bytes):
                raw = item[0]
                break
            if isinstance(item, bytes):
                raw = item
        if not raw:
            return []
        return parse_bodystructure(raw)

    def plan_fetch(self, uid: str, *, folder: str, size: int = 0) -> MessageFetchPlan:
        """决定这封邮件是整封下载，还是只拉取小分部。"""
        plan = MessageFetchPlan(uid=str(uid), total_size=size)
        if size and size <= self.max_attachment_bytes:
            plan.full_download = True
            return plan

        parts = self.fetch_bodystructure(uid, folder=folder)
        plan.parts = parts
        plan.oversize_parts = [p for p in parts if p.size > self.max_attachment_bytes]
        if not parts:
            # 拿不到结构时**整封下载**：宁可多花带宽，也不能让正文变成占位符。
            # （此前这里返回 False，配合失效的预检，导致所有邮件都只取到头部。）
            logger.warning(
                "无法获取 uid=%s 的邮件结构，将整封下载以保证正文完整", uid
            )
            plan.full_download = True
            return plan
        plan.full_download = not plan.oversize_parts
        return plan

    # ---- 拉取 --------------------------------------------------------

    def fetch_envelopes(
        self, uids: Sequence[str], *, folder: str
    ) -> list[Envelope]:
        """批量拉取头部概要（用于去重与增量判定）。"""
        from .mail_parser import decode_mime_words, extract_addresses

        envelopes: list[Envelope] = []
        for message in self.iter_messages(uids, folder=folder, headers_only=True):
            from_list = extract_addresses(message.headers.get("From"))
            envelopes.append(
                Envelope(
                    uid=str(message.uid),
                    message_id=decode_mime_words(message.headers.get("Message-ID"))
                    .strip()
                    .strip("<>"),
                    subject=decode_mime_words(message.headers.get("Subject")),
                    sender=from_list[0] if from_list else "",
                    date=message.date,
                    size=message.size or 0,
                )
            )
        return envelopes

    def iter_messages(
        self,
        uids: Sequence[str],
        *,
        folder: str,
        headers_only: bool = False,
        mark_seen: bool = False,
    ) -> Iterator[MailMessage]:
        """分批拉取邮件（§11.2 分页），每批之间检查取消。"""
        if not uids:
            return
        if folder != self._current_folder:
            self.select_folder(folder)

        batch_size = max(1, int(self.config.sync.fetch_batch_size))
        for batch in _chunks(list(uids), batch_size):
            self.cancel.raise_if_cancelled()
            try:
                yield from self._fetch_batch(
                    batch, folder=folder, headers_only=headers_only, mark_seen=mark_seen
                )
            except (imaplib.IMAP4.abort, OSError) as exc:
                logger.warning("批量拉取中断（%s），重连后重试该批次", exc)
                self._reconnect()
                self.select_folder(folder)
                yield from self._fetch_batch(
                    batch, folder=folder, headers_only=headers_only, mark_seen=mark_seen
                )

    def _fetch_batch(
        self,
        batch: Sequence[str],
        *,
        folder: str,
        headers_only: bool,
        mark_seen: bool,
    ) -> Iterator[MailMessage]:
        criteria = "UID " + ",".join(str(u) for u in batch)
        for attempt in range(2):
            self.cancel.raise_if_cancelled()
            try:
                messages = self.mailbox.fetch(
                    criteria,
                    mark_seen=mark_seen,
                    headers_only=headers_only,
                    bulk=True,
                )
                # 触发实际网络读取，把异常暴露在此处
                yield from messages
                return
            except (imaplib.IMAP4.abort, OSError) as exc:
                if attempt == 1:
                    raise
                logger.warning("拉取批次失败（%s），重连后重试", exc)
                self._reconnect()
                self.select_folder(folder)

    def fetch_one(self, uid: str, *, folder: str) -> MailMessage | None:
        for message in self.iter_messages([uid], folder=folder):
            return message
        return None

    # ---- 分部拉取（超大邮件专用） ------------------------------------

    def fetch_text_parts(
        self, uid: str, sections: Sequence[str], *, folder: str
    ) -> list[bytes]:
        """只拉取指定的 MIME 文本分部，跳过巨大附件。

        返回「MIME 头部 + 空行 + 分部内容」拼接后的字节串，
        可直接喂给 :func:`email.message_from_bytes` 得到可解码的 Message。
        """
        if not sections:
            return []
        if folder != self._current_folder:
            self.select_folder(folder)

        items = []
        for section in sections:
            items.append(f"BODY.PEEK[{section}.MIME]")
            items.append(f"BODY.PEEK[{section}]")
        command = "(" + " ".join(items) + ")"

        self.cancel.raise_if_cancelled()
        try:
            typ, data = self.mailbox.client.uid("FETCH", str(uid), command)
        except (AttributeError, TypeError, NotImplementedError):
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("分部拉取失败（uid=%s）：%s", uid, exc)
            return []
        if typ != "OK" or not data:
            return []

        # 响应形如：[(b'1 (BODY[1.MIME] {123}', mime_bytes), b' BODY[1] {456}', body_bytes), b')']
        chunks: dict[str, list[bytes]] = {}
        pending: str | None = None
        for item in data:
            if isinstance(item, tuple):
                head, payload = item[0], item[1]
                section = _section_from_head(head)
                if section:
                    pending = section
                    chunks.setdefault(section, [])
                    if isinstance(payload, (bytes, bytearray)):
                        chunks[section].append(bytes(payload))
                elif pending:
                    if isinstance(payload, (bytes, bytearray)):
                        chunks[pending].append(bytes(payload))
            elif isinstance(item, bytes) and pending:
                section = _section_from_head(item)
                if section:
                    pending = section
                    chunks.setdefault(section, [])
                else:
                    chunks[pending].append(item)

        parts: list[bytes] = []
        for section in sections:
            mime_key = f"{section}.MIME"
            body = b"".join(chunks.get(section, []))
            mime = b"".join(chunks.get(mime_key, []))
            if not body and not mime:
                continue
            if mime and not mime.rstrip().endswith(b"\r\n\r\n"):
                mime = mime.rstrip(b"\r\n") + b"\r\n\r\n"
            parts.append(mime + body)
        return parts

    def text_sections(self, parts: Sequence[PartInfo]) -> list[str]:
        """从 BODYSTRUCTURE 里挑出文本分部编号（限制数量，避免异常结构）。"""
        sections = [p.section for p in parts if p.is_text and p.size <= self.max_attachment_bytes]
        return sections[:4]

    # ---- 健康检查 ----------------------------------------------------

    def ping(self) -> bool:
        try:
            self.mailbox.client.noop()
            return True
        except (AttributeError, TypeError, NotImplementedError):
            raise
        except Exception:  # noqa: BLE001
            return False

    def capabilities(self) -> list[str]:
        try:
            return sorted(str(c) for c in self.mailbox.client.capabilities)
        except (AttributeError, TypeError, NotImplementedError):
            raise
        except Exception:  # noqa: BLE001
            return []


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------

def _chunks(items: list[Any], size: int) -> Iterator[list[Any]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


_BODY_SECTION_RE = re.compile(rb"BODY\[([0-9.]+)(?:\.MIME)?\]")


def _section_from_head(head: bytes) -> str | None:
    """从 FETCH 响应头部解析出 ``BODY[1.2]`` 里的分部编号。"""
    match = _BODY_SECTION_RE.search(head or b"")
    if not match:
        return None
    section = match.group(1).decode("ascii")
    if b".MIME]" in head:
        return f"{section}.MIME"
    return section


def _mask(address: str) -> str:
    if "@" not in address:
        return "***"
    local, _, domain = address.partition("@")
    return f"{local[:2]}***@{domain}" if len(local) > 2 else f"{local[:1]}***@{domain}"


def measure_rtt(client: ImapClient) -> float:
    """测量一次 NOOP 往返耗时（doctor 命令用）。"""
    start = time.monotonic()
    client.ping()
    return time.monotonic() - start
