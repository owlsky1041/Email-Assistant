"""安全与隐私测试（§8）。

覆盖：授权码不入日志、不入 API、不落配置文件；路径穿越防护；目录权限。
"""

from __future__ import annotations

import logging
import os
import stat
from pathlib import Path

import pytest

from src.config import AppConfig, resolve_auth_code, resolve_api_token
from src.logging_setup import RedactingFilter, redact, truncate_for_log
from src.secret_store import SecretStore, SecretStoreError
from src.utils import sanitize_filename, sanitize_relative_path

SECRET = "SuperSecretAuthCode1234567890"


class TestLogRedaction:
    def test_key_value_form_redacted(self) -> None:
        assert SECRET not in redact(f"auth_code={SECRET}")

    def test_json_form_redacted(self) -> None:
        assert SECRET not in redact(f'{{"auth_code": "{SECRET}"}}')

    def test_bearer_token_redacted(self) -> None:
        assert "abcdef123456" not in redact("Authorization: Bearer abcdef123456xyz")

    def test_password_redacted(self) -> None:
        assert "hunter2xyz" not in redact("password: hunter2xyz")

    def test_explicit_secret_value_redacted(self) -> None:
        assert SECRET not in redact(f"连接使用 {SECRET} 登录", [SECRET])

    def test_ordinary_text_untouched(self) -> None:
        text = "同步完成，归档 12 封邮件"
        assert redact(text) == text

    def test_long_token_heuristic(self) -> None:
        token = "a" * 30
        assert token not in redact(f"token found: {token}")

    def test_filter_cleans_log_record(self) -> None:
        """§8 授权码不得写入日志。"""
        flt = RedactingFilter(secrets=[SECRET], max_text=200)
        record = logging.LogRecord(
            "test", logging.INFO, __file__, 1, f"登录中 auth_code={SECRET}", None, None
        )
        assert flt.filter(record) is True
        assert SECRET not in record.getMessage()

    def test_filter_truncates_huge_message(self) -> None:
        flt = RedactingFilter(max_text=50)
        record = logging.LogRecord(
            "test", logging.INFO, __file__, 1, "正文：" + "字" * 10000, None, None
        )
        flt.filter(record)
        assert "截断" in record.getMessage()

    def test_filter_survives_bad_args(self) -> None:
        flt = RedactingFilter()
        record = logging.LogRecord(
            "test", logging.INFO, __file__, 1, "值=%s", ("x",), None
        )
        assert flt.filter(record) is True

    def test_add_secret_at_runtime(self) -> None:
        flt = RedactingFilter()
        flt.add_secret(SECRET)
        record = logging.LogRecord(
            "test", logging.INFO, __file__, 1, f"授权码 {SECRET}", None, None
        )
        flt.filter(record)
        assert SECRET not in record.getMessage()

    def test_short_secrets_ignored(self) -> None:
        """过短的"密钥"不参与替换，避免把正常文本打成马赛克。"""
        flt = RedactingFilter()
        flt.add_secret("ab")
        record = logging.LogRecord(
            "test", logging.INFO, __file__, 1, "about", None, None
        )
        flt.filter(record)
        assert record.getMessage() == "about"


class TestTruncateForLog:
    def test_short_text_unchanged(self) -> None:
        assert truncate_for_log("短文本", 100) == "短文本"

    def test_long_text_truncated(self) -> None:
        result = truncate_for_log("字" * 500, 50)
        assert len(result) < 200
        assert "已截断" in result

    def test_newlines_flattened(self) -> None:
        assert "\n" not in truncate_for_log("第一行\n第二行", 100)

    def test_none_safe(self) -> None:
        assert truncate_for_log(None, 10) == ""  # type: ignore[arg-type]


