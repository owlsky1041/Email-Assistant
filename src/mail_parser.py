"""邮件 MIME 解析（§11.2 防御性编程）。

职责
----
* 把 ``email.message.Message`` 解析成 :class:`~src.models.ParsedMessage`；
* 正确解码 RFC 2047 编码的头部与 RFC 2231 编码的附件名（中文邮件重灾区）；
* 遍历嵌套 MIME，提取 ``text/plain`` / ``text/html`` / 附件 / 内联图片；
* 超过阈值的附件**只记录元数据不下载**（§3.2 超大附件保护）。
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.message import Message
from email.utils import collapse_rfc2231_value, getaddresses, parsedate_to_datetime
from typing import Any

from .cleaner import DEFAULT_POLICY, CleanPolicy, compose_body
from .models import AttachmentMeta, ParsedMessage
from .utils import sanitize_filename

logger = logging.getLogger(__name__)

# 这些 content-type 视为正文而非附件
_BODY_TYPES = ("text/plain", "text/html")

# 这些视为「附件」，即使 disposition 是 inline
_FORCE_ATTACHMENT_TYPES = (
    "application/",
    "image/",
    "audio/",
    "video/",
    "message/rfc822",
    "text/calendar",
    "text/csv",
)

# 常见字符集回退顺序（中文邮件经常声明错误）
_CHARSET_FALLBACKS = ("utf-8", "gb18030", "big5", "shift_jis", "euc-kr", "latin-1")

# HTML 中内联图片 MIME 类型（用于判断 inline 是否值得保存）
_IMAGE_TYPES = ("image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp",
                "image/tiff", "image/svg+xml")


# ---------------------------------------------------------------------------
# 头部解码
# ---------------------------------------------------------------------------

def decode_mime_words(value: Any) -> str:
    """解码 RFC 2047 头部（``=?utf-8?B?...?=``）。

    注意：``bytes`` 输入必须先还原成 ``str`` 再走 RFC 2047 解码。
    早期实现在 bytes 分支直接返回，导致 IMAP BODYSTRUCTURE 里的
    编码附件名（``("name" "=?utf-8?B?...?=")``）永远解不出来。
    """
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray)):
        text = _decode_bytes_to_str(bytes(value))
    else:
        text = str(value)
    if "=?" not in text:
        return text
    try:
        return str(make_header(decode_header(text)))
    except Exception:  # noqa: BLE001 - 畸形头部极常见，必须容错
        parts: list[str] = []
        for chunk, enc in decode_header(text):
            if isinstance(chunk, bytes):
                try:
                    parts.append(chunk.decode(enc or "utf-8", "replace"))
                except (LookupError, UnicodeDecodeError):
                    parts.append(chunk.decode("utf-8", "replace"))
            else:
                parts.append(chunk)
        return "".join(parts)


def _decode_bytes_to_str(raw: bytes) -> str:
    """把字节串还原为文本（RFC 2047 编码本身是 ASCII 的）。"""
    for enc in _CHARSET_FALLBACKS:
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def decode_filename(part: Message) -> str:
    """解码附件名，兼容 RFC 2231 与 RFC 2047。"""
    raw = part.get_filename()
    if raw is None:
        # 有些客户端把文件名放在 Content-Type 的 name 参数里
        raw = part.get_param("name", header="content-type")
    if raw is None:
        return ""
    if isinstance(raw, tuple):  # RFC 2231 三元组 (charset, language, value)
        try:
            raw = collapse_rfc2231_value(raw)
        except Exception:  # noqa: BLE001
            raw = str(raw)
    name = decode_mime_words(raw)
    name = name.replace("\r", "").replace("\n", "").strip()
    return name or ""


def decode_payload_text(part: Message) -> str:
    """解码正文字节流，自动处理错误的 charset 声明。"""
    try:
        payload = part.get_payload(decode=True)
    except Exception:  # noqa: BLE001
        logger.debug("正文解码失败", exc_info=True)
        return ""
    if payload is None:
        raw = part.get_payload(decode=False)
        return raw if isinstance(raw, str) else ""

    declared = part.get_content_charset()
    candidates: list[str] = []
    if declared:
        candidates.append(declared.lower())
    candidates.extend(_CHARSET_FALLBACKS)

    best_text = ""
    best_score = -1.0
    seen: set[str] = set()
    for enc in candidates:
        if enc in seen:
            continue
        seen.add(enc)
        try:
            text = payload.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
        # 用替换字符比例评估解码质量
        bad = text.count("\ufffd")
        score = 1.0 - (bad / max(len(text), 1))
        if score > best_score:
            best_score = score
            best_text = text
        if bad == 0:
            break
    if not best_text:
        best_text = payload.decode("utf-8", "replace")
    return best_text


def extract_addresses(value: Any) -> list[str]:
    """把头部地址列表解析为标准 ``name <addr>`` 字符串列表。"""
    raw = decode_mime_words(value)
    if not raw:
        return []
    try:
        pairs = getaddresses([raw])
    except Exception:  # noqa: BLE001
        return [raw]
    out: list[str] = []
    for name, addr in pairs:
        name = (name or "").strip()
        addr = (addr or "").strip()
        if name and addr:
            out.append(f"{name} <{addr}>")
        elif addr:
            out.append(addr)
        elif name:
            out.append(name)
    return out


def _parse_date(value: Any) -> tuple[datetime | None, str]:
    raw = decode_mime_words(value).strip()
    if not raw:
        return None, ""
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        return None, raw
    if dt is None:
        return None, raw
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt, raw


def _normalize_content_id(value: Any) -> str:
    cid = decode_mime_words(value).strip()
    return cid.strip("<>").strip()


# ---------------------------------------------------------------------------
# 解析器
# ---------------------------------------------------------------------------

class MailParser:
    """把 IMAP 邮件对象解析为领域模型。"""

    def __init__(
        self,
        *,
        max_attachment_bytes: int = 50 * 1024 * 1024,
        download_attachments: bool = True,
        max_body_bytes: int = 2 * 1024 * 1024,
        policy: "CleanPolicy | None" = None,
    ) -> None:
        self.max_attachment_bytes = max_attachment_bytes
        self.download_attachments = download_attachments
        self.max_body_bytes = max_body_bytes
        self.policy = policy or DEFAULT_POLICY

    # -- 对外接口 --------------------------------------------------------

    def parse(
        self,
        message: Message,
        *,
        folder: str,
        uid: str,
        uidvalidity: int | None = None,
        size_bytes: int = 0,
    ) -> ParsedMessage:
        """解析单封邮件。

        :param message: ``email.message.Message``（imap-tools 的 ``MailMessage.obj``）
        """
        result = ParsedMessage(
            uid=str(uid),
            folder=folder,
            uidvalidity=uidvalidity,
            size_bytes=size_bytes,
        )

        headers = message
        result.message_id = self._normalize_message_id(headers.get("Message-ID"))
        result.subject = decode_mime_words(headers.get("Subject")) or "(无主题)"
        result.in_reply_to = decode_mime_words(headers.get("In-Reply-To"))
        result.references = decode_mime_words(headers.get("References"))

        from_list = extract_addresses(headers.get("From"))
        if from_list:
            result.sender = self._bare_address(from_list[0])
            result.sender_name = self._display_name(from_list[0])
        result.recipients = ", ".join(extract_addresses(headers.get("To")))
        result.cc = ", ".join(extract_addresses(headers.get("Cc")))
        reply_to = extract_addresses(headers.get("Reply-To"))
        result.reply_to = reply_to[0] if reply_to else ""

        result.date, result.date_raw = _parse_date(headers.get("Date"))

        # 常用头部留档（避免把整个头部塞进数据库）
        for key in ("List-Id", "List-Unsubscribe", "X-Mailer", "Return-Path", "Delivered-To"):
            value = headers.get(key)
            if value:
                result.raw_headers[key] = decode_mime_words(value)[:500]

        # -- 遍历 MIME 树 --
        text_parts: list[str] = []
        html_parts: list[str] = []
        part_index = 0

        for part in self._walk(message):
            ctype = (part.get_content_type() or "").lower()
            disposition = (part.get_content_disposition() or "").lower()
            filename = decode_filename(part)
            content_id = _normalize_content_id(part.get("Content-ID"))

            is_multipart = part.is_multipart()
            if is_multipart and ctype != "message/rfc822":
                continue  # 容器节点，由 _walk 递归处理

            # --- 正文 ---
            if ctype in _BODY_TYPES and disposition != "attachment" and not filename:
                index = len(text_parts) if ctype == "text/plain" else len(html_parts)
                text = decode_payload_text(part)
                if not text:
                    continue
                if len(text) > self.max_body_bytes:
                    logger.debug("正文超过 %d 字节，已截断", self.max_body_bytes)
                    text = text[: self.max_body_bytes]
                if ctype == "text/plain":
                    text_parts.append(text)
                else:
                    html_parts.append(text)
                _ = index
                continue

            # --- message/rfc822：作为 .eml 附件 ---
            if ctype == "message/rfc822":
                part_index += 1
                self._add_attachment(
                    result,
                    part_index,
                    filename or f"forwarded_{part_index}.eml",
                    "message/rfc822",
                    self._serialize_part(part),
                    content_id=content_id,
                    is_inline=False,
                )
                continue

            # --- 附件 / 内联资源 ---
            is_inline = disposition == "inline" or bool(content_id)
            if not filename and not is_inline:
                # 无文件名且非内联，且是强制附件类型 -> 给个兜底名字
                if not ctype.startswith(_FORCE_ATTACHMENT_TYPES):
                    continue
                filename = f"attachment_{part_index + 1}"
            if not filename and is_inline:
                ext = self._guess_extension(ctype)
                filename = f"inline_{content_id or part_index + 1}{ext}"

            part_index += 1
            payload = self._safe_payload(part)
            self._add_attachment(
                result,
                part_index,
                filename,
                ctype,
                payload,
                content_id=content_id,
                is_inline=is_inline,
            )

        # -- 组装正文 --
        raw_plain = "\n\n".join(p for p in text_parts if p.strip())
        raw_html = "\n\n".join(p for p in html_parts if p.strip())

        # cid_map 先按「附件相对路径」占位；导出器落盘后会更新
        cid_map: dict[str, str] = {}
        for meta in result.attachments:
            if meta.content_id:
                cid_map[meta.content_id] = f"attachments/{meta.filename}"

        markdown, plain = compose_body(raw_plain, raw_html, cid_map,
                                       subject=result.subject, policy=self.policy)
        result.text_plain = raw_plain
        result.text_html = raw_html
        result.body_markdown = markdown
        result.body_text = plain
        result.cid_map = cid_map

        if not result.body_text.strip():
            result.body_text = "(此邮件无正文内容)"
            result.body_markdown = result.body_markdown or "*(此邮件无正文内容)*"

        return result

    # -- 内部工具 --------------------------------------------------------

    @staticmethod
    def _walk(message: Message):
        """深度优先遍历 MIME 树（含嵌套 multipart）。"""
        for part in message.walk():
            yield part

    @staticmethod
    def _normalize_message_id(value: Any) -> str:
        raw = decode_mime_words(value).strip()
        if not raw:
            return ""
        raw = raw.strip("<>").strip()
        return raw[:512]

    @staticmethod
    def _bare_address(display: str) -> str:
        match = re.search(r"<([^>]+)>", display)
        return (match.group(1) if match else display).strip().lower()

    @staticmethod
    def _display_name(display: str) -> str:
        match = re.match(r"^(.*?)\s*<[^>]+>$", display)
        return (match.group(1).strip().strip('"') if match else "").strip()

    @staticmethod
    def _guess_extension(content_type: str) -> str:
        mapping = {
            "image/png": ".png",
            "image/jpeg": ".jpg",
            "image/gif": ".gif",
            "image/webp": ".webp",
            "image/bmp": ".bmp",
            "image/svg+xml": ".svg",
            "image/tiff": ".tiff",
        }
        return mapping.get(content_type, ".bin")

    @staticmethod
    def _safe_payload(part: Message) -> bytes:
        try:
            payload = part.get_payload(decode=True)
        except Exception:  # noqa: BLE001 - 畸形编码必须容错
            logger.debug("附件负载解码失败：%s", part.get_content_type(), exc_info=True)
            return b""
        if payload is None:
            raw = part.get_payload(decode=False)
            if isinstance(raw, str):
                return raw.encode("utf-8", "replace")
            return b""
        return payload

    @staticmethod
    def _serialize_part(part: Message) -> bytes:
        try:
            return part.as_bytes()
        except Exception:  # noqa: BLE001
            return b""

    def _add_attachment(
        self,
        result: ParsedMessage,
        part_index: int,
        filename: str,
        content_type: str,
        payload: bytes,
        *,
        content_id: str,
        is_inline: bool,
    ) -> None:
        safe_name = sanitize_filename(filename, max_bytes=120, fallback=f"attachment_{part_index}")
        size = len(payload)

        meta = AttachmentMeta(
            filename=safe_name,
            content_type=content_type or "application/octet-stream",
            size_bytes=size,
            content_id=content_id or None,
            is_inline=is_inline,
            part_index=part_index,
        )

        # §3.2 / §7 超大附件保护：只记录元数据
        if size > self.max_attachment_bytes:
            meta.downloaded = False
            meta.skip_reason = (
                f"超过大小阈值 {self.max_attachment_bytes // (1024 * 1024)}MB"
                f"（实际 {size / (1024 * 1024):.1f}MB）"
            )
            result.attachments.append(meta)
            logger.info("跳过超大附件 %s（%.1fMB）", safe_name, size / (1024 * 1024))
            return

        if not self.download_attachments:
            meta.downloaded = False
            meta.skip_reason = "配置为不下载附件"
            result.attachments.append(meta)
            return

        if size == 0:
            meta.downloaded = False
            meta.skip_reason = "附件内容为空"
            result.attachments.append(meta)
            return

        result.attachments.append(meta)
        result.attachment_payloads[part_index] = payload


def parse_message_size(message: Message) -> int:
    """估算邮件序列化后大小（用于没有 RFC822.SIZE 的场景）。"""
    try:
        return len(message.as_bytes())
    except Exception:  # noqa: BLE001
        return 0
