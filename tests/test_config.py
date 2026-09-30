"""配置测试：默认值、环境变量覆盖、路径解析、校验。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from src.config import (
    AppConfig,
    PROJECT_ROOT,
    load_config,
    load_config_from_mapping,
    masked_address,
)


class TestDefaults:
    def test_matches_plan_spec(self) -> None:
        """§7 计划书中的默认值必须一致。"""
        config = AppConfig()
        assert config.email.imap_server == "imap.exmail.qq.com"
        assert config.email.imap_port == 993
        assert config.storage.archive_dir == "./data/mail_archive"
        assert config.storage.sqlite_path == "./data/sqlite/mail.db"
        assert config.storage.chroma_dir == "./data/chromadb"
        assert config.sync.interval_minutes == 10
        assert config.sync.max_attachment_size_mb == 50
        assert config.embedding.model == "BAAI/bge-small-zh-v1.5"
        assert config.embedding.chunk_size == 400
        assert config.embedding.chunk_overlap == 80
        assert config.api.host == "127.0.0.1"
        assert config.api.port == 8990
        assert config.log.level == "INFO"

    def test_chunk_size_in_recommended_range(self) -> None:
        """§3.4 建议 300-500 token，重叠 50-100 token。"""
        config = AppConfig()
        assert 300 <= config.embedding.chunk_size <= 500
        assert 50 <= config.embedding.chunk_overlap <= 100

    def test_api_defaults_to_loopback(self) -> None:
        """§8 API 默认不监听 0.0.0.0。"""
        assert AppConfig().api.host == "127.0.0.1"


class TestPathResolution:
    def test_relative_paths_resolve_to_project_root(self) -> None:
        config = AppConfig()
        assert config.archive_path == (PROJECT_ROOT / "data/mail_archive").resolve()

    def test_absolute_paths_preserved(self, tmp_path: Path) -> None:
        config = load_config_from_mapping({"storage": {"sqlite_path": str(tmp_path / "x.db")}})
        assert config.sqlite_file == (tmp_path / "x.db").resolve()

    def test_all_derived_paths(self, tmp_config: AppConfig, tmp_path: Path) -> None:
        assert tmp_config.sqlite_file == (tmp_path / "mail.db").resolve()
        assert tmp_config.archive_path == (tmp_path / "archive").resolve()
        assert tmp_config.chroma_path == (tmp_path / "chroma").resolve()

    def test_ensure_directories(self, tmp_config: AppConfig, tmp_path: Path) -> None:
        for path in (
            tmp_config.archive_path,
            tmp_config.sqlite_file.parent,
            tmp_config.chroma_path,
            tmp_config.log_path,
        ):
            assert path.is_dir()


class TestValidation:
    def test_invalid_port_rejected(self) -> None:
        with pytest.raises(Exception):
            load_config_from_mapping({"email": {"imap_port": 99999}})

    def test_invalid_interval_rejected(self) -> None:
        with pytest.raises(Exception):
            load_config_from_mapping({"sync": {"interval_minutes": 0}})

    def test_overlap_must_be_less_than_size(self) -> None:
        with pytest.raises(Exception):
            load_config_from_mapping(
                {"embedding": {"chunk_size": 100, "chunk_overlap": 100}}
            )

    def test_invalid_batch_size_rejected(self) -> None:
        with pytest.raises(Exception):
            load_config_from_mapping({"sync": {"fetch_batch_size": 0}})

    def test_unknown_backend_rejected(self) -> None:
        with pytest.raises(Exception):
            load_config_from_mapping({"vector": {"backend": "nonexistent"}})

    def test_extra_keys_ignored(self) -> None:
        config = load_config_from_mapping({"unknown_section": {"foo": "bar"}})
        assert config.api.port == 8990


class TestYamlLoading:
    def test_loads_from_file(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            yaml.safe_dump(
                {
                    "email": {"address": "me@corp.com"},
                    "sync": {"interval_minutes": 15},
                    "api": {"port": 9000},
                },
                allow_unicode=True,
            ),
            encoding="utf-8",
        )
        config = load_config(config_file, create_dirs=False)
        assert config.email.address == "me@corp.com"
        assert config.sync.interval_minutes == 15
        assert config.api.port == 9000

    def test_missing_file_uses_defaults(self, tmp_path: Path) -> None:
        config = load_config(tmp_path / "absent.yaml", create_dirs=False)
        assert config.api.port == 8990

    def test_partial_file_merges_with_defaults(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.yaml"
        config_file.write_text("api:\n  port: 9111\n", encoding="utf-8")
        config = load_config(config_file, create_dirs=False)
        assert config.api.port == 9111
        assert config.email.imap_server == "imap.exmail.qq.com"

    def test_invalid_yaml_top_level(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.yaml"
        config_file.write_text("- just\n- a\n- list\n", encoding="utf-8")
        with pytest.raises(ValueError):
            load_config(config_file, create_dirs=False)


class TestEnvOverrides:
    """环境变量覆盖仅在 load_config 路径生效（load_config_from_mapping 有意不读环境）。"""

    @staticmethod
    def _load(tmp_path: Path):
        return load_config(tmp_path / "absent.yaml", create_dirs=False)

    def test_simple_override(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("EMAIL_ASSISTANT__API__PORT", "9876")
        assert self._load(tmp_path).api.port == 9876

    def test_nested_override(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("EMAIL_ASSISTANT__EMAIL__ADDRESS", "env@corp.com")
        assert self._load(tmp_path).email.address == "env@corp.com"

    def test_boolean_coercion(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("EMAIL_ASSISTANT__SYNC__DOWNLOAD_ATTACHMENTS", "false")
        assert self._load(tmp_path).sync.download_attachments is False

    def test_number_coercion(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("EMAIL_ASSISTANT__SYNC__MAX_ATTACHMENT_SIZE_MB", "12.5")
        assert self._load(tmp_path).sync.max_attachment_size_mb == 12.5

    def test_list_coercion(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("EMAIL_ASSISTANT__SYNC__FOLDERS", '["INBOX", "Sent"]')
        assert self._load(tmp_path).sync.folders == ["INBOX", "Sent"]

    def test_explicit_overrides_beat_env(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("EMAIL_ASSISTANT__API__PORT", "9876")
        config = load_config(tmp_path / "absent.yaml", overrides={"api": {"port": 1111}},
                             create_dirs=False)
        assert config.api.port == 1111

    def test_env_beats_yaml(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        config_file = tmp_path / "config.yaml"
        config_file.write_text("api:\n  port: 5555\n", encoding="utf-8")
        monkeypatch.setenv("EMAIL_ASSISTANT__API__PORT", "6666")
        assert load_config(config_file, create_dirs=False).api.port == 6666


class TestMaskedAddress:
    @pytest.mark.parametrize(
        "address,expected",
        [
            ("alice@corp.com", "al***@corp.com"),
            ("ab@corp.com", "a***@corp.com"),
            ("a@corp.com", "a***@corp.com"),
            ("invalid", "***"),
            ("", "***"),
        ],
    )
    def test_masking(self, address: str, expected: str) -> None:
        assert masked_address(address) == expected

    def test_masked_hides_local_part(self) -> None:
        assert "alice" not in masked_address("alice@corp.com")


class TestConfigWriter:
    def test_template_is_valid_yaml(self) -> None:
        from src.config_writer import render_default_config

        parsed = yaml.safe_load(render_default_config())
        assert isinstance(parsed, dict)
        # 模板必须能被 AppConfig 直接接受
        config = AppConfig.model_validate(parsed)
        assert config.api.port == 8990

    def test_template_has_no_secrets(self) -> None:
        from src.config_writer import render_default_config

        template = render_default_config()
        assert "EMAIL_ASSISTANT_AUTH_CODE" in template  # 环境变量名可以出现
        parsed = yaml.safe_load(template)
        assert not parsed["api"]["token"]

    def test_write_default_config(self, tmp_path: Path) -> None:
        from src.config_writer import write_default_config

        target = tmp_path / "config.yaml"
        written = write_default_config(target)
        assert written == target
        assert target.is_file()
        assert "腾讯企业邮箱" in target.read_text(encoding="utf-8")

    def test_write_does_not_overwrite_by_default(self, tmp_path: Path) -> None:
        from src.config_writer import write_default_config

        target = tmp_path / "config.yaml"
        target.write_text("api:\n  port: 1234\n", encoding="utf-8")
        write_default_config(target)
        assert yaml.safe_load(target.read_text(encoding="utf-8"))["api"]["port"] == 1234

    def test_write_overwrite_flag(self, tmp_path: Path) -> None:
        from src.config_writer import write_default_config

        target = tmp_path / "config.yaml"
        target.write_text("api:\n  port: 1234\n", encoding="utf-8")
        write_default_config(target, overwrite=True)
        parsed = yaml.safe_load(target.read_text(encoding="utf-8"))
        assert parsed["api"]["port"] == 8990

    def test_update_config_merges(self, tmp_path: Path) -> None:
        from src.config_writer import update_config, write_default_config

        target = tmp_path / "config.yaml"
        write_default_config(target)
        update_config({"sync": {"interval_minutes": 30}}, target)
        parsed = yaml.safe_load(target.read_text(encoding="utf-8"))
        assert parsed["sync"]["interval_minutes"] == 30
        assert parsed["api"]["port"] == 8990  # 其它配置保留

    def test_update_config_creates_file(self, tmp_path: Path) -> None:
        from src.config_writer import update_config

        target = tmp_path / "nested" / "config.yaml"
        update_config({"api": {"port": 7777}}, target)
        assert yaml.safe_load(target.read_text(encoding="utf-8"))["api"]["port"] == 7777

    def test_written_config_has_restrictive_permissions(self, tmp_path: Path) -> None:
        if os.name == "nt":
            pytest.skip("POSIX 专用")
        import stat

        from src.config_writer import write_default_config

        target = tmp_path / "config.yaml"
        write_default_config(target)
        assert stat.S_IMODE(target.stat().st_mode) == 0o600


class TestConfigSourceTracking:
    """回归测试：运行时改配置必须写回**实际加载的**那个文件。

    早期实现里 TrayApplication.set_interval 调用 update_config 时不带路径，
    于是写到了默认路径（项目根 config/config.yaml），而不是用户实际加载的
    配置文件——设置看起来生效了，重启后却丢失。
    """

    def test_source_path_recorded(self, tmp_path: Path) -> None:
        from src.config_writer import write_default_config

        target = tmp_path / "custom" / "my-config.yaml"
        write_default_config(target)
        config = load_config(target, create_dirs=False)
        assert config.source_path == target.resolve()

    def test_source_path_none_without_file(self, tmp_path: Path) -> None:
        config = load_config(tmp_path / "absent.yaml", create_dirs=False)
        assert config.source_path is None

    def test_source_path_not_serialized(self, tmp_path: Path) -> None:
        from src.config_writer import write_default_config

        target = tmp_path / "config.yaml"
        write_default_config(target)
        dumped = load_config(target, create_dirs=False).model_dump()
        assert "source_path" not in dumped, "source_path 不应污染配置序列化结果"

    def test_update_writes_to_given_path(self, tmp_path: Path) -> None:
        import yaml as _yaml

        from src.config_writer import update_config, write_default_config

        target = tmp_path / "custom.yaml"
        write_default_config(target)
        update_config({"sync": {"interval_minutes": 42}}, target)
        assert _yaml.safe_load(target.read_text(encoding="utf-8"))["sync"][
            "interval_minutes"
        ] == 42


class TestDoctorJsonOutput:
    """回归测试：doctor --json 必须输出**纯净**的 JSON，便于脚本解析。

    早期实现把人类可读的自检输出和 JSON 混在 stdout 里，
    导致 `doctor --json | jq` 之类的用法直接解析失败。
    """

    #: 仓库根目录，用 __file__ 推导。
    #: 不能用 src.config.PROJECT_ROOT —— 它会随 EMAIL_ASSISTANT_HOME
    #: 环境变量改变（这是公开支持的配置项），导致 cwd 指向别处、
    #: main.py 找不到，测试莫名失败。
    REPO_ROOT = Path(__file__).resolve().parent.parent

    def _run(self, tmp_path: Path, *extra: str):
        import json
        import subprocess
        import sys

        env = dict(os.environ)
        env["EMAIL_ASSISTANT_HOME"] = str(tmp_path / "home")

        # 必须显式指定 encoding="utf-8"：
        # Windows 上 text=True 会按区域设置（cp1252）解码，而 doctor 输出
        # 中文与 ✓/✗ 符号，必然抛 UnicodeDecodeError。
        run_kwargs = dict(
            cwd=self.REPO_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=180,
        )

        # 先初始化：没有配置文件时 doctor 会提前返回，后面的检查不会执行
        init = subprocess.run(
            [sys.executable, "main.py", "init", "--non-interactive"], **run_kwargs
        )
        assert init.returncode == 0, f"init 失败：{init.stderr}"

        proc = subprocess.run(
            [sys.executable, "main.py", "doctor", "--json", *extra], **run_kwargs
        )
        # 把 stderr 带上，便于诊断（空 stdout 时尤其重要）
        assert proc.stdout.strip(), (
            f"doctor --json 没有输出。returncode={proc.returncode}\n"
            f"stderr=\n{proc.stderr[-2000:]}"
        )
        return proc, json

    def test_stdout_is_pure_json(self, tmp_path: Path) -> None:
        proc, json = self._run(tmp_path)
        payload = json.loads(proc.stdout)  # 不抛异常即为通过
        assert "checks" in payload and "ok" in payload

    def test_no_human_readable_noise(self, tmp_path: Path) -> None:
        proc, _ = self._run(tmp_path)
        assert "— 环境自检 —" not in proc.stdout
        assert "✓" not in proc.stdout
        assert "✗" not in proc.stdout

    def test_checks_have_expected_shape(self, tmp_path: Path) -> None:
        proc, json = self._run(tmp_path)
        payload = json.loads(proc.stdout)
        names = {c["name"] for c in payload["checks"]}
        for expected in ("SQLite 可用", "FTS5 全文检索", "依赖 imap_tools"):
            assert expected in names
        for check in payload["checks"]:
            assert {"name", "ok", "detail", "hint"} <= set(check)
