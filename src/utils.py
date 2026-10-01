"""通用工具：文件名安全化、原子写入、Token 估算、哈希。"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import threading
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Iterator
from uuid import uuid4

# ---------------------------------------------------------------------------
# 文件名安全化（Windows 优先）
# ---------------------------------------------------------------------------

_ILLEGAL_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WHITESPACE = re.compile(r"\s+")
_DOTS_EDGE = re.compile(r"^[.\s]+|[.\s]+$")

# Windows 保留设备名
_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

# 常见分隔符，长度预算按 UTF-8 字节算（NTFS 单段上限 255 字节）
MAX_FILENAME_BYTES = 180


def sanitize_filename(name: str, *, max_bytes: int = MAX_FILENAME_BYTES, fallback: str = "untitled") -> str:
    """把任意字符串转成跨平台安全的文件名片段。

    注意按 **UTF-8 字节数** 截断，避免中文标题超出 NTFS 255 字节上限。
    """
    if not name:
        return fallback
    text = unicodedata.normalize("NFC", str(name))
    text = _ILLEGAL_CHARS.sub("_", text)
    text = text.replace("\u3000", " ")
    text = _WHITESPACE.sub(" ", text).strip()
    text = _DOTS_EDGE.sub("", text)
    text = text.rstrip(". ")

    if not text:
        return fallback
    # Windows 保留设备名：CON / PRN / COM1 … 无论带不带扩展名都被保留
    stem = text.split(".", 1)[0].upper()
    if stem in _RESERVED_NAMES:
        text = f"_{text}"

    encoded = text.encode("utf-8")
    if len(encoded) > max_bytes:
        # 保证不会在 UTF-8 字符中间截断
        truncated = encoded[:max_bytes]
        while truncated:
            try:
                text = truncated.decode("utf-8")
                break
            except UnicodeDecodeError:
                truncated = truncated[:-1]
        else:
            text = fallback
        text = text.rstrip(". ")
    return text or fallback


def sanitize_relative_path(parts: list[str]) -> Path:
    """把 IMAP 文件夹层级转成安全相对路径，阻止 `..` 逃逸。"""
    safe: list[str] = []
    for part in parts:
        for sub in re.split(r"[/\\]", str(part)):
            sub = sub.strip()
            if not sub or sub in (".", ".."):
                continue
            cleaned = sanitize_filename(sub, max_bytes=80, fallback="folder")
            if cleaned:
                safe.append(cleaned)
    return Path(*safe) if safe else Path("INBOX")


# ---------------------------------------------------------------------------
# 时间
# ---------------------------------------------------------------------------

def format_timestamp(dt: datetime | None, fmt: str = "%Y%m%d_%H%M%S") -> str:
    if dt is None:
        return "00000000_000000"
    return dt.strftime(fmt)


def iso_or_empty(dt: datetime | None) -> str:
    return dt.isoformat() if dt else ""


# ---------------------------------------------------------------------------
# 文件写入
# ---------------------------------------------------------------------------

def atomic_write_bytes(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    """§11.2 临时文件机制：先写 ``.tmp``，再原子重命名。

    避免断网 / 崩溃留下半成品文件。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        if os.name != "nt":
            try:
                tmp.chmod(mode)
            except OSError:
                pass
        os.replace(tmp, path)
    except BaseException:
        _safe_unlink(tmp)
        raise


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8", mode: int = 0o600) -> None:
    atomic_write_bytes(path, text.encode(encoding), mode=mode)


