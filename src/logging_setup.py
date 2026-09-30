"""日志配置：轮转 + 敏感信息脱敏（§8）。

关键要求：
- 授权码、API token 不得出现在日志中；
- 不得记录完整邮件正文，超长内容自动截断。
"""

from __future__ import annotations

import logging
import logging.handlers
import re
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from .config import AppConfig

LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)-28s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# 关键词 -> 替换模板（保留可读性，便于排障）
_SENSITIVE_KEYS = (
    "auth_code",
    "authorization",
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "access_key",
    "credential",
)

_KEY_VALUE_RE = re.compile(
    r"(?i)\b(" + "|".join(_SENSITIVE_KEYS) + r")\b\s*[:=]\s*(['\"]?)([^\s'\",;)}\]]+)\2"
)
_BEARER_RE = re.compile(r"(?i)\b(bearer\s+)([A-Za-z0-9._\-+/=]{8,})")
# 形如 16 位以上的连续字母数字串（腾讯授权码通常为长随机串）
_LONG_TOKEN_RE = re.compile(r"\b[A-Za-z0-9]{24,}\b")


def redact(text: str, extra_secrets: Iterable[str] = ()) -> str:
    """脱敏函数：可独立测试与复用。

    顺序很重要：``_BEARER_RE`` 必须先于 ``_KEY_VALUE_RE``。
    否则 ``Authorization: Bearer <token>`` 会先被 key-value 规则
    替换成 ``Authorization=***REDACTED***``，把 ``Bearer`` 这个锚点吃掉，
    真正的令牌反而原样留在日志里。
    """
    if not text:
        return text
    out = str(text)
    for secret in extra_secrets:
        if secret and len(secret) >= 4:
            out = out.replace(secret, "***REDACTED***")
    out = _BEARER_RE.sub(lambda m: f"{m.group(1)}***REDACTED***", out)
    out = _KEY_VALUE_RE.sub(lambda m: f"{m.group(1)}=***REDACTED***", out)
    out = _LONG_TOKEN_RE.sub("***REDACTED***", out)
    return out


def truncate_for_log(text: str, limit: int = 200) -> str:
    """§8：日志中不得记录完整邮件正文。"""
    if text is None:
        return ""
    flat = str(text).replace("\r\n", " ").replace("\n", " ").strip()
    if len(flat) <= limit:
        return flat
    return f"{flat[:limit]}…（已截断，共 {len(flat)} 字符）"


class RedactingFilter(logging.Filter):
    """在日志落盘前统一脱敏。"""

    def __init__(self, secrets: Iterable[str] = (), max_text: int = 200) -> None:
        super().__init__()
        self._secrets = [s for s in secrets if s and len(s) >= 4]
        self._max_text = max_text

    def add_secret(self, secret: str | None) -> None:
        if secret and len(secret) >= 4 and secret not in self._secrets:
            self._secrets.append(secret)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001
            return True
        cleaned = redact(message, self._secrets)
        if len(cleaned) > self._max_text * 8:
            cleaned = cleaned[: self._max_text * 8] + "…（日志已截断）"
            record.args = ()
        # 记住格式化后的消息，避免 logging 再次用 args 渲染
        record.msg = cleaned
        record.args = ()
        return True


_configured = False


def setup_logging(config: "AppConfig", *, force: bool = False) -> logging.Logger:
    """初始化根日志器：控制台 + 轮转文件。"""
    global _configured
    root = logging.getLogger()
    if _configured and not force:
        return root

    level = getattr(logging, str(config.log.level).upper(), logging.INFO)
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    redactor = RedactingFilter(max_text=config.log.max_logged_text)
    formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)

    log_dir: Path = config.log_path
    log_dir.mkdir(parents=True, exist_ok=True)

    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "email-assistant.log",
        maxBytes=config.log.max_bytes,
        backupCount=config.log.backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.addFilter(redactor)
    file_handler.setLevel(level)
    root.addHandler(file_handler)

    error_handler = logging.handlers.RotatingFileHandler(
        log_dir / "error.log",
        maxBytes=config.log.max_bytes,
        backupCount=config.log.backup_count,
        encoding="utf-8",
    )
    error_handler.setFormatter(formatter)
    error_handler.addFilter(redactor)
    error_handler.setLevel(logging.WARNING)
    root.addHandler(error_handler)

    if config.log.console:
        console = logging.StreamHandler()
        console.setFormatter(
            logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s", datefmt="%H:%M:%S")
        )
        console.addFilter(redactor)
        console.setLevel(level)
        root.addHandler(console)

    # 降低第三方库噪音
    for noisy in ("urllib3", "chromadb", "httpx", "httpcore", "apscheduler.executors"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _configured = True
    root.info("日志系统已初始化，输出目录：%s", log_dir)
    return root


def register_runtime_secret(secret: str | None) -> None:
    """把运行时才拿到的授权码加入脱敏列表。"""
    if not secret:
        return
    for handler in logging.getLogger().handlers:
        for flt in handler.filters:
            if isinstance(flt, RedactingFilter):
                flt.add_secret(secret)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
