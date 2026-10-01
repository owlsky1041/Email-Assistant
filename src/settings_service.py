"""设置服务：读取、校验并写回配置，以及连接测试。

安全要点
--------
* **授权码永不返回**。``read()`` 只报告"是否已配置"与来源，绝不带出明文。
* 所有写入都经过 pydantic 校验后才落盘，避免把配置写成程序起不来的状态。
* 写配置走 :func:`src.config_writer.update_config`，保留 YAML 注释。
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

from .config import AppConfig, resolve_auth_code
from .config_writer import update_config
from .secret_store import SecretStore, SecretStoreError

logger = logging.getLogger(__name__)

#: 源码树根（main.py 所在目录）。**不要**用 config.PROJECT_ROOT：
#: 那个值会随 EMAIL_ASSISTANT_HOME 变化，用来定位源码文件必然出错。
SOURCE_ROOT = Path(__file__).resolve().parent.parent

#: 允许通过设置界面修改的字段白名单。
#: 明确列出而不是"整份配置随便改"，避免界面误改到不该碰的项。
EDITABLE_SECTIONS = (
    "email", "storage", "sync", "embedding", "vector", "search", "api", "log", "tray",
)

#: 单值"数据根目录"对应的派生路径
DATA_ROOT_KEYS = {
    "archive_dir": "data/mail_archive",
    "attachment_dir": "data/attachments",
    "sqlite_path": "data/sqlite/mail.db",
    "chroma_dir": "data/chromadb",
    "backup_dir": "data/backups",
}


class SettingsError(RuntimeError):
    """设置校验或保存失败。"""


class SettingsService:
    """封装设置读写与连通性测试。"""

    #: 用户可能慢慢挑目录，给足时间；超时后再提示可手工输入
    DIALOG_TIMEOUT = 600

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        #: 目录选择对话框串行化，避免同时弹出多个
        self._dialog_lock = threading.Lock()

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------

    def read(self) -> dict[str, Any]:
        """返回给前端的配置视图（**不含任何密钥**）。"""
        c = self.config
        auth_code = resolve_auth_code(c)
        try:
            auth_backend = SecretStore(c).backend_name(c.email.auth_code_ref)
        except Exception:  # noqa: BLE001
            auth_backend = "unknown"

        return {
            "email": {
                "address": c.email.address,
                "imap_server": c.email.imap_server,
                "imap_port": c.email.imap_port,
                "use_ssl": c.email.use_ssl,
                "connect_timeout": c.email.connect_timeout,
                "read_timeout": c.email.read_timeout,
            },
            "auth": {
                # 只报告状态，永不返回值本身
                "configured": bool(auth_code),
                "backend": auth_backend,
                "length": len(auth_code) if auth_code else 0,
                "env_var": c.email.auth_code_env,
            },
            "storage": {
                "data_root": str(_common_root(c)),
                "archive_dir": c.storage.archive_dir,
                "attachment_dir": c.storage.attachment_dir,
                "sqlite_path": c.storage.sqlite_path,
                "chroma_dir": c.storage.chroma_dir,
                "backup_dir": c.storage.backup_dir,
                "attachment_layout": c.storage.attachment_layout,
                "resolved": {
                    "archive": str(c.archive_path),
                    "sqlite": str(c.sqlite_file),
                    "chroma": str(c.chroma_path),
                    "logs": str(c.log_path),
                },
                "exists": {
                    "archive": c.archive_path.exists(),
                    "sqlite": c.sqlite_file.exists(),
                },
                "database_size_mb": _size_mb(c.sqlite_file),
                "archive_size_mb": _dir_size_mb(c.archive_path),
            },
            "sync": {
                "interval_minutes": c.sync.interval_minutes,
                "max_attachment_size_mb": c.sync.max_attachment_size_mb,
                "folders": list(c.sync.folders),
                "exclude_folders": list(c.sync.exclude_folders),
                "fetch_batch_size": c.sync.fetch_batch_size,
                "fetch_workers": c.sync.fetch_workers,
                "full_scan_interval_hours": c.sync.full_scan_interval_hours,
                "reconcile_deletions": c.sync.reconcile_deletions,
                "dedupe_by_message_id": c.sync.dedupe_by_message_id,
                "max_messages_per_run": c.sync.max_messages_per_run,
                "download_attachments": c.sync.download_attachments,
            },
            "embedding": {
                "backend": c.embedding.backend,
                "model_dir": c.embedding.model_dir,
                "model_ready": _model_ready(c),
                "chunk_size": c.embedding.chunk_size,
                "chunk_overlap": c.embedding.chunk_overlap,
            },
            "vector": {"backend": c.vector.backend},
            "api": {
                "host": c.api.host,
                "port": c.api.port,
                "has_token": bool(c.api.token),
                "token_env": c.api.token_env,
            },
            "log": {"level": c.log.level, "dir": c.log.dir},
            "tray": {
                "notify_on_new_mail": c.tray.notify_on_new_mail,
                "click_action": c.tray.click_action,
                "webmail_url": c.tray.webmail_url,
            },
            "runtime": {
                "config_path": str(c.source_path) if c.source_path else "",
                "data_root_env": os.environ.get("EMAIL_ASSISTANT_HOME", ""),
                "frozen": _is_frozen(),
            },
        }

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------

    def write(self, patch: dict[str, Any]) -> dict[str, Any]:
        """校验并保存配置片段。返回 ``{section: [变更的字段]}``。

        ``data_root`` 是便捷键（一次性重写全部存储路径），
        必须在"未知配置段"校验**之前**摘出来，否则会被自己挡掉。
        """
        if not isinstance(patch, dict) or not patch:
            raise SettingsError("没有需要保存的内容")
        patch = dict(patch)

        data_root = patch.pop("data_root", None)
        if data_root:
            patch = _merge_patch(patch, self._expand_data_root(str(data_root)))
        if not patch:
            raise SettingsError("没有需要保存的内容")

        unknown = set(patch) - set(EDITABLE_SECTIONS)
        if unknown:
            raise SettingsError(f"不允许修改的配置段：{sorted(unknown)}")

        for section, values in patch.items():
            if not isinstance(values, dict):
                raise SettingsError(f"{section} 必须是对象")

        # 先合并到当前配置做完整校验，避免写出程序无法启动的配置
        merged = self.config.model_dump()
        merged.pop("source_path", None)
        merged = _merge_patch(merged, patch)
        try:
            AppConfig.model_validate(merged)
        except Exception as exc:  # noqa: BLE001 - pydantic 的报错对用户不友好，转成一句
            raise SettingsError(f"配置校验失败：{_first_error(exc)}") from exc

        update_config(patch, self.config.source_path)
        # 让当前进程立即生效，无需重启
        for section, values in patch.items():
            target = getattr(self.config, section)
            for key, value in values.items():
                setattr(target, key, value)

        logger.info("设置已保存：%s", sorted(patch))
        return {
            section: sorted(values) for section, values in patch.items() if isinstance(values, dict)
        }

    def write_data_root(self, root: str) -> dict[str, Any]:
        """只改数据根目录（把全部存储路径指到该目录下）。"""
        if not root or not str(root).strip():
            raise SettingsError("数据根目录不能为空")
        expanded = self._expand_data_root(str(root).strip())
        self.write(expanded)
        return expanded

    @staticmethod
    def _expand_data_root(root: str) -> dict[str, dict[str, str]]:
        base = Path(root).expanduser()
        storage = {
            key: str(base / Path(rel))
            for key, rel in DATA_ROOT_KEYS.items()
        }
        storage["log_dir"] = str(base / "logs")
        return {"storage": {k: v for k, v in storage.items() if k != "log_dir"},
                "log": {"dir": storage["log_dir"]}}

    # ------------------------------------------------------------------
    # 授权码
    # ------------------------------------------------------------------

    def set_auth_code(self, code: str) -> dict[str, Any]:
        """保存授权码（写入密钥库，绝不落配置文件）。"""
        code = (code or "").strip()
        if not code:
            raise SettingsError("授权码不能为空")
        if len(code) < 6:
            raise SettingsError("授权码长度异常，请检查是否复制完整")

        try:
            backend = SecretStore(self.config).set(self.config.email.auth_code_ref, code)
        except SecretStoreError as exc:
            raise SettingsError(str(exc)) from exc

        logger.info("授权码已更新（后端：%s）", backend)
        return {"backend": backend}

    def clear_auth_code(self) -> bool:
        return SecretStore(self.config).delete(self.config.email.auth_code_ref)

    # ------------------------------------------------------------------
    # 连通性
    # ------------------------------------------------------------------

    def test_connection(self, *, auth_code: str | None = None) -> dict[str, Any]:
        """测试 IMAP 登录，返回文件夹数量等摘要。

        :param auth_code: 界面上刚输入、尚未保存的授权码。仅用于本次测试。
        """
        from .imap_client import ImapAuthError, ImapClient, ImapError

        code = (auth_code or "").strip() or resolve_auth_code(self.config)
        if not code:
            return {"ok": False, "error": "尚未配置授权码"}
        if not self.config.email.address:
            return {"ok": False, "error": "尚未填写邮箱地址"}

        try:
            with ImapClient(self.config, code) as client:
                folders = client.list_folders()
                info = client.select_folder("INBOX") if any(
                    f.name.upper() == "INBOX" for f in folders
                ) else {}
                return {
                    "ok": True,
                    "server": self.config.email.imap_server,
                    "folder_count": len(folders),
                    "selectable": sum(1 for f in folders if f.selectable),
                    "inbox_messages": info.get("MESSAGES", 0),
                    "folders": [f.name for f in folders if f.selectable][:200],
                }
        except ImapAuthError as exc:
            return {"ok": False, "error": str(exc), "kind": "auth"}
        except ImapError as exc:
            return {"ok": False, "error": str(exc), "kind": "connection"}
        except Exception as exc:  # noqa: BLE001
            logger.exception("连接测试失败")
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "kind": "unknown"}

    # ------------------------------------------------------------------
    # 目录选择
    # ------------------------------------------------------------------

    def pick_directory(self, initial: str = "") -> dict[str, Any]:
        """弹出系统原生目录选择对话框。

        **通过独立子进程执行**：tkinter 要求对话框跑在主线程，
        而本方法由 FastAPI 的工作线程调用，在那里创建 Tk 窗口不会显示
        （表现为请求一直挂起、用户什么也看不到）。

        结果经临时文件回传，不依赖子进程的 stdout ——
        打包成窗口版 exe（``console=False``）时 stdout 可能不可用。
        """
        from .config import is_frozen
        from .tray_app import has_display

        if not has_display():
            return {"ok": False, "error": "当前环境没有图形界面，请手工输入路径"}

        with self._dialog_lock:
            handle, out_name = tempfile.mkstemp(prefix="ea-pick-", suffix=".txt")
            os.close(handle)
            out_path = Path(out_name)
            try:
                if is_frozen():
                    # 打包后 sys.executable 就是本程序，直接复用自身的子命令
                    cmd = [sys.executable, "_pick-directory", "--out", str(out_path)]
                else:
                    # 源码运行：main.py 在**源码树根**，不是 config.PROJECT_ROOT。
                    # 后者会随 EMAIL_ASSISTANT_HOME 变化（受支持的用户配置），
                    # 用它拼路径会让设置了该变量的用户永远找不到 main.py。
                    cmd = [
                        sys.executable,
                        str(SOURCE_ROOT / "main.py"),
                        "_pick-directory",
                        "--out",
                        str(out_path),
                    ]
                if initial:
                    cmd += ["--initial", initial]

                kwargs: dict[str, Any] = {
                    "stdout": subprocess.DEVNULL,
                    "stderr": subprocess.PIPE,
                    "timeout": self.DIALOG_TIMEOUT,
                }
                if sys.platform == "win32":
                    # 避免弹出多余的控制台窗口
                    kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)

                try:
                    proc = subprocess.run(cmd, **kwargs)
                except subprocess.TimeoutExpired:
                    return {"ok": False, "error": "等待目录选择超时，请重试或手工输入路径"}
                except OSError as exc:
                    return {"ok": False, "error": f"无法启动目录选择框：{exc}"}

                raw = ""
                try:
                    raw = out_path.read_text(encoding="utf-8").strip()
                except OSError:
                    raw = ""

                if not raw:
                    if proc.returncode == 0:
                        return {"ok": False, "cancelled": True, "error": "已取消"}
                    detail = ""
                    if proc.stderr:
                        detail = proc.stderr.decode("utf-8", "replace").strip()
                    return {
                        "ok": False,
                        "error": detail or f"目录选择框退出（代码 {proc.returncode}）",
                    }
                return {"ok": True, "path": str(Path(raw))}
            finally:
                out_path.unlink(missing_ok=True)

    def open_path(self, path: str) -> dict[str, Any]:
        """在系统文件管理器中打开目录。"""
        from .tray_app import open_local_path

        target = Path(path).expanduser() if path else _common_root(self.config)
        if not target.exists():
            try:
                target.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                return {"ok": False, "error": f"目录不存在且无法创建：{exc}"}
        return {"ok": open_local_path(target), "path": str(target)}

    # ------------------------------------------------------------------
    # 迁移
    # ------------------------------------------------------------------

    def migrate_data(self, new_root: str, *, delete_source: bool = False) -> dict[str, Any]:
        """把现有数据搬到新目录。

        直接改路径会让旧数据"消失"（程序在新目录建空库），
        对已经同步过上万封邮件的用户是灾难，因此提供显式迁移。
        """
        target = Path(new_root).expanduser().resolve()
        current = _common_root(self.config).resolve()
        if target == current:
            return {"ok": False, "error": "新旧目录相同，无需迁移"}
        if not current.exists():
            return {"ok": False, "error": f"当前数据目录不存在：{current}"}

        target.mkdir(parents=True, exist_ok=True)
        moved: list[str] = []
        skipped: list[str] = []

        # 按**实际配置的路径**搬运，而不是硬编码 mail_archive/ 这类目录名。
        # 用户完全可能把归档放在 D:\我的邮件 之类的自定义目录，
        # 按名字猜会静默地"什么都没搬"，而界面还显示成功。
        c = self.config
        plan: list[tuple[str, Path, Path]] = [
            ("mail_archive", c.archive_path, target / "mail_archive"),
            ("attachments", c.attachment_path, target / "attachments"),
            ("sqlite", c.sqlite_file.parent, target / "sqlite"),
            ("chromadb", c.chroma_path, target / "chromadb"),
            ("backups", c.backup_path, target / "backups"),
        ]
        seen: set[Path] = set()
        for name, src, dst in plan:
            try:
                src = src.resolve()
            except OSError:
                continue
            if src in seen or not src.exists():
                continue
            # 防护：源目录就是数据根、或目标位于源目录内部时，
            # shutil.move 会抛 "Cannot move a directory into itself"。
            # 配置把 sqlite 直接放在根目录下时很容易触发。
            if src == current or _is_within(target, src) or _is_within(src, target):
                logger.info("跳过迁移 %s（与数据根或目标目录重叠）", src)
                continue
            seen.add(src)
            if dst.exists() and any(dst.iterdir()):
                skipped.append(name)
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            moved.append(name)

        self.write_data_root(str(target))
        logger.info("数据已迁移到 %s（迁移 %s，跳过 %s）", target, moved, skipped)
        return {
            "ok": True,
            "target": str(target),
            "moved": moved,
            "skipped": skipped,
            "deleted_source": bool(delete_source and not skipped),
        }


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------

def _is_frozen() -> bool:
    from .config import is_frozen

    return is_frozen()


def _common_root(config: AppConfig) -> Path:
    """推导出一个能覆盖全部存储路径的"数据根目录"。"""
    candidates = [
        config.archive_path,
        config.sqlite_file.parent,
        config.chroma_path,
        config.backup_path,
    ]
    try:
        common = Path(os.path.commonpath([str(p) for p in candidates]))
    except ValueError:
        return config.archive_path.parent
    # commonpath 可能落到 data/sqlite 这类子目录，向上取到与 mail_archive 同级
    while common.name in ("sqlite", "chromadb", "backups", "mail_archive", "attachments"):
        if common.parent == common:
            break
        common = common.parent
    return common


def _merge_patch(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """两层的深合并（配置只有两层，够用且不会误合并列表）。"""
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in base.items()}
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = {**out[key], **value}
        else:
            out[key] = value
    return out


def _is_within(child: Path, parent: Path) -> bool:
    """``child`` 是否位于 ``parent`` 之内（含相等）。"""
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except (ValueError, OSError):
        return False


def _size_mb(path: Path) -> float:
    try:
        return round(path.stat().st_size / 1024 / 1024, 2) if path.is_file() else 0.0
    except OSError:
        return 0.0


def _dir_size_mb(path: Path) -> float:
    if not path.is_dir():
        return 0.0
    total = 0
    try:
        for item in path.rglob("*"):
            if item.is_file():
                total += item.stat().st_size
    except OSError:
        pass
    return round(total / 1024 / 1024, 2)


def _model_ready(config: AppConfig) -> bool:
    from .model_manager import check_model_dir

    try:
        return check_model_dir(config.model_path).ready
    except Exception:  # noqa: BLE001
        return False


def _first_error(exc: Exception) -> str:
    """把 pydantic 的多行报错压成一句人话。"""
    text = str(exc)
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("Value error,") or line.startswith("Assertion failed,"):
            return line.split(",", 1)[-1].strip()
        if "value_error" in line or line.startswith("Input should be"):
            return line
    return text.splitlines()[0] if text else "未知错误"
