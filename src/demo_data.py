"""演示数据生成（供 ``python main.py demo`` 使用）。

把生成逻辑放进包内而不是仅放在 ``scripts/``，是为了让**打包后的应用**
也能自证可用——安装包不包含 scripts 目录，用户装完却无邮箱可连时，
需要一条命令就能看到完整效果。
"""

from __future__ import annotations

import email
import logging
import random
from datetime import datetime, timedelta, timezone
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

from .context import AppContext
from .models import FolderState

logger = logging.getLogger(__name__)

SUBJECTS: list[tuple[str, str]] = [
    ("季度报销发票汇总", "本季度差旅报销发票已整理完毕，请财务审核。共计 12 张发票，合计金额 8600 元。"),
    ("服务器扩容申请", "由于业务快速增长，现申请对订单服务进行扩容，预计需要新增三台 8C16G 机器。"),
    ("年度绩效考核通知", "请各位同事在本月底前完成自评，并提交给直属主管进行复核。"),
    ("产品需求评审会议纪要", "会上确认了三条核心需求，优先级最高的是结算流程重构，预计下个迭代启动。"),
    ("差旅费报销新规", "自下月起，差旅费报销需附电子发票，纸质发票将不再受理，请提前准备。"),
    ("关于办公区网络升级的通知", "本周六将进行办公区网络设备升级，届时网络会中断约两小时。"),
    ("客户合同续签提醒", "华东区三家重点客户的合同将在下月中旬到期，请相关负责人尽快跟进续签。"),
    ("新员工入职培训安排", "下周一上午九点在三楼会议室进行新员工入职培训，请提前十分钟到场。"),
    ("系统安全漏洞修复公告", "已在昨日凌晨完成安全补丁的灰度发布，请各业务方验证核心链路是否正常。"),
    ("月度经营数据通报", "上月整体营收环比增长 8.3%，其中企业服务线贡献了主要增量。"),
]

SENDERS: list[tuple[str, str]] = [
    ("爱丽丝", "alice@corp.com"),
    ("鲍勃", "bob@corp.com"),
    ("陈晓", "chenxiao@corp.com"),
    ("丁一", "dingyi@corp.com"),
]

FOLDERS = ["INBOX", "INBOX", "INBOX", "Sent", "Archive", "INBOX/财务"]


def build_demo_eml(
    subject: str,
    body: str,
    sender_name: str,
    sender_addr: str,
    date: datetime,
    message_id: str,
    attachment: tuple[str, bytes, str] | None = None,
) -> bytes:
    msg: MIMEMultipart = MIMEMultipart("mixed")
    msg.attach(MIMEText(body, "plain", "utf-8"))
    msg.attach(MIMEText(f"<p>{body}</p>", "html", "utf-8"))

    msg["Subject"] = subject
    msg["From"] = f"{sender_name} <{sender_addr}>"
    msg["To"] = "me@corp.com"
    msg["Date"] = date.strftime("%a, %d %b %Y %H:%M:%S +0000")
    msg["Message-ID"] = f"<{message_id}>"

    if attachment:
        name, payload, mime = attachment
        maintype, _, subtype = mime.partition("/")
        part = MIMEBase(maintype, subtype)
        part.set_payload(payload)
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", "attachment", filename=name)
        msg.attach(part)

    return msg.as_bytes()


def reset_archive(context: AppContext) -> None:
    """清空数据库与向量库（不动磁盘上的 Markdown 文件）。"""
    with context.db.transaction() as conn:
        for table in ("kb_vectors", "kb_chunks", "attachments", "messages",
                      "sync_log", "folders"):
            conn.execute(f"DELETE FROM {table}")
    context.db.rebuild_fts()
    if context._vector_store is not None:
        context._vector_store.reset()


def generate_demo_data(
    context: AppContext,
    *,
    count: int = 12,
    reset: bool = False,
    seed: int = 20240301,
) -> dict[str, Any]:
    """写入演示邮件并建立索引。"""
    from .mail_parser import MailParser

    config = context.config
    account = config.email.address or "demo"

    if reset:
        reset_archive(context)

    parser = MailParser()
    rng = random.Random(seed)
    base_date = datetime(2024, 3, 1, 9, 0, tzinfo=timezone.utc)
    created = 0

    for index in range(count):
        subject, body = SUBJECTS[index % len(SUBJECTS)]
        if index >= len(SUBJECTS):
            subject = f"{subject}（第 {index // len(SUBJECTS) + 1} 期）"
        sender_name, sender_addr = SENDERS[index % len(SENDERS)]
        folder = FOLDERS[index % len(FOLDERS)]
        date = base_date + timedelta(hours=index * 7, minutes=rng.randint(0, 59))
        message_id = f"demo-{index + 1}@corp.com"

        attachment = None
        if index % 4 == 2:
            attachment = (f"{subject[:8]}.txt", (body * 3).encode("utf-8"), "text/plain")

        raw = build_demo_eml(subject, body, sender_name, sender_addr, date,
                            message_id, attachment)
        parsed = parser.parse(
            email.message_from_bytes(raw),
            folder=folder,
            uid=str(index + 1),
            uidvalidity=1,
            size_bytes=len(raw),
        )
        archive = context.sync.exporter.export(parsed, account=account)
        record = context.sync._to_record(parsed, archive, duplicate_of=None)
        context.db.insert_message(record, archive.attachments)
        context.db.upsert_folder(
            FolderState(account=account, name=folder, uidvalidity=1, last_uid=index + 1)
        )
        context.db.update_folder_state(account, folder, last_uid=index + 1, uidvalidity=1)
        created += 1

    stats = context.indexer.index_pending()
    return {"created": created, "index": stats.to_dict(), "archive": str(config.archive_path)}
