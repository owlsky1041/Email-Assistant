"""配置文件生成与写回（§7 配置向导）。

* :func:`render_default_config` —— 生成带中文注释的完整配置模板；
* :func:`write_default_config` —— 首次初始化时落盘；
* :func:`update_config` —— 运行时原子修改配置（保留注释，若安装了 ruamel.yaml）。
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from .config import PROJECT_ROOT, AppConfig, find_config_file

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path("config/config.yaml")

CONFIG_TEMPLATE = """\
# ============================================================================
#  腾讯企业邮箱邮件管理助手 —— 配置文件
#
#  安全提示：
#    * 本文件【不要】写入授权码。授权码请用以下任一方式提供：
#        1) 环境变量  EMAIL_ASSISTANT_AUTH_CODE
#        2) 执行 `python main.py auth set` 写入系统密钥库 / 本地加密文件
#    * 本文件建议权限设为 600。
# ============================================================================

email:
  address: ""                          # 你的企业邮箱地址，例如 you@yourcorp.com
  imap_server: "imap.exmail.qq.com"    # 腾讯企业邮箱 IMAP 服务器
  imap_port: 993
  use_ssl: true
  auth_code_env: "EMAIL_ASSISTANT_AUTH_CODE"
  auth_code_ref: "default"             # 本地密钥库中的键名
  connect_timeout: 30
  read_timeout: 120
  max_retries: 3
  retry_backoff_seconds: 2.0

storage:
  archive_dir: "./data/mail_archive"   # Markdown 归档根目录
  attachment_dir: "./data/attachments" # attachment_layout=global 时生效
  sqlite_path: "./data/sqlite/mail.db"
  chroma_dir: "./data/chromadb"
  backup_dir: "./data/backups"
  attachment_layout: "sibling"         # sibling=邮件同级 attachments/ ; global=统一目录
  per_account_subdir: true             # 归档目录下按账号分层（多账号预留）

sync:
  interval_minutes: 10                 # 定时同步间隔
  max_attachment_size_mb: 50           # 超过此大小的附件只记录元数据
  folders: []                          # 留空 = 同步全部文件夹
  exclude_folders: ["垃圾邮件", "Junk"] # 排除的文件夹
  fetch_batch_size: 50                 # 每批拉取封数
  full_scan_interval_hours: 24         # 全量 UID 比对周期
  reconcile_deletions: true            # 处理网页端删除/移动的邮件
  dedupe_by_message_id: true           # 跨文件夹按 Message-ID 去重
  max_messages_per_run: 0              # 单次同步上限，0=不限
  download_attachments: true
  max_body_index_size_kb: 2048

embedding:
  backend: "auto"                      # auto | onnx | sentence-transformers | hashing
  model: "BAAI/bge-small-zh-v1.5"
  model_dir: "./data/models"           # onnx 后端的本地模型目录（需含 model.onnx + tokenizer.json）
  dimension: 512                       # hashing 兜底后端的维度
  chunk_size: 400                      # 目标切片 token 数（建议 300-500）
  chunk_overlap: 80                    # 重叠 token 数（建议 50-100）
  batch_size: 32
  normalize: true
  query_prefix: "为这个句子生成表示以用于检索相关文章："
  device: "cpu"

vector:
  backend: "auto"                      # auto | chroma | sqlite-vec | sqlite-bruteforce
  collection: "mail_chunks"
  cache_blobs: true                    # 保留向量副本以加速重建索引
  max_results: 20
  bruteforce_cache_limit: 200000

search:
  default_limit: 20
  rrf_k: 60                            # RRF 融合参数，一般无需调整
  keyword_weight: 1.0
  vector_weight: 1.0
  snippet_length: 200
  candidate_multiplier: 5
  # 向量相似度下限。0 = 自适应（沿用嵌入后端推荐值：hashing 0.25 / bge 系列 0.35）
  #
  # ⚠️ 单一阈值无法完美区分相关与无关。对 BAAI/bge-small-zh-v1.5 的中文语料实测：
  #       相关查询 top1 ∈ [0.39, 0.57]
  #       无关查询 top1 ∈ [0.33, 0.45]
  #   两者存在重叠区，因此请结合返回结果中的 vector_score 自行判断，
  #   或按你自己的邮件语料重新标定：
  #       python scripts/calibrate_threshold.py
  min_vector_score: 0.0
  # 相对截断：丢弃比最佳命中低超过该值的向量结果（0 = 关闭，用于裁长尾）
  vector_score_margin: 0.0

