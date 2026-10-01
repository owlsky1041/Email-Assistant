"""内容寻址的附件仓库（blob store）。

为什么单独做一层
----------------
附件占了归档体积的 99%，而"人工可读的目录树"和"内容的唯一性"是两件
互相拉扯的事：

* 用户要能直接翻目录看到 ``attachments/技术附件.pdf``；
* 程序要能保证同一份内容只存一次，并且随时能校验、能重建。

于是分两层：``blobs/<sha前2>/<sha次2>/<sha256>`` 是**权威副本**，
目录树里的附件是它的**硬链接**（跨卷/不支持硬链接时退化为复制）。
两层共享同一个 inode，因此不会多占磁盘。

关于"派生数据"的定位
--------------------
blob 目录里的每一个字节在归档目录里都存在（作为硬链接），所以它是
**可重建**的：删掉它不影响任何数据，``migrate-blobs`` 能从归档目录重建。
正因如此，``backup`` 不需要打包它 —— 备份归档目录就等于备份了全部内容。
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator
from uuid import uuid4

logger = logging.getLogger(__name__)

#: 分片层级：blobs/ab/cd/<sha> —— 避免单目录塞进几十万个文件
SHARD_DEPTH = 2
SHARD_WIDTH = 2

_CHUNK = 1024 * 1024


def normalize_digest(digest: str) -> str:
    """统一成小写十六进制，非法的直接拒绝（防目录穿越）。"""
    value = (digest or "").strip().lower()
    if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ValueError(f"不是合法的 sha256：{digest!r}")
    return value


def sha256_file(path: Path) -> str:
    """流式计算文件哈希（大附件不能一次性读进内存）。"""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(_CHUNK)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


@dataclass(frozen=True)
class BlobStats:
    """blob 仓库的体检结果。"""

    total: int = 0
    missing: int = 0
    corrupted: int = 0
    bytes_total: int = 0

    @property
    def ok(self) -> bool:
        return self.missing == 0 and self.corrupted == 0

    def describe(self) -> str:
        parts = [f"{self.total} 个引用", f"{self.bytes_total / 1048576:.1f} MB"]
        if self.missing:
            parts.append(f"{self.missing} 个文件缺失")
        if self.corrupted:
            parts.append(f"{self.corrupted} 个内容损坏")
        return "、".join(parts) + ("（校验通过）" if self.ok else "（需要修复）")


class BlobStore:
    """按内容哈希存取附件。

    :param root: ``blobs`` 根目录。
    :param mode: 落盘权限（POSIX）。
    """

    def __init__(self, root: str | Path, *, mode: int = 0o600) -> None:
        self.root = Path(root)
        self.mode = mode

    # ---- 路径 ----

    def path_for(self, digest: str) -> Path:
        value = normalize_digest(digest)
        parts = [
            value[i * SHARD_WIDTH : (i + 1) * SHARD_WIDTH]
            for i in range(SHARD_DEPTH)
        ]
        return self.root.joinpath(*parts, value)

    def has(self, digest: str) -> bool:
        try:
            return self.path_for(digest).is_file()
        except ValueError:
            return False

    def iter_blobs(self) -> Iterator[Path]:
        if not self.root.is_dir():
            return
        for path in self.root.rglob("*"):
            if path.is_file():
                yield path

    # ---- 写入 ----

    def put_bytes(self, payload: bytes, *, digest: str | None = None) -> Path:
        """写入内容，返回 blob 路径。已存在则直接复用（幂等）。"""
        value = digest or hashlib.sha256(payload).hexdigest()
        target = self.path_for(value)
        if target.is_file() and target.stat().st_size == len(payload):
            return target

        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(
            f".{target.name}.{os.getpid()}.{threading.get_ident()}.{uuid4().hex[:8]}.tmp"
        )
        try:
            with open(tmp, "wb") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            if os.name != "nt":
                os.chmod(tmp, self.mode)
            # **不能**无条件 os.replace：并发写同一份内容时，两个线程各有
            # 自己的临时文件，后到的 replace 会把先到者的 blob 换掉。此时
            # 已经硬链接到旧 blob 的附件就与后来的附件指向不同 inode ——
            # 去重静默失效，而且磁盘占用翻倍。
            # 先用 link 做"存在就不覆盖"的原子落位，失败再退回 replace。
            try:
                os.link(tmp, target)
            except FileExistsError:
                pass  # 别的线程已经写好同样的内容，直接复用它
            except OSError:
                os.replace(tmp, target)  # 文件系统不支持硬链接
        except OSError:
            _safe_unlink(tmp)
            raise
        finally:
            _safe_unlink(tmp)
        return target

    def adopt(self, source: Path, *, digest: str | None = None) -> Path:
        """把已有文件**硬链接**进仓库（不搬运、不删除原文件）。

        用于给历史归档补建 blob：即使中途失败，原文件也毫发无损。
        """
        value = digest or sha256_file(source)
        target = self.path_for(value)
        if target.is_file():
            return target
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(source, target)
            return target
        except OSError:
            pass  # 跨卷等，退化为复制
        tmp = target.with_name(
            f".{target.name}.{os.getpid()}.{threading.get_ident()}.{uuid4().hex[:8]}.tmp"
        )
        try:
            shutil.copy2(source, tmp)
            try:
                os.link(tmp, target)
            except FileExistsError:
                pass
            except OSError:
                os.replace(tmp, target)
        except OSError:
            _safe_unlink(tmp)
            raise
        finally:
            _safe_unlink(tmp)
        return target

    # ---- 读取与校验 ----

    def link_into(self, digest: str, destination: Path) -> Path | None:
        """把 blob 链接/复制到目标路径（目标必须尚不存在）。"""
        source = self.path_for(digest)
        if not source.is_file():
            return None
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(source, destination)
            return destination
        except FileExistsError:
            return None
        except OSError:
            pass
        try:
            shutil.copy2(source, destination)
            return destination
        except OSError as exc:
            logger.debug("从 blob 恢复失败：%s", exc)
            return None

    def verify(self, digest: str, *, deep: bool = True) -> bool:
        """校验某个 blob：存在，且（``deep`` 时）内容哈希对得上。"""
        try:
            path = self.path_for(digest)
        except ValueError:
            return False
        if not path.is_file():
            return False
        if not deep:
            return True
        return sha256_file(path) == normalize_digest(digest)

    def stats(self) -> BlobStats:
        total = size = 0
        for path in self.iter_blobs():
            total += 1
            try:
                size += path.stat().st_size
            except OSError:
                continue
        return BlobStats(total=total, bytes_total=size)


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
