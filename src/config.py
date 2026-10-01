"""配置加载与校验。

设计要点
--------
1. 授权码 **绝不** 写入 YAML 配置文件；配置里只保存 ``auth_code_ref``
   （密钥库中的键名）或 ``auth_code_env``（环境变量名）。
2. 支持 ``EMAIL_ASSISTANT__SECTION__KEY`` 形式的环境变量覆盖，便于容器化部署。
3. 所有相对路径统一相对 **项目根目录** 解析，避免受当前工作目录影响。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


def is_frozen() -> bool:
    """是否运行在 PyInstaller 等打包产物中。"""
    return bool(getattr(sys, "frozen", False))


APP_DIR_NAME = "EmailAssistant"


def _is_writable(directory: Path) -> bool:
    """目录能不能真的写文件。

    ``os.access(W_OK)`` 在 Windows 上对目录不可靠（只读属性和 ACL 是两回事），
    因此这里实打实地建一个临时文件再删掉。
    """
    probe = directory / f".write-probe-{os.getpid()}"
    try:
        probe.write_text("", encoding="utf-8")
        return True
    except OSError:
        return False
    finally:
        try:
            probe.unlink(missing_ok=True)
        except OSError:
            pass


def _user_data_root() -> Path:
    """当前平台的「用户数据目录」基准。"""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Local"
        return root / APP_DIR_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_DIR_NAME
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / APP_DIR_NAME


def runtime_root() -> Path:
    """应用根目录（配置 / data / logs 的基准）。

    解析顺序：

    1. 环境变量 ``EMAIL_ASSISTANT_HOME``（显式指定，便于绿色版/多实例）
    2. **macOS 应用包内** —— 用 ``~/Library/Application Support/EmailAssistant``。
       包体在签名后只读，且升级时整包替换，绝不能把用户数据放进去。
    3. **可写的打包产物目录** —— 绿色免安装，配置和数据就在程序旁边
    4. **不可写的打包产物目录**（典型：装进 ``C:\\Program Files``）——
       退回用户数据目录。安装包是以管理员身份装进去的，普通用户运行时
       没有写权限；若仍往程序目录写配置，第一次「保存设置」就会失败。
    5. **源码运行** —— 项目根

    打包场景必须与 ``__file__`` 脱钩：PyInstaller 把模块放进 ``_internal/``，
    按 ``__file__`` 推导会把用户的配置和数据埋进包体内部。
    """
    override = os.environ.get("EMAIL_ASSISTANT_HOME")
    if override:
        path = Path(override).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        return path.resolve()

    if is_frozen():
        exe_dir = Path(sys.executable).resolve().parent
        if sys.platform == "darwin" and ".app/Contents/MacOS" in exe_dir.as_posix():
            base = Path.home() / "Library" / "Application Support" / APP_DIR_NAME
            base.mkdir(parents=True, exist_ok=True)
            return base
        if _is_writable(exe_dir):
            return exe_dir
        fallback = _user_data_root()
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback

    return Path(__file__).resolve().parent.parent


# 源码模式下是仓库根；打包后是可执行文件所在目录（或 macOS 用户数据目录）
PROJECT_ROOT = runtime_root()

ENV_PREFIX = "EMAIL_ASSISTANT__"


class EmailConfig(BaseModel):
    """IMAP 账号配置。"""

    address: str = ""
    imap_server: str = "imap.exmail.qq.com"
    imap_port: int = 993
    use_ssl: bool = True
    # auth_code 本身不落盘；下面两个字段用于「从哪里取授权码」
    auth_code_env: str = "EMAIL_ASSISTANT_AUTH_CODE"
    auth_code_ref: str = "default"  # 本地加密密钥库中的键名
    # 连接行为
    connect_timeout: int = 30
    read_timeout: int = 120
    max_retries: int = 3
    retry_backoff_seconds: float = 2.0

    @field_validator("imap_port")
    @classmethod
    def _check_port(cls, v: int) -> int:
        if not (0 < v < 65536):
            raise ValueError("imap_port 必须在 1-65535 之间")
        return v


class StorageConfig(BaseModel):
    """本地归档路径配置。"""

    archive_dir: str = "./data/mail_archive"
    attachment_dir: str = "./data/attachments"
    sqlite_path: str = "./data/sqlite/mail.db"
    chroma_dir: str = "./data/chromadb"
    backup_dir: str = "./data/backups"
    #: 内容寻址的附件仓库：``blobs/<sha前2>/<sha次2>/<sha256>``。
    #: 人工可读的归档目录里放的是它的硬链接（跨卷时退化为复制）。
    #: 这是**可重建的派生数据** —— 归档目录本身已经含全部内容，
    #: 因此备份不需要带上它，`migrate-blobs` 随时能从归档重建。
    blob_dir: str = "./data/blobs"
    # sibling: 附件放在邮件同级 attachments/ 子目录（计划书 3.2 默认）
    # global:  附件统一放在 storage.attachment_dir/<账号>/<文件夹>/
    attachment_layout: Literal["sibling", "global"] = "sibling"
    # 归档根目录下是否再按账号分一层（为多账号预留）
    per_account_subdir: bool = True
    # 目录权限（POSIX 下生效，Windows 忽略）
    dir_mode: int = 0o700
    file_mode: int = 0o600


class SyncConfig(BaseModel):
    """同步策略配置。"""

    interval_minutes: int = 10
    max_attachment_size_mb: float = 50.0
    folders: list[str] = Field(default_factory=list)  # 空 = 全部文件夹
    exclude_folders: list[str] = Field(default_factory=list)
    fetch_batch_size: int = 50  # §11.2 分批拉取
    # 并发下载连接数。IMAP 连接不是线程安全的，因此每个工作线程会建立
    # 独立连接；1 = 顺序下载。腾讯企业邮箱对并发连接有限制，建议 3-5。
    fetch_workers: int = 3
    full_scan_interval_hours: int = 24  # §11.2 定期全量 UID 比对
    reconcile_deletions: bool = True
    # 按 Message-ID 跨文件夹去重：同一封邮件已在别处归档时不再重复下载与嵌入
    dedupe_by_message_id: bool = True
    max_messages_per_run: int = 0  # 0 = 不限制
    download_attachments: bool = True
    # 单封邮件正文超过此大小则不索引（避免异常巨大的邮件拖垮管线）
    max_body_index_size_kb: int = 2048

    @field_validator("interval_minutes")
    @classmethod
    def _check_interval(cls, v: int) -> int:
        if v < 1:
            raise ValueError("interval_minutes 必须 >= 1")
        return v

    @field_validator("fetch_batch_size")
    @classmethod
    def _check_batch(cls, v: int) -> int:
        if not (1 <= v <= 1000):
            raise ValueError("fetch_batch_size 建议在 1-1000 之间")
        return v

    @field_validator("fetch_workers")
    @classmethod
    def _check_workers(cls, v: int) -> int:
        if not (1 <= v <= 16):
            raise ValueError("fetch_workers 必须在 1-16 之间（过多会被服务端限流）")
        return v


class CleanConfig(BaseModel):
    """正文清洗策略。

    **默认保留转发/引用历史**：转发邮件里的历史内容往往是知识库里
    最有价值的部分，删掉无法恢复。需要更"干净"的正文时才显式开启。
    """

    strip_signature: bool = True
    strip_quoted_history: bool = False
    strip_legal_disclaimer: bool = True
    #: 只在邮件末尾该比例区域内寻找噪音标记（默认最后 30%）。
    #: 范围开得太大时，出现在转发历史中间的签名/免责声明会把
    #: 其后的全部内容一并截掉。
    noise_tail_ratio: float = 0.3

    @field_validator("noise_tail_ratio")
    @classmethod
    def _check_ratio(cls, v: float) -> float:
        if not (0.05 <= v <= 1.0):
            raise ValueError("noise_tail_ratio 必须在 0.05-1.0 之间")
        return v


class EmbeddingConfig(BaseModel):
    """嵌入模型配置。"""

    # auto | onnx | sentence-transformers | hashing
    backend: Literal["auto", "onnx", "sentence-transformers", "hashing"] = "auto"
    model: str = "BAAI/bge-small-zh-v1.5"
    #: 下载 ONNX 模型时用的仓库，与 ``model`` 分开维护。
    #: ``model`` 是 sentence-transformers 的仓库名，而**很多官方仓库并不提供
    #: ONNX**（BAAI/bge-small-zh-v1.5 就是），拿它去下载必然 404。
    #: 默认值是实测能下到 ``onnx/model.onnx`` 的社区导出；但社区导出的池化
    #: 方式可能与官方不一致、检索区分度变差，生产环境建议用 `model import`
    #: 导入本项目自带的模型。
    onnx_repo: str = "Xenova/bge-small-zh-v1.5"
    onnx_endpoint: str = "https://hf-mirror.com"
    model_dir: str = "./data/models"  # onnx 后端本地模型目录
    dimension: int = 512  # hashing 后端的维度
    chunk_size: int = 400  # token
    chunk_overlap: int = 80  # token
    batch_size: int = 32
    normalize: bool = True
    # bge 系列检索时建议加前缀
    query_prefix: str = "为这个句子生成表示以用于检索相关文章："
    device: str = "cpu"

    @model_validator(mode="after")
    def _check_chunk(self) -> "EmbeddingConfig":
        if self.chunk_overlap >= self.chunk_size:
            raise ValueError("chunk_overlap 必须小于 chunk_size")
        return self


class VectorConfig(BaseModel):
    """向量库配置。"""

    # auto | chroma | sqlite-vec | sqlite-bruteforce
    backend: Literal["auto", "chroma", "sqlite-vec", "sqlite-bruteforce"] = "auto"
    collection: str = "mail_chunks"
    max_results: int = 20
    # 在 kb_vectors.vector_blob 保留向量副本：
    # * sqlite 后端必须为 True（BLOB 就是向量本体）
    # * chroma 后端为 True 时可跳过重复嵌入，显著加速「重建索引」
    cache_blobs: bool = True
    # sqlite-bruteforce 后端一次性加载到内存的向量条数上限（安全阀）
    bruteforce_cache_limit: int = 200_000


class SearchConfig(BaseModel):
    """检索配置。"""

    default_limit: int = 20
    rrf_k: int = 60  # §11.4 RRF 融合参数
    keyword_weight: float = 1.0
    vector_weight: float = 1.0
    snippet_length: int = 200
    candidate_multiplier: int = 5  # 每路召回 limit*multiplier 再融合
    # 向量余弦相似度下限：低于该值的向量命中会被丢弃。
    # 纯 ANN 检索永远会返回"最近的邻居"，即使它们毫不相关；
    # 对 RAG 场景来说，把无关片段喂给模型比返回空结果更糟。
    #
    # 0 表示**自适应**：使用嵌入后端给出的推荐值
    # （hashing 0.25；bge 系列 0.35）。显式设置则覆盖推荐值。
    #
    # ⚠️ 注意：单一绝对阈值无法完美区分相关与无关。实测
    # BAAI/bge-small-zh-v1.5 的中文语料分布存在重叠：
    #     相关查询 top1 ∈ [0.39, 0.57]
    #     无关查询 top1 ∈ [0.33, 0.45]
    # 因此调用方仍应参考返回结果里的 vector_score 自行判断。
    min_vector_score: float = 0.0
    # 相对截断：丢弃比最佳命中低超过该值的向量结果，用于裁掉长尾。
    # 0 表示关闭。开启后与 min_vector_score 取较严者。
    vector_score_margin: float = 0.0


class ApiConfig(BaseModel):
    """知识库 API 配置。"""

    host: str = "127.0.0.1"
    port: int = 8990
    # 空 = 不鉴权（仅回环地址可达）；设置后需 Authorization: Bearer <token>
    token: str = ""
    token_env: str = "EMAIL_ASSISTANT_API_TOKEN"
    cors_origins: list[str] = Field(default_factory=list)
    request_timeout: int = 60

    @field_validator("host")
    @classmethod
    def _force_loopback(cls, v: str) -> str:
        """§8 安全要求：API 默认不监听 0.0.0.0。

        这里做「默认值保护」而非硬性禁止——若用户明确写 0.0.0.0，
        由 kb_api 启动时打印显著警告并记录审计日志。
        """
        return v


class LogConfig(BaseModel):
    """日志配置。"""

    level: str = "INFO"
    dir: str = "./logs"
    max_bytes: int = 10 * 1024 * 1024
    backup_count: int = 5
    console: bool = True
    # §8：日志中不得记录完整邮件正文 —— 超过该长度的正文会被截断
    max_logged_text: int = 200


class TrayConfig(BaseModel):
    """托盘配置。"""

    enabled: bool = True
    notify_on_new_mail: bool = True
    # 通知点击后打开：markdown | webmail | both
    click_action: Literal["markdown", "webmail", "both"] = "markdown"
    webmail_url: str = "https://exmail.qq.com/"
    minimize_to_tray: bool = True
    # 无 GUI 环境（服务器/CI）自动跳过托盘
    auto_disable_on_headless: bool = True


class AppConfig(BaseModel):
    """根配置。"""

    email: EmailConfig = Field(default_factory=EmailConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    sync: SyncConfig = Field(default_factory=SyncConfig)
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    vector: VectorConfig = Field(default_factory=VectorConfig)
    clean: CleanConfig = Field(default_factory=CleanConfig)
    search: SearchConfig = Field(default_factory=SearchConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)
    log: LogConfig = Field(default_factory=LogConfig)
    tray: TrayConfig = Field(default_factory=TrayConfig)

    #: 实际加载该配置的文件路径。运行时改写配置时必须写回**同一个文件**，
    #: 否则设置会被静默丢弃。``exclude=True`` 使其不参与序列化。
    source_path: Path | None = Field(default=None, exclude=True, repr=False)

    # ---- 派生属性 ----
    @property
    def archive_path(self) -> Path:
        return self.resolve(self.storage.archive_dir)

    @property
    def attachment_path(self) -> Path:
        return self.resolve(self.storage.attachment_dir)

    @property
    def sqlite_file(self) -> Path:
        return self.resolve(self.storage.sqlite_path)

    @property
    def chroma_path(self) -> Path:
        return self.resolve(self.storage.chroma_dir)

    @property
    def backup_path(self) -> Path:
        return self.resolve(self.storage.backup_dir)

    @property
    def blob_path(self) -> Path:
        return self.resolve(self.storage.blob_dir)

    @property
    def log_path(self) -> Path:
        return self.resolve(self.log.dir)

    @property
    def model_path(self) -> Path:
        return self.resolve(self.embedding.model_dir)

    @staticmethod
    def resolve(p: str | Path) -> Path:
        """相对路径一律相对项目根目录解析。"""
        path = Path(p).expanduser()
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        return path.resolve()

    def ensure_directories(self) -> None:
        for d in (
            self.archive_path,
            self.attachment_path,
            self.sqlite_file.parent,
            self.chroma_path,
            self.backup_path,
            self.blob_path,
            self.log_path,
            self.model_path,
        ):
            d.mkdir(parents=True, exist_ok=True)
            _harden_dir(d, self.storage.dir_mode)


def _harden_dir(path: Path, mode: int) -> None:
    """§8：本地目录权限隔离（POSIX）。"""
    if os.name == "nt":
        return
    try:
        path.chmod(mode)
    except OSError:
        pass


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _coerce_env_value(raw: str) -> Any:
    """把环境变量字符串转成合适的 Python 类型。"""
    low = raw.strip().lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if low in ("null", "none", ""):
        return None
    # JSON 风格数组 / 对象
    if raw.strip().startswith(("[", "{")):
        import json

        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    return raw


def _env_overrides() -> dict[str, Any]:
    """解析 ``EMAIL_ASSISTANT__EMAIL__ADDRESS`` 这类环境变量。"""
    data: dict[str, Any] = {}
    for key, value in os.environ.items():
        if not key.startswith(ENV_PREFIX):
            continue
        parts = [p.lower() for p in key[len(ENV_PREFIX) :].split("__") if p]
        if not parts:
            continue
        cursor = data
        for part in parts[:-1]:
            cursor = cursor.setdefault(part, {})
        cursor[parts[-1]] = _coerce_env_value(value)
    return data


DEFAULT_CONFIG_PATHS = (
    "config/config.yaml",
    "config/config.yml",
    "config.yaml",
)


def find_config_file(explicit: str | Path | None = None) -> Path | None:
    if explicit:
        p = AppConfig.resolve(explicit)
        return p if p.is_file() else None
    for candidate in DEFAULT_CONFIG_PATHS:
        p = PROJECT_ROOT / candidate
        if p.is_file():
            return p
    return None


def load_config(
    path: str | Path | None = None,
    *,
    overrides: dict[str, Any] | None = None,
    create_dirs: bool = True,
) -> AppConfig:
    """加载配置。

    优先级：默认值 < YAML 文件 < 环境变量 < 显式 overrides
    """
    data: dict[str, Any] = {}
    config_file = find_config_file(path)
    if config_file is not None:
        with open(config_file, "r", encoding="utf-8") as fh:
            loaded = yaml.safe_load(fh) or {}
        if not isinstance(loaded, dict):
            raise ValueError(f"配置文件格式错误（顶层必须是映射）: {config_file}")
        data = loaded

    data = _deep_merge(data, _env_overrides())
    if overrides:
        data = _deep_merge(data, overrides)

    config = AppConfig.model_validate(data)
    config.source_path = config_file
    if create_dirs:
        config.ensure_directories()
    return config


def load_config_from_mapping(data: dict[str, Any], *, create_dirs: bool = False) -> AppConfig:
    """测试 / 嵌入式场景：直接从字典构造配置。"""
    config = AppConfig.model_validate(data)
    if create_dirs:
        config.ensure_directories()
    return config


# ---- 授权码解析 ----------------------------------------------------------

def resolve_api_token(config: AppConfig) -> str:
    """API token 优先取环境变量，其次配置文件。"""
    env_name = config.api.token_env
    if env_name and os.environ.get(env_name):
        return os.environ[env_name]
    return config.api.token


def resolve_auth_code(config: AppConfig) -> str | None:
    """按优先级取授权码：环境变量 -> 本地加密密钥库。

    绝不从 YAML 读取明文授权码。返回 ``None`` 表示未配置。
    """
    env_name = config.email.auth_code_env
    if env_name:
        value = os.environ.get(env_name)
        if value:
            return value.strip()

    # 延迟导入，避免无 cryptography 时影响配置模块
    from .secret_store import SecretStore

    store = SecretStore(config)
    return store.get(config.email.auth_code_ref)


def masked_address(address: str) -> str:
    """日志用脱敏邮箱：``ab***@example.com``。"""
    if not address or "@" not in address:
        return "***"
    local, _, domain = address.partition("@")
    if len(local) <= 2:
        return f"{local[:1]}***@{domain}"
    return f"{local[:2]}***@{domain}"