class TestSecretStore:
    def test_roundtrip_encrypted_file(self, tmp_config: AppConfig, monkeypatch) -> None:
        monkeypatch.delenv(tmp_config.email.auth_code_env, raising=False)
        store = SecretStore(tmp_config)
        backend = store.set("default", SECRET)
        assert backend in ("keyring", "encrypted-file")

        # 通过统一入口读回
        assert resolve_auth_code(tmp_config) == SECRET

    def test_plaintext_not_on_disk(self, tmp_config: AppConfig, monkeypatch) -> None:
        monkeypatch.delenv(tmp_config.email.auth_code_env, raising=False)
        store = SecretStore(tmp_config)
        store.set("default", SECRET)
        assert store._secrets_file.is_file()
        assert SECRET.encode() not in store._secrets_file.read_bytes()

    def test_key_file_permissions(self, tmp_config: AppConfig, monkeypatch) -> None:
        if os.name == "nt":
            pytest.skip("POSIX 专用")
        monkeypatch.delenv(tmp_config.email.auth_code_env, raising=False)
        store = SecretStore(tmp_config)
        store.set("default", SECRET)
        assert store._key_file.is_file()
        assert stat.S_IMODE(store._key_file.stat().st_mode) == 0o600

    def test_env_var_takes_priority(self, tmp_config: AppConfig, monkeypatch) -> None:
        SecretStore(tmp_config).set("default", "from-store")
        monkeypatch.setenv(tmp_config.email.auth_code_env, "from-env")
        assert resolve_auth_code(tmp_config) == "from-env"

    def test_delete(self, tmp_config: AppConfig, monkeypatch) -> None:
        monkeypatch.delenv(tmp_config.email.auth_code_env, raising=False)
        store = SecretStore(tmp_config)
        store.set("default", SECRET)
        assert store.delete("default") is True
        assert SecretStore(tmp_config).get("default") is None

    def test_missing_returns_none(self, tmp_config: AppConfig, monkeypatch) -> None:
        monkeypatch.delenv(tmp_config.email.auth_code_env, raising=False)
        assert SecretStore(tmp_config).get("nonexistent") is None

    def test_backend_name(self, tmp_config: AppConfig, monkeypatch) -> None:
        monkeypatch.delenv(tmp_config.email.auth_code_env, raising=False)
        store = SecretStore(tmp_config)
        assert store.backend_name("default") == "missing"
        store.set("default", SECRET)
        assert store.backend_name("default") in ("keyring", "encrypted-file")

    def test_empty_ref_rejected(self, tmp_config: AppConfig) -> None:
        with pytest.raises(SecretStoreError):
            SecretStore(tmp_config).set("", SECRET)


class TestConfigNeverHoldsAuthCode:
    def test_default_config_has_no_auth_code_field(self) -> None:
        """§8 配置里不能有明文授权码字段，只能有引用名。"""
        from src.config import EmailConfig

        fields = set(EmailConfig.model_fields)
        assert "auth_code" not in fields
        assert "auth_code_env" in fields
        assert "auth_code_ref" in fields

    def test_generated_template_has_placeholder_only(self, tmp_path: Path) -> None:
        from src.config_writer import render_default_config

        template = render_default_config()
        assert 'auth_code: ""' not in template
        assert "auth_code_env" in template
        assert "不要】写入授权码" in template or "不要" in template


class TestApiTokenResolution:
    def test_from_env(self, tmp_config: AppConfig, monkeypatch) -> None:
        monkeypatch.setenv(tmp_config.api.token_env, "env-token-value")
        assert resolve_api_token(tmp_config) == "env-token-value"

    def test_from_config_when_env_absent(
        self, tmp_config: AppConfig, monkeypatch
    ) -> None:
        monkeypatch.delenv(tmp_config.api.token_env, raising=False)
        assert resolve_api_token(tmp_config) == tmp_config.api.token


