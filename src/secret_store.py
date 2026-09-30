"""授权码安全存储（§8：不保存明文密码）。

优先级
------
1. **环境变量**（由 :func:`src.config.resolve_auth_code` 优先处理）——
   最安全，推荐在 CI / 服务器使用。
2. **操作系统密钥库**（``keyring``，若已安装）——Windows 凭据管理器 /
   macOS Keychain / Linux SecretService。
3. **本地加密文件**（兜底）——Fernet 对称加密，密钥文件权限 0600。
   注意：密钥与密文同机存放，属于「防误读」而非「防本机攻击者」，
   文档中已如实说明。

YAML 配置文件只保存一个逻辑键名（``email.auth_code_ref``），永不保存明文。
"""

from __future__ import annotations

import json
import logging
import os
import stat
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from .config import AppConfig

logger = logging.getLogger(__name__)

SERVICE_NAME = "email-assistant"
SECRETS_FILENAME = ".secrets.enc"
KEY_FILENAME = ".secrets.key"


class SecretStoreError(RuntimeError):
    """密钥库操作失败。"""


def _try_keyring():  # type: ignore[no-untyped-def]
    try:
        import keyring  # type: ignore

        backend = keyring.get_keyring()
        # 明确排除「明文内存」后端，否则等同于没加密
        if "fail" in backend.__class__.__name__.lower():
            return None
        if "chainer" in backend.__class__.__name__.lower():
            # ChainerBackend 需要实际探测
            try:
                keyring.get_password(SERVICE_NAME, "__probe__")
            except Exception:  # noqa: BLE001
                return None
        return keyring
    except Exception:  # noqa: BLE001 - 缺少依赖或后端不可用
        return None


def _try_fernet():  # type: ignore[no-untyped-def]
    try:
        from cryptography.fernet import Fernet, InvalidToken  # type: ignore

        return Fernet, InvalidToken
    except Exception:  # noqa: BLE001
        return None, None


class SecretStore:
    """授权码读取/写入的统一入口。"""

    def __init__(self, config: "AppConfig") -> None:
        self.config = config
        # 密钥库必须与**实际加载的配置文件**放在一起。
        # 固定写到项目根 config/ 会让「用 --config 指定别的配置文件」
        # 以及测试环境互相污染。
        if config.source_path is not None:
            self._dir = Path(config.source_path).parent
        else:
            self._dir = config.resolve("config")
        self._secrets_file = self._dir / SECRETS_FILENAME
        self._key_file = self._dir / KEY_FILENAME

    # ---- 对外接口 ----

    def get(self, ref: str) -> str | None:
        """读取授权码；不存在返回 ``None``。"""
        if not ref:
            return None
        keyring = _try_keyring()
        if keyring is not None:
            try:
                value = keyring.get_password(SERVICE_NAME, ref)
                if value:
                    return value
            except Exception:  # noqa: BLE001
                logger.debug("keyring 读取失败，回退到本地加密文件", exc_info=True)

        data = self._load_file()
        value = data.get(ref)
        if value:
            return value
        return None

    def set(self, ref: str, value: str) -> str:
        """写入授权码，返回实际使用的后端名称。"""
        if not ref:
            raise SecretStoreError("auth_code_ref 不能为空")

        keyring = _try_keyring()
        if keyring is not None:
            try:
                keyring.set_password(SERVICE_NAME, ref, value)
                return "keyring"
            except Exception:  # noqa: BLE001
                logger.debug("keyring 写入失败，回退到本地加密文件", exc_info=True)

        Fernet, _ = _try_fernet()
        if Fernet is None:
            raise SecretStoreError(
                "无法安全保存授权码：未安装 keyring，且缺少 cryptography。"
                "请执行 `pip install cryptography`，或改用环境变量 "
                f"{self.config.email.auth_code_env}。"
            )

        data = self._load_file()
        data[ref] = value
        self._save_file(data)
        return "encrypted-file"

    def delete(self, ref: str) -> bool:
        removed = False
        keyring = _try_keyring()
        if keyring is not None:
            try:
                keyring.delete_password(SERVICE_NAME, ref)
                removed = True
            except Exception:  # noqa: BLE001
                pass
        data = self._load_file()
        if ref in data:
            del data[ref]
            self._save_file(data)
            removed = True
        return removed

    def backend_name(self, ref: str) -> str:
        """报告当前授权码来自哪个后端（用于 ``doctor`` 命令）。"""
        if os.environ.get(self.config.email.auth_code_env):
            return "environment"
        keyring = _try_keyring()
        if keyring is not None:
            try:
                if keyring.get_password(SERVICE_NAME, ref):
                    return "keyring"
            except Exception:  # noqa: BLE001
                pass
        if ref in self._load_file():
            return "encrypted-file"
        return "missing"

    # ---- 本地加密文件实现 ----

    def _load_file(self) -> dict[str, str]:
        if not self._secrets_file.is_file():
            return {}
        Fernet, InvalidToken = _try_fernet()
        if Fernet is None:
            logger.warning("缺少 cryptography，无法读取本地密钥库")
            return {}
        key = self._load_or_create_key()
        if key is None:
            return {}
        try:
            raw = self._secrets_file.read_bytes()
            if not raw.strip():
                return {}
            payload = Fernet(key).decrypt(raw)
            parsed = json.loads(payload.decode("utf-8"))
            if not isinstance(parsed, dict):
                return {}
            return {str(k): str(v) for k, v in parsed.items()}
        except InvalidToken:
            logger.error(
                "本地密钥库解密失败（密钥文件可能已更换）：%s", self._secrets_file
            )
            return {}
        except Exception:  # noqa: BLE001
            logger.exception("读取本地密钥库失败")
            return {}

    def _save_file(self, data: dict[str, str]) -> None:
        Fernet, _ = _try_fernet()
        if Fernet is None:
            raise SecretStoreError("缺少 cryptography，无法写入本地密钥库")
        key = self._load_or_create_key(create=True)
        assert key is not None
        token = Fernet(key).encrypt(json.dumps(data, ensure_ascii=False).encode("utf-8"))
        self._dir.mkdir(parents=True, exist_ok=True)
        tmp = self._secrets_file.with_suffix(".enc.tmp")
        tmp.write_bytes(token)
        _restrict(tmp)
        os.replace(tmp, self._secrets_file)
        _restrict(self._secrets_file)

    def _load_or_create_key(self, create: bool = False) -> bytes | None:
        if self._key_file.is_file():
            try:
                return self._key_file.read_bytes().strip()
            except OSError:
                logger.exception("读取密钥文件失败")
                return None
        if not create:
            return None
        Fernet, _ = _try_fernet()
        if Fernet is None:
            return None
        self._dir.mkdir(parents=True, exist_ok=True)
        key = Fernet.generate_key()
        self._key_file.write_bytes(key)
        _restrict(self._key_file)
        logger.info(
            "已生成本地加密密钥：%s（权限 0600，请勿提交到版本库）", self._key_file
        )
        return key


def _restrict(path: Path) -> None:
    """POSIX 下收紧文件权限到 0600。"""
    if os.name == "nt":
        return
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
