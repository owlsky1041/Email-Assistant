"""给已有归档补建/校验内容寻址仓库。

为什么单独成模块
----------------
blob 是**派生数据**：归档目录里已经有全部内容（blob 只是同一 inode 的
另一个名字）。所以这两个操作的设计前提是**只增不删**：

* ``migrate`` 只把已有文件硬链接进 blobs，从不移动、从不删除原文件；
  中途断电也最多是少建几个 blob，重跑即可。
* ``verify`` 只读，用来发现"数据库说有、磁盘上没了"的附件 ——
  这是单纯看数据库永远发现不了的问题。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from .blob_store import BlobStore, sha256_file
from .config import AppConfig

logger = logging.getLogger(__name__)


@dataclass
class MigrateStats:
    scanned: int = 0
    adopted: int = 0
    already: int = 0
    missing: int = 0
    failed: int = 0
    bytes_adopted: int = 0
    #: 内容与 blob 相同、但仍占独立 inode 的文件数（可用 --relink 回收）
    relinkable: int = 0
    relinked: int = 0
    bytes_reclaimed: int = 0
    errors: list[str] = field(default_factory=list)

    def describe(self) -> str:
        parts = [
            f"扫描 {self.scanned} 条",
            f"新建 blob {self.adopted} 个（{self.bytes_adopted / 1048576:.1f} MB）",
            f"已存在 {self.already} 个",
        ]
        if self.relinked:
            parts.append(
                f"已合并重复 {self.relinked} 个（回收 {self.bytes_reclaimed / 1048576:.1f} MB）"
            )
        elif self.relinkable:
            parts.append(f"可合并重复 {self.relinkable} 个（加 --relink 回收）")
        if self.missing:
            parts.append(f"文件缺失 {self.missing} 个")
        if self.failed:
            parts.append(f"失败 {self.failed} 个")
        return "、".join(parts)


def _relink_to_blob(store: BlobStore, path: Path, digest: str) -> bool:
    """把一份内容相同但独立占盘的附件换成指向 blob 的硬链接。

    先在同目录建好新链接再 ``os.replace`` 原子替换，因此路径**中途不会消失**；
    任何一步失败都保留原文件。
    """
    try:
        blob = store.path_for(digest)
    except ValueError:
        return False
    if not blob.is_file():
        return False
    try:
        if path.stat().st_ino == blob.stat().st_ino:
            return False  # 已经是同一条链接
    except OSError:
        return False

    # 换掉之前先确认内容真的一致，避免拿错误的 blob 顶替
    try:
        if sha256_file(path) != digest:
            return False
    except OSError:
        return False

    tmp = path.with_name(f".{path.name}.relink.{os.getpid()}.{uuid4().hex[:8]}.tmp")
    try:
        os.link(blob, tmp)
        os.replace(tmp, path)
        return True
    except OSError as exc:
        logger.debug("合并重复附件失败 %s：%s", path, exc)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def migrate_blobs(
    config: AppConfig,
    db,  # noqa: ANN001 - Database，避免循环导入
    *,
    limit: int = 0,
    relink: bool = False,
    on_progress=None,  # noqa: ANN001
) -> MigrateStats:
    """把已有附件补建进 blob 仓库。

    :param limit: 只处理前 N 条（0 = 全部），便于分批观察。
    :param relink: 把"内容相同但仍各占一份 inode"的附件合并成硬链接。
        默认关闭 —— 它确实会改动已有文件，虽然用的是原子替换且有内容校验。
    """
    store = BlobStore(config.blob_path, mode=config.storage.file_mode)
    stats = MigrateStats()

    sql = (
        "SELECT a.id, a.local_path, a.sha256, a.size_bytes "
        "FROM attachments a WHERE a.downloaded = 1 AND a.local_path IS NOT NULL "
        "ORDER BY a.id"
    )
    if limit:
        sql += f" LIMIT {int(limit)}"

    for row in db.query(sql):
        stats.scanned += 1
        path = Path(row["local_path"])

        if not path.is_file():
            # 绝不假装成功：文件没了就是没了，如实计数并留下路径
            stats.missing += 1
            if len(stats.errors) < 20:
                stats.errors.append(f"文件缺失：{path}")
            continue

        digest = row["sha256"]
        if not digest:
            try:
                digest = sha256_file(path)
            except OSError as exc:
                stats.failed += 1
                stats.errors.append(f"计算哈希失败 {path}：{exc}")
                continue

        if store.has(digest):
            stats.already += 1
            blob_path = store.path_for(digest)
            try:
                same_inode = path.stat().st_ino == blob_path.stat().st_ino
            except OSError:
                same_inode = True  # 读不到就别动它
            if not same_inode:
                if relink and _relink_to_blob(store, path, digest):
                    stats.relinked += 1
                    stats.bytes_reclaimed += int(row["size_bytes"] or 0)
                else:
                    stats.relinkable += 1
            continue

        try:
            store.adopt(path, digest=digest)
        except (OSError, ValueError) as exc:
            stats.failed += 1
            stats.errors.append(f"建立 blob 失败 {path}：{exc}")
            continue

        stats.adopted += 1
        stats.bytes_adopted += int(row["size_bytes"] or 0)
        if on_progress is not None and stats.adopted % 50 == 0:
            on_progress(stats)

    if on_progress is not None:
        on_progress(stats)
    return stats


@dataclass
class VerifyStats:
    checked: int = 0
    ok: int = 0
    missing_file: int = 0
    missing_blob: int = 0
    corrupted: int = 0
    repaired: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def healthy(self) -> bool:
        """只看**真实的数据完整性**。

        ``missing_blob`` 不算不健康：那是"还没迁移进 blob 仓库"的提示，
        文件本身好好的。把它算成损坏会让所有老归档一上来就报红。
        """
        return not (self.missing_file or self.corrupted)

    def describe(self) -> str:
        parts = [f"校验 {self.checked} 条", f"完好 {self.ok} 条"]
        for label, value in (
            ("归档文件缺失", self.missing_file),
            ("内容损坏", self.corrupted),
            ("已从 blob 修复", self.repaired),
            ("尚未纳入 blob", self.missing_blob),
        ):
            if value:
                parts.append(f"{label} {value} 条")
        return "、".join(parts)


def verify_blobs(
    config: AppConfig,
    db,  # noqa: ANN001
    *,
    deep: bool = True,
    repair: bool = False,
    limit: int = 0,
    on_progress=None,  # noqa: ANN001
) -> VerifyStats:
    """校验归档完整性，可选就地修复。

    :param deep: 逐字节重算 sha256（慢但能发现内容被改动）。
    :param repair: 归档文件缺失但 blob 在时，从 blob 重建硬链接。
    """
    store = BlobStore(config.blob_path, mode=config.storage.file_mode)
    stats = VerifyStats()

    sql = (
        "SELECT a.id, a.local_path, a.sha256, a.downloaded "
        "FROM attachments a WHERE a.downloaded = 1 ORDER BY a.id"
    )
    if limit:
        sql += f" LIMIT {int(limit)}"

    for row in db.query(sql):
        stats.checked += 1
        digest = (row["sha256"] or "").lower()
        path = Path(row["local_path"]) if row["local_path"] else None

        file_ok = bool(path and path.is_file())
        blob_ok = bool(digest) and store.has(digest)

        if not file_ok:
            if repair and blob_ok and path is not None:
                if store.link_into(digest, path) is not None:
                    stats.repaired += 1
                    file_ok = True
            if not file_ok:
                # 只统计**修复之后仍然缺失**的：已经重建好的不该继续报红
                stats.missing_file += 1
                if len(stats.errors) < 20:
                    reason = (
                        "blob 里也没有，无法自动修复"
                        if not blob_ok
                        else "可加 --repair 从 blob 重建"
                    )
                    stats.errors.append(f"归档文件缺失：{path}（{reason}）")

        if digest and not blob_ok:
            stats.missing_blob += 1

        if deep and digest and file_ok and path is not None:
            try:
                actual = sha256_file(path)
            except OSError as exc:
                stats.corrupted += 1
                stats.errors.append(f"读取失败 {path}：{exc}")
                continue
            if actual != digest:
                stats.corrupted += 1
                if len(stats.errors) < 20:
                    stats.errors.append(
                        f"内容与记录不符：{path}（记录 {digest[:12]}，实际 {actual[:12]}）"
                    )
                continue

        if file_ok and (blob_ok or not digest):
            stats.ok += 1

        if on_progress is not None and stats.checked % 100 == 0:
            on_progress(stats)

    if on_progress is not None:
        on_progress(stats)
    return stats
