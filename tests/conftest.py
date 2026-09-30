"""pytest 公共夹具。

核心是一个 **可编程的假 IMAP 客户端**，用来在没有真实邮箱的情况下
验证增量同步、去重、水位推进、超大附件保护与全量比对等关键行为。
"""

from __future__ import annotations

import email
import sys
from datetime import datetime, timezone
from email import encoders
from email.message import Message
from email.mime.base import MIMEBase
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config import AppConfig, load_config_from_mapping  # noqa: E402
from src.context import AppContext  # noqa: E402
from src.imap_client import FolderInfoLite, PartInfo  # noqa: E402
from src.models import utcnow  # noqa: E402


# ---------------------------------------------------------------------------
# 基础夹具
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_config(tmp_path: Path) -> AppConfig:
    """完全隔离的临时配置（不触碰真实 data/ 目录）。"""
    config = load_config_from_mapping(
        {
            "email": {"address": "tester@corp.com", "auth_code_env": "EA_TEST_AUTH_CODE"},
            "storage": {
                "archive_dir": str(tmp_path / "archive"),
                "attachment_dir": str(tmp_path / "attachments"),
                "sqlite_path": str(tmp_path / "mail.db"),
                "chroma_dir": str(tmp_path / "chroma"),
                "backup_dir": str(tmp_path / "backups"),
                "per_account_subdir": True,
            },
            "log": {"dir": str(tmp_path / "logs"), "console": False, "level": "WARNING"},
            "embedding": {
                "backend": "hashing",
                "dimension": 128,
                "chunk_size": 120,
                "chunk_overlap": 30,
                "batch_size": 8,
            },
            "vector": {"backend": "sqlite-bruteforce"},
            "sync": {"fetch_batch_size": 10, "full_scan_interval_hours": 24},
            "api": {"token": "test-token-abcdefghijklmnop", "host": "127.0.0.1", "port": 8990},
        },
        create_dirs=True,
    )
    # 关键：把密钥库指向临时目录。
    # 否则 SecretStore 会回退到项目根的 config/，让测试互相污染
    # 用户真实的授权码存储。
    config.source_path = tmp_path / "config" / "config.yaml"
    config.source_path.parent.mkdir(parents=True, exist_ok=True)
    return config


@pytest.fixture
def context(tmp_config: AppConfig) -> AppContext:
    ctx = AppContext(tmp_config, configure_logging=True)
    try:
        yield ctx
    finally:
        ctx.close()


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """确保测试环境不读取宿主机上真实的授权码 / API token。"""
    monkeypatch.setenv("EA_TEST_AUTH_CODE", "test-auth-code-0123456789")
    monkeypatch.delenv("EMAIL_ASSISTANT_AUTH_CODE", raising=False)
    monkeypatch.delenv("EMAIL_ASSISTANT_API_TOKEN", raising=False)


# ---------------------------------------------------------------------------
# 造信工具
# ---------------------------------------------------------------------------

def build_eml(
    *,
    subject: str = "测试主题",
    sender: str = "爱丽丝 <alice@corp.com>",
    to: str = "tester@corp.com",
    date: datetime | None = None,
    text: str = "这是纯文本正文。",
    html: str | None = None,
    message_id: str = "<msg-1@corp.com>",
    attachments: list[tuple[str, bytes, str]] | None = None,
    inline_images: list[tuple[str, bytes, str]] | None = None,
    charset: str = "utf-8",
) -> bytes:
    """构造一封真实结构的 MIME 邮件字节串。

    :param attachments: ``[(文件名, 内容, mime类型)]``
    :param inline_images: ``[(content-id, 内容, mime类型)]``
    """
    date = date or datetime(2024, 3, 1, 10, 0, tzinfo=timezone.utc)

    if html is not None or attachments or inline_images:
        msg: Message = MIMEMultipart("mixed")
        if html is not None:
            alternative = MIMEMultipart("alternative")
            alternative.attach(MIMEText(text, "plain", charset))
            alternative.attach(MIMEText(html, "html", charset))
            msg.attach(alternative)
        else:
            msg.attach(MIMEText(text, "plain", charset))
    else:
        msg = MIMEText(text, "plain", charset)

    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to
    msg["Date"] = date.strftime("%a, %d %b %Y %H:%M:%S +0000")
    msg["Message-ID"] = message_id

    for cid, payload, mime_type in inline_images or []:
        _, _, subtype = mime_type.partition("/")
        part = MIMEImage(payload, _subtype=subtype or "png")
        part.add_header("Content-ID", f"<{cid}>")
        part.add_header("Content-Disposition", "inline", filename=f"{cid}.png")
        msg.attach(part)

    for filename, payload, mime_type in attachments or []:
        maintype, _, subtype = mime_type.partition("/")
        part = MIMEBase(maintype or "application", subtype or "octet-stream")
        part.set_payload(payload)
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", "attachment", filename=filename)
        msg.attach(part)

    return msg.as_bytes()


