"""设置界面的**纯逻辑层**：草稿模型 ↔ 配置的双向转换、校验、保存、连通性测试。

这一层刻意不依赖 ``tkinter``，因此可以在没有显示器的环境（CI、服务器）
里完整测试。窗口只负责把控件绑定到 :class:`SettingsDraft` 的字段上。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from ..config import AppConfig
from ..config_writer import update_config
from ..model_manager import ModelStatus, check_model_dir

logger = logging.getLogger(__name__)


def project_base() -> Path:
    """数据路径的解析基准。

    走 :meth:`AppConfig.resolve` 而不是自己去拼运行根基常量：
    前者是项目里唯一的路径解析入口，会跟随 ``EMAIL_ASSISTANT_HOME``
    （绿色版 / 多实例用户的合法配置）。
    """
    return AppConfig.resolve(".")


#: 数据根目录下的标准子路径。用户只填一个根目录，其余全部派生，
#: 避免让非技术用户面对五六个意义不明的路径输入框。
LAYOUT: dict[str, str] = {
    "archive_dir": "mail_archive",
    "attachment_dir": "attachments",
    "sqlite_path": "sqlite/mail.db",
    "chroma_dir": "chromadb",
    "backup_dir": "backups",
    # 漏掉它会让界面上改数据根目录后，blob 仓库仍指回默认的 ./data/blobs
    # （相对项目目录），内容散落到两个地方。
    "blob_dir": "blobs",
}

#: 派生路径里不带 ``data/`` 前缀的两项（沿用既有默认位置）
MODEL_SUBDIR = "models"
LOG_SUBDIR = "logs"

DEFAULT_DATA_ROOT = "./data"

#: 实测能下到 ``onnx/model.onnx`` 的社区仓库。
#: 注意：社区导出的池化方式可能与官方 sentence-transformers 不一致、
#: 检索区分度变差，界面上必须把这一点讲清楚，不能默默用。
SUGGESTED_REPOS = (
    "Xenova/bge-small-zh-v1.5",
)
DEFAULT_ENDPOINT = "https://hf-mirror.com"

BACKEND_LABELS: dict[str, str] = {
    "auto": "自动（优先 ONNX，失败逐级降级）",
    "onnx": "ONNX Runtime（推荐，无需 PyTorch）",
    "ollama": "Ollama 本地模型（需先运行 ollama serve）",
    "sentence-transformers": "sentence-transformers（需要 PyTorch）",
    "hashing": "hashing（占位，无真实语义，仅供离线演示）",
}


# ----------------------------------------------------------------------
# 路径派生
# ----------------------------------------------------------------------


def portable_path(path: Path | str) -> str:
    """把项目根目录下的路径写成相对形式，换机器也能直接用。"""
    p = Path(path).expanduser()
    try:
        rel = p.resolve().relative_to(project_base())
    except (ValueError, OSError):
        return str(p)
    return f"./{rel.as_posix()}"


def layout_from_root(root: str) -> dict[str, str]:
    """由数据根目录派生各子路径（全部保持与 ``root`` 同样的相对/绝对风格）。

    刻意**不含** ``log.dir``：日志不是用户要定位的数据，把它一起搬走
    只会让老用户升级后找不到日志，收益为零。
    """
    base = Path(root).expanduser()
    out = {key: (base / sub).as_posix() for key, sub in LAYOUT.items()}
    out["model_dir"] = (base / MODEL_SUBDIR).as_posix()
    return out


def detect_data_root(config: AppConfig) -> tuple[str, list[str]]:
    """从现有配置反推数据根目录。

    :return: ``(根目录, 不一致项说明)``。
        不一致项为空表示现有配置完全符合标准布局；否则界面上要提示用户
        「保存后会统一到标准布局」，而不是悄悄改掉。
    """
    resolved = {
        "archive_dir": config.archive_path,
        "attachment_dir": config.attachment_path,
        "sqlite_path": config.sqlite_file,
        "chroma_dir": config.chroma_path,
        "backup_dir": config.backup_path,
        "blob_dir": config.blob_path,
        "model_dir": config.model_path,
    }
    root = config.archive_path.parent
    mismatches: list[str] = []
    for key, sub in {**LAYOUT, "model_dir": MODEL_SUBDIR}.items():
        expected = (root / sub).resolve()
        actual = resolved[key]
        if actual != expected:
            mismatches.append(f"{key}: {actual} → {expected}")
    return portable_path(root), mismatches


# ----------------------------------------------------------------------
# 草稿
# ----------------------------------------------------------------------


@dataclass
class SettingsDraft:
    """界面上所有可编辑字段的扁平快照。"""

    # ---- 邮箱接入 ----
    address: str = ""
    imap_server: str = "imap.exmail.qq.com"
    imap_port: int = 993
    use_ssl: bool = True
    #: 空字符串表示「保持已保存的授权码不变」——绝不把明文回显到界面上
    auth_code: str = ""

    # ---- 存储 ----
    data_root: str = DEFAULT_DATA_ROOT

    # ---- 同步 ----
    folders: list[str] = field(default_factory=list)
    exclude_folders: list[str] = field(default_factory=lambda: ["垃圾邮件", "Junk"])
    interval_minutes: int = 10
    fetch_workers: int = 3
    fetch_batch_size: int = 50
    max_attachment_size_mb: float = 50.0
    max_messages_per_run: int = 0
    download_attachments: bool = True
    reconcile_deletions: bool = True

    # ---- 嵌入模型 ----
    embedding_backend: str = "auto"
    model_repo: str = SUGGESTED_REPOS[0]
    model_endpoint: str = DEFAULT_ENDPOINT
    # 选 backend=ollama 时用这两项
    ollama_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "nomic-embed-text"

    # ---- 只读展示用 ----
    auth_code_present: bool = False
    auth_backend: str = ""

    # ------------------------------------------------------------------

    @classmethod
    def from_config(cls, config: AppConfig, *, auth_code_present: bool = False,
                    auth_backend: str = "") -> "SettingsDraft":
        root, _ = detect_data_root(config)
        return cls(
            address=config.email.address,
            imap_server=config.email.imap_server,
            imap_port=config.email.imap_port,
            use_ssl=config.email.use_ssl,
            auth_code="",
            data_root=root,
            folders=list(config.sync.folders),
            exclude_folders=list(config.sync.exclude_folders),
            interval_minutes=config.sync.interval_minutes,
            fetch_workers=config.sync.fetch_workers,
            fetch_batch_size=config.sync.fetch_batch_size,
            max_attachment_size_mb=config.sync.max_attachment_size_mb,
            max_messages_per_run=config.sync.max_messages_per_run,
            download_attachments=config.sync.download_attachments,
            reconcile_deletions=config.sync.reconcile_deletions,
            embedding_backend=config.embedding.backend,
            model_repo=config.embedding.onnx_repo,
            model_endpoint=config.embedding.onnx_endpoint,
            ollama_url=config.embedding.ollama_url,
            ollama_model=config.embedding.ollama_model,
            auth_code_present=auth_code_present,
            auth_backend=auth_backend,
        )

    def copy(self, **changes: Any) -> "SettingsDraft":
        return replace(self, **changes)

    # ------------------------------------------------------------------

    def validate(self, *, require_auth_code: bool = False) -> list[str]:
        """返回人类可读的错误列表；空列表表示可以保存。"""
        errors: list[str] = []

        address = self.address.strip()
        if not address:
            errors.append("邮箱账号不能为空")
        elif "@" not in address or address.startswith("@") or address.endswith("@"):
            errors.append(f"邮箱账号格式不对：{address}")

        if not self.imap_server.strip():
            errors.append("IMAP 服务器不能为空")

        if not (0 < int(self.imap_port) < 65536):
            errors.append("IMAP 端口必须在 1-65535 之间")

        if require_auth_code and not self.auth_code and not self.auth_code_present:
            errors.append("还没有授权码：请填写邮箱客户端授权码（不是登录密码）")

        if not self.data_root.strip():
            errors.append("数据存放目录不能为空")
        else:
            try:
                Path(self.data_root).expanduser()
            except (OSError, ValueError):
                errors.append(f"数据存放目录非法：{self.data_root}")

        if int(self.interval_minutes) < 1:
            errors.append("同步间隔至少 1 分钟")
        if not (1 <= int(self.fetch_workers) <= 16):
            errors.append("并发下载连接数必须在 1-16 之间（过多会被服务端限流）")
        if not (1 <= int(self.fetch_batch_size) <= 1000):
            errors.append("分批拉取大小必须在 1-1000 之间")
        if float(self.max_attachment_size_mb) <= 0:
            errors.append("附件大小上限必须大于 0")
        if int(self.max_messages_per_run) < 0:
            errors.append("单次同步封数上限不能为负数")

        if self.embedding_backend not in BACKEND_LABELS:
            errors.append(f"未知的嵌入后端：{self.embedding_backend}")

        return errors

    # ------------------------------------------------------------------

    def to_patch(self) -> dict[str, Any]:
        """转成 ``update_config()`` 需要的深合并 patch。"""
        paths = layout_from_root(self.data_root)
        return {
            "email": {
                "address": self.address.strip(),
                "imap_server": self.imap_server.strip(),
                "imap_port": int(self.imap_port),
                "use_ssl": bool(self.use_ssl),
            },
            "storage": {
                "archive_dir": paths["archive_dir"],
                "attachment_dir": paths["attachment_dir"],
                "sqlite_path": paths["sqlite_path"],
                "chroma_dir": paths["chroma_dir"],
                "backup_dir": paths["backup_dir"],
                "blob_dir": paths["blob_dir"],
            },
            "sync": {
                "folders": list(self.folders),
                "exclude_folders": list(self.exclude_folders),
                "interval_minutes": int(self.interval_minutes),
                "fetch_workers": int(self.fetch_workers),
                "fetch_batch_size": int(self.fetch_batch_size),
                "max_attachment_size_mb": float(self.max_attachment_size_mb),
                "max_messages_per_run": int(self.max_messages_per_run),
                "download_attachments": bool(self.download_attachments),
                "reconcile_deletions": bool(self.reconcile_deletions),
            },
            "embedding": {
                "backend": self.embedding_backend,
                "onnx_repo": self.model_repo.strip(),
                "onnx_endpoint": self.model_endpoint.strip() or DEFAULT_ENDPOINT,
                "ollama_url": self.ollama_url.strip() or "http://127.0.0.1:11434",
                "ollama_model": self.ollama_model.strip() or "nomic-embed-text",
                "model_dir": paths["model_dir"],
            },
        }


# ----------------------------------------------------------------------
# 保存
# ----------------------------------------------------------------------


@dataclass
class SaveResult:
    config_path: Path
    secret_backend: str = ""
    directories: list[Path] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def save_draft(
    draft: SettingsDraft,
    config: AppConfig,
    *,
    require_auth_code: bool = False,
) -> SaveResult:
    """校验并落盘。授权码写进密钥库，**绝不写进 YAML**。"""
    errors = draft.validate(require_auth_code=require_auth_code)
    if errors:
        raise ValueError("；".join(errors))

    target = config.source_path
    if target is None:
        from ..config_writer import config_file_path

        target = config_file_path(None)

    warnings: list[str] = []
    _, mismatches = detect_data_root(config)
    if mismatches:
        warnings.append("原有自定义路径已统一为标准布局：" + "；".join(mismatches))

    update_config(draft.to_patch(), target)

    secret_backend = ""
    if draft.auth_code:
        from ..secret_store import SecretStore

        secret_backend = SecretStore(config).set(config.email.auth_code_ref, draft.auth_code)

    # 目录不能等到第一次同步才创建——用户点了保存就应该能在文件管理器里看到
    paths = layout_from_root(draft.data_root)
    created: list[Path] = []
    for key in ("archive_dir", "attachment_dir", "backup_dir", "model_dir"):
        d = Path(paths[key]).expanduser()
        if not d.is_absolute():
            d = project_base() / d
        try:
            d.mkdir(parents=True, exist_ok=True)
            created.append(d)
        except OSError as exc:
            warnings.append(f"目录创建失败 {d}：{exc}")
    sqlite_parent = Path(paths["sqlite_path"]).expanduser()
    if not sqlite_parent.is_absolute():
        sqlite_parent = project_base() / sqlite_parent
    try:
        sqlite_parent.parent.mkdir(parents=True, exist_ok=True)
        created.append(sqlite_parent.parent)
    except OSError as exc:
        warnings.append(f"目录创建失败 {sqlite_parent.parent}：{exc}")

    return SaveResult(
        config_path=Path(target), secret_backend=secret_backend,
        directories=created, warnings=warnings,
    )


# ----------------------------------------------------------------------
# 连通性测试
# ----------------------------------------------------------------------


@dataclass
class ConnectionResult:
    ok: bool
    message: str
    folders: list[str] = field(default_factory=list)
    inbox_total: int = 0


def test_connection(
    draft: SettingsDraft,
    *,
    auth_code: str,
    timeout: int = 20,
    base_config: AppConfig | None = None,
) -> ConnectionResult:
    """只读地验证账号能不能登录，并顺便把文件夹列表带回来。

    **不会**修改服务端任何状态：只做 LOGIN / LIST / SELECT(只读) / LOGOUT。

    :param base_config: 用于补齐 ``sync`` / ``log`` 等其余配置段。
        ``ImapClient`` 不只读 ``email``，还会读 ``sync.fetch_batch_size``
        和 ``sync.max_attachment_size_mb``；只塞一个带 ``.email`` 的壳
        会在 connect 时炸 ``AttributeError``。
    """
    from ..imap_client import ImapClient
    from ..config import EmailConfig

    if not auth_code:
        return ConnectionResult(False, "没有可用的授权码，请先填写")

    try:
        config = (base_config or AppConfig()).model_copy(deep=True)
    except Exception:  # noqa: BLE001 - 兜底：拿不到完整配置就现造一个
        config = AppConfig()
    config.email = EmailConfig(
        address=draft.address.strip(),
        imap_server=draft.imap_server.strip(),
        imap_port=int(draft.imap_port),
        use_ssl=bool(draft.use_ssl),
        connect_timeout=timeout,
        read_timeout=max(timeout, 30),
        max_retries=1,
    )

    client: Any = None
    try:
        client = ImapClient(config, auth_code)
        client.connect()
        folders = [f.name for f in client.list_folders()]
        inbox_total = 0
        try:
            status = client.select_folder("INBOX")
            inbox_total = int(status.get("MESSAGES", 0))
        except Exception:  # noqa: BLE001 - 有些账号没有 INBOX 或权限受限
            logger.debug("读取 INBOX 状态失败", exc_info=True)
        return ConnectionResult(
            True,
            f"连接成功：{len(folders)} 个文件夹"
            + (f"，INBOX 共 {inbox_total} 封" if inbox_total else ""),
            folders=folders,
            inbox_total=inbox_total,
        )
    except Exception as exc:  # noqa: BLE001 - 失败原因要原样展示给用户
        return ConnectionResult(False, _explain_error(exc))
    finally:
        if client is not None:
            try:
                client.disconnect()
            except Exception:  # noqa: BLE001
                logger.debug("断开测试连接失败", exc_info=True)


def _explain_error(exc: Exception) -> str:
    """把 IMAP 的英文报错翻译成用户能照着做的提示。"""
    text = str(exc)
    low = text.lower()
    if "auth" in low or "login" in low or "authenticate" in low:
        return (
            "认证失败：账号或授权码不对。\n"
            "腾讯企业邮箱需要在「设置 → 客户端专用密码」里生成授权码，"
            "不能直接用网页登录密码。"
        )
    if "getaddrinfo" in low or "name or service not known" in low or "nodename" in low:
        return f"找不到服务器：{text}。请检查 IMAP 服务器地址和网络。"
    if "timed out" in low or "timeout" in low:
        return "连接超时：服务器无响应。请检查网络、端口是否被封。"
    if "certificate" in low or "ssl" in low:
        return f"TLS/SSL 握手失败：{text}。可以试试取消勾选 SSL 或在端口 143 上用 STARTTLS。"
    if "refused" in low:
        return f"连接被拒绝：{text}。请确认端口正确（SSL 通常是 993）。"
    return f"连接失败：{type(exc).__name__}: {text}"


# ----------------------------------------------------------------------
# 模型
# ----------------------------------------------------------------------


def model_status(config: AppConfig) -> ModelStatus:
    return check_model_dir(config.model_path)


def download_model_to_config(
    draft: SettingsDraft,
    config: AppConfig,
    *,
    on_progress: Any = None,
) -> ModelStatus:
    """按界面上的选择下载模型到「数据根目录/models」。"""
    from ..model_manager import download_model

    paths = layout_from_root(draft.data_root)
    target = Path(paths["model_dir"]).expanduser()
    if not target.is_absolute():
        target = project_base() / target
    return download_model(
        target,
        repo=draft.model_repo.strip() or None,
        endpoint=draft.model_endpoint.strip() or DEFAULT_ENDPOINT,
        on_progress=on_progress,
    )


def import_model_to_config(
    source: str,
    draft: SettingsDraft,
    config: AppConfig,
) -> ModelStatus:
    from ..model_manager import import_model

    paths = layout_from_root(draft.data_root)
    target = Path(paths["model_dir"]).expanduser()
    if not target.is_absolute():
        target = project_base() / target
    return import_model(source, target)