class ChunkedFileWriter:
    """分块流式写入器（§11.2）。

    用法::

        with ChunkedFileWriter(target, expected_size=n) as w:
            for block in payload_iter:
                w.write(block)
        # 退出时校验大小并原子重命名；校验失败则删除临时文件
    """

    def __init__(
        self,
        target: Path,
        *,
        expected_size: int | None = None,
        chunk_size: int = 64 * 1024,
        mode: int = 0o600,
    ) -> None:
        self.target = Path(target)
        # 临时文件名必须**每个写入器唯一**：并发归档两个同名附件时，
        # 共享 `<name>.tmp` 会让先提交的一方把该路径 rename 走，
        # 后提交的一方直接 `os.replace` 失败（No such file or directory）。
        self.tmp = self.target.with_name(
            f".{self.target.name}.{os.getpid()}.{threading.get_ident()}"
            f".{uuid4().hex[:8]}.tmp"
        )
        self.expected_size = expected_size
        self.chunk_size = chunk_size
        self.mode = mode
        self.written = 0
        self._digest = hashlib.sha256()
        self._fh = None

    def __enter__(self) -> "ChunkedFileWriter":
        self.target.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.tmp, "wb")
        return self

    def write(self, data: bytes) -> int:
        if self._fh is None:
            raise RuntimeError("ChunkedFileWriter 未打开")
        if not data:
            return 0
        self._fh.write(data)
        self._digest.update(data)
        self.written += len(data)
        return len(data)

    def write_iter(self, blocks) -> int:  # type: ignore[no-untyped-def]
        total = 0
        for block in blocks:
            total += self.write(block)
        return total

    @property
    def sha256(self) -> str:
        return self._digest.hexdigest()

    def commit(self) -> str:
        """校验并原子重命名，返回 sha256。"""
        if self._fh is None:
            raise RuntimeError("ChunkedFileWriter 未打开")
        self._fh.flush()
        os.fsync(self._fh.fileno())
        self._fh.close()
        self._fh = None

        if self.expected_size is not None and self.written != self.expected_size:
            _safe_unlink(self.tmp)
            raise OSError(
                f"附件大小校验失败：期望 {self.expected_size} 字节，实际 {self.written} 字节"
            )
        if os.name != "nt":
            try:
                self.tmp.chmod(self.mode)
            except OSError:
                pass
        os.replace(self.tmp, self.target)
        return self.sha256

    def abort(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None
        _safe_unlink(self.tmp)

    def __exit__(self, exc_type, exc, tb) -> bool:  # type: ignore[no-untyped-def]
        if exc_type is not None:
            self.abort()
            return False
        if self._fh is not None:
            self.commit()
        return False


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk_size)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def dedupe_path(path: Path) -> Path:
    """若目标已存在，追加 ``_1``、``_2`` … 直到不冲突。

    ⚠️ 这只是**尽力而为**：``exists()`` 与后续写入之间不是原子的，
    并发归档时两个线程可能拿到同一个名字。需要排他占位请用
    :func:`claim_path` / :func:`link_or_copy_exclusive`。
    """
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    for i in range(1, 1000):
        candidate = path.with_name(f"{stem}_{i}{suffix}")
        if not candidate.exists():
            return candidate
    return path.with_name(f"{stem}_{os.getpid()}{suffix}")


def candidate_paths(path: Path, limit: int = 1000) -> Iterator[Path]:
    """依次产出 ``x``、``x_1``、``x_2`` … 供原子占位使用。"""
    stem, suffix = path.stem, path.suffix
    yield path
    for i in range(1, limit):
        yield path.with_name(f"{stem}_{i}{suffix}")


def claim_path(path: Path, *, mode: int = 0o600, limit: int = 1000) -> Path:
    """**原子地**占住一个不重名的路径并返回它。

    用 ``O_CREAT|O_EXCL`` 创建：并发线程里只有一个能成功，其余自动退到
    下一个候选名。仅靠 ``dedupe_path()`` 的 ``exists()`` 判断做不到这点 ——
    两个工作线程会同时认为名字可用，然后互相覆盖对方的附件。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    for candidate in candidate_paths(path, limit):
        try:
            fd = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
        except FileExistsError:
            continue
        os.close(fd)
        return candidate
    raise OSError(f"同名文件过多，无法占位：{path}")


def link_or_copy_exclusive(source: Path, path: Path, *, limit: int = 1000) -> Path | None:
    """把 ``source`` 用硬链接（优先）或复制放到一个不重名的路径上。

    硬链接本身是原子的：目标已存在会抛 ``FileExistsError``，并发下不会
    互相覆盖。复制没有排他语义，退化为"先查存在再写"。
    """
    for candidate in candidate_paths(path, limit):
        try:
            os.link(source, candidate)
            return candidate
        except FileExistsError:
            continue
        except OSError:
            break  # 跨卷 / 文件系统不支持硬链接

    for candidate in candidate_paths(path, limit):
        if candidate.exists():
            continue
        try:
            shutil.copy2(source, candidate)
            return candidate
        except FileExistsError:
            continue
        except OSError:
            return None
    return None


# ---------------------------------------------------------------------------
# Token 估算（§11.4 轻量化：不加载 tokenizer 也能估长度）
# ---------------------------------------------------------------------------

_CJK_TOKEN_RE = re.compile(
    r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]"
)
_LATIN_WORD_RE = re.compile(r"[A-Za-z0-9]+")


def estimate_tokens(text: str) -> int:
    """粗略估算 token 数。

    经验规则（对 bge / cl100k 系列误差约 ±15%）：
    * CJK 字符：约 1 token / 字
    * 拉丁词：约 1.3 token / 词
    * 标点与空白：按 0.25 token 计
    """
    if not text:
        return 0
    cjk = len(_CJK_TOKEN_RE.findall(text))
    words = len(_LATIN_WORD_RE.findall(text))
    other = max(len(text) - cjk - sum(len(w) for w in _LATIN_WORD_RE.findall(text)), 0)
    return int(cjk + words * 1.3 + other * 0.25) + 1


def human_size(num_bytes: int | float) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size) < 1024.0:
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024.0
    return f"{size:.1f}PB"


def optional_import_works(module: str) -> tuple[bool, str]:
    """安全探测可选依赖是否可用。

    返回 ``(是否可用, 失败原因)``。**必须捕获所有异常**：
    例如无显示环境下 ``import pystray`` 抛的是
    ``Xlib.error.DisplayNameError`` 而非 ``ImportError``。

    另外注意：探测结果应缓存，反复 import 失败代价不低。
    """
    try:
        __import__(module)
        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