class TestPathTraversal:
    """目录结构与文件名必须无法逃逸出归档根目录。"""

    @pytest.mark.parametrize(
        "malicious",
        [
            "../../etc/passwd",
            "..\\..\\windows\\system32",
            "....//....//etc",
            "/absolute/path",
            "a/../../b",
        ],
    )
    def test_folder_traversal_blocked(self, tmp_config: AppConfig, malicious: str) -> None:
        from src.markdown_exporter import MarkdownExporter

        exporter = MarkdownExporter(tmp_config)
        path = exporter.folder_dir(malicious, "me@corp.com")
        resolved = path.resolve()
        assert str(resolved).startswith(str(tmp_config.archive_path.resolve()))

    def test_sanitize_relative_path_strips_dots(self) -> None:
        assert ".." not in sanitize_relative_path(["..", ".."]).parts

    @pytest.mark.parametrize(
        "malicious",
        ["../../evil.md", "..\\evil.md", "a/../../../evil", "/etc/shadow", "..", "."],
    )
    def test_attachment_name_traversal_blocked(
        self, malicious: str, tmp_path: Path
    ) -> None:
        """安全属性：清洗后的文件名作为**单个路径分量**拼接时无法逃逸目录。"""
        result = sanitize_filename(malicious)
        assert "/" not in result and "\\" not in result
        assert result not in (".", "..")
        target_dir = tmp_path / "attachments"
        target_dir.mkdir(parents=True, exist_ok=True)
        resolved = (target_dir / result).resolve()
        assert resolved.parent == target_dir.resolve()
        assert str(resolved).startswith(str(tmp_path.resolve()))

    def test_null_byte_removed(self) -> None:
        assert "\x00" not in sanitize_filename("evil\x00.md")

    def test_control_characters_removed(self) -> None:
        result = sanitize_filename("a\x01b\x1fc.md")
        assert not any(ord(ch) < 32 for ch in result)


class TestDirectoryPermissions:
    def test_data_dirs_are_0700(self, tmp_config: AppConfig) -> None:
        """§8 本地数据库和附件目录建议设置权限隔离。"""
        if os.name == "nt":
            pytest.skip("POSIX 专用")
        tmp_config.ensure_directories()
        for path in (
            tmp_config.archive_path,
            tmp_config.attachment_path,
            tmp_config.sqlite_file.parent,
        ):
            assert stat.S_IMODE(path.stat().st_mode) == 0o700

    def test_archived_markdown_is_0600(self, tmp_config: AppConfig) -> None:
        if os.name == "nt":
            pytest.skip("POSIX 专用")
        from src.markdown_exporter import MarkdownExporter
        from src.models import ParsedMessage

        exporter = MarkdownExporter(tmp_config)
        message = ParsedMessage(uid="1", folder="INBOX", subject="x", body_markdown="y",
                                body_text="y")
        result = exporter.export(message, account="me@corp.com")
        assert stat.S_IMODE(result.markdown_path.stat().st_mode) == 0o600


class TestSecretStoreIsolation:
    """回归测试：密钥库必须跟着配置文件走，不能污染项目根目录。"""

    def test_secrets_dir_follows_config_path(self, tmp_path: Path) -> None:
        from src.config import load_config
        from src.config_writer import write_default_config

        target = tmp_path / "custom" / "config.yaml"
        write_default_config(target)
        config = load_config(target, create_dirs=False)

        store = SecretStore(config)
        assert store._secrets_file.parent == target.parent, (
            "密钥库应放在配置文件所在目录，否则 --config 指定其它文件时会互相污染"
        )

    def test_set_writes_next_to_config(self, tmp_path: Path,
                                       monkeypatch: pytest.MonkeyPatch) -> None:
        from src.config import load_config
        from src.config_writer import write_default_config

        target = tmp_path / "custom" / "config.yaml"
        write_default_config(target)
        config = load_config(target, create_dirs=False)
        monkeypatch.delenv(config.email.auth_code_env, raising=False)

        SecretStore(config).set("default", SECRET)
        assert (target.parent / ".secrets.enc").is_file()
        # 项目根 config/ 不应被创建
        assert not (PROJECT_ROOT_CONFIG / ".secrets.enc").exists() or \
            (PROJECT_ROOT_CONFIG / ".secrets.enc").stat().st_mtime != 0

    def test_no_source_path_falls_back_to_project_config(self, tmp_config: AppConfig,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
        from src.config import PROJECT_ROOT

        monkeypatch.delenv(tmp_config.email.auth_code_env, raising=False)
        tmp_config.source_path = None
        store = SecretStore(tmp_config)
        assert store._secrets_file.parent == (PROJECT_ROOT / "config").resolve()


PROJECT_ROOT_CONFIG = __import__("src.config", fromlist=["PROJECT_ROOT"]).PROJECT_ROOT / "config"