def parse_eml(raw: bytes) -> Message:
    return email.message_from_bytes(raw)


# ---------------------------------------------------------------------------
# 假 IMAP 客户端
# ---------------------------------------------------------------------------

class FakeMailMessage:
    """模拟 imap-tools 的 ``MailMessage``（只需要 ``.obj`` / ``.size`` / ``.uid``）。"""

    __slots__ = ("obj", "size", "uid", "headers")

    def __init__(self, obj: Message, uid: str, size: int) -> None:
        self.obj = obj
        self.size = size
        self.uid = uid
        self.headers = obj


class FakeImapClient:
    """可编程的假 IMAP 服务端。

    用 ``folders`` 描述邮箱内容：``{"INBOX": {uid: 邮件字节串}}``。
    """

    def __init__(
        self,
        folders: dict[str, dict[str, bytes]],
        *,
        uidvalidity: int = 1,
        uidvalidity_by_folder: dict[str, int] | None = None,
        part_sizes: dict[str, list[PartInfo]] | None = None,
        fail_uids: set[str] | None = None,
        max_attachment_bytes: int = 50 * 1024 * 1024,
    ) -> None:
        self.folders = folders
        self.uidvalidity = uidvalidity
        self.uidvalidity_by_folder = uidvalidity_by_folder or {}
        self.part_sizes = part_sizes or {}
        self.fail_uids = fail_uids or set()
        self.max_attachment_bytes = max_attachment_bytes
        self.current: str | None = None
        self.calls: list[tuple[str, Any]] = []
        self.connected = False

    # -- 生命周期 --

    def connect(self) -> "FakeImapClient":
        self.connected = True
        return self

    def disconnect(self) -> None:
        self.connected = False

    def __enter__(self) -> "FakeImapClient":
        return self.connect()

    def __exit__(self, *exc: Any) -> bool:
        self.disconnect()
        return False

    # -- 文件夹 --

    def list_folders(self) -> list[FolderInfoLite]:
        return [
            FolderInfoLite(name=name, delimiter="/", selectable=True, flags=())
            for name in self.folders
        ]

    def select_folder(self, folder: str) -> dict[str, int]:
        if folder not in self.folders:
            raise RuntimeError(f"文件夹不存在：{folder}")
        self.current = folder
        self.calls.append(("select", folder))
        return {
            "UIDVALIDITY": self.uidvalidity_by_folder.get(folder, self.uidvalidity),
            "UIDNEXT": max((int(u) for u in self.folders[folder]), default=0) + 1,
            "MESSAGES": len(self.folders[folder]),
        }

    # -- 检索 --

    def search_uids(
        self, criteria: str = "ALL", *, folder: str | None = None, _record: bool = True
    ) -> list[str]:
        target = folder or self.current
        assert target is not None
        if _record:
            self.calls.append(("search", criteria))
        return sorted(self.folders[target], key=int)

    def search_uids_since(self, last_uid: int, *, folder: str) -> list[str]:
        self.calls.append(("search_since", last_uid))
        all_uids = self.search_uids("ALL", folder=folder, _record=False)
        return [u for u in all_uids if int(u) > last_uid]

    # -- 预检 --

    def fetch_sizes(self, uids: list[str], *, folder: str) -> dict[str, int]:
        data = self.folders[folder]
        return {u: len(data[u]) for u in uids if u in data}

    def fetch_bodystructure(self, uid: str, *, folder: str) -> list[PartInfo]:
        return self.part_sizes.get(f"{folder}:{uid}", [])

    def plan_fetch(self, uid: str, *, folder: str, size: int = 0):
        from src.imap_client import MessageFetchPlan

        plan = MessageFetchPlan(uid=str(uid), total_size=size)
        if size and size <= self.max_attachment_bytes:
            plan.full_download = True
            return plan
        parts = self.fetch_bodystructure(uid, folder=folder)
        plan.parts = parts
        plan.oversize_parts = [p for p in parts if p.size > self.max_attachment_bytes]
        plan.full_download = not plan.oversize_parts and bool(
            size and size <= self.max_attachment_bytes
        )
        return plan

    def text_sections(self, parts: list[PartInfo]) -> list[str]:
        return [p.section for p in parts if p.is_text][:4]

    # -- 拉取 --

    def iter_messages(
        self,
        uids: list[str],
        *,
        folder: str,
        headers_only: bool = False,
        mark_seen: bool = False,
    ):
        target = folder or self.current
        assert target is not None
        data = self.folders[target]
        for uid in uids:
            if f"{target}:{uid}" in self.fail_uids or uid in self.fail_uids:
                raise RuntimeError(f"模拟拉取失败 uid={uid}")
            raw = data.get(uid)
            if raw is None:
                continue
            obj = parse_eml(raw)
            if headers_only:
                header_msg = email.message.Message()
                for key, value in obj.items():
                    header_msg[key] = value
                yield FakeMailMessage(header_msg, uid, len(raw))
            else:
                self.calls.append(("fetch", (target, uid)))
                yield FakeMailMessage(obj, uid, len(raw))

    def fetch_one(self, uid: str, *, folder: str) -> FakeMailMessage | None:
        for message in self.iter_messages([uid], folder=folder):
            return message
        return None

    def fetch_text_parts(self, uid: str, sections: list[str], *, folder: str) -> list[bytes]:
        raw = self.folders[folder].get(uid)
        if raw is None:
            return []
        obj = parse_eml(raw)
        out: list[bytes] = []
        for index, part in enumerate(
            [p for p in obj.walk() if not p.is_multipart()], start=1
        ):
            if str(index) in sections:
                mime_headers = b"".join(
                    f"{k}: {v}\r\n".encode() for k, v in part.items()
                )
                out.append(mime_headers + b"\r\n" + part.get_payload(decode=False).encode()
                           if isinstance(part.get_payload(decode=False), str)
                           else mime_headers + b"\r\n" + (part.get_payload(decode=True) or b""))
        return out

    def ping(self) -> bool:
        return True

    def capabilities(self) -> list[str]:
        return ["IMAP4REV1", "UIDPLUS", "MOVE"]


@pytest.fixture
def fake_mailbox() -> dict[str, dict[str, bytes]]:
    """一个包含 3 封邮件的收件箱 + 1 封已发送邮件。"""
    return {
        "INBOX": {
            "1": build_eml(
                subject="三月报销发票汇总",
                text="本季度差旅报销发票已整理完毕，请财务审核。共计 12 张发票。",
                message_id="<inbox-1@corp.com>",
            ),
            "2": build_eml(
                subject="季度财报（含附件）",
                text="详细数据见附件。",
                message_id="<inbox-2@corp.com>",
                attachments=[("财报Q1.xlsx", b"PK\x03\x04fake-xlsx-content" * 10,
                              "application/vnd.ms-excel")],
            ),
            "3": build_eml(
                subject="Re: 服务器扩容申请",
                text="同意扩容，请提交具体预算。",
                message_id="<inbox-3@corp.com>",
            ),
        },
        "Sent": {
            "10": build_eml(
                subject="回复：年度绩效考核",
                sender="tester@corp.com",
                to="hr@corp.com",
                text="已收到，本周内完成自评。",
                message_id="<sent-10@corp.com>",
            )
        },
    }
