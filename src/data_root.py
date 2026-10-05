"""数据目录的定位、切换与"升级不丢数据"。

为什么单独成模块
----------------
程序有多个数据落点（归档、附件、数据库、向量库、内容仓库、模型、备份），
全都由**一个数据根目录**派生。升级时最危险的操作是"换了个数据根" ——
程序会在新位置建一个空库，界面看起来正常，但上万封邮件"消失"了。

因此这里提供两件事：

* :func:`describe_paths` —— 把每个落点列清楚，让用户知道自己的数据在哪；
* :func:`adopt_data_root` —— 把配置指向**已经存在**的数据根，
  只改配置、不搬文件，因此是安全且可回滚的。

搬文件（真正的迁移）走 ``settings_service.migrate_data``，
它在界面上有按钮；这里不重复实现。
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import AppConfig, is_frozen

logger = logging.getLogger(__name__)

#: 数据根目录下必须存在的文件（用来判断"这确实是一个已有归档"）
_REQUIRED = ("sqlite/mail.db",)
#: 这些子目录任一存在也算"像是有数据"
_HINTS = ("mail_archive", "attachments", "chromadb", "blobs")


@dataclass(frozen=True)
class PathReport:
    root: Path
    entries: list[tuple[str, Path, bool]]
    has_database: bool
    message_count: int = 0

    def render(self) -> str:
        lines = [f"数据根目录：{self.root}", ""]
        for label, path, exists in self.entries:
            mark = "✓" if exists else "·"
            lines.append(f"  {mark} {label:<10} {path}")
        lines.append("")
        if self.has_database:
            lines.append(f"数据库中的邮件：{self.message_count} 封")
        else:
            lines.append("该目录下还没有数据库（尚未同步过，或数据在别处）")
        return "\n".join(lines)


def describe_paths(config: AppConfig, *, probe: bool = True) -> PathReport:
    """列出所有数据落点及存在情况。"""
    entries = [
        ("邮件归档", config.archive_path, config.archive_path.is_dir()),
        ("附件", config.attachment_path, config.attachment_path.is_dir()),
        ("数据库", config.sqlite_file, config.sqlite_file.is_file()),
        ("向量库", config.chroma_path, config.chroma_path.is_dir()),
        ("内容仓库", config.blob_path, config.blob_path.is_dir()),
        ("嵌入模型", config.model_path, config.model_path.is_dir()),
        ("备份", config.backup_path, config.backup_path.is_dir()),
        ("日志", config.log_path, config.log_path.is_dir()),
    ]
    count = 0
    if config.sqlite_file.is_file():
        try:
            import sqlite3

            conn = sqlite3.connect(f"file:{config.sqlite_file}?mode=ro", uri=True)
            try:
                row = conn.execute(
                    "SELECT COUNT(*) FROM messages WHERE deleted_at IS NULL"
                ).fetchone()
                count = int(row[0]) if row else 0
            finally:
                conn.close()
        except Exception:  # noqa: BLE001 - 库损坏时不该让命令挂掉
            logger.debug("统计邮件数失败", exc_info=True)
    return PathReport(
        root=_common_root(config),
        entries=entries,
        has_database=config.sqlite_file.is_file(),
        message_count=count,
    )


def _common_root(config: AppConfig) -> Path:
    """由归档目录反推数据根（与设置界面里的推断保持一致）。"""
    return config.archive_path.parent


def looks_like_archive(root: Path) -> bool:
    """这个目录像不像"已经同步过数据"的根目录。"""
    for rel in _REQUIRED:
        if (root / rel).is_file():
            return True
    return any((root / name).is_dir() for name in _HINTS)


def candidate_roots(config: AppConfig) -> list[Path]:
    """可能装着已有数据的目录（按可能性排序）。

    覆盖三种真实的升级场景：

    1. 绿色版解压到了新目录 —— 旧目录还在原处（无法自动知道，需用户指定）；
    2. 装进 ``Program Files`` 后又换回绿色版 —— 数据在用户数据目录；
    3. 绿色版升级成安装版 —— 数据在**旧的可执行文件旁边**。
    """
    out: list[Path] = []
    active = _common_root(config).resolve()

    if is_frozen():
        exe_dir = Path(sys.executable).resolve().parent
        out.append(exe_dir / "data")
        out.append(exe_dir)

    # 平台用户数据目录
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if base:
            out.append(Path(base) / "EmailAssistant" / "data")
            out.append(Path(base) / "EmailAssistant")
        base2 = os.environ.get("APPDATA")
        if base2:
            out.append(Path(base2) / "EmailAssistant" / "data")
    elif sys.platform == "darwin":
        out.append(Path.home() / "Library/Application Support/EmailAssistant")
    else:
        xdg = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local/share")
        out.append(Path(xdg) / "EmailAssistant")

    seen: set[Path] = set()
    result: list[Path] = []
    for path in out:
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved == active or resolved in seen:
            continue
        seen.add(resolved)
        if looks_like_archive(resolved):
            result.append(resolved)
    return result


@dataclass
class AdoptResult:
    ok: bool
    root: Path
    message_count: int = 0
    error: str = ""


def adopt_data_root(config: AppConfig, new_root: str | Path) -> AdoptResult:
    """把配置指向一个**已存在**的数据根（不搬文件、不删文件）。

    这是升级时最安全的做法：只改 ``storage.*`` 路径，数据原地不动；
    如果选错了，改回来即可，没有任何不可逆操作。
    """
    from .config_writer import update_config

    root = Path(new_root).expanduser().resolve()
    if not root.exists():
        return AdoptResult(False, root, error=f"目录不存在：{root}")
    if not looks_like_archive(root):
        return AdoptResult(
            False,
            root,
            error=(
                f"{root} 看起来不是数据目录（既没有 sqlite/mail.db，"
                f"也没有 {'/'.join(_HINTS)} 之类的子目录）"
            ),
        )

    patch = {
        "storage": {
            "archive_dir": str(root / "mail_archive"),
            "attachment_dir": str(root / "attachments"),
            "sqlite_path": str(root / "sqlite" / "mail.db"),
            "chroma_dir": str(root / "chromadb"),
            "backup_dir": str(root / "backups"),
            "blob_dir": str(root / "blobs"),
        },
        "embedding": {"model_dir": str(root / "models")},
    }
    target = config.source_path
    update_config(patch, target)

    # 重新加载以拿到真实邮件数（证明"确实接上了已有数据"）
    try:
        from .config import load_config

        fresh = load_config(target)
        report = describe_paths(fresh)
        return AdoptResult(True, root, message_count=report.message_count)
    except Exception as exc:  # noqa: BLE001 - 配置已写入，读不回来只影响回显
        logger.debug("采用数据目录后重新加载失败", exc_info=True)
        return AdoptResult(True, root, error=f"（配置已写入，但重新加载失败：{exc}）")