api:
  host: "127.0.0.1"                    # 安全要求：不要改成 0.0.0.0
  port: 8990
  token: ""                            # 留空=不鉴权（仅回环地址可访问）
  token_env: "EMAIL_ASSISTANT_API_TOKEN"
  cors_origins: []                     # 为空则不启用 CORS
  request_timeout: 60

log:
  level: "INFO"
  dir: "./logs"
  max_bytes: 10485760
  backup_count: 5
  console: true
  max_logged_text: 200                 # 日志中正文的最大长度（防泄露）

tray:
  enabled: true
  notify_on_new_mail: true
  click_action: "markdown"             # markdown | webmail | both
  webmail_url: "https://exmail.qq.com/"
  minimize_to_tray: true
  auto_disable_on_headless: true
"""


def render_default_config() -> str:
    return CONFIG_TEMPLATE


def config_file_path(explicit: str | Path | None = None) -> Path:
    existing = find_config_file(explicit)
    if existing:
        return existing
    if explicit:
        return AppConfig.resolve(explicit)
    return PROJECT_ROOT / DEFAULT_CONFIG_PATH


def write_default_config(
    path: str | Path | None = None, *, overwrite: bool = False
) -> Path:
    """写入带注释的配置模板。"""
    target = config_file_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and not overwrite:
        logger.info("配置文件已存在，未覆盖：%s", target)
        return target
    _atomic_write(target, CONFIG_TEMPLATE)
    logger.info("已生成配置文件：%s", target)
    return target


def update_config(
    patch: Mapping[str, Any], path: str | Path | None = None
) -> Path:
    """把 ``patch`` 深合并进现有配置并写回（尽量保留注释）。

    :param path: 目标配置文件。**务必传实际加载的那个文件**
        （通常是 ``config.source_path``），否则运行时的设置改动会被写到
        另一个文件里，看起来"生效了"但重启后丢失。
        未指定时回退到默认路径。
    """
    target = config_file_path(path)
    data: dict[str, Any] = {}
    if target.is_file():
        with open(target, "r", encoding="utf-8") as fh:
            loaded = yaml.safe_load(fh) or {}
        if isinstance(loaded, dict):
            data = loaded

    merged = _deep_merge(data, dict(patch))

    if _ruamel_available():
        _write_with_ruamel(target, merged)
    else:
        body = yaml.safe_dump(merged, allow_unicode=True, sort_keys=False, default_flow_style=False)
        header = (
            "# 注意：本次由程序写入，原有注释已丢失（安装 ruamel.yaml 可保留注释）\n"
            "# 授权码请勿写入本文件，使用 `python main.py auth set` 或环境变量。\n"
        )
        _atomic_write(target, header + body)

    logger.info("配置已更新：%s", target)
    return target


def _ruamel_available() -> bool:
    try:
        import ruamel.yaml  # type: ignore # noqa: F401

        return True
    except Exception:  # noqa: BLE001
        return False


def _write_with_ruamel(target: Path, data: dict[str, Any]) -> None:
    from io import StringIO

    from ruamel.yaml import YAML  # type: ignore

    yaml_rt = YAML()
    yaml_rt.preserve_quotes = True
    yaml_rt.width = 4096
    yaml_rt.indent(mapping=2, sequence=4, offset=2)

    if target.is_file():
        with open(target, "r", encoding="utf-8") as fh:
            document = yaml_rt.load(fh) or {}
        _ruamel_merge(yaml_rt, document, data)
    else:
        document = data

    buffer = StringIO()
    yaml_rt.dump(document, buffer)
    _atomic_write(target, buffer.getvalue())


def _ruamel_merge(yaml_rt, document, patch: dict[str, Any]) -> None:  # type: ignore[no-untyped-def]
    for key, value in patch.items():
        if isinstance(value, dict) and hasattr(document.get(key), "keys"):
            _ruamel_merge(yaml_rt, document[key], value)
        else:
            document[key] = value


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _atomic_write(target: Path, text: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    if os.name != "nt":
        try:
            tmp.chmod(0o600)
        except OSError:
            pass
    os.replace(tmp, target)
